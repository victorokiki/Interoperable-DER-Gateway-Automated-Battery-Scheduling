"""
tests/audit/repro_findings.py — executable evidence for the adversarial review.

NOT part of the default run (the file name doesn't match test_*.py), so it
never changes the result of `python -m pytest`. Run it explicitly:

    python -m pytest tests/audit/repro_findings.py -v -rxXs -p no:cacheprovider

Conventions
  * @finding(...) tests assert the CORRECT behaviour and raise Reproduced
    when the code still behaves wrongly, so XFAIL == finding reproduced.
    They are strict: if a finding is fixed the test XPASSes, strict mode
    reports that as a failure, and the marker should be removed so the test
    becomes a regression test. Any other exception (a broken precondition)
    is reported as a normal FAILURE, never as "reproduced".
  * Unmarked tests either record exactly what goes on the wire (items that
    can only be judged against the Endurium ICD) or show a finding that was
    checked and NOT reproduced.

Fakes: FakeModbus stands in for ModbusDriver (scheduler-level tests);
FakeBMS stands in for pymodbus' client plus an Endurium-like register map
(driver-level tests); Utility is an in-process aiohttp IEEE 2030.5 server.
"""

import asyncio
import importlib.util
import logging
import sqlite3
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock
from xml.sax.saxutils import escape

import aiohttp
import pytest
from aiohttp import web

import capstone.modbus_driver as modbus_mod
import capstone.scheduler as sched_mod
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient, build_mtls_context
from capstone.modbus_driver import ModbusDriver, _decode_int32, _encode_int32
from capstone.reliability import ReliabilityManager, ReliabilityState
from capstone.scheduler import Scheduler
from capstone.state import BatteryMode, GatewayState

ROOT = Path(__file__).resolve().parents[2]
CLIENT_CFG = {"ieee_server_url": "http://127.0.0.1:9", "ieee_poll_interval_sec": 1,
              "offline_timeout_sec": 30, "der_set_max_w": 250_000}      # setMaxW, for opModFixedW percentages


class Reproduced(Exception):
    """Raised by a @finding test when the defect is still present."""


def expect(condition, message):
    if not condition:
        raise Reproduced(message)


def finding(ref, text):
    return pytest.mark.xfail(strict=True, raises=Reproduced, reason=f"{ref}: {text}")


# ── Fakes and helpers ───────────────────────────────────────────────────

class FakeModbus:
    """Scheduler-facing stand-in for ModbusDriver. Records every setpoint the
    'BMS' accepted and, like the driver, remembers the last confirmed one
    (commanded_kw, None = unknown) and why the last write failed."""

    def __init__(self, fail=None, gate=None):
        self.writes = []                      # (kw, source)
        self.attempts = {}
        self.fail = fail or (lambda source, attempt: False)
        self.gate = gate
        self.commanded_kw = None
        self.last_dispatch_error = None

    async def set_power(self, kw, source=""):
        attempt = self.attempts[source] = self.attempts.get(source, 0) + 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail(source, attempt):
            self.commanded_kw, self.last_dispatch_error = None, ("error", "simulated failure")
            return False
        self.writes.append((kw, source))
        self.commanded_kw, self.last_dispatch_error = kw, None
        return True

    async def safe_idle(self, source="safe_idle"):
        return await self.set_power(0.0, source)


class _Resp:
    def __init__(self, registers=None):
        self.registers = registers or []

    def isError(self):
        return False


class FakeBMS:
    """Stand-in for pymodbus' AsyncModbusTcpClient talking to an Endurium-like
    register map (addresses as used by modbus_driver.py)."""

    def __init__(self, bms_type=1, operating_state=4):
        self.connected = False
        self.hr = {}
        # addresses per SPC100644 Rev 03 status table (31000 + addr)
        self.ir = {1000: operating_state, 1001: 1, 1003: 0, 1004: 500, 1036: 0, 1038: bms_type, 1039: 0, 1040: 0}
        self.writes = []                      # (monotonic time, address, values, unit id)
        self.ignore_writes = False
        self.fail_reads = set()
        self.write_delay = {}
        self.set_input_int32(1019, -10_000)   # Charge Power Limit (P < 0), generous
        self.set_input_int32(1021, 10_000)    # Discharge Power Limit (P > 0)

    async def connect(self):
        self.connected = True
        return True

    async def write_registers(self, address, values, slave=0, **_):
        if address in self.write_delay:
            await asyncio.sleep(self.write_delay[address])
        self.writes.append((time.monotonic(), address, list(values), slave))
        if not self.ignore_writes:
            for i, value in enumerate(values):
                self.hr[address + i] = value
            if address == 1000:                   # Operating Command, state machine of spec p.16
                command, state = values[0], self.ir[1000]
                if command == 1 and state in (1, 2, 4):
                    self.ir[1000] = 2
                elif command == 2 and state == 2:
                    self.ir[1000] = 4
                elif command == 0 and state == 2:
                    self.ir[1000] = 1
        return _Resp()

    async def read_input_registers(self, address, count=1, slave=0, **_):
        if address in self.fail_reads:
            raise ConnectionError("simulated read failure")
        return _Resp([self.ir.get(address + i, 0) for i in range(count)])

    async def read_holding_registers(self, address, count=1, slave=0, **_):
        return _Resp([self.hr.get(address + i, 0) for i in range(count)])

    def setpoint_kw(self, address=1009):
        """Device setpoint in HARDWARE sign (negative = charging)."""
        return _decode_int32([self.hr.get(address, 0), self.hr.get(address + 1, 0)])

    def set_input_int32(self, address, value):
        self.ir[address], self.ir[address + 1] = _encode_int32(value)


