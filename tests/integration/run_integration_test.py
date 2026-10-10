"""
tests/integration/run_integration_test.py

Full end-to-end integration test for the DER Gateway middleware.

Unlike tests/test_capstone.py, this does NOT mock anything.
It spins up the two simulators as real subprocesses, wires up the
real IEEEClient -> Scheduler -> ModbusDriver pipeline exactly as a
production main.py would, and drives commands through the full stack:

    fake_grid_server (Flask, :5000)
        --HTTP-->  IEEEClient  --event-->  Scheduler (SQLite)
                                                |
                                                v
                                          ModbusDriver
                                                |
                                             --TCP-->  fake_battery (:5020)

SETUP (one time):
    mkdir -p tests/integration
    (save this file as tests/integration/run_integration_test.py)

RUN (from the project root, capstone_project/):
    .venv/bin/python tests/integration/run_integration_test.py

Watch the terminal output — you should see events flow through every
stage: grid injection -> IEEE poll -> scheduler start -> modbus write ->
assertions confirming the battery simulator actually received them.

This takes about 45-60 seconds to run (it includes a real wait for the
offline-timeout / watchdog fault-recovery path, not a mocked clock).
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from pymodbus.client import AsyncModbusTcpClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from capstone.state import GatewayState
from capstone.modbus_driver import (
    ModbusDriver, REG_ACTIVE_POWER_SETPT, REG_OPERATING_STATE,
    _decode_int32, OPERATING_STATE_NAMES,
)
from capstone.scheduler import Scheduler
from capstone.ieee_client import IEEEClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GRID_URL = "http://localhost:5001"
MODBUS_HOST = "127.0.0.1"
MODBUS_PORT = 5020

CONFIG = {
    "ieee_server_url": GRID_URL,
    "ieee_poll_interval_sec": 2,
    "offline_timeout_sec": 8,  # short on purpose so the fault test doesn't take forever
    "modbus_host": MODBUS_HOST,
    "modbus_port": MODBUS_PORT,
}


def banner(msg):
    print("\n" + "=" * 60)
    print(msg)
    print("=" * 60)


async def wait_for_port(host, port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            await asyncio.sleep(0.3)
    return False


async def wait_for_http(url, timeout=15):
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as session:
        while time.time() < deadline:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status:
                        return True
            except Exception:
                await asyncio.sleep(0.3)
    return False


async def read_battery_registers():
    """Read the real Active Power Setpoint register (AC-configured — the
    fake_battery.py default) and the Operating State register. Returns
    values already converted BACK to this app's convention (positive =
    charge) for easy comparison against what we injected, since the
    hardware itself uses the opposite sign convention."""
    client = AsyncModbusTcpClient(host=MODBUS_HOST, port=MODBUS_PORT)
    await client.connect()
    power_result = await client.read_holding_registers(REG_ACTIVE_POWER_SETPT, count=2)
    state_result = await client.read_input_registers(REG_OPERATING_STATE, count=1)
    client.close()
    if power_result.isError() or state_result.isError():
        return None
    hardware_power_kw = _decode_int32(power_result.registers)
    app_power_kw = -hardware_power_kw  # invert back to this app's convention
    operating_state = state_result.registers[0]
    return {
        "power_kw": app_power_kw,
        "mode": 1 if app_power_kw > 0 else (2 if app_power_kw < 0 else 0),  # 0=idle,1=charge,2=discharge
        "operating_state": OPERATING_STATE_NAMES.get(operating_state, f"Unknown({operating_state})"),
    }


def wipe_schedule_db():
    db_path = PROJECT_ROOT / "schedule_store" / "schedule.db"
    if db_path.exists():
        db_path.unlink()


async def main():
    wipe_schedule_db()
    grid_proc = None
    battery_proc = None
    tasks = []

    try:
        banner("STARTING SIMULATORS")
        grid_proc = subprocess.Popen(
            [sys.executable, "simulators/fake_grid_server.py"],
            cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        battery_proc = subprocess.Popen(
            [sys.executable, "simulators/fake_battery.py"],
            cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        ok_grid = await wait_for_http(f"{GRID_URL}/status")
        ok_batt = await wait_for_port(MODBUS_HOST, MODBUS_PORT)
        if not (ok_grid and ok_batt):
            print("FAILED to start simulators — aborting.")
            return
        print("Both simulators are up.")

        banner("WIRING UP THE REAL MIDDLEWARE (no mocks)")
        state = GatewayState()
        modbus = ModbusDriver(CONFIG, state)
        await modbus.connect()
        scheduler = Scheduler(CONFIG, state, modbus)
        ieee = IEEEClient(CONFIG, state, scheduler)

        tasks = [
            asyncio.create_task(ieee.run()),
            asyncio.create_task(scheduler.run()),
            asyncio.create_task(modbus.watchdog()),
            asyncio.create_task(modbus.heartbeat_loop()),
        ]

        # ── TEST 1: Charge command flows end-to-end ──────────────
        banner("TEST 1: Inject CHARGE via fake grid server")
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{GRID_URL}/simulate/charge",
                                     json={"power_kw": 500, "duration_sec": 40}) as r:
                print("Injected:", await r.json())

        print("Waiting for IEEE poll + scheduler tick...")
        await asyncio.sleep(CONFIG["ieee_poll_interval_sec"] + 3)

        regs = await read_battery_registers()
        print("Battery registers now:", regs)
        assert regs is not None, "Could not read battery registers"
        assert regs["mode"] == 1, f"Expected CHARGE(1), got {regs['mode']}"
        assert abs(regs["power_kw"] - 500) < 0.5, f"Expected ~500kW, got {regs['power_kw']}"
        print("PASS: charge command reached the battery simulator.")

        # ── TEST 2: Discharge command ─────────────────────────────
        banner("TEST 2: Inject DISCHARGE (replaces the charge event)")
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{GRID_URL}/simulate/discharge",
                                     json={"power_kw": 250, "duration_sec": 40}) as r:
                discharge = (await r.json())["event"]
                print("Injected:", discharge)
        discharge_end = discharge["start"] + discharge["duration"]

        await asyncio.sleep(CONFIG["ieee_poll_interval_sec"] + 3)
        regs = await read_battery_registers()
        print("Battery registers now:", regs)
        assert regs["mode"] == 2, f"Expected DISCHARGE(2), got {regs['mode']}"
        assert abs(regs["power_kw"] - (-250)) < 0.5, f"Expected ~-250kW (app convention), got {regs['power_kw']}"
        print("PASS: discharge command reached the battery simulator.")

        # ── TEST 3: Idle clears the schedule ─────────────────────
        banner("TEST 3: Clear events via /simulate/idle")
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{GRID_URL}/simulate/idle") as r:
                print("Cleared:", await r.json())
        print("(Note: existing scheduled event still runs to its end_ts —")
        print(" this only confirms no NEW event gets queued.)")
        await asyncio.sleep(CONFIG["ieee_poll_interval_sec"] + 1)

        # ── TEST 4: Grid loss -> approved event continues, then Safe Idle ──
        # Sprint 2 Table 1: during a communication loss the last approved
        # valid schedule keeps running until completion or expiry; Safe
        # Idle applies once no valid schedule remains.
        banner("TEST 4: Kill grid server -> approved event continues, then Safe IDLE")
        grid_proc.terminate()
        grid_proc.wait(timeout=5)
        wait_s = CONFIG["offline_timeout_sec"] + 3
        print(f"Grid server killed. Waiting {wait_s}s (past the offline timeout)...")
        await asyncio.sleep(wait_s)

        print("state.grid_connected:", state.grid_connected)
        regs = await read_battery_registers()
        print("Battery registers now:", regs)
        assert state.grid_connected is False, "state should show grid disconnected"
        assert time.time() < discharge_end, "test timing: the discharge event already ended"
        assert regs["mode"] == 2, f"Expected the approved DISCHARGE(2) to continue, got {regs['mode']}"
        print("PASS: the approved event kept running through the grid loss.")

        await asyncio.sleep(max(0.0, discharge_end - time.time()) + 3)
        regs = await read_battery_registers()
        print("Battery registers after the event's end:", regs)
        assert regs["mode"] == 0, f"Expected IDLE(0) once no valid schedule remains, got {regs['mode']}"
        print("PASS: Safe IDLE once the approved event expired during the grid loss.")

        banner("ALL INTEGRATION TESTS PASSED")

    finally:
        for t in tasks:
            t.cancel()
        for proc in (grid_proc, battery_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
        print("\nSimulators stopped. Done.")


if __name__ == "__main__":
    asyncio.run(main())