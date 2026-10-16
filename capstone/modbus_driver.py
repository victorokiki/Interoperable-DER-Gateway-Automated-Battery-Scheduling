"""
capstone/modbus_driver.py
Modbus TCP Driver — Endurium BMS Interface

Implements the REAL register map from SPC100644 Endurium BMS Modbus
Interface Specification, Rev. 03 (see project ICD: ICD_Endurium_BMS.docx).
This replaces the earlier placeholder driver, which used an invented
CHARGE/DISCHARGE/IDLE mode register that does not exist on real hardware.

──────────────────────────────────────────────────
KEY DIFFERENCES FROM THE PLACEHOLDER DRIVER
──────────────────────────────────────────────────
1. There is no single "mode" register. The BMS is driven through an
   Off/Standby/On state machine (Reg 41000), and charge/discharge happens
   as a side effect of a power setpoint's SIGN, not a chosen mode. The
   driver follows the documented transitions (spec p.16): Off -> Standby
   -> On, and On again once a Fault has cleared back to Standby.

2. DC-configured and AC-configured systems use different registers
   entirely (41007 vs 41009). This driver reads Reg 31038 at connect
   time to detect which one it's talking to, rather than assuming — and
   writes no setpoint at all while the type is unknown (31038 defines
   only 0 = DC and 1 = AC).

3. The real hardware's sign convention is OPPOSITE the convention used
   everywhere else in this codebase (scheduler.py / ieee_client.py treat
   positive kW = charge). Rather than flip that convention throughout the
   app, the sign is inverted ONLY at the point of writing to the register
   — see set_power() below.

4. The BMS requires a continuous heartbeat (Reg 41002, ~1s) independent
   of the TCP connection being open. Per spec §4.5.1 the controller reads
   the BMS heartbeat (31003) and echoes that value back. Without it the
   BMS will treat the controller as lost even on a healthy socket. This
   is implemented as heartbeat_loop(), which main.py must run as its own
   background task alongside watchdog().

5. The Operating Limits must be adhered to at all times (spec §6.2.6):
   every setpoint is limited to the Charge / Discharge Power Limits the
   BMS publishes (31019 / 31021) before it is written, and a write is only
   confirmed once the BMS reads it back, is On, and is not in
   User-Controlled mode (spec §6.1: writes are then accepted but not
   stored).

──────────────────────────────────────────────────
WHERE THE ICD IS SILENT (working answers E1-E10)
──────────────────────────────────────────────────
Each is a setting or a runtime check, so a different answer from Invinity
changes configuration, not code.
  E1  On is commanded automatically only from Standby, and from Off only
      when the gateway takes over (its own start-up, a reconnection, a BMS
      restart or an operator releasing User-Controlled mode) and
      BMS_START_FROM_OFF allows it. An Off that appears while the gateway
      is running was commanded by someone (spec p.16: Off is entered only
      by an Off command, and a restarted BMS resumes its stored state), so
      it is left alone. Outside a take-over, at most three automatic On
      commands an hour; then an alarm, and nothing more until the next
      take-over. Nothing in User-Controlled mode or Fault; never Clear
      Alerts.
  E2  41000 is read before On: if it already holds On while the BMS is in
      Standby, Standby is written first, so On is a change.
  E3  After a restart or reconnection the setpoint, the dispatch modes and
      the limits are unknown until read again.
  E4  A BMS heartbeat (31003) stuck for over 5 s while the registers still
      answer: confirmation withdrawn, 0 kW commanded, an alarm, and no
      non-zero setpoint until it has incremented again for 10 s.
  E5  31039 bit 6 (No Active Controller) is read and audited at connect.
  E6  A non-zero setpoint is confirmed in state 4 (On), or in a state code
      the ICD's table does not list while the measured power follows it.
  E7  AC: 41018-41021 are read at every connect and status poll; the ones
      reading 0 (Invalid) are set for fixed active-power dispatch
      (Grid-Feeding, Fixed Active Power, Fixed Q at 0 kVAR) and read back.
      Per-Array scope or P-f droop set by someone else is left as it is:
      an alarm, and no dispatch reported as confirmed while it stays.
  E8  DC-configured systems are not dispatched: the plant controller drives
      the PCS (spec §2), and 41007 is only a target.
  E9  The unit ID is a setting (default 255); at connect 31038 is read with
      it, then 1, then 0, and the first that answers is used.
  E10 The limits are the magnitudes of 31019 / 31021, held within the
      operator's 41014 / 41016 caps when those are non-zero (read, never
      written).
──────────────────────────────────────────────────
"""

import asyncio
import logging
import math
import time
from fractions import Fraction
from pymodbus.client import AsyncModbusTcpClient
from capstone.state import BatteryMode, GatewayState

log = logging.getLogger("modbus_driver")

# ── BMS Control Registers (Holding, addr = reg - 40000) ─────────────────
REG_OPERATING_COMMAND   = 1000  # 0=Off, 1=Standby, 2=On
REG_CLEAR_ALERTS        = 1001  # 1=Clear All Alerts
REG_OPERATOR_HEARTBEAT  = 1002  # echo of the BMS heartbeat 31003 (spec §4.5.1)
REG_DC_POWER_TARGET     = 1007  # DC-configured only, int32, kW, x1
REG_ACTIVE_POWER_SETPT  = 1009  # AC-configured only, int32, kW, x1
REG_REACTIVE_POWER_SETPT = 1011  # AC-configured only, int32, kVAR, x1
REG_OPERATING_CHARGE_CAP = 1014  # Operating Charge Power Limit Permitted, int32 kW (operator cap, §6.2.7)
REG_OPERATING_DISCHARGE_CAP = 1016  # Operating Discharge Power Limit Permitted, int32 kW
REG_GRID_CONTROL_SCOPE  = 1018  # 0 = System-Level, 1 = Per-Array
REG_GRID_MODE           = 1019  # 0 = Invalid, 1 = Grid-Feeding
REG_ACTIVE_POWER_MODE   = 1020  # 0 = Invalid, 1 = Fixed Active Power, 2 = P-f Droop
REG_REACTIVE_POWER_MODE = 1021  # 0 = Invalid, 1 = Fixed Q, 2 = Fixed PF

# ── BMS Status Registers (Input, addr = reg - 30000) ────────────────────
REG_OPERATING_STATE     = 1000  # enum, see OPERATING_STATE_NAMES
REG_OPERATING_STATUS    = 1001  # 0 Invalid/1 Normal/2 Warning/3 Fault/4 Critical
REG_HEARTBEAT           = 1003  # BMS's own heartbeat, increments 1/s
REG_STATE_OF_CHARGE     = 1004  # uint16, x10 -> percent
REG_DC_POWER            = 1007  # DC Power (measured), int32 kW, P > 0 = discharging
REG_ACTIVE_POWER        = 1009  # Active Power (measured, AC), int32 kW, P > 0 = exporting
REG_REACTIVE_POWER      = 1011  # Reactive Power (measured, AC), int32 kVAR (sign convention not in the ICD)
REG_CHARGE_POWER_LIMIT  = 1019  # int32 kW, P < 0: most charge power permitted right now
REG_DISCHARGE_POWER_LIMIT = 1021  # int32 kW, P > 0: most discharge power permitted right now
REG_CONTROL_MODE        = 1036  # 0 = Normal, 1 = User-Controlled (spec §6.1)
REG_BMS_TYPE            = 1038  # 0 = DC-configured, 1 = AC-configured
REG_BMS_ALERTS          = 1039  # bitfield, uint32 (2 registers)

OPERATING_COMMAND_OFF     = 0
OPERATING_COMMAND_STANDBY = 1
OPERATING_COMMAND_ON      = 2

STATE_OFF, STATE_STANDBY, STATE_ON, STATE_FAULT = 1, 2, 4, 10
OPERATING_STATE_NAMES = {
    0: "Init", 1: "Off", 2: "Standby", 3: "Starting up", 4: "On",
    5: "Shutting Down", 6: "Transitioning to Standby", 7: "Turning Off",
    8: "Waiting for Connection", 9: "Waiting for Disconnection", 10: "Fault",
}
OPERATING_STATUS_NAMES = {0: "Invalid", 1: "Normal", 2: "Warning", 3: "Fault", 4: "Critical Fault"}

CONTROL_NORMAL, CONTROL_USER = 0, 1
CONTROL_MODE_NAMES = {CONTROL_NORMAL: "Normal", CONTROL_USER: "User-Controlled"}

BMS_TYPE_DC = 0
BMS_TYPE_AC = 1

