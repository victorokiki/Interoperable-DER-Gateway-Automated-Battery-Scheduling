"""
tests/fakes.py
Test doubles shared by the regression suites.

FakeBMS stands in for pymodbus' AsyncModbusTcpClient talking to an Endurium
BMS, using the register addresses capstone.modbus_driver uses (SPC100644
Rev 03, zero-based: 41009 -> 1009). It applies every accepted write to its
register map, records it, and can inject the failures the gateway has to
survive: a refused write (Modbus exception), a dead link, failing reads and
writes that hang until released.
"""

import asyncio
import time
import xml.etree.ElementTree as ET

from aiohttp import web

from capstone.modbus_driver import MAX_REGISTER_RATE, MAX_REGISTERS_PER_REQUEST, ModbusDriver, _decode_int32
from capstone.state import GatewayState

AC_SETPOINT = 1009   # 41009 Active Power Setpoint (AC-configured), int32 kW, P > 0 = discharge
DC_TARGET = 1007     # 41007 DC Power Target (DC-configured)


class Response:
    def __init__(self, registers=None, exception_code=None):
        self.registers = registers or []
        self.exception_code = exception_code

    def isError(self):
        return self.exception_code is not None


OFF, STANDBY, ON, FAULT = 1, 2, 4, 10


def _words(value):
    value &= 0xFFFFFFFF
    return [value >> 16, value & 0xFFFF]


