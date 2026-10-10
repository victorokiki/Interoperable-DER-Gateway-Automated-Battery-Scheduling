"""
tests/audit/e2e_findings.py — end-to-end evidence with real processes.

Starts the real fake_battery.py, fake_grid_server.py and the real gateway
(`python -m capstone`) as separate processes in a throw-away working
directory (the project's own schedule_store/ is never touched), drives
them over HTTP, and reads what the battery registers actually hold.

    python tests/audit/e2e_findings.py

Needs ports 5001 and 5020 free; takes about two minutes. The battery
simulator is started through a small launcher that binds 127.0.0.1 instead
of the simulator's 0.0.0.0 (avoids a Windows firewall prompt); everything
else is the unmodified project code.
"""

import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from pymodbus.client import AsyncModbusTcpClient

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
GRID = "http://127.0.0.1:5001"
T0 = time.monotonic()
STATE_NAMES = {1: "Off", 2: "Standby", 4: "On", 10: "Fault"}

BATTERY_LAUNCHER = r"""
import importlib.util, sys, threading
from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
spec = importlib.util.spec_from_file_location("fake_battery", sys.argv[1])
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)                      # module body only, not its __main__ block
threading.Thread(target=sim.simulate_and_log, daemon=True).start()
slave = ModbusSlaveContext(hr=sim.hr_store, ir=sim.ir_store, zero_mode=True)
StartTcpServer(context=ModbusServerContext(slaves=slave, single=True), address=("127.0.0.1", 5020))
"""


def say(msg):
    print(f"[{time.monotonic() - T0:6.1f}s] {msg}", flush=True)


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.2)
    return False


class Run:
    def __init__(self):
        self.work = Path(tempfile.mkdtemp(prefix="gateway_e2e_"))
        self.procs = {}

    def start(self, name, args, env=None):
        log = open(self.work / f"{name}.log", "a", encoding="utf-8", errors="replace")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.procs[name] = subprocess.Popen(args, cwd=self.work, stdout=log, stderr=subprocess.STDOUT,
                                            env=env, creationflags=flags)
        return self.procs[name]

    def start_battery(self):
        return self.start("battery", [PY, "-c", BATTERY_LAUNCHER, str(ROOT / "simulators" / "fake_battery.py")])

    def start_grid(self):
        return self.start("grid", [PY, str(ROOT / "simulators" / "fake_grid_server.py")])

    def start_gateway(self, name="gateway"):
        env = dict(os.environ, IEEE_POLL_INTERVAL_SEC="1", OFFLINE_TIMEOUT_SEC="5", PYTHONUNBUFFERED="1")
        return self.start(name, [PY, "-m", "capstone"], env)

    def stop(self, name):
        proc = self.procs.get(name)
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    def log(self, name):
        return (self.work / f"{name}.log").read_text(encoding="utf-8", errors="replace")

    def snapshot(self):
        try:
            return json.loads((self.work / "schedule_store" / "live_status.json").read_text())
        except (OSError, ValueError):
            return {}


async def battery():
    """What the battery holds right now (app sign: + = charge), or None."""
    client = AsyncModbusTcpClient("127.0.0.1", port=5020, retries=0, reconnect_delay=0, timeout=2)
    try:
        if not await client.connect():
            return None
        sp = await client.read_holding_registers(1009, count=2)
        hb = await client.read_holding_registers(1002, count=1)
        st = await client.read_input_registers(1000, count=1)
        bms_hb = await client.read_input_registers(1003, count=1)
        raw = (sp.registers[0] << 16) | sp.registers[1]
        hw = raw - (1 << 32) if raw >= 1 << 31 else raw
        return {"app_kw": -hw, "heartbeat": hb.registers[0], "bms_heartbeat": bms_hb.registers[0],
                "op_state": STATE_NAMES.get(st.registers[0], st.registers[0])}
    except Exception:  # noqa: BLE001
        return None
    finally:
        client.close()


async def post(path, payload=None):
    async with aiohttp.ClientSession() as http:
        async with http.post(GRID + path, json=payload or {}) as r:
            return r.status