MODE_INVALID = 0
SCOPE_NAMES = {0: "System-Level", 1: "Per-Array"}
GRID_MODE_NAMES = {0: "Invalid", 1: "Grid-Feeding"}
ACTIVE_MODE_NAMES = {0: "Invalid", 1: "Fixed Active Power", 2: "P-f Droop"}
REACTIVE_MODE_NAMES = {0: "Invalid", 1: "Fixed Reactive Power (Q)", 2: "Fixed Power Factor (PF)"}
SCOPE_SYSTEM, GRID_FEEDING, ACTIVE_FIXED, REACTIVE_FIXED_Q = 0, 1, 1, 1

# 31039 bit 6, counting from bit 0 as the ICD's other bitfields do (31043).
ALERT_NO_ACTIVE_CONTROLLER = 1 << 6

INT32_MIN, INT32_MAX = -(1 << 31), (1 << 31) - 1
HEARTBEAT_STALE_SEC = 5   # 31003 increments every 1 s (spec p.23)
HEARTBEAT_RESUME_SEC = 10  # E4: incrementing again this long before dispatch resumes
AUTO_ON_LIMIT, AUTO_ON_WINDOW_SEC = 3, 3600   # E1: automatic On outside a take-over
ON_SETTLE_SEC = 5.0       # SPC100644 Rev 05 6.2.4: wait this long after the BMS reports On before any non-zero setpoint
COMMAND_SETTLE_SEC = 1.0  # E2: between Standby and On, so a BMS that scans for changes sees both
DEFAULT_UNIT_ID = 255     # Modbus TCP: 0xFF for a device addressed directly by IP
PROBE_UNIT_IDS = (1, 0)   # E9: tried after the configured unit ID
PROBE_TIMEOUT_SEC = 2.0   # shorter than pymodbus' 3 s, so an unanswered probe never drops the link
MAX_REGISTERS_PER_REQUEST = 25   # SPC100644 §5.1: per read and per write; more gets exception 03
REGISTER_PACE_LIMIT = 45         # the driver waits rather than ask for more than this in any 1 s (the BMS drops above 50)
MAX_REGISTER_RATE = 50           # §5.2: register accesses per second per server, all clients together;
                                 # requests above it are dropped, not queued
REGISTER_RATE_WINDOW_SEC = 1.0


class RegisterMeter:
    """Counts the registers this gateway asks the BMS for (§5.2). The BMS
    limits each server to MAX_REGISTER_RATE register accesses a second and
    silently drops the rest; the control port takes one connection, so this
    gateway is its only client and its own count is the server's."""

    def __init__(self):
        self._recent = []         # (monotonic time, registers) within the window
        self.total = 0            # registers requested since start
        self.largest_request = 0
        self.peak_rate = 0        # most registers requested in any window so far

    def note(self, count: int) -> int:
        """Record a request for count registers; return the registers asked
        for in the last REGISTER_RATE_WINDOW_SEC, this request included."""
        now = time.monotonic()
        self._recent = [(t, n) for t, n in self._recent if now - t < REGISTER_RATE_WINDOW_SEC]
        self._recent.append((now, count))
        self.total += count
        self.largest_request = max(self.largest_request, count)
        rate = sum(n for _, n in self._recent)
        self.peak_rate = max(self.peak_rate, rate)
        return rate

    async def wait_for_room(self, count: int, limit: int = REGISTER_PACE_LIMIT):
        """Sleep until `count` more registers fit under `limit` in the last window, so a burst (a dispatch, a
        status poll, a recorder sample landing in the same second) is spread out instead of being dropped by the
        BMS. No await between this returning and note(), so two requests cannot both take the last room."""
        while True:
            now = time.monotonic()
            self._recent = [(t, n) for t, n in self._recent if now - t < REGISTER_RATE_WINDOW_SEC]
            if sum(n for _, n in self._recent) + count <= limit or not self._recent:
                return
            oldest = min(t for t, _ in self._recent)
            await asyncio.sleep(max(0.005, oldest + REGISTER_RATE_WINDOW_SEC - now + 0.005))

    @property
    def rate(self) -> int:
        """Registers requested in the last window."""
        now = time.monotonic()
        return sum(n for t, n in self._recent if now - t < REGISTER_RATE_WINDOW_SEC)