class _FastAsyncio:
    """modbus_driver's view of asyncio with sleep() shortened, so loops with a
    hard-coded cadence (watchdog 10 s, heartbeat 1 s) run in milliseconds.
    Control logic is unchanged."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(delay, result=None):
        await asyncio.sleep(0.001)
        return result


class Utility:
    """In-process IEEE 2030.5 server whose behaviour can be switched. Its
    DeviceCapability (/dcap) lists one DER program whose DERControlList
    (/derp/1/derc) is .xml."""

    def __init__(self):
        self.mode = "serve"                   # serve | 503 | html, for every resource
        self.xml = derc()
        self.responses = []                   # (subject, body) of every DERControlResponse POST

    async def start(self):
        app = web.Application()
        app.router.add_get("/dcap", self._dcap)
        app.router.add_get("/derp", self._programs)
        app.router.add_get("/derp/1/derc", self._get)
        app.router.add_post("/{path:.*}", self._post)          # responses go to each event's replyTo
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.url = "http://127.0.0.1:%d" % self.runner.addresses[0][1]
        return self

    async def stop(self):
        await self.runner.cleanup()

    def _serve(self, text):
        if self.mode == "503":
            return web.Response(status=503, text="maintenance")
        if self.mode == "html":
            return web.Response(text="<html><body>Please log in</body></html>", content_type="text/html")
        return web.Response(text=text, content_type="application/xml")

    async def _dcap(self, request):
        return self._serve('<DeviceCapability xmlns="urn:ieee:std:2030.5:ns" href="/dcap">'
                           '<DERProgramListLink href="/derp" all="1"/></DeviceCapability>')

    async def _programs(self, request):
        return self._serve('<DERProgramList xmlns="urn:ieee:std:2030.5:ns" href="/derp" all="1" results="1">'
                           '<DERProgram href="/derp/1"><mRID>A1</mRID><DERControlListLink href="/derp/1/derc"/>'
                           '<primacy>1</primacy></DERProgram></DERProgramList>')

    async def _get(self, request):
        return self._serve(self.xml)

    async def _post(self, request):
        body = await request.text()
        try:
            subject = ET.fromstring(body).findtext("{*}subject")
        except ET.ParseError:
            subject = None
        self.responses.append((subject, body))
        return web.Response(status=201)


def derc(*events):
    """DERControlList XML. Event keys: mrid (None = omit), start, duration,
    value (W, + = charge; sent as opModTargetW, whose IEEE 2030.5 sign is
    the opposite: + = discharge), mult, status (EventStatus.currentStatus),
    base (raw DERControlBase), response_required (hex; asks for responses
    at /rsps/1/rsp)."""
    parts = []
    for e in events:
        mrid = "" if e.get("mrid") is None else f"<mRID>{e['mrid']}</mRID>"
        respond = ("" if "response_required" not in e else
                   f' replyTo="/rsps/1/rsp" responseRequired="{e["response_required"]}"')
        status = ("" if "status" not in e else
                  f"<EventStatus><currentStatus>{e['status']}</currentStatus><dateTime>{e['start']}</dateTime>"
                  f"<potentiallySuperseded>false</potentiallySuperseded></EventStatus>")
        value = e.get("value", 0)
        base = e.get("base", f"<opModTargetW><multiplier>{e.get('mult', 0)}</multiplier>"
                             f"<value>{-value if isinstance(value, int) else value}</value></opModTargetW>")
        parts.append(f"<DERControl{respond}>{mrid}{status}<interval><duration>{e['duration']}</duration>"
                     f"<start>{e['start']}</start></interval><DERControlBase>{base}</DERControlBase></DERControl>")
    return (f'<DERControlList xmlns="urn:ieee:std:2030.5:ns" all="{len(parts)}" results="{len(parts)}">'
            f'{"".join(parts)}</DERControlList>')


def first_control(xml):
    return ET.fromstring(xml).find("{*}DERControl")


def ev(mrid, start, end, kw):
    return {"mrid": mrid, "start": start, "end": end, "power_kw": kw, "source": "t"}


async def tick(scheduler, now):
    """One scheduler tick, exactly as Scheduler.run() does it."""
    await scheduler.tick(now)


def make_driver(fake=None, audit_log=None, offline_timeout=30):
    """Real ModbusDriver with the pymodbus client swapped for FakeBMS.
    Must be called inside a running event loop (pymodbus requires it)."""
    state = GatewayState()
    drv = ModbusDriver({"modbus_host": "127.0.0.1", "modbus_port": 9, "offline_timeout_sec": offline_timeout},
                       state, audit_log=audit_log)
    drv._client = fake if fake is not None else FakeBMS()
    return drv, state


def load_grid_server(monkeypatch, status_file=None):
    spec = importlib.util.spec_from_file_location("fake_grid_server_under_test",
                                                  ROOT / "simulators" / "fake_grid_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if status_file is not None:
        monkeypatch.setattr(mod, "BATTERY_STATUS_FILE", status_file)
    return mod


def logged_in_client(grid):
    client = grid.app.test_client()
    with client.session_transaction() as session:  # no password involved
        session["logged_in"] = True
        session["username"] = "operator"
    return client


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "schedule.db"
    monkeypatch.setattr(sched_mod, "DB_PATH", path)
    return path


# ── C1: edge-triggered control, no reconciliation ────────────────────────

def test_C1a_interrupted_rows_are_recovered_after_restart(db):
    """C1a (fixed in Phase 1). The database as the earlier scheduler left it
    after a crash: RUNNING killed mid-start (executed=-1), ENDED killed
    mid-stop (executed=-2). The restarted scheduler must re-apply RUNNING."""
    now = 10_000
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events (mrid TEXT PRIMARY KEY, start_ts INTEGER NOT NULL, "
                 "end_ts INTEGER NOT NULL, power_kw REAL NOT NULL, source TEXT, executed INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, 't', ?)",
                     [("RUNNING", now - 60, now + 600, 300.0, -1), ("ENDED", now - 600, now - 10, -200.0, -2)])
    conn.commit()
    conn.close()
    fake = FakeModbus()
    asyncio.run(tick(Scheduler({}, GatewayState(), fake), now))
    assert fake.writes and fake.writes[-1][0] == 300.0, \
        f"after restart the scheduler wrote {fake.writes} — RUNNING (300 kW, in window) never re-applied"


def test_C1b_rejected_successor_does_not_strand_previous_setpoint(db):
    """C1b (fixed in Phase 1; now a regression test): a rejected successor strands the previous event's setpoint forever."""
    fake = FakeModbus(fail=lambda source, n: source == "schedule:B")
    s = Scheduler({}, GatewayState(), fake)
    s.add_event(ev("A", 0, 100, 300.0))
    s.add_event(ev("B", 100, 200, -900.0))

    async def run():
        for now in (50, 100, 150, 200, 201, 500):
            await tick(s, now)
    asyncio.run(run())
    expect(fake.writes[-1][0] == 0.0,
           f"setpoint history {[kw for kw, _ in fake.writes]}: {fake.writes[-1][0]} kW left on the battery "
           f"after every event has ended")


def test_C1c_unverified_write_is_still_stopped(db):
    """C1c (fixed in Phase 1; now a regression test): a write that lands but fails verification is never stopped once its window closes."""
    async def run():
        fake = FakeBMS()
        drv, state = make_driver(fake)
        await drv.connect()
        fake.fail_reads.add(modbus_mod.REG_OPERATING_STATE)      # every post-write verify read fails
        s = Scheduler({}, state, drv)
        s.add_event(ev("E", 1000, 1002, 300.0))
        for now in (1000, 1001, 1002, 1003, 1010):
            await tick(s, now)
        return fake.setpoint_kw(), state.mode
    wire, mode = asyncio.run(run())
    expect(wire == 0, f"device setpoint {wire} kW (hardware sign) after the event ended; gateway believes {mode.value}")


def test_C1d_active_event_resumes_after_graceful_restart(db):
    """C1d (fixed in Phase 1): E was running when Ctrl+C zeroed the battery."""
    now = 10_000
    before = Scheduler({}, GatewayState(), FakeModbus())
    before.add_event(ev("E", now - 60, now + 600, 300.0))
    asyncio.run(tick(before, now - 60))
    fake = FakeModbus()                                      # the restarted gateway's driver
    asyncio.run(tick(Scheduler({}, GatewayState(), fake), now))
    assert fake.writes and fake.writes[-1][0] == 300.0, \
        f"restarted scheduler wrote {fake.writes}; event E (300 kW) stays unexecuted for 600 s"


# ── C2: cancellation / withdrawal ────────────────────────────────────────

def test_C2a_cancelled_event_is_not_executed(db):
    """C2a (fixed in Phase 1): the server's explicit EventStatus Cancelled is honoured."""
    now = int(time.time())
    fake = FakeModbus()
    s = Scheduler({}, GatewayState(), fake)
    IEEEClient(CLIENT_CFG, GatewayState(), s)._parse_and_dispatch(
        derc(dict(mrid="X", start=now - 5, duration=600, value=300000, status=2)))
    asyncio.run(tick(s, now))
    assert all(kw != 300.0 for kw, _ in fake.writes), f"cancelled event executed: {fake.writes}"