class FakeBMS:
    """Behaves as SPC100644 Rev 03 describes where it is explicit:
      * 41000 Operating Command follows the state machine (p.16): Standby
        from Off or On, On only from Standby, Off only from Standby;
      * User-Controlled mode (31036 = 1) accepts writes but does not store
        them (§6.1);
      * 31019 / 31021 publish the Charge / Discharge Power Limits (generous
        by default); a Fault sets both to 0 (§6.2.8) and clears to Standby;
      * the measured power (31009 AC / 31007 DC) follows the setpoint while
        On and, on an AC-configured BMS, while 41018-41021 select
        System-Level, Grid-Feeding, Fixed Active Power (§6.3: 0 = Invalid,
        the power-up value here); it is 0 otherwise;
      * 31003, the BMS heartbeat, increments every second (p.23) unless
        heartbeat_live is False; beat() and assignments to ir[1003] still
        set it directly.
    Where the ICD is silent it can play either answer: whether 41000 acts on
    every write or only on a change (change_triggered), which unit IDs it
    answers (answers_units; others get exception 11, or no answer when
    other_units is "silent"), and state codes outside the ICD's table that
    still deliver power (running_states)."""

    def __init__(self, bms_type=1, operating_state=ON):
        self.connected = False
        self.hr = {}
        self.ir = {1000: operating_state, 1001: 1, 1003: 0, 1004: 500, 1036: 0,
                   1038: bms_type, 1039: 0, 1040: 0}
        self.set_limits(charge_kw=10_000, discharge_kw=10_000)
        self.accept_on = True       # False: the BMS never leaves Standby when told On
        self.change_triggered = False   # True: 41000 acts only when its value changes
        self.running_states = {ON}  # states in which power flows
        self.heartbeat_live = True  # 31003 increments every second
        self._beat_at = time.monotonic()
        self.answers_units = None   # unit IDs answered (None: any, like a Modbus TCP device addressed by IP)
        self.other_units = "exception"  # other unit IDs: "exception" (11) or "silent" (never answered)
        self.unit_log = []          # unit ID of every request, in order
        self.writes = []            # (monotonic time, address, [words]) of every write the BMS stored
        self.ignored = []           # writes accepted but not stored (User-Controlled mode)
        self.reject = None          # callable(address, words) -> Modbus exception code, or None to accept
        self.link_down = False      # reads and writes raise ConnectionError
        self.fail_reads = set()     # input-register addresses whose reads raise
        self.holding_read_errors = {}   # holding address -> Modbus exception code, or None: no answer (raises)
        self.write_gate = None      # asyncio.Event: writes wait until it is set
        self.gated = []             # addresses of writes that reached the gate
        self.access_log = []        # (monotonic time, registers) of every request that reached the BMS (§5)
        self.enforce_rate = False   # True: requests above 50 registers a second are dropped (§5.2.1)
        self.dropped = []           # (monotonic time, registers) of the requests dropped that way

    # ── test controls ─────────────────────────────────────────────────
    @property
    def state(self):
        return self.ir[1000]

    def set_limits(self, charge_kw, discharge_kw):
        """Largest charge / discharge power permitted, as positive kW."""
        self.ir[1019], self.ir[1020] = _words(-charge_kw)     # 31019: P < 0
        self.ir[1021], self.ir[1022] = _words(discharge_kw)   # 31021: P > 0
        self._limits = (charge_kw, discharge_kw)

    def user_controlled(self, on=True):
        self.ir[1036] = 1 if on else 0

    def fault(self):
        self.ir[1000] = FAULT
        self.ir[1019] = self.ir[1020] = self.ir[1021] = self.ir[1022] = 0
        self._refresh_measured()

    def clear_fault(self):
        self.ir[1000] = STANDBY
        self.set_limits(*self._limits)
        self._refresh_measured()

    def reboot(self, into=OFF):
        """Restart: setpoints and dispatch modes lost, heartbeat restarts, state `into`."""
        self.hr.clear()
        self.ir[1003] = 0
        self._beat_at = time.monotonic()
        self.ir[1000] = into
        self._refresh_measured()

    def beat(self, steps=1):
        self.ir[1003] = (self.ir[1003] + steps) & 0xFFFF

    def _live_heartbeat(self):
        elapsed = int(time.monotonic() - self._beat_at)
        if elapsed:
            self._beat_at += elapsed
            if self.heartbeat_live:
                self.beat(elapsed)

    def dispatch_modes_set(self):
        """41018-41021 select fixed active-power dispatch (AC-configured)."""
        return (self.hr.get(1018, 0), self.hr.get(1019, 0), self.hr.get(1020, 0)) == (0, 1, 1)

    def _refresh_measured(self):
        address = 1009 if self.ir[1038] == 1 else 1007
        flowing = self.state in self.running_states and (self.ir[1038] != 1 or self.dispatch_modes_set())
        words = [self.hr.get(address, 0), self.hr.get(address + 1, 0)] if flowing else [0, 0]
        self.ir[address], self.ir[address + 1] = words

    def peak_register_rate(self, window=1.0):
        """Most registers requested in any `window` seconds so far: what the
        BMS's 50-a-second load check (§5.2) would have seen at worst."""
        peak = total = start = 0
        for i, (t, n) in enumerate(self.access_log):
            total += n
            while t - self.access_log[start][0] >= window:
                total -= self.access_log[start][1]
                start += 1
            peak = max(peak, total)
        return peak

    def largest_request(self):
        return max((n for _, n in self.access_log), default=0)

    async def _admit(self, count):
        """Apply the load rules of SPC100644 §5 to a request for `count`
        registers: log it, answer exception 03 (Illegal Data Value, §4.3) when
        it is over 25, and never answer it when the server is over 50 a
        second and enforce_rate is set. Returns an exception code or None."""
        now = time.monotonic()
        self.access_log.append((now, count))
        if count > MAX_REGISTERS_PER_REQUEST:
            return 3
        if self.enforce_rate and self.peak_register_rate() > MAX_REGISTER_RATE:
            self.dropped.append((now, count))
            await asyncio.Event().wait()          # dropped: no answer, the caller's timeout ends it
        return None

    async def _answer(self, slave):
        """Record the unit ID; False if this BMS does not answer it."""
        self.unit_log.append(slave)
        if self.answers_units is None or slave in self.answers_units:
            return True
        if self.other_units == "silent":
            await asyncio.Event().wait()          # never answered: the caller's timeout ends it
        return False

    # ── pymodbus client interface ─────────────────────────────────────
    async def connect(self):
        self.connected = True
        return True

    def close(self):
        self.connected = False

    async def write_registers(self, address, values, slave=0, **_):
        if self.write_gate is not None:
            self.gated.append(address)          # this write is now in flight, held at the gate
            await self.write_gate.wait()
        if self.link_down:
            raise ConnectionError("simulated link failure")
        refused = await self._admit(len(values))
        if refused is not None:
            return Response(exception_code=refused)
        if not await self._answer(slave):
            return Response(exception_code=11)
        code = self.reject(address, list(values)) if self.reject else None
        if code is not None:
            return Response(exception_code=code)
        if self.ir[1036] == 1:
            self.ignored.append((time.monotonic(), address, list(values)))
            return Response()
        self.writes.append((time.monotonic(), address, list(values)))
        before = self.hr.get(address)
        for i, value in enumerate(values):
            self.hr[address + i] = value
        if address == 1000 and not (self.change_triggered and before == values[0]):
            command, state = values[0], self.state
            if command == 1 and state in (OFF, ON, STANDBY):
                self.ir[1000] = STANDBY
            elif command == 2 and state == STANDBY and self.accept_on:
                self.ir[1000] = ON
            elif command == 0 and state == STANDBY:
                self.ir[1000] = OFF
        self._refresh_measured()
        return Response()

    async def read_input_registers(self, address, count=1, slave=0, **_):
        if self.link_down or address in self.fail_reads:
            raise ConnectionError("simulated read failure")
        refused = await self._admit(count)
        if refused is not None:
            return Response(exception_code=refused)
        if not await self._answer(slave):
            return Response(exception_code=11)
        self._live_heartbeat()
        return Response([self.ir.get(address + i, 0) for i in range(count)])

    async def read_holding_registers(self, address, count=1, slave=0, **_):
        if self.link_down:
            raise ConnectionError("simulated read failure")
        refused = await self._admit(count)
        if refused is not None:
            return Response(exception_code=refused)
        if not await self._answer(slave):
            return Response(exception_code=11)
        if address in self.holding_read_errors:
            code = self.holding_read_errors[address]
            if code is None:
                raise ConnectionError("simulated read failure")
            return Response(exception_code=code)
        return Response([self.hr.get(address + i, 0) for i in range(count)])

    # What the battery holds, in the app's convention (+ = charge).
    def setpoint_kw(self, address=AC_SETPOINT):
        return -_decode_int32([self.hr.get(address, 0), self.hr.get(address + 1, 0)])

    def setpoint_history(self, address=AC_SETPOINT):
        """Every setpoint the BMS stored, in order (app convention)."""
        return [-_decode_int32(words) for _, a, words in self.writes if a == address]

    def commands(self):
        """Every Operating Command (41000) the BMS stored, in order."""
        return [words[0] for _, a, words in self.writes if a == 1000]

    def heartbeats(self):
        """Every Operator Heartbeat (41002) the BMS stored, in order."""
        return [words[0] for _, a, words in self.writes if a == 1002]


