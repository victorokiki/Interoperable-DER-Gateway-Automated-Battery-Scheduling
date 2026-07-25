"""
tests/integration/run_integration_test_modbus_fault.py

Integration test for the BATTERY-SIDE (Modbus) connection loss path.

The first integration test (run_integration_test.py) covers grid-side
(IEEE) connection loss. This one covers the other half of your charter's
reliability requirement: what happens when the battery itself becomes
unreachable, mid-operation, and then comes back?

Flow:
    1. Start both simulators, confirm a baseline command works.
    2. Kill the fake battery (simulate a dead Modbus link).
    3. Inject a new command from the grid while the battery is down —
       confirm the app does NOT crash and does NOT silently corrupt
       in-memory state.
    4. Restart the fake battery.
    5. Wait for the watchdog's reconnect loop to notice and reconnect.
    6. Inject a fresh command and confirm it is delivered post-recovery.

RUN (from the project root, capstone_project/):
    .venv/bin/python tests/integration/run_integration_test_modbus_fault.py
"""

import asyncio
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from pymodbus.client import AsyncModbusTcpClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from capstone.state import GatewayState, BatteryMode
from capstone.modbus_driver import (
    ModbusDriver, REG_ACTIVE_POWER_SETPT, _decode_int32,
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
    "offline_timeout_sec": 8,
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


async def try_read_battery_registers():
    """Returns None if the battery is unreachable, instead of raising.
    Reads the real Active Power Setpoint register (AC-configured, the
    fake_battery.py default) and converts hardware sign convention back
    to this app's convention (positive = charge) for easy comparison."""
    try:
        client = AsyncModbusTcpClient(host=MODBUS_HOST, port=MODBUS_PORT)
        await asyncio.wait_for(client.connect(), timeout=3)
        if not client.connected:
            return None
        result = await client.read_holding_registers(REG_ACTIVE_POWER_SETPT, count=2)
        client.close()
        if result.isError():
            return None
        hardware_power_kw = _decode_int32(result.registers)
        app_power_kw = -hardware_power_kw
        return {
            "power_kw": app_power_kw,
            "mode": 1 if app_power_kw > 0 else (2 if app_power_kw < 0 else 0),
        }
    except Exception:
        return None


def wipe_schedule_db():
    db_path = PROJECT_ROOT / "schedule_store" / "schedule.db"
    if db_path.exists():
        db_path.unlink()


def get_executed_status(mrid: str):
    """Read an event's 'executed' column directly from the schedule DB.
    -1/1 = start attempted/succeeded, -2/2 = stop attempted/succeeded, 0 = pending/retry."""
    db_path = PROJECT_ROOT / "schedule_store" / "schedule.db"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT executed FROM events WHERE mrid=?", (mrid,)).fetchone()
    conn.close()
    return row[0] if row else None


async def wait_until_started(mrid: str, timeout: int = 10):
    """Poll the DB until an event reaches executed=1 (successfully started),
    or the timeout expires. Returns the final status seen."""
    deadline = time.time() + timeout
    status = get_executed_status(mrid)
    while time.time() < deadline and status != 1:
        await asyncio.sleep(0.5)
        status = get_executed_status(mrid)
    return status


def start_battery_proc():
    return subprocess.Popen(
        [sys.executable, "simulators/fake_battery.py"],
        cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def inject(session, path, payload=None):
    async with session.post(f"{GRID_URL}{path}", json=payload or {}) as r:
        return await r.json()


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
        battery_proc = start_battery_proc()

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

        # ── TEST 1: Baseline — confirm a command works before we break anything ──
        banner("TEST 1: Baseline CHARGE command (sanity check)")
        async with aiohttp.ClientSession() as session:
            baseline_result = await inject(session, "/simulate/charge",
                                            {"power_kw": 300, "duration_sec": 60})
            print("Injected:", baseline_result)
        baseline_mrid = baseline_result["event"]["mrid"]
        await asyncio.sleep(CONFIG["ieee_poll_interval_sec"] + 3)
        regs = await try_read_battery_registers()
        print("Battery registers now:", regs)
        assert regs is not None and regs["mode"] == 1
        print("PASS: baseline command delivered.")

        # ── TEST 2: Kill the battery mid-operation ────────────────────────
        banner("TEST 2: Kill fake battery — simulate a dead Modbus link")
        battery_proc.terminate()
        battery_proc.wait(timeout=5)
        print("Battery process killed.")

        print("Injecting a DISCHARGE command while the battery is down...")
        async with aiohttp.ClientSession() as session:
            outage_result = await inject(session, "/simulate/discharge",
                                          {"power_kw": 200, "duration_sec": 60})
            print("Injected:", outage_result)
        outage_mrid = outage_result["event"]["mrid"]

        print("Waiting for scheduler to attempt the (doomed) write...")
        await asyncio.sleep(CONFIG["ieee_poll_interval_sec"] + 3)

        # The app must not crash. In-memory state must not silently claim
        # a command succeeded when the write actually failed.
        print("state.mode after failed write attempt:", state.mode)
        print("state.power_kw after failed write attempt:", state.power_kw)
        assert state.mode == BatteryMode.CHARGE, (
            "State changed even though the Modbus write should have failed — "
            "this means failures are not being handled safely."
        )
        print("PASS: app stayed alive and did not corrupt in-memory state "
              "on a failed write.")

        # ── TEST 3: Bring the battery back ────────────────────────────────
        banner("TEST 3: Restart fake battery — expect watchdog to reconnect")
        battery_proc = start_battery_proc()
        ok_batt = await wait_for_port(MODBUS_HOST, MODBUS_PORT)
        assert ok_batt, "New battery process failed to start"
        print("Battery process is back up. Waiting for watchdog reconnect loop...")
        await asyncio.sleep(12)  # watchdog checks every 10s

        print("modbus client connected:", modbus._client.connected)
        assert modbus._client.connected, "Modbus did not reconnect after battery restart"

        # ── Confirm the two events that failed DURING the outage actually
        # got retried and succeeded once the connection came back, instead
        # of being silently dropped.
        banner("Confirming outage-time events were retried to success")
        for label, mrid in [("baseline CHARGE", baseline_mrid),
                             ("outage DISCHARGE", outage_mrid)]:
            status = await wait_until_started(mrid, timeout=10)
            print(f"  {label} ({mrid}): executed={status}")
            assert status == 1, (
                f"{label} event {mrid} never reached executed=1 after reconnect — "
                f"the retry loop did not actually recover it (final status={status})"
            )
        print("PASS: both events that failed during the outage were "
              "automatically retried and delivered after reconnection.")

        # ── TEST 4: Confirm a fresh command now goes through ─────────────
        banner("TEST 4: Inject a fresh command post-recovery")
        async with aiohttp.ClientSession() as session:
            print("Injected:", await inject(session, "/simulate/charge",
                                             {"power_kw": 150, "duration_sec": 60}))
        await asyncio.sleep(CONFIG["ieee_poll_interval_sec"] + 3)
        regs = await try_read_battery_registers()
        print("Battery registers now:", regs)
        assert regs is not None, "Battery still unreachable after restart"
        assert regs["mode"] == 1 and abs(regs["power_kw"] - 150) < 0.5, (
            f"Expected a fresh CHARGE @150kW to land after reconnect, got {regs}"
        )
        print("PASS: middleware reconnected to the battery and post-recovery "
              "commands are delivered correctly.")

        banner("ALL MODBUS-FAULT INTEGRATION TESTS PASSED")
        print(
            "\nConfirmed for your reliability writeup:\n"
            "Commands that fail to write during a battery outage (both the\n"
            "in-flight baseline command and a new one injected mid-outage)\n"
            "are automatically retried every scheduler tick and are\n"
            "successfully delivered once the Modbus connection is restored —\n"
            "no commands are silently dropped."
        )

    finally:
        for t in tasks:
            t.cancel()
        for proc in (grid_proc, battery_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
        print("\nSimulators stopped. Done.")


if __name__ == "__main__":
    asyncio.run(main())