def test_C2b_withdrawn_event_does_not_resume(db):
    """C2b (fixed in Phase 1; now a regression test): an event the utility withdrew resumes after the event that replaced it ends."""
    t = 10_000
    fake = FakeModbus()
    s = Scheduler({}, GatewayState(), fake)
    client = IEEEClient(CLIENT_CFG, GatewayState(), s)
    only_b = derc(dict(mrid="B", start=t + 60, duration=600, value=-200000))

    async def run():
        client._parse_and_dispatch(derc(dict(mrid="A", start=t, duration=3600, value=500000)))
        await tick(s, t)
        client._parse_and_dispatch(only_b)          # server now lists only B (A replaced)
        await tick(s, t + 60)
        client._parse_and_dispatch(only_b)
        await tick(s, t + 660)                      # B ends
    asyncio.run(run())
    history = [kw for kw, _ in fake.writes]
    expect(history[-1] != 500.0, f"setpoint history {history}: the withdrawn +500 kW event came back")


def test_C2c_force_idle_idles_the_running_event(db, monkeypatch):
    """C2c (fixed in Phase 1): the portal's Clear / Force Idle button now also
    engages the gateway's operator override, which outranks every schedule.
    (An empty utility list on its own is left alone: what removal from the
    list means is set by the utility's IEEE 2030.5 profile, not known yet.)"""
    t = 10_000
    fake = FakeModbus()
    s = Scheduler({}, GatewayState(), fake)
    client = IEEEClient(CLIENT_CFG, GatewayState(), s)
    portal = logged_in_client(load_grid_server(monkeypatch))

    async def run():
        client._parse_and_dispatch(derc(dict(mrid="A", start=t, duration=3600, value=500000)))
        await tick(s, t)
        assert portal.post("/dashboard/send", data={"action": "idle"}).status_code == 302
        client._parse_and_dispatch(derc())
        for i in range(1, 6):
            await tick(s, t + i)
    asyncio.run(run())
    assert fake.writes[-1][0] == 0.0, f"setpoint history {[kw for kw, _ in fake.writes]}"


# ── C3: loss of the utility link ─────────────────────────────────────────

async def _charging_through_an_outage(offline_for):
    """E (+300 kW charge, valid for an hour) is running; the utility link
    then goes down for `offline_for` seconds. Returns (fake, state, s, client,
    xml, now) with the scheduler ticked once during the outage."""
    fake = FakeBMS()
    drv, state = make_driver(fake, offline_timeout=5)
    await drv.connect()
    s = Scheduler({"offline_timeout_sec": 5}, state, drv)
    client = IEEEClient(CLIENT_CFG, state, s)
    now = int(time.time())
    xml = derc(dict(mrid="E", start=now - 5, duration=3600, value=300000))
    client._parse_and_dispatch(xml)
    state.mark_grid_contact()
    await tick(s, now)
    assert fake.setpoint_kw() == -300                          # precondition: charging
    state.mark_grid_lost()
    state.last_grid_contact = time.time() - offline_for
    await tick(s, now + 1)
    return fake, state, s, client, xml, now


def test_C3a_event_resumes_after_utility_outage(db):
    """C3a, recovery half (fixed in Phase 1): once the utility is back, the
    still-valid event is re-applied."""
    async def run():
        fake, state, s, client, xml, now = await _charging_through_an_outage(offline_for=60)
        client._parse_and_dispatch(xml)                        # utility back, same event re-served
        state.mark_grid_contact()
        for i in range(2, 6):
            await tick(s, now + i)
        return fake.setpoint_kw()
    assert asyncio.run(run()) == -300, "event still valid and re-served, battery left at 0 kW"


def test_C3a_valid_event_continues_through_a_prolonged_outage(db):
    """C3a (fixed in Phase 2; now a regression test): a valid approved event was
    abandoned (0 kW) once the utility had been unreachable longer than
    offline_timeout_sec. Sprint 2 Table 1 row 2: continue the last approved
    valid schedule until completion or expiry."""
    fake, state, s, *_ = asyncio.run(_charging_through_an_outage(offline_for=60))
    expect(fake.setpoint_kw() == -300,
           f"60 s into the outage the battery is at {fake.setpoint_kw()} kW (hardware sign); "
           f"authority={state.authority}")


def test_C3b_http_error_outage_is_handled_like_network_loss(db):
    """C3b (fixed in Phase 2; now a regression test): during an HTTP-error
    outage the FSM reported SAFE_IDLE while a valid approved event was still
    running. Sprint 2 Table 1: continuing the event is correct; SAFE_IDLE is
    only for 'no valid schedule'."""
    async def run():
        util = await Utility().start()
        fake = FakeBMS()
        drv, state = make_driver(fake, offline_timeout=2)
        await drv.connect()
        s = Scheduler({"offline_timeout_sec": 2}, state, drv)
        rm = ReliabilityManager({"offline_timeout_sec": 2}, state)
        client = IEEEClient(dict(CLIENT_CFG, ieee_server_url=util.url, offline_timeout_sec=2), state, s,
                            reliability_manager=rm)
        now = int(time.time())
        util.xml = derc(dict(mrid="E", start=now - 5, duration=3600, value=300000))
        async with aiohttp.ClientSession() as http:
            await client._poll(http)
            await tick(s, now)
            assert fake.setpoint_kw() == -300                  # precondition: charging
            util.mode = "503"
            state.last_grid_contact = time.time() - 60         # 60 s of HTTP 503
            for i in range(4):
                await client._poll(http)
                await tick(s, now + 1 + i)
        await util.stop()
        return fake.setpoint_kw(), state.grid_connected, rm.reliability_state
    wire, grid, fsm = asyncio.run(run())
    expect(not (wire != 0 and fsm is ReliabilityState.SAFE_IDLE),
           f"after 60 s of HTTP 503 the device setpoint is {wire} kW (hardware sign) "
           f"but the FSM says {fsm.value}; grid_connected={grid}")


def test_C3c_non_xml_200_is_not_healthy(db):
    """C3c (fixed in Phase 2; now a regression test): an HTTP 200 HTML page (proxy / captive portal) counts as a healthy utility link."""
    async def run():
        util = await Utility().start()
        util.mode = "html"
        state = GatewayState()
        state.mark_grid_lost()
        rm = ReliabilityManager({"offline_timeout_sec": 30}, state)
        for _ in range(3):
            rm.record_poll_failure()
        client = IEEEClient(dict(CLIENT_CFG, ieee_server_url=util.url), state,
                            Scheduler({}, state, FakeModbus()), reliability_manager=rm)
        async with aiohttp.ClientSession() as http:
            await client._poll(http)
        await util.stop()
        return rm.reliability_state, state.grid_connected
    fsm, grid = asyncio.run(run())
    expect(fsm is not ReliabilityState.CONNECTED, f"HTML page accepted: FSM={fsm.value}, grid_connected={grid}")


# ── C4 / C5: protocol model and wire contract ────────────────────────────

def test_C4_conformant_opModFixedW_is_accepted():
    """C4 (fixed in step 1 of the working answers; now a regression test): a schema-conformant opModFixedW (SignedPerCent) is rejected."""
    client = IEEEClient(CLIENT_CFG, GatewayState(), MagicMock())
    ctrl = first_control(derc(dict(mrid="STD", start=int(time.time()), duration=600,
                                   base="<opModFixedW>-5000</opModFixedW>")))
    expect(client._extract_event(ctrl) is not None,
           "<opModFixedW>-5000</opModFixedW> (charge at 50 %) rejected as 'no opModFixedW setpoint'")