def describe(b, snap):
    if b is None:
        return "battery unreachable"
    return (f"battery holds {b['app_kw']:+} kW ({b['op_state']})  |  gateway snapshot: "
            f"{snap.get('mode')} {snap.get('power_kw')} kW, FSM {snap.get('reliability_state')}")


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for port in (5001, 5020):
        if port_open(port):
            sys.exit(f"port {port} already in use — stop whatever is running there first")
    run = Run()
    say(f"working directory: {run.work}")
    try:
        run.start_battery()
        run.start_grid()
        assert wait_port(5020) and wait_port(5001), "simulators did not start"
        await asyncio.sleep(1)

        say("S0 [S8] battery simulator vs SPC100644 Rev 03, probed before the gateway connects")
        first = AsyncModbusTcpClient("127.0.0.1", port=5020, retries=0, reconnect_delay=0)
        second = AsyncModbusTcpClient("127.0.0.1", port=5020, retries=0, reconnect_delay=0)
        both = await first.connect() and await second.connect()
        before = (await first.read_input_registers(1000, count=1)).registers[0]
        await first.write_registers(1000, [2])                           # On, straight from Off
        await asyncio.sleep(1.2)
        after = (await first.read_input_registers(1000, count=1)).registers[0]
        huge = await first.write_registers(1009, [0x7FFF, 0xFFFF])       # +2,147,483,647 kW
        await first.write_registers(1009, [0, 0])
        await first.write_registers(1000, [0])                           # back to Off for the gateway
        await asyncio.sleep(1.2)
        first.close()
        second.close()
        say(f"   second simultaneous control connection accepted: {both}  (spec §3: max 1, further refused)")
        say(f"   On written while {STATE_NAMES.get(before)}: state became {STATE_NAMES.get(after)}  "
            f"(spec p.16: Off -> Standby -> On only)")
        say(f"   setpoint +2,147,483,647 kW accepted: {not huge.isError()}  (spec §4.3: out of range -> exception 03)")

        run.start_gateway()
        await asyncio.sleep(4)

        say("S1 baseline: POST /simulate/charge 300 kW for 600 s")
        await post("/simulate/charge", {"power_kw": 300, "duration_sec": 600})
        await asyncio.sleep(3)
        b = await battery()
        say("   " + describe(b, run.snapshot()))
        if b:
            say(f"   [S6] BMS heartbeat 31003 = {b['bms_heartbeat']}, operator heartbeat 41002 = {b['heartbeat']} "
                f"(spec §4.5.1: the controller echoes the BMS value)")

        say("S2 [C2b] POST /simulate/discharge 100 kW for 6 s (server REPLACES the charge event)")
        await post("/simulate/discharge", {"power_kw": 100, "duration_sec": 6})
        await asyncio.sleep(3)
        say("   during discharge: " + describe(await battery(), run.snapshot()))
        await asyncio.sleep(6)
        say("   after it ended:   " + describe(await battery(), run.snapshot()))

        say("S3 [C2c] POST /simulate/idle (dashboard 'Clear / Force Idle')")
        status = await post("/simulate/idle")
        say(f"   HTTP {status} (the list is cleared before the handler prints, so it IS empty either way)")
        await asyncio.sleep(4)
        say("   " + describe(await battery(), run.snapshot()))

        say("S4 [C3a/H1] kill the utility server; sample once per second")
        run.stop("grid")
        seen, first_zero = [], None
        for i in range(1, 21):
            await asyncio.sleep(1)
            b, snap = await battery(), run.snapshot()
            label = snap.get("reliability_state")
            if not seen or seen[-1] != label:
                seen.append(label)
            if b and b["app_kw"] == 0 and first_zero is None:
                first_zero = i
        say(f"   FSM labels seen: {seen}; setpoint first read as 0 kW {first_zero} s after the kill")

        say("S5 restart utility server; POST /simulate/charge 150 kW for 600 s")
        run.start_grid()
        assert wait_port(5001)
        await asyncio.sleep(2)
        await post("/simulate/charge", {"power_kw": 150, "duration_sec": 600})
        await asyncio.sleep(3)
        say("   " + describe(await battery(), run.snapshot()))

        say("S6 [C1e/H3/H8] reboot the BMS (kill + restart the battery process)")
        log_before = len(run.log("gateway"))
        run.stop("battery")
        await asyncio.sleep(3)
        run.start_battery()
        assert wait_port(5020)
        elapsed = 0
        for mark in (2, 6, 12, 16):
            await asyncio.sleep(mark - elapsed)
            elapsed = mark
            say(f"   +{mark:2d}s after restart: " + describe(await battery(), run.snapshot()))
        tail = run.log("gateway")[log_before:]
        say(f"   gateway log since the reboot: 'Modbus disconnected' x{tail.count('Modbus disconnected')}, "
            f"'Sent Operating Command: On' x{tail.count('Sent Operating Command: On')}, "
            f"'Battery →' x{tail.count('Battery →')}")

        say("S7 [H5] start a SECOND gateway instance on the same machine/DB; sample the heartbeat register")
        run.start_gateway("gateway2")
        await asyncio.sleep(5)
        beats = []
        for _ in range(25):
            b = await battery()
            if b:
                beats.append(b["heartbeat"])
            await asyncio.sleep(0.2)
        drops = sum(1 for a, b in zip(beats, beats[1:]) if b < a)
        say(f"   Reg 41002 samples: {beats}")
        say(f"   the heartbeat went BACKWARDS {drops} times (two independent counters)")
        run.stop("gateway2")

        say("S8 [N1] Windows stop: charge 200 kW, then stop the gateway the way a service manager would")
        await post("/simulate/charge", {"power_kw": 200, "duration_sec": 600})
        await asyncio.sleep(3)
        say("   before stop: " + describe(await battery(), run.snapshot()))
        pid = run.procs["gateway"].pid
        tk = subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, text=True)
        say(f"   taskkill /PID {pid} (no /F): {(tk.stdout + tk.stderr).strip()}")
        os.kill(pid, signal.SIGTERM)
        run.procs["gateway"].wait(timeout=10)
        await asyncio.sleep(3)
        say("   after SIGTERM: " + describe(await battery(), {}))
        say(f"   gateway log contains 'Forcing battery to safe IDLE': "
            f"{'Forcing battery to safe IDLE' in run.log('gateway')}")

        say("S9 audit log written by the gateway(s)")
        db = run.work / "schedule_store" / "audit_log.db"
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT ts_utc, event_type, source, detail, outcome FROM audit_log ORDER BY id").fetchall()
        finally:
            conn.close()
        transitions = [r[3] for r in rows if r[1] == "state_transition"]
        dupes = sum(1 for a, b in zip(rows, rows[1:]) if a[1:] == b[1:] and a[1] != "grid_poll")
        say(f"   {len(rows)} rows; {len(transitions)} state transitions: {transitions}")
        say(f"   consecutive identical non-poll rows (double writers): {dupes}")
        say(f"   rows mentioning a register address (1009/1007): {sum(1 for r in rows if '1009' in (r[3] or '') or '1007' in (r[3] or ''))}")
        forced = run.log("gateway").count("forcing safe idle")
        audited = sum(1 for r in rows if r[1] != "state_transition" and "idle" in (r[3] or "").lower())
        say(f"   watchdog idles in the gateway log: {forced}; audit rows recording them: {audited}")
        say(f"   audit rows about the BMS reboot / Off state / reconnect: "
            f"{sum(1 for r in rows if r[1].startswith('bms') or 'reconnect' in (r[3] or '').lower())}")
        say(f"   example schedule_received detail: {next((r[3] for r in rows if r[1] == 'schedule_received'), None)!r}")
    finally:
        for name in list(run.procs):
            run.stop(name)
        say(f"logs kept in {run.work}")


if __name__ == "__main__":
    asyncio.run(main())