async def until(condition, timeout=5.0):
    """Let the event loop (and the scheduler's worker threads) run until
    condition() holds: a fixed number of loop turns is not enough once
    database work runs in a thread."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def make_driver(fake=None, audit_log=None, state=None, event_bus=None, **settings):
    """A real ModbusDriver whose pymodbus client is a FakeBMS, already
    connected (BMS type read, On sent). settings are extra config keys
    (e.g. modbus_unit_id). Must run inside an event loop."""
    state = state or GatewayState()
    # bms_on_settle_sec=0: the 5 s hold after On (Rev 05 6.2.4) would slow every other test; its own tests set it.
    driver = ModbusDriver({"modbus_host": "127.0.0.1", "modbus_port": 9, "offline_timeout_sec": 30,
                           "bms_on_settle_sec": 0, **settings},
                          state, audit_log=audit_log, event_bus=event_bus)
    driver._client = fake if fake is not None else FakeBMS()
    await driver.connect()
    return driver, state


class Utility:
    """In-process IEEE 2030.5 server. Its DeviceCapability (/dcap) lists one
    DER program (/derp/1, primacy 1) whose DERControlList (/derp/1/derc) is
    .xml and whose DefaultDERControl (/derp/1/dderc) is .default when set;
    .poll_rate, when set, is the program list's pollRate. mode: "serve",
    "503" (HTTP error), "html" (a 200 that is not IEEE 2030.5, like a proxy
    or captive-portal page), for every resource. stop() makes it
    unreachable. DERControlResponses are accepted at any path (an event's
    replyTo)."""

    def __init__(self, xml=None):
        self.mode = "serve"
        self.xml = xml or derc()
        self.default = None              # DefaultDERControl XML, or None: the program has none
        self.poll_rate = None            # DERProgramList pollRate, or None
        self.responses = []              # (subject, body) of every DERControlResponse POST
        self.response_paths = []         # the path each one was POSTed to
        self.response_types = []         # its Content-Type
        self.response_status = 201       # HTTP status returned to DERControlResponse POSTs

    async def start(self, port=0):
        app = web.Application()
        app.router.add_get("/dcap", self._dcap)
        app.router.add_get("/derp", self._programs)
        app.router.add_get("/derp/1/derc", self._get)
        app.router.add_get("/derp/1/dderc", self._default)
        app.router.add_post("/{path:.*}", self._post)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        self.port = self.runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{self.port}"
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
        rate = "" if self.poll_rate is None else f' pollRate="{self.poll_rate}"'
        default = "" if self.default is None else '<DefaultDERControlLink href="/derp/1/dderc"/>'
        return self._serve(f'<DERProgramList xmlns="urn:ieee:std:2030.5:ns" href="/derp" all="1" results="1"'
                           f'{rate}><DERProgram href="/derp/1"><mRID>A1</mRID>{default}'
                           f'<DERControlListLink href="/derp/1/derc"/><primacy>1</primacy></DERProgram>'
                           f'</DERProgramList>')

    async def _get(self, request):
        return self._serve(self.xml)

    async def _default(self, request):
        return web.Response(status=404) if self.default is None else self._serve(self.default)

    async def _post(self, request):
        body = await request.text()
        try:
            subject = ET.fromstring(body).findtext("{*}subject")
        except ET.ParseError:
            subject = None
        self.responses.append((subject, body))
        self.response_paths.append(request.path)
        self.response_types.append(request.headers.get("Content-Type"))
        return web.Response(status=self.response_status)

    def statuses(self, subject=None):
        """The status of every response received, in order, optionally for one event."""
        return [int(ET.fromstring(body).findtext("{*}status")) for s, body in self.responses
                if subject is None or s == subject]


def audit_rows(audit, event_type=None):
    """Audit rows oldest first, optionally of one event_type."""
    rows = list(reversed(audit.recent(100000)))
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


def target_w(value, mult=0):
    """An opModTargetW asking for value x 10^mult W in the gateway's
    convention (+ = charge). IEEE 2030.5 counts the DER's output (+ =
    discharge), so a whole number is negated on the way out; anything else
    (a malformed number under test) is sent as it is."""
    wire = -value if isinstance(value, int) else value
    return f"<opModTargetW><multiplier>{mult}</multiplier><value>{wire}</value></opModTargetW>"


def derc(*events):
    """IEEE 2030.5 DERControlList XML. Event keys: mrid, start, duration,
    value (W, + = charge; sent as opModTargetW), mult, status
    (EventStatus.currentStatus, optional), response_required (hex, e.g.
    "03"; asks for DERControlResponses at reply_to, default /rsps/1/rsp)."""
    parts = []
    for e in events:
        status = ("" if "status" not in e else
                  f"<EventStatus><currentStatus>{e['status']}</currentStatus>"
                  f"<dateTime>{e['start']}</dateTime><potentiallySuperseded>false</potentiallySuperseded>"
                  f"</EventStatus>")
        respond = ("" if "response_required" not in e else
                   f' replyTo="{e.get("reply_to", "/rsps/1/rsp")}" responseRequired="{e["response_required"]}"')
        parts.append(f"<DERControl{respond}><mRID>{e['mrid']}</mRID>{status}<interval>"
                     f"<duration>{e['duration']}</duration><start>{e['start']}</start></interval>"
                     f"<DERControlBase>{target_w(e['value'], e.get('mult', 0))}</DERControlBase>"
                     f"</DERControl>")
    return (f'<DERControlList xmlns="urn:ieee:std:2030.5:ns" all="{len(parts)}" results="{len(parts)}">'
            f'{"".join(parts)}</DERControlList>')