async def _modbus_responder(frames, bms_type=1):
    """Minimal Modbus TCP server that records every request PDU. It keeps
    the holding registers written (FC16) for FC03 read-back and publishes
    BMS type, state On, Normal control mode and +/-10 MW power limits."""
    holding = {}
    inputs = {1038: bms_type, 1000: 4, 1036: 0}
    inputs.update(zip((1019, 1020), _encode_int32(-10_000)))
    inputs.update(zip((1021, 1022), _encode_int32(10_000)))

    async def handle(reader, writer):
        try:
            while True:
                tid, _, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                pdu = await reader.readexactly(length - 1)
                frames.append((unit, pdu))
                if pdu[0] in (0x03, 0x04):
                    address, count = struct.unpack(">HH", pdu[1:5])
                    table = holding if pdu[0] == 0x03 else inputs
                    values = [table.get(address + i, 0) for i in range(count)]
                    body = bytes([pdu[0], 2 * count]) + b"".join(struct.pack(">H", v) for v in values)
                elif pdu[0] == 0x10:
                    address, count = struct.unpack(">HH", pdu[1:5])
                    for i in range(count):
                        holding[address + i] = struct.unpack(">H", pdu[6 + 2 * i:8 + 2 * i])[0]
                    body = pdu[:5]
                else:
                    body = bytes([pdu[0] | 0x80, 0x01])
                writer.write(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
    return await asyncio.start_server(handle, "127.0.0.1", 0)


def test_C5_wire_frames_as_sent():
    """Not a correctness verdict — records the exact Modbus TCP frames a real
    pymodbus 3.6.9 client sends, to check against the Endurium ICD.
    Updated in step 4 of the working answers (requirement change): unit ID
    255 instead of 0 (E9), the alerts read at connect (E5), the dispatch
    modes set and read back before the first setpoint (E7; this responder's
    holding registers power up as 0 = Invalid) and the operator caps read
    with the limits (E10)."""
    frames = []

    async def run():
        server = await _modbus_responder(frames)
        port = server.sockets[0].getsockname()[1]
        drv = ModbusDriver({"modbus_host": "127.0.0.1", "modbus_port": port, "offline_timeout_sec": 5},
                           GatewayState())
        await drv.connect()
        await drv.set_power(500.0, source="t")
        drv._client.close()
        server.close()
    asyncio.run(run())
    writes = [(unit, pdu.hex(" ")) for unit, pdu in frames if pdu[0] == 0x10]
    reads = [(unit, pdu.hex(" ")) for unit, pdu in frames if pdu[0] == 0x04]
    readbacks = [(unit, pdu.hex(" ")) for unit, pdu in frames if pdu[0] == 0x03]
    # The BMS already reports On, so no Operating Command is sent (spec p.16).
    assert writes == [(255, "10 03 fb 00 01 02 00 01"),             # FC16 @1019 := 1 (Grid-Feeding)
                      (255, "10 03 fc 00 01 02 00 01"),             # FC16 @1020 := 1 (Fixed Active Power)
                      (255, "10 03 f3 00 02 04 00 00 00 00"),       # FC16 @1011-1012 := 0 kVAR
                      (255, "10 03 fd 00 01 02 00 01"),             # FC16 @1021 := 1 (Fixed Q)
                      (255, "10 03 f1 00 02 04 ff ff fe 0c")]       # FC16 @1009-1010 := -500, high word first
    assert reads[0] == (255, "04 04 0e 00 01")                      # FC04 @1038 x1 (BMS type; unit-ID probe)
    assert (255, "04 04 0f 00 02") in reads                         # FC04 @1039 x2 (alerts)
    assert (255, "04 03 fb 00 04") in reads                         # FC04 @1019 x4 (power limits)
    assert readbacks == [(255, "03 03 fa 00 04"),                   # FC03 @1018 x4 (dispatch modes)
                         (255, "03 03 fa 00 04"),                   # the same, read back
                         (255, "03 03 f6 00 04"),                   # FC03 @1014 x4 (operator caps)
                         (255, "03 03 f1 00 02")]                   # FC03 @1009 x2 (read-back)


# ── H1: FSM transition logic ─────────────────────────────────────────────

# The dispatch arbiter's published decision while a confirmed approved event runs.
RUNNING_APPROVED_EVENT = {"authority": "approved_schedule", "mrid": "E", "desired_kw": 300.0,
                          "reason": "event E", "until": None, "confirmed": True}

def test_H1a_fsm_stays_autonomous_while_schedule_valid():
    """H1a (fixed in Phase 2; now a regression test): FSM drops to SAFE_IDLE with a valid schedule once offline > timeout."""
    state = GatewayState()
    rm = ReliabilityManager({"offline_timeout_sec": 30}, state)
    state.last_grid_contact = time.time() - 60
    state.authority = RUNNING_APPROVED_EVENT         # what the dispatch arbiter publishes
    for _ in range(4):                               # the call IEEEClient makes per failed poll
        rm.record_poll_failure()
    expect(rm.reliability_state is ReliabilityState.AUTONOMOUS,
           f"FSM={rm.reliability_state.value} although a valid schedule exists")


def test_H1b_fsm_does_not_flap_while_autonomous(tmp_path):
    """H1b (fixed in Phase 2; now a regression test): every failed poll logs AUTONOMOUS->DISCONNECTED->AUTONOMOUS."""
    state = GatewayState()
    audit = AuditLog(db_path=tmp_path / "audit.db")
    rm = ReliabilityManager({"offline_timeout_sec": 30}, state, audit_log=audit)
    state.last_grid_contact = time.time() - 10
    state.authority = RUNNING_APPROVED_EVENT
    for _ in range(3):
        rm.record_poll_failure()
    assert rm.reliability_state is ReliabilityState.AUTONOMOUS      # precondition
    before = len(audit.recent(1000))
    for _ in range(3):
        rm.record_poll_failure()
    rows = audit.recent(1000)
    added = [r["detail"] for r in rows[: len(rows) - before]]
    expect(not added, f"{len(added)} transitions audited while nothing changed: {added}")


# ── H2: precedence ───────────────────────────────────────────────────────

def test_H2_retry_does_not_invert_precedence(db):
    """H2 (fixed in Phase 1; now a regression test): a retried start overrides a later-starting event that already took control."""
    now = 10_000
    fake = FakeModbus(fail=lambda source, n: source == "schedule:A" and n == 1)
    s = Scheduler({}, GatewayState(), fake)
    s.add_event(ev("A", now - 10, now + 3600, 500.0))     # earlier start
    s.add_event(ev("B", now - 5, now + 1800, -200.0))     # later start — wins by _active_successor's rule

    async def run():
        await tick(s, now)
        await tick(s, now + 1)
    asyncio.run(run())
    history = [kw for kw, _ in fake.writes]
    expect(history[-1] == -200.0, f"setpoint history {history}: A's retry overrode B")


# ── H3: success without device confirmation ──────────────────────────────

def test_H3a_write_to_bms_that_is_off_is_not_success():
    """H3a (fixed in Phase 3; now a regression test): set_power reports success while the BMS reports Operating State = Off."""
    async def run():
        fake = FakeBMS()
        drv, state = make_driver(fake)
        await drv.connect()
        fake.ir[1000] = 1                         # the BMS drops to Off behind the gateway's back
        return await drv.set_power(300.0, source="t"), state.mode
    ok, mode = asyncio.run(run())
    expect(ok is False, f"set_power returned {ok}; state.mode={mode.value}; BMS Operating State=Off")


def test_H3b_unknown_bms_type_disables_control(caplog):
    """H3b (fixed in Phase 3; now a regression test): an unknown BMS type (31038 = 2) is logged as DC but written to the AC register."""
    caplog.set_level(logging.INFO, logger="modbus_driver")

    async def run():
        fake = FakeBMS(bms_type=2)
        drv, _ = make_driver(fake)
        await drv.connect()
        ok = await drv.set_power(300.0, source="t")
        return ok, [address for _, address, _, _ in fake.writes]
    ok, addresses = asyncio.run(run())
    logged = [r.getMessage() for r in caplog.records if "BMS type" in r.getMessage()]
    expect(not ok and 1009 not in addresses and 1007 not in addresses,
           f"log: {logged}; set_power={ok}; registers written={addresses}")


def test_H3c_frozen_bms_heartbeat_is_detected():
    """H3c (fixed in Phase 3; now a regression test): a frozen BMS heartbeat (31003 never changes) goes unnoticed."""
    async def run():
        fake = FakeBMS()                                   # 31003 stays 0 forever
        drv, state = make_driver(fake)
        await drv.connect()
        await drv.heartbeat_once()
        drv._heartbeat_changed_at -= 10                    # ten seconds without a change
        await drv.heartbeat_once()
        return state.bms_heartbeat_stale
    expect(asyncio.run(run()) is True, "31003 unchanged for 10 s and nothing reported it")


# ── H4: operating envelope / encoding ────────────────────────────────────

def test_H4a_out_of_range_setpoint_is_rejected():
    """H4a (fixed in Phase 3 in the driver, and since Phase 4 also at the
    utility boundary; now a regression test): an out-of-range setpoint wraps
    in int32 and reverses direction instead of being rejected."""
    event = IEEEClient(CLIENT_CFG, GatewayState(), MagicMock())._extract_event(
        first_control(derc(dict(mrid="BIG", start=0, duration=60, value=-3000, mult=9))))
    expect(event is None, f"-3000 x 10^9 W accepted from the utility: {event}")

    async def run():
        fake = FakeBMS()
        drv, _ = make_driver(fake)
        await drv.connect()
        ok = await drv.set_power(-3_000_000_000.0, source="t")    # the same request, straight to the driver
        return ok, [words for _, address, words, _ in fake.writes if address == 1009]
    ok, written = asyncio.run(run())
    expect(not ok and not written,
           f"requested -3,000,000,000 kW (discharge) -> set_power={ok}; setpoint words written "
           f"{[[hex(w) for w in words] for words in written]}")


def test_H4b_state_matches_value_written():
    """H4b (fixed in Phase 4; now a regression test): sub-kW setpoints are written as 0 while state records the requested value."""
    async def run():
        fake = FakeBMS()
        drv, state = make_driver(fake)
        await drv.connect()
        await drv.set_power(0.4, source="t")
        return fake.setpoint_kw(), state.mode, state.power_kw
    wire, mode, kw = asyncio.run(run())
    expect(kw == wire and not (wire == 0 and mode is not BatteryMode.IDLE),
           f"wire setpoint {wire} kW; state says {mode.value} {kw} kW")


def test_H4c_existing_charge_fixture_is_half_a_kilowatt():
    """Evidence for M9: test_capstone.py's SAMPLE_XML_CHARGE (500000 x 10^-3 W,
    -500 W as an opModTargetW since step 1) is 0.5 kW. Until Phase 4 the
    driver truncated it to 0; it now rounds to the nearest whole kW, halves
    away from zero: +1 kW (hardware -1)."""
    event = IEEEClient(CLIENT_CFG, GatewayState(), MagicMock())._extract_event(
        first_control(derc(dict(mrid="TEST001", start=0, duration=3600, value=500000, mult=-3))))
    assert event["power_kw"] == 0.5

    async def run():
        fake = FakeBMS()
        drv, _ = make_driver(fake)
        await drv.connect()
        await drv.set_power(event["power_kw"], source="t")
        return fake.writes[-1][2]
    assert asyncio.run(run()) == [0xFFFF, 0xFFFF]


# ── H6: failure handling ─────────────────────────────────────────────────

def test_H6a_bad_event_does_not_abort_the_list(db):
    """H6a (fixed in Phase 2; now a regression test): one bad multiplier aborts the whole DERControlList."""
    s = Scheduler({}, GatewayState(), FakeModbus())
    xml = derc(dict(mrid="A", start=0, duration=60, value=1000),
               dict(mrid="BAD", start=0, duration=60, value=1, mult=400),
               dict(mrid="B", start=0, duration=60, value=2000))
    try:
        IEEEClient(CLIENT_CFG, GatewayState(), s)._parse_and_dispatch(xml)
        error = None
    except Exception as exc:  # noqa: BLE001 — the point is that it escapes
        error = exc
    with s._db() as conn:
        stored = [row[0] for row in conn.execute("SELECT mrid FROM events ORDER BY mrid")]
    expect(error is None and "B" in stored, f"raised {error!r}; stored={stored}")


def test_H6b_data_error_is_not_reported_as_grid_loss(db):
    """H6b (fixed in Phase 2; now a regression test): a data error after HTTP 200 is reported as utility-link loss and later events are dropped."""
    async def run():
        util = await Utility().start()
        util.xml = derc(dict(mrid="A", start=0, duration=60, value=1000),
                        dict(mrid="BAD", start=0, duration=60, value=1, mult=400),
                        dict(mrid="B", start=0, duration=60, value=2000))
        state = GatewayState()
        s = Scheduler({}, state, FakeModbus())
        async with aiohttp.ClientSession() as http:
            await IEEEClient(dict(CLIENT_CFG, ieee_server_url=util.url), state, s)._poll(http)
        await util.stop()
        with s._db() as conn:
            stored = [row[0] for row in conn.execute("SELECT mrid FROM events ORDER BY mrid")]
        return state.grid_connected, stored, [m for m, _ in util.responses]
    grid, stored, acked = asyncio.run(run())
    expect(grid and "B" in stored, f"server reachable but grid_connected={grid}; stored={stored}; acknowledged={acked}")


def test_H6c_scheduler_survives_a_database_error(db):
    """H6c (fixed in Phase 1; now a regression test): one SQLite error kills the scheduler task for good."""
    async def run():
        s = Scheduler({}, GatewayState(), FakeModbus())
        now = int(time.time())
        s.add_event(ev("E", now - 5, now + 600, 100.0))
        blocker = sqlite3.connect(db, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")                 # another writer holds the lock > 5 s
        task = asyncio.create_task(s.run())
        await asyncio.sleep(6.5)
        blocker.rollback()
        blocker.close()                                    # lock released
        await asyncio.sleep(2.0)
        died = task.done()
        error = task.exception() if died else None
        if not died:
            task.cancel()
        return died, error
    died, error = asyncio.run(run())
    expect(not died, f"scheduler task still dead 2 s after the lock was released: {error!r}")


# ── H7: audit trail ──────────────────────────────────────────────────────

def test_H7a_audit_record_carries_the_event_date(db, tmp_path):
    """H7a (fixed in Phase 1; now a regression test): the audit log records event start/end as HH:MM:SS with no date."""
    audit = AuditLog(db_path=tmp_path / "audit.db")
    start = int(time.time()) + 2 * 86400
    Scheduler({}, GatewayState(), FakeModbus(), audit_log=audit).add_event(ev("FUTURE", start, start + 3600, 100.0))
    detail = audit.recent(1)[0]["detail"]
    date = time.strftime("%Y-%m-%d", time.gmtime(start))
    expect(date in detail, f"audit detail {detail!r} — event date {date} not recorded")


def test_H7b_every_modbus_write_is_audited(tmp_path, monkeypatch):
    """H7b (fixed in Phase 3; now a regression test): register writes and watchdog-forced idles never reach the audit log."""
    monkeypatch.setattr(modbus_mod, "asyncio", _FastAsyncio())
    audit = AuditLog(db_path=tmp_path / "audit.db")

    async def run():
        fake = FakeBMS()
        drv, state = make_driver(fake, audit_log=audit, offline_timeout=5)
        await drv.connect()
        await drv.set_power(300.0, source="schedule:E")
        state.mark_grid_lost()
        state.last_grid_contact = time.time() - 60
        watchdog = asyncio.create_task(drv.watchdog())
        await asyncio.sleep(0.05)
        watchdog.cancel()
        return len(fake.writes)
    n_writes = asyncio.run(run())
    rows = audit.recent(1000)
    expect(rows, f"{n_writes} Modbus writes (On, setpoint, watchdog idles) -> {len(rows)} audit rows")


def test_H7c_failed_event_gets_a_terminal_audit_record(db, tmp_path):
    """H7c (fixed in Phase 1): one row for the failing dispatch, not one per
    retry, and a final record when the window closes without E ever running."""
    audit = AuditLog(db_path=tmp_path / "audit.db")
    s = Scheduler({}, GatewayState(), FakeModbus(fail=lambda source, n: source == "schedule:E"),
                  audit_log=audit)
    s.add_event(ev("E", 1000, 1010, 100.0))

    async def run():
        for now in range(1000, 1015):
            await tick(s, now)
    asyncio.run(run())
    rows = list(reversed(audit.recent(1000)))
    failures = [r for r in rows if r["event_type"] == "dispatch" and r["source"] == "schedule:E"]
    final = [r for r in rows if r["event_type"] == "schedule_status" and "mrid=E" in r["detail"]][-1]
    assert len(failures) == 1, f"{len(failures)} dispatch rows for E's failing start"
    assert final["outcome"] == "expired" and "ever being applied" in final["detail"], final


# ── H8: dashboard truthfulness ───────────────────────────────────────────

def test_H8a_dashboard_flags_stale_device_data(tmp_path, monkeypatch):
    """H8a (fixed in Phase 5; now a regression test): the dashboard shows device data of any age as 'updated 0s ago'."""
    import capstone.__main__ as main_mod
    status = tmp_path / "live_status.json"
    monkeypatch.setattr(main_mod, "STATUS_FILE", status)
    grid = load_grid_server(monkeypatch, status)
    state = GatewayState()
    state.soc_pct, state.operating_state, state.operating_status = 87.5, "On", "Normal"
    state.last_status_poll_at = time.time() - 3600    # last successful BMS read: an hour ago
    rm = ReliabilityManager({"offline_timeout_sec": 30}, state)

    async def run():
        task = asyncio.create_task(main_mod.publish_status_loop(state, rm))
        await asyncio.sleep(0.2)
        task.cancel()
    asyncio.run(run())
    battery = logged_in_client(grid).get("/api/status").get_json()["battery"]
    expect(battery.get("device_stale") is True and battery.get("device_age_sec", 0) >= 3600,
           f"dashboard payload for a BMS not read in an hour: {battery}")


def test_H8b_state_reflects_device_not_request():
    """H8b (fixed in Phase 3; now a regression test): state (and the dashboard) say CHARGE even when the device never took the setpoint."""
    async def run():
        fake = FakeBMS()
        drv, state = make_driver(fake)
        await drv.connect()
        fake.ignore_writes = True
        ok = await drv.set_power(300.0, source="t")
        return ok, state.mode, state.power_kw, fake.setpoint_kw()
    ok, mode, kw, wire = asyncio.run(run())
    expect(not (ok and mode is BatteryMode.CHARGE and wire == 0),
           f"set_power={ok}; state={mode.value} {kw} kW; device setpoint={wire}")


def test_H8c_dashboard_shows_bms_status(monkeypatch):
    """H8c (fixed in Phase 5; now a regression test): BMS operating_status (Fault/Critical) is never shown on the dashboard."""
    grid = load_grid_server(monkeypatch)
    expect("operating_status" in grid.DASHBOARD_TEMPLATE, "DASHBOARD_TEMPLATE never renders operating_status")


# ── H9 / M8: operator portal ─────────────────────────────────────────────

@finding("H9a", "/simulate/* dispatches the battery without logging in")
def test_H9a_simulate_requires_login(monkeypatch):
    grid = load_grid_server(monkeypatch)
    response = grid.app.test_client().post("/simulate/charge", json={"power_kw": 500, "duration_sec": 60})
    expect(response.status_code in (401, 403), f"anonymous POST /simulate/charge -> {response.status_code} "
                                               f"{response.get_json()}")


def test_H9b_nan_power_is_rejected(monkeypatch):
    """H9b (fixed in Phase 6; now a regression test): one anonymous NaN request breaks the utility feed for every later poll."""
    grid = load_grid_server(monkeypatch)
    client = grid.app.test_client()
    client.post("/simulate/charge", json={"power_kw": "nan", "duration_sec": 60})
    feed = [client.get("/derp/1/derc").status_code for _ in range(3)]
    expect(feed == [200, 200, 200], f"GET /derp/1/derc after the NaN request -> {feed}")


def test_M8_markup_in_mrid_is_rejected_or_escaped(monkeypatch):
    """M8 (fixed in Phase 6; now a regression test): an mRID containing HTML is stored and served unescaped to a page that renders it with innerHTML."""
    grid = load_grid_server(monkeypatch)
    payload = "<img src=x onerror=\"fetch('/dashboard/send',{method:'POST'})\">"
    xml = derc(dict(mrid=escape(payload), start=int(time.time()), duration=600, value=1000))
    anonymous = grid.app.test_client().post("/simulate/raw", data=xml, content_type="application/xml")
    served = logged_in_client(grid).get("/api/status").get_json()["active_events"][0]["mrid"]
    rendered_raw = "innerHTML" in grid.DASHBOARD_TEMPLATE and "'<tr><td>' + e.mrid" in grid.DASHBOARD_TEMPLATE
    expect(served != payload or not rendered_raw,
           f"anonymous POST /simulate/raw -> {anonymous.status_code}; /api/status serves {served!r}; "
           f"template concatenates e.mrid into innerHTML")


# ── H10: DERControlResponse ──────────────────────────────────────────────

async def _responses_after_polls(xml, polls):
    """Poll `polls` times, delivering whatever responses each poll queued
    (since step 2 they are sent by the response loop, not the poll)."""
    util = await Utility().start()
    util.xml = xml
    client = IEEEClient(dict(CLIENT_CFG, ieee_server_url=util.url), GatewayState(),
                        Scheduler({}, GatewayState(), FakeModbus()))
    async with aiohttp.ClientSession() as http:
        for _ in range(polls):
            await client._poll(http)
            await client.send_responses(http)
    await util.stop()
    return util.responses


def _statuses(responses):
    return [ET.fromstring(body).findtext("{*}status") for _, body in responses]


# The events ask for responses as CSIP's example DERControl does
# (replyTo, responseRequired "03"): IEEE 2030.5 sends none otherwise.

def test_H10a_response_sent_once_per_status_change(db):
    """H10a (fixed in step 2 of the working answers; now a regression test): the same DERControlResponse is re-POSTed on every poll."""
    responses = asyncio.run(_responses_after_polls(
        derc(dict(mrid="E", start=int(time.time()), duration=600, value=1000, response_required="03")),
        polls=3))
    expect(len(responses) == 1, f"{len(responses)} Responses for one unchanged event over 3 polls")


def test_H10b_response_has_required_fields(db):
    """H10b (fixed in step 2 of the working answers; now a regression test): DERControlResponse lacks the schema's required endDeviceLFDI and subject."""
    responses = asyncio.run(_responses_after_polls(
        derc(dict(mrid="E", start=int(time.time()), duration=600, value=1000, response_required="03")),
        polls=1))
    body = responses[0][1]
    expect("endDeviceLFDI" in body and "<subject>" in body, f"body sent: {body}")


# ── Medium findings ──────────────────────────────────────────────────────

def test_M1_final_shutdown_write_is_idle_even_with_a_queued_start():
    """M1 (shutdown race) — NOT REPRODUCED. With a start queued on the driver
    lock behind a slow heartbeat, __main__'s sequence (cancel tracked tasks,
    gather, then safe_idle) still ends on 0: asyncio.Lock is FIFO, so the
    queued start writes before the idle."""
    async def run():
        fake = FakeBMS()
        drv, _ = make_driver(fake)
        await drv.connect()
        fake.write_delay[1002] = 0.3

        async def heartbeat_once():
            async with drv._lock:
                await drv._write_holding_registers(1002, [1])
        heartbeat = asyncio.create_task(heartbeat_once())
        await asyncio.sleep(0.01)
        start = asyncio.create_task(drv.set_power(300.0, source="schedule:X"))
        await asyncio.sleep(0.01)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await asyncio.wait_for(drv.safe_idle(), timeout=5)
        await asyncio.gather(start, return_exceptions=True)
        return fake.setpoint_kw(), [values for _, address, values, _ in fake.writes if address == 1009]
    final, setpoint_writes = asyncio.run(run())
    assert setpoint_writes == [[0xFFFF, 0xFED4], [0, 0]]      # the queued start first, then the idle
    assert final == 0


def test_M2_update_during_inflight_write_is_applied(db):
    """M2 (fixed in Phase 1)."""
    async def run():
        gate = asyncio.Event()
        fake = FakeModbus(gate=gate)
        s = Scheduler({}, GatewayState(), fake)
        now = 10_000
        s.add_event(ev("E", now - 5, now + 600, 100.0))
        first = asyncio.create_task(s.tick(now))
        for _ in range(5):
            await asyncio.sleep(0)                             # start write for 100 kW is in flight
        s.add_event(ev("E", now - 5, now + 600, 200.0))        # utility updates E meanwhile
        gate.set()
        await first
        for i in range(1, 4):
            await tick(s, now + i)
        return [kw for kw, _ in fake.writes]
    history = asyncio.run(run())
    assert history and history[-1] == 200.0, f"setpoint history {history}: the update to 200 kW never applied"


@finding("M3", "the operator heartbeat stalls for the whole duration of a slow setpoint write")
def test_M3_heartbeat_keeps_its_cadence_during_a_slow_write():
    async def run():
        fake = FakeBMS()
        drv, _ = make_driver(fake)
        await drv.connect()
        fake.write_delay[1009] = 2.5
        heartbeat = asyncio.create_task(drv.heartbeat_loop())
        await asyncio.sleep(1.5)
        await drv.set_power(300.0, source="t")
        await asyncio.sleep(1.5)
        heartbeat.cancel()
        beats = [t for t, address, _, _ in fake.writes if address == 1002]
        return max(b - a for a, b in zip(beats, beats[1:]))
    gap = asyncio.run(run())
    expect(gap < 2.0, f"longest gap between operator heartbeats: {gap:.2f} s (nominal cadence 1 s)")


def test_M3b_locked_database_does_not_freeze_the_event_loop(db):
    """M3b (fixed in Phase 6; now a regression test): a locked SQLite database freezes the whole event loop (heartbeat, polling) for the busy timeout."""
    async def run():
        s = Scheduler({}, GatewayState(), FakeModbus())
        now = int(time.time())
        s.add_event(ev("E", now - 5, now + 600, 100.0))
        blocker = sqlite3.connect(db, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")                   # e.g. a second instance or a DB browser
        beats = []

        async def other_work():                              # stands in for heartbeat / polling
            while True:
                beats.append(time.monotonic())
                await asyncio.sleep(0.1)
        worker = asyncio.create_task(other_work())
        await asyncio.sleep(0.3)
        try:
            await s.tick(now)                                # one scheduler tick; E takes authority -> a write
        except sqlite3.OperationalError:
            pass
        await asyncio.sleep(0.3)
        worker.cancel()
        blocker.rollback()
        blocker.close()
        return max(b - a for a, b in zip(beats, beats[1:]))
    gap = asyncio.run(run())
    expect(gap < 1.0, f"event loop frozen for {gap:.1f} s while one SQLite call waited on the lock")


def test_M5a_events_without_mrid_are_not_merged(db):
    """M5a (fixed in Phase 6; now a regression test): two DERControls without an mRID are merged as 'unknown'; one is silently lost."""
    s = Scheduler({}, GatewayState(), FakeModbus())
    IEEEClient(CLIENT_CFG, GatewayState(), s)._parse_and_dispatch(
        derc(dict(mrid=None, start=100, duration=60, value=1000),
             dict(mrid=None, start=500, duration=60, value=-2000)))
    with s._db() as conn:
        rows = conn.execute("SELECT mrid, start_ts, power_kw FROM events").fetchall()
    expect(len(rows) != 1, f"two distinct events stored as {rows}")


def test_M5b_duplicate_mrid_in_one_list_is_not_reexecuted(db):
    """M5b (fixed in Phase 1; now a regression test): a list containing the same mRID twice re-executes it on every poll."""
    fake = FakeModbus()
    s = Scheduler({}, GatewayState(), fake)
    client = IEEEClient(CLIENT_CFG, GatewayState(), s)
    now = 10_000
    xml = derc(dict(mrid="D", start=now - 5, duration=600, value=1000),
               dict(mrid="D", start=now - 5, duration=600, value=2000))

    async def run():
        for i in range(3):
            client._parse_and_dispatch(xml)
            await tick(s, now + i)
    asyncio.run(run())
    expect(len(fake.writes) == 1, f"{len(fake.writes)} writes over 3 polls: {fake.writes}")


def test_M5c_negative_duration_is_rejected():
    """M5c (fixed in Phase 4; now a regression test): a negative duration is accepted instead of rejected."""
    event = IEEEClient(CLIENT_CFG, GatewayState(), MagicMock())._extract_event(
        first_control(derc(dict(mrid="NEG", start=1000, duration=-60, value=1000))))
    expect(event is None, f"accepted: {event}")


def test_M5d_expired_event_is_not_acknowledged_as_received(db):
    """M5d (fixed in step 2 of the working answers; now a regression test): an already-expired event is acknowledged as EventReceived.
    Since step 2 statuses are numeric: it is answered only with 254,
    "Rejected - event received after it had expired"."""
    responses = asyncio.run(_responses_after_polls(
        derc(dict(mrid="OLD", start=int(time.time()) - 7200, duration=600, value=1000,
                  response_required="03")), polls=1))
    expect(_statuses(responses) == ["254"], f"responses sent: {responses}")


def test_M7a_client_offers_the_2030_5_cipher():
    """M7a (fixed in step 5 of the working answers; now a regression test): the IEEE 2030.5 mandatory cipher TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8 is never offered.
    Offered is not enough on OpenSSL 3, which allows CCM_8 only at security
    level 0: tests/test_reporting.py proves a real handshake negotiates it."""
    ctx = build_mtls_context({"tls_ca_cert": str(ROOT / "certs" / "ca.crt"),
                              "tls_client_cert": str(ROOT / "certs" / "client.crt"),
                              "tls_client_key": str(ROOT / "certs" / "client.key")})
    names = [c["name"] for c in ctx.get_ciphers()]
    expect(any("CCM8" in name for name in names), f"{len(names)} ciphers offered, none CCM8")


def test_M7b_missing_tls_settings_fail_closed(monkeypatch):
    """M7b (fixed in Phase 6; now a regression test): with no TLS settings the client silently uses plain HTTP.
    Plain HTTP is now refused at startup unless the utility is this machine
    (the local simulator) or ALLOW_INSECURE_TRANSPORT is set, and the transport
    in use is always announced."""
    import capstone.__main__ as main_mod
    for name in ("TLS_CA_CERT", "TLS_CLIENT_CERT", "TLS_CLIENT_KEY", "ALLOW_INSECURE_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IEEE_SERVER_URL", "http://utility.example:5001")
    try:
        outcome, refused = f"accepted: {main_mod.load_config()['transport']}", False
    except ValueError as exc:
        outcome, refused = repr(exc), True
    expect(refused, f"plain HTTP to a remote utility with no TLS settings: {outcome}")


# ── Low findings ─────────────────────────────────────────────────────────

def test_L2_invalid_config_is_rejected(monkeypatch):
    """L2 (fixed in Phase 6; now a regression test): nonsensical configuration (poll interval 0, negative timeout) is accepted."""
    import capstone.__main__ as main_mod
    monkeypatch.setenv("IEEE_POLL_INTERVAL_SEC", "0")
    monkeypatch.setenv("OFFLINE_TIMEOUT_SEC", "-5")
    try:
        cfg = main_mod.load_config()
        outcome, rejected = f"accepted poll={cfg['ieee_poll_interval_sec']} timeout={cfg['offline_timeout_sec']}", False
    except ValueError as exc:
        outcome, rejected = repr(exc), True
    expect(rejected, outcome)



# ── S: conformance with SPC100644 Endurium BMS Modbus Interface Spec Rev 03 ──
# These show what the GATEWAY does against a BMS behaving as the spec
# describes; what real hardware then does still needs hardware testing.

def test_S1_ac_dispatch_modes_are_configured():
    """S1 (fixed in step 4 of the working answers, E7; now a regression test): AC dispatch modes are never set: 41019 Grid Mode / 41020 Active Power Control Mode (0 = Invalid; spec §6.3, p.33)."""
    async def run():
        fake = FakeBMS(bms_type=1)
        drv, _ = make_driver(fake)
        await drv.connect()
        await drv.set_power(300.0, source="t")
        return {address: values for _, address, values, _ in fake.writes}
    written = asyncio.run(run())
    expect(written.get(1019) == [1] and written.get(1020) == [1],
           f"registers written: {sorted(written)} — Grid Mode (41019) and Active Power Control Mode (41020) "
           f"never set to Grid-Feeding / Fixed Active Power")


def test_S2_dc_configured_dispatch_is_not_reported_as_executed():
    """S2 (fixed in step 4 of the working answers, E8; now a regression test): on a DC-configured BMS only 41007 'DC Power Target' (a forecast, spec p.32) is written, yet dispatch is reported."""
    async def run():
        fake = FakeBMS(bms_type=0)
        drv, state = make_driver(fake)
        await drv.connect()
        ok = await drv.set_power(300.0, source="t")
        return ok, state.mode, sorted({address for _, address, _, _ in fake.writes})
    ok, mode, addresses = asyncio.run(run())
    expect(not ok or 1003 in addresses or 1005 in addresses,
           f"set_power={ok}, state={mode.value}, registers written={addresses}: no readiness command "
           f"(41003/41005) and no PCS path — on DC systems the plant controller drives the PCS (spec §2)")


def test_S3_setpoint_respects_published_limits():
    """S3 (fixed in Phase 3; now a regression test): published power limits 31019/31021 are ignored; spec §6.2.6 says they must be adhered to at all times."""
    async def run():
        fake = FakeBMS()
        fake.set_input_int32(1019, -100)          # Charge Power Limit: 100 kW available (P_Charge < 0)
        fake.set_input_int32(1021, 100)           # Discharge Power Limit (P_Discharge > 0)
        drv, _ = make_driver(fake)
        await drv.connect()
        await drv.set_power(300.0, source="t")    # app convention: +300 = charge 300 kW
        return fake.setpoint_kw()
    written = asyncio.run(run())
    expect(written >= -100, f"BMS publishes a 100 kW charge limit; gateway wrote {written} kW (hardware sign)")


def test_S4a_turn_on_follows_the_documented_state_machine():
    """S4a (fixed in Phase 3; now a regression test): from Off the gateway writes On directly; the documented state machine needs Standby first (p.16)."""
    async def run():
        fake = FakeBMS(operating_state=1)         # Off
        drv, _ = make_driver(fake)
        await drv.connect()
        return [values[0] for _, address, values, _ in fake.writes if address == 1000]
    commands = asyncio.run(run())
    expect(commands[:1] == [1], f"Operating Command writes: {commands} (1 = Standby, 2 = On); BMS was Off")


def test_S4b_on_is_reissued_after_fault_recovery():
    """S4b (fixed in Phase 3; now a regression test): after a Fault clears the BMS sits in Standby (p.16); the gateway never re-issues On and keeps reporting dispatch."""
    async def run():
        fake = FakeBMS(operating_state=4)         # On
        drv, state = make_driver(fake)
        await drv.connect()
        fake.ir[1000] = 2                         # Fault cleared -> Standby (spec p.16)
        await drv.supervise()                     # the 10 s status poll
        ok = await drv.set_power(300.0, source="t")
        on_commands = sum(1 for _, address, values, _ in fake.writes if address == 1000 and values == [2])
        return ok, state.mode, on_commands
    ok, mode, on_commands = asyncio.run(run())
    expect(on_commands >= 1 and ok,
           f"BMS in Standby; On sent {on_commands}x; set_power={ok}; state={mode.value}")


def test_S5_user_controlled_mode_is_detected():
    """S5 (fixed in Phase 3; now a regression test): User-Controlled mode (31036 = 1) makes writes 'accepted but not stored' (§6.1); the gateway reports success."""
    async def run():
        fake = FakeBMS()
        fake.ir[1036] = 1
        drv, state = make_driver(fake)
        await drv.connect()
        fake.ignore_writes = True                 # what the spec says the BMS does in this mode
        ok = await drv.set_power(300.0, source="t")
        return ok, state.mode, fake.setpoint_kw()
    ok, mode, wire = asyncio.run(run())
    expect(not ok, f"Control Mode = User-Controlled; set_power={ok}; state={mode.value}; stored setpoint={wire}")


def test_S6_heartbeat_echoes_the_bms_heartbeat(monkeypatch):
    """S6 (fixed in Phase 3; now a regression test): operator heartbeat is the gateway's own counter, not an echo of 31003 (§4.5.1), and restarts at 1."""
    monkeypatch.setattr(modbus_mod, "asyncio", _FastAsyncio())

    async def run():
        fake = FakeBMS()
        fake.ir[1003] = 5000                      # BMS heartbeat already running when the gateway (re)starts
        drv, _ = make_driver(fake)
        await drv.connect()
        heartbeat = asyncio.create_task(drv.heartbeat_loop())
        for value in (5001, 5002, 5003):
            await asyncio.sleep(0.005)
            fake.ir[1003] = value
        await asyncio.sleep(0.005)
        heartbeat.cancel()
        return [values[0] for _, address, values, _ in fake.writes if address == 1002][:5]
    written = asyncio.run(run())
    expect(written and written[0] >= 5000,
           f"BMS heartbeat 31003 = 5000..5003; gateway wrote 41002 = {written}")


def test_L4_entity_expansion_bomb_is_blocked_by_expat():
    """L4 (XML entity bomb) — NOT REPRODUCED on this runtime: the bundled
    expat refuses the amplification (10 MB from ~0.5 KB of input)."""
    entities = '<!ENTITY a "aaaaaaaaaa">' + "".join(
        f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">' for i in range(6))
    bomb = (f'<?xml version="1.0"?><!DOCTYPE l [{entities}]>'
            f'<DERControlList xmlns="urn:ieee:std:2030.5:ns"><DERControl><mRID>&g;</mRID></DERControl></DERControlList>')
    with pytest.raises(ET.ParseError, match="amplification"):
        ET.fromstring(bomb)