def _encode_int32(value: int) -> list:
    """Encode a signed 32-bit value as [high_word, low_word], per spec
    4.4: 'the most significant word is transmitted first'. A value that
    int32 cannot hold raises ValueError instead of wrapping into a
    different (possibly opposite-sign) command."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{value!r} is not an integer register value")
    if not INT32_MIN <= value <= INT32_MAX:
        raise ValueError(f"{value} is outside the int32 range of the register")
    if value < 0:
        value += 1 << 32
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def _decode_int32(regs: list) -> int:
    value = (regs[0] << 16) | regs[1]
    if value >= (1 << 31):
        value -= (1 << 32)
    return value


def _limit(power_kw: float, limits_kw: tuple) -> float:
    """Keep an app-convention setpoint (+ = charge) inside the BMS's
    current (max_charge_kw, max_discharge_kw)."""
    max_charge, max_discharge = limits_kw
    return min(max(power_kw, -max_discharge), max_charge)


def _round_kw(power_kw: float) -> int:
    """The setpoint registers hold whole kW (int32, x1 scaling, spec p.31):
    round to the nearest kW, halves away from zero, on the exact value of
    the float (float addition would round 0.49999999999999994 up). The
    limits are whole kW, so a limited value rounded this way never exceeds
    them."""
    exact = Fraction(power_kw)
    magnitude = math.floor(abs(exact) + Fraction(1, 2))
    return magnitude if exact >= 0 else -magnitude


def _written_kw(power_kw: float, limits_kw: tuple) -> int:
    """What set_power() writes for power_kw under limits_kw (app sign)."""
    return _round_kw(_limit(power_kw, limits_kw))


def _follows(measured_kw, setpoint_kw) -> bool:
    """E6: is the measured power (app sign) doing what the setpoint asks —
    flowing the same way, or nothing for 0 kW?"""
    if measured_kw is None:
        return False
    if setpoint_kw == 0:
        return measured_kw == 0
    return measured_kw != 0 and (measured_kw > 0) == (setpoint_kw > 0)


class ReadOnlyError(RuntimeError):
    """A write was attempted while BMS_READ_ONLY is on."""


async def _refuse_write(*args, **kwargs):
    log.warning("BMS_READ_ONLY is on: a register write was refused and nothing was sent to the BMS")
    raise ReadOnlyError("BMS_READ_ONLY is on: nothing is written to the BMS")


_refuse_write._register_metered = True   # a refused write is not a register access


class ModbusDriver:
    def __init__(self, config: dict, state: GatewayState, audit_log=None, event_bus=None):
        self.host   = config["modbus_host"]
        self.port   = config["modbus_port"]
        self.state  = state
        self.offline_timeout = config["offline_timeout_sec"]
        self.audit_log = audit_log   # optional — NF-03
        self.event_bus = event_bus   # optional — NF-05, used for BMS alert notifications
        # E9: the unit ID configured, and the one the BMS last answered on.
        self.unit_id = config.get("modbus_unit_id", DEFAULT_UNIT_ID)
        self._unit = self.unit_id
        # E1: may the gateway command a BMS it finds Off at a take-over out of Off?
        self.start_from_off = config.get("bms_start_from_off", True)
        self.register_meter = RegisterMeter()
        # Pace every request so no 1 s window holds more than REGISTER_PACE_LIMIT registers: found on the real
        # BMS, where a scheduled start landed on a heartbeat, a recorder sample and a dispatch in the same second
        # (51 registers, one over what the BMS accepts; it silently drops the excess).
        self.pace_registers = bool(config.get("register_pacing", True))
        # Rev 05 6.2.4: the strings need about a second to connect to the DC bus after On, and power commanded
        # at once can leave some of them out. No non-zero setpoint until the BMS has been On this long.
        self.on_settle_sec = float(config.get("bms_on_settle_sec", ON_SETTLE_SEC))
        # BMS_MAX_POWER_KW: a hard ceiling for this run on any setpoint, whatever the BMS allows and
        # whoever asks (the dashboard's Run now defaults to 500 kW). 0 = no cap of our own.
        self.max_power_kw = int(config.get("bms_max_power_kw", 0))
        self._on_since = None   # monotonic time the BMS was first seen On (None: not On, or not seen yet)
        # BMS_READ_ONLY: look but never touch. Every write is refused before it leaves the gateway.
        self.read_only = bool(config.get("bms_read_only", False))
        self._client = AsyncModbusTcpClient(host=self.host, port=self.port)
        self._lock   = asyncio.Lock()   # prevent concurrent writes
        self._bms_type = None           # populated on connect(): BMS_TYPE_DC / BMS_TYPE_AC
        self._last_alerts_raw = None    # last-seen BMS Alerts [31039] value, to detect changes
        # Last setpoint (app convention) the BMS confirmed holding, or None
        # when unknown: before the first write, after any failed or
        # unconfirmed write, after a (re)connect and whenever the BMS stops
        # holding it. requested_kw is what the caller asked for when it was
        # written (the two differ when a limit applied). The scheduler
        # re-sends its decision whenever is_applied() says otherwise.
        self.commanded_kw: float | None = None
        self.requested_kw: float | None = None
        self.commanded_source: str | None = None
        # Why the last set_power()/safe_idle() failed: (kind, detail) with kind
        # one of "exception" (the BMS refused it), "timeout", "disconnected",
        # "not_ready", "limits_unavailable", "out_of_range", "verify_failed",
        # "readback_mismatch", "bms_state", "user_controlled",
        # "not_dispatchable" (DC-configured, E8), "bms_unhealthy" (frozen
        # heartbeat, E4), "grid_modes" (E7), "settling" (On for under 5 s, Rev 05 6.2.4:
        # a retry, never a rejection), "error"; None after a success.
        self.last_dispatch_error: tuple | None = None
        self.limits_kw: tuple | None = None   # (max_charge_kw, max_discharge_kw), both >= 0
        self._written = None                  # (register, words) of the confirmed setpoint
        self._last_bms_heartbeat = None
        self._heartbeat_changed_at = None
        self._frozen_since = None             # E4: when 31003 was found stuck (None: healthy)
        self._resumed_at = None               # E4: when 31003 started changing again
        self._taking_over = True              # E1: the next state decision is a take-over
        self._auto_on_times = []              # E1: automatic On commands outside a take-over
        self._auto_on_stopped = False         # E1: the hourly limit was reached
        self._control_mode = None             # 31036 last seen, to notice a release (E1)
        self._grid_modes = None               # E7 (AC): None unchecked, True in place, else (kind, detail)
        self._power_beyond_setpoint = False   # E7: the modes let power flow that 41009 does not decide
        self._pending_alarm = None            # raised by the next _raise_pending_alarm()
        self._reported = {}                   # topic -> condition last logged/audited
        self._last_write_failure = None       # last failed write audited (see _audit_write)
        self._reconnect_attempts = 0          # watchdog reconnects tried since the link was lost
        self._link_up = None                  # Modbus link state last recorded (see _track_link)

    # ── Register budget (§5.1, §5.2) ───────────────────────────

    @property
    def _client(self):
        return self._metered_client

    @_client.setter
    def _client(self, client):
        """Every client assigned here (the real one, or a test's fake) has its
        read and write methods counted by register_meter, and a request for
        more than MAX_REGISTERS_PER_REQUEST registers refused before it is
        sent (the BMS would answer exception 03). In read-only mode the write
        methods are replaced by ones that refuse, so no register is ever
        written, whatever asks (heartbeat echo, commands, setpoints)."""
        if self.read_only:
            for name in ("write_registers", "write_register", "write_coil", "write_coils"):
                if getattr(client, name, None) is not None:
                    setattr(client, name, _refuse_write)
        for name in ("read_input_registers", "read_holding_registers", "write_registers", "write_register"):
            method = getattr(client, name, None)
            if method is not None and not getattr(method, "_register_metered", False):
                setattr(client, name, self._meter(method, name))
        self._metered_client = client

    def _meter(self, method, name):
        async def metered(*args, **kwargs):
            if name.startswith("read"):
                count = kwargs.get("count", args[1] if len(args) > 1 else 1)
            elif name == "write_registers":
                count = len(args[1] if len(args) > 1 else kwargs["values"])
            else:
                count = 1
            if count > MAX_REGISTERS_PER_REQUEST:
                raise ValueError(f"{name} of {count} registers exceeds the BMS limit of "
                                 f"{MAX_REGISTERS_PER_REQUEST} per request (SPC100644 §5.1)")
            if self.pace_registers:
                await self.register_meter.wait_for_room(count)
            rate = self.register_meter.note(count)
            over = rate > MAX_REGISTER_RATE
            if over or self._reported.get("register_rate"):    # audit going over, and the recovery
                self._report("register_rate", over,
                             f"{rate} registers requested in {REGISTER_RATE_WINDOW_SEC:g} s: the BMS drops "
                             f"requests above {MAX_REGISTER_RATE} a second (SPC100644 §5.2)" if over
                             else "register request rate back under the BMS limit",
                             "over_limit" if over else "ok")
            return await method(*args, **kwargs)
        metered._register_metered = True
        return metered

    # ── Connection ─────────────────────────────────────────────

    async def connect(self):
        log.info(f"Connecting to Modbus TCP at {self.host}:{self.port}")
        self._forget_setpoint()   # device state unknown until the next confirmed write
        self._on_since = None     # how long it has been On is unknown again after a (re)connect
        await self._client.connect()
        self._track_link()
        if not self._client.connected:
            log.error("Modbus TCP connection FAILED. Will retry in watchdog.")
            return

        log.info("Modbus TCP connected.")
        await self._take_over()

    async def _take_over(self):
        """After a (re)connection (E3): nothing the BMS held before is
        assumed. Find the unit ID and type, see whether the BMS lost its
        controller (E5), set the dispatch modes before On (spec §6.2.2
        advises configuring the power requests first) and apply E1."""
        self._taking_over = True
        self._grid_modes = None
        await self._detect_bms_type()
        await self._check_controller_alert()
        if self._bms_type == BMS_TYPE_AC:
            async with self._lock:
                await self._ensure_grid_modes()
            await self._raise_pending_alarm()
        await self.ensure_bms_on()

    async def _detect_bms_type(self):
        """Read Reg 31038 to find out if this is a DC- or AC-configured
        BMS, rather than assuming. Any other answer (or no answer) leaves
        the type unknown, and no setpoint is written until it is known.

        The read is also the unit-ID probe (E9): the configured unit ID
        first, then 1, then 0, each given PROBE_TIMEOUT_SEC; the first that
        answers is used from then on. Only this harmless read probes."""
        tried, value = [], None
        for unit in [self.unit_id] + [u for u in PROBE_UNIT_IDS if u != self.unit_id]:
            try:
                result = await asyncio.wait_for(
                    self._client.read_input_registers(REG_BMS_TYPE, count=1, slave=unit),
                    timeout=PROBE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                tried.append(f"unit {unit}: no answer in {PROBE_TIMEOUT_SEC:g} s")
                continue
            except Exception as e:
                tried.append(f"unit {unit}: read failed ({e})")
                if not self._client.connected:
                    break                      # the link is down: no other unit ID will answer
                continue
            if result.isError():
                tried.append(f"unit {unit}: exception {getattr(result, 'exception_code', None)}")
                continue
            self._unit, value = unit, result.registers[0]
            self._report("modbus_unit", unit, f"Modbus unit ID {unit} answers"
                         + (f" ({'; '.join(tried)})" if tried else ""),
                         "configured" if unit == self.unit_id else "probed")
            break
        if value in (BMS_TYPE_DC, BMS_TYPE_AC):
            self._bms_type = value
            label = "AC-configured" if value == BMS_TYPE_AC else "DC-configured"
            log.info(f"Detected BMS type: {label} (Reg 31038 = {value})")
            if value == BMS_TYPE_AC:
                self.state.dispatch_kind = "ac_active_power"
                self._report("bms_type", label, f"BMS type {label} (31038 = {value})", "detected")
            else:
                self.state.dispatch_kind = "dc_not_dispatchable"
                self._report("bms_type", label, f"BMS type {label} (31038 = {value}): not dispatched by "
                             f"this gateway — the plant controller drives the PCS (spec §2), and 41007 "
                             f"is only a target", "detected")
        else:
            self._bms_type = None
            self.state.dispatch_kind = None
            problem = f"unrecognised value {value}" if value is not None else "; ".join(tried)
            log.error(f"BMS type unknown ({problem}) — no setpoint will be written until "
                      f"Reg 31038 reads 0 (DC) or 1 (AC)")
            self._report("bms_type", "unknown", f"BMS type unknown: {problem}", "blocked")

    async def _check_controller_alert(self):
        """E5: did the BMS lose its controller before this connection
        (31039 bit 6, No Active Controller)? Audited either way."""
        try:
            result = await self._client.read_input_registers(REG_BMS_ALERTS, count=2, slave=self._unit)
            if result.isError():
                return
        except Exception as e:
            log.warning(f"BMS alerts read failed: {e}")
            return
        alerts = _decode_int32(result.registers) & 0xFFFFFFFF
        if alerts & ALERT_NO_ACTIVE_CONTROLLER:
            log.warning("BMS reports No Active Controller (31039 bit 6): it lost contact with its "
                        "controller before this connection")
            self._audit("bms_condition", f"at connect: 31039 bit 6 (No Active Controller) set — the BMS "
                        f"lost contact with its controller (alerts 0x{alerts:08X})", "observed")
        else:
            self._audit("bms_condition", f"at connect: 31039 bit 6 (No Active Controller) clear "
                        f"(alerts 0x{alerts:08X})", "observed")

    async def ensure_bms_on(self):
        """Bring the BMS to On along the documented state machine (spec
        p.16, §6.2): Off -> Standby -> On, and On again once a Fault has
        cleared back to Standby (§6.2.1: Standby awaits the On command).
        Nothing is commanded in User-Controlled mode (§6.1: an operator has
        control), in Fault (latching faults need the operator's Clear
        Alerts, §6.2.8) or while a transition is under way.

        E1: Off is left only at a take-over and only if start_from_off
        allows it; outside a take-over, On is commanded at most
        AUTO_ON_LIMIT times per AUTO_ON_WINDOW_SEC."""
        state, mode = await self._read_state_and_mode()
        if state is None:
            return
        if mode != CONTROL_NORMAL:
            return
        taking_over = self._end_take_over()
        if state == STATE_OFF:
            if not taking_over:
                self._report("bms_off", "left_alone", "BMS is Off, commanded while the gateway was running "
                             "(spec p.16: Off is entered only by an Off command): the gateway does not "
                             "start it — bring it to Standby to resume", "waiting")
                return
            if not self.start_from_off:
                self._report("bms_off", "not_allowed", "BMS is Off and BMS_START_FROM_OFF is off: the "
                             "gateway does not start it — bring it to Standby to resume", "waiting")
                return
            await self._command(OPERATING_COMMAND_STANDBY, "BMS is Off: Standby first (Off -> Standby -> On)")
            state = await self._wait_for_state_change(STATE_OFF)
        if state == STATE_STANDBY:
            if not taking_over and not await self._automatic_on_allowed():
                return
            await self._command_on("BMS is in Standby: On")

    def _end_take_over(self) -> bool:
        """Consume the take-over flag (E1). True if this state decision is a
        take-over, which also re-arms the automatic On limit."""
        if not self._taking_over:
            return False
        self._taking_over = False
        if self._auto_on_stopped:
            self._audit("bms_alarm", "automatic On re-armed by a take-over (start-up, reconnection, "
                        "BMS restart or release of User-Controlled mode)", "cleared")
        self._auto_on_times.clear()
        self._auto_on_stopped = False
        self._reported.pop("alarm:bms_auto_on", None)
        return True

    async def _automatic_on_allowed(self) -> bool:
        """E1: an On outside a take-over (a Fault that cleared to Standby, or
        a Standby someone else commanded) is counted; past the limit an
        alarm is raised and nothing more is commanded until the next
        take-over."""
        if self._auto_on_stopped:
            return False
        now = time.monotonic()
        self._auto_on_times = [t for t in self._auto_on_times if now - t < AUTO_ON_WINDOW_SEC]
        if len(self._auto_on_times) >= AUTO_ON_LIMIT:
            self._auto_on_stopped = True
            await self._alarm("bms_auto_on", "stopped",
                              f"BMS back in Standby after {AUTO_ON_LIMIT} automatic On commands within "
                              f"{AUTO_ON_WINDOW_SEC // 60} min: the gateway stops commanding On — an "
                              f"operator has to look at it (releasing User-Controlled mode or restarting "
                              f"the gateway re-arms it)")
            return False
        self._auto_on_times.append(now)
        return True

    async def _command_on(self, why: str):
        """E2: if 41000 already holds On while the BMS is in Standby, a BMS
        that acts only on a change would ignore another On: write Standby
        (the state it is already in) first."""
        try:
            held = await self._client.read_holding_registers(REG_OPERATING_COMMAND, count=1, slave=self._unit)
            already_on = not held.isError() and list(held.registers) == [OPERATING_COMMAND_ON]
        except Exception as e:
            log.warning(f"Operating Command read failed: {e}")
            already_on = False
        if already_on:
            await self._command(OPERATING_COMMAND_STANDBY, "41000 already holds On while the BMS is in "
                                "Standby: Standby first, so that On is a change")
            await asyncio.sleep(COMMAND_SETTLE_SEC)
        await self._command(OPERATING_COMMAND_ON, why)

    async def _read_state_and_mode(self):
        """(operating state, control mode) from 31000 and 31036, recorded on
        GatewayState and reported when they change; (None, None) if unreadable."""
        try:
            st = await self._client.read_input_registers(REG_OPERATING_STATE, count=2, slave=self._unit)
            cm = await self._client.read_input_registers(REG_CONTROL_MODE, count=1, slave=self._unit)
            if st.isError() or cm.isError():
                return None, None
        except Exception as e:
            log.warning(f"Operating state read failed: {e}")
            return None, None
        state, status, mode = st.registers[0], st.registers[1], cm.registers[0]
        self.state.operating_state_code = state
        self.state.operating_state = OPERATING_STATE_NAMES.get(state, f"Unknown({state})")
        self.state.operating_status = OPERATING_STATUS_NAMES.get(status, f"Unknown({status})")
        self._report("operating_state", state, f"BMS operating state {self.state.operating_state}",
                     "observed")
        self._note_control_mode(mode)
        self._track_on(state)
        return state, mode

    def _track_on(self, state: int):
        """Remember when the BMS was first seen On, and forget it as soon as it is seen in any other state."""
        if state != STATE_ON:
            self._on_since = None
        elif self._on_since is None:
            self._on_since = time.monotonic()

    async def _on_settling(self):
        """Rev 05 6.2.4: None when a non-zero setpoint may be written, else ("settling", why). A BMS first seen
        On (also at gateway start-up, when the time is unknown) is waited for on_settle_sec before power is
        commanded; one in any other state is left to the checks that follow, which refuse it."""
        if self.on_settle_sec <= 0:
            return None
        state, _ = await self._read_state_and_mode()
        if state != STATE_ON or self._on_since is None:
            return None
        seen = time.monotonic() - self._on_since
        if seen >= self.on_settle_sec:
            return None
        return ("settling", f"BMS has been On for {seen:.1f} s: no non-zero setpoint until {self.on_settle_sec:g} s "
                            f"so its strings can connect to the DC bus (SPC100644 Rev 05 6.2.4); retrying")

    def _note_control_mode(self, mode: int):
        """Record 31036. An operator handing control back (User-Controlled
        -> Normal) is a take-over for E1, like a start-up."""
        self.state.control_mode = CONTROL_MODE_NAMES.get(mode, f"Unknown({mode})")
        self._report("control_mode", mode, f"BMS control mode {self.state.control_mode}"
                     + (" — Modbus writes are accepted but not stored (spec §6.1)" if mode == CONTROL_USER else ""),
                     "observed")
        if self._control_mode == CONTROL_USER and mode == CONTROL_NORMAL:
            self._taking_over = True
        self._control_mode = mode

    async def _wait_for_state_change(self, from_state: int, timeout: float = 10.0, step: float = 0.5):
        """Poll 31000 until the state leaves from_state (or timeout)."""
        deadline = time.monotonic() + timeout
        state = from_state
        while state == from_state and time.monotonic() < deadline:
            await asyncio.sleep(step)
            state, _ = await self._read_state_and_mode()
        return state

    async def _command(self, value: int, why: str) -> bool:
        async with self._lock:
            success = await self._write_holding_registers(REG_OPERATING_COMMAND, [value])
        name = {0: "Off", 1: "Standby", 2: "On"}[value]
        if success:
            log.info(f"Sent Operating Command: {name} ({why})")
        else:
            log.error(f"Failed to send Operating Command: {name} ({why})")
        self._audit("bms_command", f"41000 := {value} ({name}): {why}",
                    "success" if success else "failed")
        return success

    # ── Dispatch modes (E7, AC-configured) ─────────────────────

    async def _ensure_grid_modes(self):
        """E7: fixed active-power dispatch needs System-Level scope (41018),
        Grid-Feeding (41019), Fixed Active Power (41020) and a valid reactive
        mode (41021). The ones reading 0 (Invalid) are written — Fixed Q
        after setting 41011 to 0 kVAR — and read back; a valid value someone
        else set is left as it is. Returns None when dispatch is in place,
        else ("grid_modes", detail), which set_power() then returns. The
        caller holds self._lock.

        In User-Controlled mode nothing is written (the BMS would not store
        it, spec §6.1) and nothing is decided: the control-mode check refuses
        any confirmation, and the modes are checked again once control is
        handed back."""
        try:
            control = await self._client.read_input_registers(REG_CONTROL_MODE, count=1, slave=self._unit)
            user_controlled = not control.isError() and control.registers[0] == CONTROL_USER
        except Exception:
            user_controlled = False
        if user_controlled:
            self._grid_modes = None
            return None
        modes, failure = await self._read_holding(REG_GRID_CONTROL_SCOPE, 4)
        if modes is None:
            return self._modes_unreadable(failure)
        writes = []
        if modes[1] == MODE_INVALID:
            writes.append((REG_GRID_MODE, [GRID_FEEDING], "41019 Grid Mode := 1 (Grid-Feeding)"))
        if modes[2] == MODE_INVALID:
            writes.append((REG_ACTIVE_POWER_MODE, [ACTIVE_FIXED],
                           "41020 Active Power Control Mode := 1 (Fixed Active Power)"))
        if modes[3] == MODE_INVALID:
            writes.append((REG_REACTIVE_POWER_SETPT, [0, 0], "41011 Reactive Power Setpoint := 0 kVAR"))
            writes.append((REG_REACTIVE_POWER_MODE, [REACTIVE_FIXED_Q],
                           "41021 Reactive Power Control Mode := 1 (Fixed Q)"))
        for address, words, what in writes:
            ok, error = await self._write(address, words)
            self._audit("bms_command", f"{what}: the dispatch modes read 0 (Invalid)",
                        "success" if ok else "failed")
            if not ok:
                return self._set_grid_modes(modes, f"{what} failed: {error[0]} {error[1] or ''}".rstrip(),
                                            "no dispatch is reported as confirmed until they are set")
        if writes:
            modes, failure = await self._read_holding(REG_GRID_CONTROL_SCOPE, 4)
            if modes is None:
                return self._modes_unreadable(failure)
        scope, grid, active, reactive = modes
        problems = []
        if scope != SCOPE_SYSTEM:
            problems.append(f"41018 Grid Control Scope is {SCOPE_NAMES.get(scope, scope)} "
                            f"(system-level commands are ignored)")
        if grid != GRID_FEEDING:
            problems.append(f"41019 Grid Mode is {GRID_MODE_NAMES.get(grid, grid)}")
        if active != ACTIVE_FIXED:
            problems.append(f"41020 Active Power Control Mode is {ACTIVE_MODE_NAMES.get(active, active)}")
        if reactive == MODE_INVALID or reactive not in REACTIVE_MODE_NAMES:
            problems.append(f"41021 Reactive Power Control Mode is {REACTIVE_MODE_NAMES.get(reactive, reactive)}")
        return self._set_grid_modes(modes, "; ".join(problems) + ": fixed active-power dispatch is not in effect"
                                    if problems else None,
                                    "they are left as they are (someone may have set them deliberately), and "
                                    "no dispatch is reported as confirmed while they stay")

    def _modes_unreadable(self, failure: str):
        """41018-41021 could not be read. If the BMS refused the read, that is
        a verdict (and an alarm). If it did not answer, the link is failing,
        which is reported where the link is watched: nothing is decided, and
        the modes are read again on the next attempt."""
        problem = f"dispatch modes 41018-41021 unreadable ({failure})"
        if failure.startswith("exception"):
            return self._set_grid_modes(None, problem, "no dispatch is reported as confirmed until they can "
                                                       "be read and allow it")
        self._grid_modes = None
        return ("grid_modes", problem)

    def _set_grid_modes(self, modes, problem, consequence=""):
        """Keep the E7 verdict: True when fixed active-power dispatch is in
        place, else ("grid_modes", problem). Also kept, for safe_idle():
        whether the modes let the BMS deliver power the system setpoint does
        not decide (Per-Array scope, P-f droop, a value the ICD does not
        list). Reported, and alarmed, when it changes."""
        was = self._grid_modes
        if modes is None:
            described = "unreadable"
        else:
            scope, grid, active, reactive = modes
            self._power_beyond_setpoint = (scope != SCOPE_SYSTEM or grid not in (MODE_INVALID, GRID_FEEDING)
                                           or active not in (MODE_INVALID, ACTIVE_FIXED))
            described = (f"41018 {SCOPE_NAMES.get(scope, scope)}, 41019 {GRID_MODE_NAMES.get(grid, grid)}, "
                         f"41020 {ACTIVE_MODE_NAMES.get(active, active)}, "
                         f"41021 {REACTIVE_MODE_NAMES.get(reactive, reactive)}")
        if problem is None:
            self._grid_modes = True
            self._report("grid_modes", tuple(modes), f"dispatch modes in place: {described}", "confirmed")
            if self._reported.pop("alarm:grid_modes", None) is not None:
                self._audit("bms_alarm", f"dispatch modes in place again: {described}", "cleared")
            return None
        blocked = ("grid_modes", problem)
        self._grid_modes = blocked
        if was is True:
            self._forget_setpoint()            # a confirmation no longer vouched for
        self._report("grid_modes", ("blocked",) + tuple(modes or ()),
                     f"dispatch modes: {described} — {problem}", "blocked")
        self._pending_alarm = ("grid_modes", problem, f"BMS dispatch modes: {problem}; {consequence}")
        return blocked

    # ── Command API ────────────────────────────────────────────

    async def set_power(self, power_kw: float, source: str = "manual") -> bool:
        """
        Set the requested power level, in THIS APP'S convention:
          power_kw > 0  →  CHARGE    at  power_kw
          power_kw < 0  →  DISCHARGE at |power_kw|
          power_kw = 0  →  safe idle (zero setpoint)

        The value written is first held inside the Charge / Discharge
        Power Limits the BMS publishes right now (31019 / 31021, spec
        §6.2.6) and the operator's caps (41014 / 41016). Returns True only
        once the BMS confirms the setpoint: read back unchanged, operating
        state On, Normal control mode.

        NOTE: the real hardware's sign convention is the OPPOSITE of the
        above (positive = discharge/export). The inversion happens here,
        at the boundary, so callers (scheduler.py) don't need to change.
        """
        if not isinstance(power_kw, (int, float)) or isinstance(power_kw, bool) \
                or not math.isfinite(power_kw) or abs(power_kw) > INT32_MAX:
            # Not a setpoint the int32 kW register can express: refuse it
            # rather than limit, wrap or guess (spec §4.4).
            error = ("out_of_range", f"{power_kw!r} kW is not a finite value the int32 kW "
                                     f"setpoint register can hold")
            self._record_dispatch(None, None, source, error)
            self._audit("modbus_write", f"src={source} requested={power_kw!r} kW written=- "
                        f"register=none", f"not written: {error[1]}")
            return False

        if power_kw == 0:
            return await self.safe_idle(source=source)

        if self._bms_type is None:
            await self._detect_bms_type()
            if self._bms_type is None:
                self._record_dispatch(None, None, source, ("not_ready", "BMS type unknown"))
                return False
        if self._not_dispatchable(source, power_kw):
            return False
        if self._frozen_since is not None:
            error = ("bms_unhealthy", f"BMS heartbeat 31003 stopped: no non-zero setpoint until it has "
                                      f"incremented again for {HEARTBEAT_RESUME_SEC} s")
            self._record_dispatch(None, None, source, error)
            self._audit_write(source, power_kw, None, None, f"not written: {error[1]}")
            return False

        async with self._lock:
            settling = await self._on_settling()
            if settling is not None:
                self._record_dispatch(None, None, source, settling)
                self._audit_write(source, power_kw, None, None, f"not written: {settling[1]}")
                return False
            if self._grid_modes is not True:
                blocked = await self._ensure_grid_modes()
                await self._raise_pending_alarm()
                if blocked is not None:
                    self._record_dispatch(None, None, source, blocked)
                    self._audit_write(source, power_kw, None, None, f"not written: {blocked[1]}")
                    return False
            limits = await self._read_limits()
            if limits is None:
                self._record_dispatch(None, None, source, ("limits_unavailable",
                                      "Charge/Discharge Power Limits 31019/31021 or the caps 41014/41016 unreadable"))
                self._audit_write(source, power_kw, None, None, "not written: limits unavailable")
                return False
            written_kw = _written_kw(power_kw, limits)
            hardware_value_kw = -written_kw  # invert: app convention -> hardware convention
            reg_addr = REG_ACTIVE_POWER_SETPT
            try:
                reg_words = _encode_int32(hardware_value_kw)
            except ValueError as e:
                self._record_dispatch(None, None, source, ("out_of_range", str(e)))
                self._audit_write(source, power_kw, written_kw, None, f"not written: {e}")
                return False

            success, error = await self._write(reg_addr, reg_words)
            if success:
                error = await self._verify_dispatch(reg_addr, reg_words, expect_on=True, setpoint_kw=written_kw)
                success = error is None
                if not success:
                    log.error(f"Setpoint write not confirmed by the BMS: {error[1]}")

            if success:
                bmode = BatteryMode.CHARGE if written_kw > 0 else (
                    BatteryMode.DISCHARGE if written_kw < 0 else BatteryMode.IDLE)
                self.state.mode     = bmode
                self.state.power_kw = float(written_kw)
                self.state.last_command_source = source
                adjusted = (f" (requested {power_kw:+.1f} kW; BMS limits / whole-kW register)"
                            if written_kw != power_kw else "")
                log.info(f"Battery → {bmode.value} at {abs(written_kw)} kW{adjusted} "
                         f"[hw_setpoint={hardware_value_kw} kW, src: {source}]")
                self._record_dispatch(float(written_kw), power_kw, source, None, (reg_addr, reg_words))
            else:
                log.error("Modbus write failed — battery state unknown")
                self._record_dispatch(None, None, source, error)
            self._audit_write(source, power_kw, written_kw, reg_addr,
                              "confirmed" if success else f"not confirmed: {error[0]} {error[1] or ''}".rstrip(),
                              verified="read back unchanged, BMS On, Normal control mode" if success else "")
            return success

    async def safe_idle(self, source: str = "safe_idle") -> bool:
        """Zero the power setpoint (safe state). Used on shutdown, grid
        loss, or an explicit idle command.

        Note: this does NOT transition the BMS to Standby — it keeps the
        unit On but at zero power, which is faster to resume from. If a
        deeper safe state (Standby) is ever required, call go_to_standby()
        separately."""
        if self._bms_type is None:
            await self._detect_bms_type()
            if self._bms_type is None:
                log.error("Cannot idle — BMS type unknown")
                self._record_dispatch(None, None, source, ("not_ready", "BMS type unknown"))
                return False
        if self._not_dispatchable(source, 0.0):
            return False

        async with self._lock:
            reg_addr = REG_ACTIVE_POWER_SETPT
            words = _encode_int32(0)
            success, error = await self._write(reg_addr, words)
            if success:
                error = await self._verify_dispatch(reg_addr, words, expect_on=False)
                if error is None and self._power_beyond_setpoint and isinstance(self._grid_modes, tuple):
                    # Per-Array scope or P-f droop: the BMS may deliver power
                    # whatever the system setpoint says (E7).
                    error = self._grid_modes
                success = error is None
            if success:
                self.state.mode     = BatteryMode.IDLE
                self.state.power_kw = 0.0
                self.state.last_command_source = source
                log.info(f"Battery → IDLE (zero power setpoint, src: {source})")
                self._record_dispatch(0.0, 0.0, source, None, (reg_addr, words))
            else:
                self._record_dispatch(None, None, source, error)
            self._audit_write(source, 0.0, 0.0, reg_addr,
                              "confirmed" if success else f"not confirmed: {error[0]} {error[1] or ''}".rstrip(),
                              verified="read back unchanged, Normal control mode" if success else "")
            return success

    def _not_dispatchable(self, source, power_kw) -> bool:
        """E8: a DC-configured BMS is driven by the plant controller through
        the PCS (spec §2); 41007 is only a target. Nothing is written and
        nothing is reported as dispatched."""
        if self._bms_type != BMS_TYPE_DC:
            return False
        error = ("not_dispatchable", "DC-configured BMS: the plant controller drives the PCS; "
                                     "this gateway has no path to command its power")
        self._record_dispatch(None, None, source, error)
        self._audit_write(source, power_kw, None, None, f"not written: {error[1]}")
        return True

    def is_applied(self, power_kw: float) -> bool:
        """Is the BMS confirmed to be holding what set_power(power_kw) would
        write now (given the limits last read)? Never while the Modbus link is
        down: nothing the BMS confirmed before can be vouched for then."""
        if not self._client.connected:
            return False
        if self.commanded_kw is None or self.requested_kw != power_kw:
            return False
        if power_kw == 0:
            return self.commanded_kw == 0
        return self.limits_kw is not None and self.commanded_kw == _written_kw(power_kw, self.limits_kw)

    def _record_dispatch(self, commanded_kw, requested_kw, source, error, written=None):
        """Remember what the BMS last confirmed (None = unknown) and why the
        last dispatch write failed, for the scheduler's reconciliation."""
        self.commanded_kw = commanded_kw
        self.requested_kw = requested_kw if commanded_kw is not None else None
        self.commanded_source = source if commanded_kw is not None else None
        self.last_dispatch_error = error
        self._written = written if commanded_kw is not None else None
        self.state.commanded_kw = commanded_kw

    def _forget_setpoint(self):
        """The BMS may no longer hold the last setpoint (reconnect, restart,
        not On, read-back changed): make the scheduler re-send it."""
        self.commanded_kw = self.requested_kw = self.commanded_source = None
        self._written = None
        self.state.commanded_kw = None

    async def _read_limits(self):
        """Read 31019 / 31021 (one request) and return the app-convention
        (max_charge_kw, max_discharge_kw), or None if unreadable. E10: the
        magnitude of each limit is used, so either sign convention works, and
        on an AC-configured BMS each is held within the operator's cap
        41014 / 41016 when that cap is non-zero (spec §6.2.7: caps for
        commissioning and maintenance — read here, never written)."""
        try:
            result = await self._client.read_input_registers(REG_CHARGE_POWER_LIMIT, count=4, slave=self._unit)
            if result.isError():
                return None
        except Exception as e:
            log.warning(f"Power limit read failed: {e}")
            return None
        charge_hw = _decode_int32(result.registers[0:2])       # P < 0 per the ICD's table
        discharge_hw = _decode_int32(result.registers[2:4])    # P > 0
        if charge_hw > 0 or discharge_hw < 0:
            self._report("limit_signs", (charge_hw > 0, discharge_hw < 0),
                         f"power limits read charge {charge_hw:+} kW (31019), discharge {discharge_hw:+} kW "
                         f"(31021), not the signs the ICD's table gives: their magnitudes are used", "observed")
        limits = [abs(charge_hw), abs(discharge_hw)]
        if self._bms_type == BMS_TYPE_AC:
            caps, _ = await self._read_holding(REG_OPERATING_CHARGE_CAP, 4)
            if caps is None:
                return None
            caps = (abs(_decode_int32(caps[0:2])), abs(_decode_int32(caps[2:4])))
            if any(caps):
                self._report("operator_caps", caps, f"operator caps 41014 / 41016: charge <= {caps[0] or '-'} kW, "
                             f"discharge <= {caps[1] or '-'} kW (0 = no cap)", "observed")
            elif self._reported.get("operator_caps") is not None:
                self._report("operator_caps", None, "operator caps 41014 / 41016 removed", "observed")
            limits = [min(limit, cap) if cap else limit for limit, cap in zip(limits, caps)]
        if self.max_power_kw > 0:
            self._report("max_power_cap", self.max_power_kw,
                         f"BMS_MAX_POWER_KW={self.max_power_kw}: setpoints are held to {self.max_power_kw} kW "
                         f"whatever is requested", "observed")
            limits = [min(limit, self.max_power_kw) for limit in limits]
        self.limits_kw = tuple(limits)
        self.state.limits_kw = {"max_charge_kw": self.limits_kw[0], "max_discharge_kw": self.limits_kw[1]}
        return self.limits_kw

    async def _read_measured_kw(self):
        """Measured power (31009 AC / 31007 DC) in the app's sign, or None."""
        register = REG_DC_POWER if self._bms_type == BMS_TYPE_DC else REG_ACTIVE_POWER
        try:
            result = await self._client.read_input_registers(register, count=2, slave=self._unit)
            return None if result.isError() else -_decode_int32(result.registers)
        except Exception:
            return None

    async def _verify_dispatch(self, reg_addr: int, words: list, expect_on: bool, setpoint_kw: float = 0.0):
        """Confirm a setpoint write the ways the spec allows: read the
        register back (FC03, §4.2), check the control mode (31036: in
        User-Controlled mode writes are accepted but not stored, §6.1) and,
        for a non-zero setpoint, that the BMS is On (31000; E6: or in a state
        code the ICD's table does not list while the measured power follows
        the setpoint). Returns None when confirmed, else (kind, detail). This
        read also catches a half-open TCP connection where the write only
        appeared to succeed."""
        try:
            readback = await asyncio.wait_for(
                self._client.read_holding_registers(reg_addr, count=2, slave=self._unit), timeout=2)
            state_mode = await asyncio.wait_for(self._read_state_and_mode(), timeout=2)
        except Exception as e:
            return ("verify_failed", f"read-back failed ({type(e).__name__})")
        state, mode = state_mode
        if readback.isError() or state is None:
            return ("verify_failed", "read-back refused")
        if mode == CONTROL_USER:
            return ("user_controlled", "BMS is in User-Controlled mode: writes are accepted "
                                       "but not stored (spec §6.1)")
        if list(readback.registers) != list(words):
            return ("readback_mismatch", f"wrote {words}, BMS holds {list(readback.registers)}")
        if expect_on and state != STATE_ON:
            if state in OPERATING_STATE_NAMES:
                return ("bms_state", f"BMS operating state is "
                                     f"{OPERATING_STATE_NAMES.get(state, state)}, not On")
            self._report("undocumented_state", state,
                         f"BMS operating state {state} is not in the ICD's table (0-10; §6.2.2 names Idle, "
                         f"Charging and Discharging without codes): treated as On only while the measured "
                         f"power follows the setpoint", "observed")
            measured = await self._read_measured_kw()
            if not _follows(measured, setpoint_kw):
                return ("bms_state", f"BMS operating state {state} is not in the ICD's table and the "
                                     f"measured power ({measured} kW) does not follow the setpoint yet")
        return None

    async def go_to_standby(self) -> bool:
        """Full Standby transition per Reg 41000=1. This disconnects all
        Arrays and halts power flow at the hardware level — a deeper
        safe state than safe_idle(), but slower to resume from."""
        success = await self._write_holding_registers(REG_OPERATING_COMMAND, [OPERATING_COMMAND_STANDBY])
        if success:
            log.info("Sent Operating Command: Standby")
        return success

    async def read_status(self) -> dict:
        """Read back SoC, operating state/status, alerts, control mode,
        power limits and measured power for monitoring."""
        try:
            soc_result = await self._client.read_input_registers(REG_STATE_OF_CHARGE, count=1, slave=self._unit)
            state_result = await self._client.read_input_registers(REG_OPERATING_STATE, count=2, slave=self._unit)
            alerts_result = await self._client.read_input_registers(REG_BMS_ALERTS, count=2, slave=self._unit)
            mode_result = await self._client.read_input_registers(REG_CONTROL_MODE, count=1, slave=self._unit)
            if soc_result.isError() or state_result.isError() or alerts_result.isError() or mode_result.isError():
                return {}
            measured_kw = measured_kvar = None
            if self._bms_type is not None:
                measured_kw = await self._read_measured_kw()
            if self._bms_type == BMS_TYPE_AC:
                reactive = await self._client.read_input_registers(REG_REACTIVE_POWER, count=2, slave=self._unit)
                if not reactive.isError():
                    measured_kvar = _decode_int32(reactive.registers)     # as the BMS reports it
            operating_state = state_result.registers[0]
            operating_status = state_result.registers[1]
            alerts_raw = _decode_int32(alerts_result.registers) & 0xFFFFFFFF
            return {
                "soc_pct": soc_result.registers[0] / 10.0,
                "operating_state_code": operating_state,
                "operating_state": OPERATING_STATE_NAMES.get(operating_state, f"Unknown({operating_state})"),
                "operating_status": OPERATING_STATUS_NAMES.get(operating_status, f"Unknown({operating_status})"),
                "control_mode_code": mode_result.registers[0],
                "control_mode": CONTROL_MODE_NAMES.get(mode_result.registers[0], "Unknown"),
                "alerts_raw": alerts_raw,
                "measured_kw": measured_kw,
                "measured_kvar": measured_kvar,
            }
        except Exception as e:
            log.warning(f"Status read failed: {e}")
        return {}

    # ── Status polling (F-04: poll SOC/limits/alerts at <=10s intervals) ─

    async def status_poll_loop(self):
        """Every 10s (per spec §4 / F-04), read back SoC, operating
        state/status, control mode, power limits, measured power and
        alerts, publish them on GatewayState, and supervise the BMS:
        bring it back On after Off/Standby (e.g. a Fault that cleared, or a
        reboot), and make the scheduler re-send the setpoint whenever the
        BMS no longer holds it. New/changed alerts are logged loudly and,
        if wired, recorded to the audit log and published on the event bus
        (NF-05)."""
        log.info("Status poll loop started (10s cadence, per spec F-04).")
        while True:
            await asyncio.sleep(10)
            if not self._client.connected:
                continue
            await self.supervise()

    async def supervise(self):
        """One status poll + BMS supervision step (see status_poll_loop)."""
        if self._bms_type is None:
            await self._detect_bms_type()
        status = await self.read_status()
        if not status:
            return
        self.state.soc_pct = status["soc_pct"]
        self.state.operating_state = status["operating_state"]
        self.state.operating_state_code = status["operating_state_code"]
        self.state.operating_status = status["operating_status"]
        self._note_control_mode(status["control_mode_code"])
        # NF-03: every BMS state and health change is audited when the poll sees it. Until now only the code paths
        # that happened to read 31000 (a command, a write's verification) did, so a Shutting Down, a Standby or a
        # return to On during manual control or normal running left no row (found by the load and audit check).
        self._report("operating_state", status["operating_state_code"],
                     f"BMS operating state {status['operating_state']}", "observed")
        self._report("operating_status", status["operating_status"],
                     f"BMS operating status {status['operating_status']}", "observed")
        self.state.measured_kw = status["measured_kw"]
        self.state.measured_kvar = status["measured_kvar"]
        self.state.last_status_poll = asyncio.get_running_loop().time()
        self.state.last_status_poll_at = time.time()
        await self._read_limits()
        if self._bms_type == BMS_TYPE_AC:
            async with self._lock:
                await self._ensure_grid_modes()     # someone may have changed them (E7)
            await self._raise_pending_alarm()

        code = status["operating_state_code"]
        running = code == STATE_ON or (
            code not in OPERATING_STATE_NAMES and self.commanded_kw is not None
            and (self.commanded_kw == 0 or _follows(status["measured_kw"], self.commanded_kw)))
        if status["control_mode_code"] == CONTROL_USER:
            self._forget_setpoint()          # the BMS ignores Modbus writes (spec §6.1)
        elif not running:
            self._forget_setpoint()          # the setpoint is not in effect until On
            if code in (STATE_OFF, STATE_STANDBY):
                await self.ensure_bms_on()
        else:
            if status["control_mode_code"] == CONTROL_NORMAL:
                self._end_take_over()        # found running: a take-over ends with nothing to command (E1)
            await self._check_setpoint_held()

        alerts_raw = status["alerts_raw"]
        if alerts_raw != self._last_alerts_raw:
            self._last_alerts_raw = alerts_raw
            if alerts_raw:
                log.warning(f"BMS Alerts changed: 0x{alerts_raw:08X}")
                self._audit("bms_alert", f"alerts_raw=0x{alerts_raw:08X}", "active")
                if self.event_bus:
                    await self.event_bus.publish("alerts", {
                        "alerts_raw": alerts_raw,
                        "operating_status": status["operating_status"],
                    })
            else:
                log.info("BMS Alerts cleared.")

    async def _check_setpoint_held(self):
        """Does the BMS still hold the setpoint it confirmed? If not, the
        scheduler re-sends it."""
        if self._written is not None:
            reg_addr, words = self._written
            try:
                readback = await self._client.read_holding_registers(reg_addr, count=2, slave=self._unit)
                if not readback.isError() and list(readback.registers) != list(words):
                    log.warning(f"BMS no longer holds the setpoint (wrote {words}, reads "
                                f"{list(readback.registers)}) — re-sending")
                    self._audit("bms_condition", f"setpoint register {40000 + reg_addr} changed "
                                f"from {words} to {list(readback.registers)}", "re-sending")
                    self._forget_setpoint()
            except Exception as e:
                log.warning(f"Setpoint read-back failed: {e}")

    # ── Heartbeat (Section 4.5.1 of spec) ───────────────────────

    async def heartbeat_loop(self):
        """
        REQUIRED by the real hardware, absent from the placeholder driver.

        Every ~1 second: read the BMS's own heartbeat register (31003) and
        echo that value to the Operator Heartbeat register (41002), as spec
        §4.5.1 describes. If this stops, the BMS will consider the
        controller lost even if the TCP socket is still open. A BMS
        heartbeat that stops changing is reported and stops dispatch (E4);
        one that jumps backwards means the BMS restarted, so its setpoint is
        re-sent.
        """
        log.info("Heartbeat loop started (1s cadence, per spec Sec 4.5.1).")
        while True:
            try:
                await self.heartbeat_once()
            except Exception as e:
                log.warning(f"Heartbeat exchange failed: {e}")
            await asyncio.sleep(1)

    async def heartbeat_once(self):
        self._track_link()
        if not self._client.connected:
            return
        bms_hb = await self._client.read_input_registers(REG_HEARTBEAT, count=1, slave=self._unit)
        if bms_hb.isError():
            return
        value = bms_hb.registers[0]
        frozen = self._note_bms_heartbeat(value)
        async with self._lock:
            await self._write_holding_registers(REG_OPERATOR_HEARTBEAT, [value])   # keep echoing (§4.5.1)
        if frozen:
            await self._alarm("bms_heartbeat", "frozen",
                              f"BMS heartbeat 31003 stuck at {value} for over {HEARTBEAT_STALE_SEC} s while its "
                              f"registers still answer: dispatch confirmation withdrawn, 0 kW commanded, no "
                              f"non-zero setpoint until it has incremented again for {HEARTBEAT_RESUME_SEC} s")
            await self.safe_idle(source="bms_heartbeat_frozen")

    def _note_bms_heartbeat(self, value: int) -> bool:
        """Track 31003. Returns True when it has just been found stuck (E4):
        the caller raises the alarm and commands 0 kW."""
        now = time.monotonic()
        last = self._last_bms_heartbeat
        if last is None or value != last:
            if last is not None and value < last and not (last > 0xFF00 and value < 0x0100):
                log.warning(f"BMS heartbeat went from {last} to {value}: the BMS restarted — "
                            f"its setpoint will be re-sent")
                self._audit("bms_condition", f"BMS heartbeat restarted ({last} -> {value})",
                            "re-sending")
                self._forget_setpoint()
                self._taking_over = True       # E1/E3: a restarted BMS is taken over afresh
                self._grid_modes = None
            self._last_bms_heartbeat, self._heartbeat_changed_at = value, now
            self.state.bms_heartbeat_stale = False
            self._report("bms_heartbeat", "live", f"BMS heartbeat 31003 changing ({value})", "live")
            if self._frozen_since is not None:
                if self._resumed_at is None:
                    self._resumed_at = now
                elif now - self._resumed_at >= HEARTBEAT_RESUME_SEC:
                    self._frozen_since = self._resumed_at = None
                    log.info("BMS heartbeat incrementing again: dispatch resumes")
                    self._audit("bms_alarm", f"BMS heartbeat 31003 incrementing again for "
                                f"{HEARTBEAT_RESUME_SEC} s: dispatch resumes", "cleared")
                    self._reported.pop("alarm:bms_heartbeat", None)
        elif now - self._heartbeat_changed_at > HEARTBEAT_STALE_SEC:
            self.state.bms_heartbeat_stale = True
            self._report("bms_heartbeat", "stale",
                         f"BMS heartbeat 31003 stuck at {value} for over {HEARTBEAT_STALE_SEC}s "
                         f"(spec: increments every 1 s)", "stale")
            self._resumed_at = None
            if self._frozen_since is None:
                self._frozen_since = now
                self._forget_setpoint()
                return True
        return False

    # ── Watchdog ───────────────────────────────────────────────

    async def watchdog(self):
        """
        Monitors the Modbus connection and reconnects. (Grid-loss safe idle
        is decided by the scheduler's dispatch arbiter, which owns every
        setpoint decision; the watchdog no longer writes setpoints itself.)
        """
        log.info("Watchdog started.")
        while True:
            await asyncio.sleep(10)
            await self.ensure_connected()

    def close(self):
        """Close the Modbus TCP connection (gateway shutdown)."""
        self._client.close()

    async def ensure_connected(self):
        """One watchdog check: if the Modbus link is down, reconnect,
        re-detect the BMS type and bring it On. The last confirmed setpoint
        becomes unknown, so the scheduler re-sends its current decision."""
        self._track_link()
        if not self._client.connected:
            log.warning("Modbus disconnected — attempting reconnect...")
            self._reconnect_attempts += 1
            self._forget_setpoint()   # device state unknown until re-confirmed
            await self._client.connect()
            if self._client.connected:
                self._track_link()
                await self._take_over()

    def _track_link(self):
        """Record a Modbus link change where it is seen: every heartbeat (1 s)
        and every watchdog check. pymodbus reconnects on its own, so the link
        can come back (and drop again) without the watchdog reconnecting it;
        recording changes only there left a restored link unaudited and the
        next loss unrecorded. A lost link also ends the BMS's confirmation,
        and whatever comes back is taken over afresh (E1/E3)."""
        up = bool(self._client.connected)
        if up == self._link_up:
            return
        was, self._link_up = self._link_up, up
        if not up:
            self._forget_setpoint()
            self._taking_over = True
            self._grid_modes = None
            self._audit("modbus_link", "connection lost — reconnecting", "reconnecting")
        elif was is not None:
            self._audit("modbus_link", f"reconnected after {self._reconnect_attempts} attempt(s)"
                        if self._reconnect_attempts else "reconnected (Modbus client reconnected on its own)",
                        "connected")
        if up:
            self._reconnect_attempts = 0

    # ── Internal ───────────────────────────────────────────────

    def _audit(self, event_type: str, detail: str, outcome: str):
        if self.audit_log:
            self.audit_log.record(event_type=event_type, source="modbus_driver",
                                  detail=detail, outcome=outcome)

    async def _alarm(self, topic: str, condition, detail: str):
        """A condition an operator has to look at: logged as an error,
        audited (bms_alarm) and published on the event bus's alerts topic
        (NF-05), once per change of condition."""
        key = f"alarm:{topic}"
        if self._reported.get(key) == condition:
            return
        self._reported[key] = condition
        log.error(detail)
        self._audit("bms_alarm", detail, "alarm")
        if self.event_bus:
            await self.event_bus.publish("alerts", {"alarm": topic, "detail": detail})

    async def _raise_pending_alarm(self):
        """Raise the alarm _set_grid_modes() left (it cannot await)."""
        pending, self._pending_alarm = self._pending_alarm, None
        if pending is not None:
            await self._alarm(*pending)

    def _audit_write(self, source, requested_kw, target_kw, reg_addr, outcome, verified=""):
        # The scheduler retries a failed setpoint every second: record a
        # failure when it first happens or changes, not on every retry (60
        # identical rows a minute during a Modbus outage). The scheduler's
        # dispatch row records the recovery and the number of attempts.
        failure = None if outcome == "confirmed" else (source, requested_kw, outcome)
        if failure is not None and failure == self._last_write_failure:
            return
        self._last_write_failure = failure
        register = f"{40000 + reg_addr}" if reg_addr is not None else "none"
        target = f"{target_kw:+.1f} kW" if target_kw is not None else "-"
        limits = (f" limits charge<={self.limits_kw[0]} discharge<={self.limits_kw[1]} kW"
                  if self.limits_kw else "")
        self._audit("modbus_write", f"src={source} requested={float(requested_kw):+} kW "
                    f"written={target} register={register}{limits}"
                    + (f"; verified: {verified}" if verified else ""), outcome)

    def _report(self, topic: str, condition, detail: str, outcome: str):
        """Log and audit a BMS condition once per change (not every poll)."""
        if self._reported.get(topic) != condition:
            self._reported[topic] = condition
            self._audit("bms_condition", detail, outcome)

    async def _read_holding(self, address: int, count: int):
        """(registers, None), or (None, why) if they cannot be read: "exception
        N" when the BMS refused the read, else what went wrong on the way."""
        try:
            result = await self._client.read_holding_registers(address, count=count, slave=self._unit)
        except Exception as e:
            log.warning(f"Holding register read at {40000 + address} failed: {e}")
            return None, f"no answer: {type(e).__name__}"
        if result.isError():
            return None, f"exception {getattr(result, 'exception_code', None)}"
        return list(result.registers), None

    async def _write_holding_registers(self, addr: int, values: list) -> bool:
        """Write one or more consecutive holding registers. Returns True
        on success. Uses write_registers (multi) even for single values
        for consistent handling of 32-bit (2-register) writes."""
        success, _ = await self._write(addr, values)
        return success

    async def _write(self, addr: int, values: list) -> tuple[bool, tuple | None]:
        """_write_holding_registers() plus the reason for a failure:
        ("exception", code) when the BMS answered with a Modbus exception,
        otherwise ("disconnected" | "timeout" | "error", detail).

        Wrapped in a hard timeout: if a write doesn't get a confirmed
        Modbus-level response within 3s, we treat it as a failure rather
        than trust an ambiguous in-flight state (e.g. a half-closed TCP
        connection where the local write briefly appears to succeed)."""
        if not self._client.connected:
            log.error("Cannot write — Modbus not connected")
            return False, ("disconnected", None)
        try:
            result = await asyncio.wait_for(
                self._client.write_registers(addr, values, slave=self._unit), timeout=3
            )
            if result is None:
                log.error(f"Register write error at addr {addr}: no response")
                return False, ("error", "no response")
            if result.isError():
                log.error(f"Register write error at addr {addr}: {result}")
                return False, ("exception", getattr(result, "exception_code", None))
            return True, None
        except asyncio.TimeoutError:
            log.error(f"Modbus write to addr {addr} timed out — treating as failure")
            return False, ("timeout", None)
        except Exception as e:
            log.error(f"Modbus exception during write: {e}")
            return False, ("error", str(e))
