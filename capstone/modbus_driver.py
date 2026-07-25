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
   as a side effect of a power setpoint's SIGN, not a chosen mode.

2. DC-configured and AC-configured systems use different registers
   entirely (41007 vs 41009). This driver reads Reg 31038 at connect
   time to detect which one it's talking to, rather than assuming.

3. The real hardware's sign convention is OPPOSITE the convention used
   everywhere else in this codebase (scheduler.py / ieee_client.py treat
   positive kW = charge). Rather than flip that convention throughout the
   app, the sign is inverted ONLY at the point of writing to the register
   — see _to_hardware_sign() below.

4. The BMS requires a continuous heartbeat (Reg 41002, incrementing,
   ~1s) independent of the TCP connection being open. Without it the
   BMS will treat the controller as lost even on a healthy socket. This
   is implemented as heartbeat_loop(), which main.py must run as its own
   background task alongside watchdog().
──────────────────────────────────────────────────
"""

import asyncio
import logging
from pymodbus.client import AsyncModbusTcpClient
from capstone.state import BatteryMode, GatewayState

log = logging.getLogger("modbus_driver")

# ── BMS Control Registers (Holding, addr = reg - 40000) ─────────────────
REG_OPERATING_COMMAND   = 1000  # 0=Off, 1=Standby, 2=On
REG_CLEAR_ALERTS        = 1001  # 1=Clear All Alerts
REG_OPERATOR_HEARTBEAT  = 1002  # must increment every write
REG_DC_POWER_TARGET     = 1007  # DC-configured only, int32, kW, x1
REG_ACTIVE_POWER_SETPT  = 1009  # AC-configured only, int32, kW, x1

# ── BMS Status Registers (Input, addr = reg - 30000) ────────────────────
REG_OPERATING_STATE     = 1000  # enum, see OPERATING_STATE_NAMES
REG_OPERATING_STATUS    = 1001  # 0 Invalid/1 Normal/2 Warning/3 Fault/4 Critical
REG_HEARTBEAT           = 1003  # BMS's own heartbeat, increments 1/s
REG_STATE_OF_CHARGE     = 1004  # uint16, x10 -> percent
REG_BMS_TYPE            = 1038  # 0 = DC-configured, 1 = AC-configured
REG_BMS_ALERTS          = 1039  # bitfield, uint32 (2 registers)

OPERATING_COMMAND_OFF     = 0
OPERATING_COMMAND_STANDBY = 1
OPERATING_COMMAND_ON      = 2

OPERATING_STATE_NAMES = {
    0: "Init", 1: "Off", 2: "Standby", 3: "Starting up", 4: "On",
    5: "Shutting Down", 6: "Transitioning to Standby", 7: "Turning Off",
    8: "Waiting for Connection", 9: "Waiting for Disconnection", 10: "Fault",
}
OPERATING_STATUS_NAMES = {0: "Invalid", 1: "Normal", 2: "Warning", 3: "Fault", 4: "Critical Fault"}

BMS_TYPE_DC = 0
BMS_TYPE_AC = 1


def _encode_int32(value: int) -> list:
    """Encode a signed 32-bit value as [high_word, low_word], per spec
    4.4: 'the most significant word is transmitted first'."""
    if value < 0:
        value += 1 << 32
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def _decode_int32(regs: list) -> int:
    value = (regs[0] << 16) | regs[1]
    if value >= (1 << 31):
        value -= (1 << 32)
    return value


class ModbusDriver:
    def __init__(self, config: dict, state: GatewayState, audit_log=None, event_bus=None):
        self.host   = config["modbus_host"]
        self.port   = config["modbus_port"]
        self.state  = state
        self.offline_timeout = config["offline_timeout_sec"]
        self.audit_log = audit_log   # optional — NF-03
        self.event_bus = event_bus   # optional — NF-05, used for BMS alert notifications
        self._client = AsyncModbusTcpClient(host=self.host, port=self.port)
        self._lock   = asyncio.Lock()   # prevent concurrent writes
        self._bms_type = None           # populated on connect(): BMS_TYPE_DC / BMS_TYPE_AC
        self._heartbeat_value = 0
        self._last_alerts_raw = None    # last-seen BMS Alerts [31039] value, to detect changes

    # ── Connection ─────────────────────────────────────────────

    async def connect(self):
        log.info(f"Connecting to Modbus TCP at {self.host}:{self.port}")
        await self._client.connect()
        if not self._client.connected:
            log.error("Modbus TCP connection FAILED. Will retry in watchdog.")
            return

        log.info("Modbus TCP connected.")
        await self._detect_bms_type()
        await self._turn_on()

    async def _detect_bms_type(self):
        """Read Reg 31038 to find out if this is a DC- or AC-configured
        BMS, rather than assuming. See ICD Section 2 (open items)."""
        try:
            result = await self._client.read_input_registers(REG_BMS_TYPE, count=1)
            if not result.isError():
                self._bms_type = result.registers[0]
                label = "AC-configured" if self._bms_type == BMS_TYPE_AC else "DC-configured"
                log.info(f"Detected BMS type: {label} (Reg 31038 = {self._bms_type})")
            else:
                log.warning("Could not read BMS Type register — defaulting to AC-configured")
                self._bms_type = BMS_TYPE_AC
        except Exception as e:
            log.warning(f"BMS type detection failed ({e}) — defaulting to AC-configured")
            self._bms_type = BMS_TYPE_AC

    async def _turn_on(self):
        """Issue the On command per the state machine (spec Sec 6.2).
        The BMS is only usable once it reports back an operating state
        of On/Starting/Idle/Charging/Discharging via Reg 31000."""
        success = await self._write_holding_registers(REG_OPERATING_COMMAND, [OPERATING_COMMAND_ON])
        if success:
            log.info("Sent Operating Command: On")
        else:
            log.error("Failed to send On command — battery will remain in Standby/Off")

    # ── Command API ────────────────────────────────────────────

    async def set_power(self, power_kw: float, source: str = "manual") -> bool:
        """
        Set the requested power level, in THIS APP'S convention:
          power_kw > 0  →  CHARGE    at  power_kw
          power_kw < 0  →  DISCHARGE at |power_kw|
          power_kw = 0  →  safe idle (zero setpoint)

        NOTE: the real hardware's sign convention is the OPPOSITE of the
        above (positive = discharge/export). The inversion happens here,
        at the boundary, so callers (scheduler.py) don't need to change.

        Returns True if the write succeeded, False otherwise.
        """
        if power_kw == 0:
            return await self.safe_idle()

        if self._bms_type is None:
            log.error("Cannot set power — BMS type not yet detected (connect() not completed)")
            return False

        hardware_value_kw = -power_kw  # invert: app convention -> hardware convention

        async with self._lock:
            reg_addr = REG_DC_POWER_TARGET if self._bms_type == BMS_TYPE_DC else REG_ACTIVE_POWER_SETPT
            reg_words = _encode_int32(int(hardware_value_kw))

            success = await self._write_holding_registers(reg_addr, reg_words)

            if success:
                # Don't fully trust a "successful" write on its own — verify
                # the connection is still genuinely alive immediately after.
                # This guards against a half-open TCP connection where a
                # write briefly appears to succeed right as the peer dies.
                success = await self._verify_connection_alive()
                if not success:
                    log.error("Write appeared to succeed but the connection "
                              "failed verification immediately after — "
                              "treating as failed, not updating state.")

            if success:
                bmode = BatteryMode.CHARGE if power_kw > 0 else BatteryMode.DISCHARGE
                self.state.mode     = bmode
                self.state.power_kw = power_kw
                self.state.last_command_source = source
                log.info(f"Battery → {bmode.value} at {abs(power_kw):.1f} kW "
                         f"[hw_setpoint={hardware_value_kw:.1f} kW, src: {source}]")
            else:
                log.error("Modbus write failed — battery state unknown")

            return success

    async def safe_idle(self) -> bool:
        """Zero the power setpoint (safe state). Used on shutdown, grid
        loss, or an explicit idle command.

        Note: this does NOT transition the BMS to Standby — it keeps the
        unit On but at zero power, which is faster to resume from. If a
        deeper safe state (Standby) is ever required, call go_to_standby()
        separately."""
        if self._bms_type is None:
            log.error("Cannot idle — BMS type not yet detected (connect() not completed)")
            return False

        async with self._lock:
            reg_addr = REG_DC_POWER_TARGET if self._bms_type == BMS_TYPE_DC else REG_ACTIVE_POWER_SETPT
            success = await self._write_holding_registers(reg_addr, _encode_int32(0))
            if success:
                self.state.mode     = BatteryMode.IDLE
                self.state.power_kw = 0.0
                log.info("Battery → IDLE (zero power setpoint)")
            return success

    async def go_to_standby(self) -> bool:
        """Full Standby transition per Reg 41000=1. This disconnects all
        Arrays and halts power flow at the hardware level — a deeper
        safe state than safe_idle(), but slower to resume from."""
        success = await self._write_holding_registers(REG_OPERATING_COMMAND, [OPERATING_COMMAND_STANDBY])
        if success:
            log.info("Sent Operating Command: Standby")
        return success

    async def read_status(self) -> dict:
        """Read back SoC, operating state/status, and alerts for monitoring."""
        try:
            soc_result = await self._client.read_input_registers(REG_STATE_OF_CHARGE, count=1)
            state_result = await self._client.read_input_registers(REG_OPERATING_STATE, count=2)
            alerts_result = await self._client.read_input_registers(REG_BMS_ALERTS, count=2)
            if soc_result.isError() or state_result.isError() or alerts_result.isError():
                return {}
            operating_state = state_result.registers[0]
            operating_status = state_result.registers[1]
            alerts_raw = _decode_int32(alerts_result.registers) & 0xFFFFFFFF
            return {
                "soc_pct": soc_result.registers[0] / 10.0,
                "operating_state": OPERATING_STATE_NAMES.get(operating_state, f"Unknown({operating_state})"),
                "operating_status": OPERATING_STATUS_NAMES.get(operating_status, f"Unknown({operating_status})"),
                "alerts_raw": alerts_raw,
            }
        except Exception as e:
            log.warning(f"Status read failed: {e}")
        return {}

    # ── Status polling (F-04: poll SOC/limits/alerts at <=10s intervals) ─

    async def status_poll_loop(self):
        """Every 10s (per spec §4 / F-04), read back SoC, operating
        state/status, and alerts, and publish them onto GatewayState so
        other modules (and a future dashboard) can see current battery
        health without issuing their own Modbus reads. New/changed
        alerts are logged loudly and, if wired, recorded to the audit
        log and published on the event bus (NF-05) — this is the one
        genuinely asynchronous, fire-and-forget notification in the
        system, unlike scheduling commands which need a direct
        success/failure return value."""
        log.info("Status poll loop started (10s cadence, per spec F-04).")
        while True:
            await asyncio.sleep(10)
            if not self._client.connected:
                continue
            status = await self.read_status()
            if not status:
                continue

            self.state.soc_pct = status["soc_pct"]
            self.state.operating_state = status["operating_state"]
            self.state.operating_status = status["operating_status"]
            self.state.last_status_poll = asyncio.get_running_loop().time()

            alerts_raw = status["alerts_raw"]
            if alerts_raw != self._last_alerts_raw:
                self._last_alerts_raw = alerts_raw
                if alerts_raw:
                    log.warning(f"BMS Alerts changed: 0x{alerts_raw:08X}")
                    if self.audit_log:
                        self.audit_log.record(
                            event_type="bms_alert",
                            source="modbus_driver",
                            detail=f"alerts_raw=0x{alerts_raw:08X}",
                            outcome="active",
                        )
                    if self.event_bus:
                        await self.event_bus.publish("alerts", {
                            "alerts_raw": alerts_raw,
                            "operating_status": status["operating_status"],
                        })
                else:
                    log.info("BMS Alerts cleared.")

    # ── Heartbeat (Section 4.5.1 of spec) ───────────────────────

    async def heartbeat_loop(self):
        """
        REQUIRED by the real hardware, absent from the placeholder driver.

        Every ~1 second: read the BMS's own heartbeat register (31003),
        then write an incrementing value to the Operator Heartbeat
        register (41002). If this stops, the BMS will consider the
        controller lost even if the TCP socket is still open.
        """
        log.info("Heartbeat loop started (1s cadence, per spec Sec 4.5.1).")
        while True:
            try:
                if self._client.connected:
                    bms_hb = await self._client.read_input_registers(REG_HEARTBEAT, count=1)
                    if not bms_hb.isError():
                        self._heartbeat_value = (self._heartbeat_value + 1) & 0xFFFF
                        async with self._lock:
                            await self._write_holding_registers(REG_OPERATOR_HEARTBEAT, [self._heartbeat_value])
            except Exception as e:
                log.warning(f"Heartbeat exchange failed: {e}")
            await asyncio.sleep(1)

    # ── Watchdog ───────────────────────────────────────────────

    async def watchdog(self):
        """
        Monitors grid connectivity and Modbus reconnection.
        If grid is unreachable for > offline_timeout seconds → safe idle.
        """
        log.info("Watchdog started.")
        while True:
            await asyncio.sleep(10)

            secs_offline = self.state.seconds_since_grid_contact()
            if not self.state.grid_connected and secs_offline > self.offline_timeout:
                log.warning(
                    f"Grid unreachable for {secs_offline:.0f}s "
                    f"(>{self.offline_timeout}s limit) → forcing safe idle"
                )
                await self.safe_idle()

            if not self._client.connected:
                log.warning("Modbus disconnected — attempting reconnect...")
                await self._client.connect()
                if self._client.connected:
                    await self._detect_bms_type()
                    await self._turn_on()

    # ── Internal ───────────────────────────────────────────────

    async def _verify_connection_alive(self) -> bool:
        """Immediately after a write reports success, confirm the
        connection can still complete a real round-trip. Catches the case
        where a write briefly 'succeeds' at the OS/socket level right as
        the peer is dying (half-open TCP), before the connection drop is
        otherwise detected."""
        try:
            result = await asyncio.wait_for(
                self._client.read_input_registers(REG_OPERATING_STATE, count=1),
                timeout=2,
            )
            return not result.isError()
        except Exception:
            return False

    async def _write_holding_registers(self, addr: int, values: list) -> bool:
        """Write one or more consecutive holding registers. Returns True
        on success. Uses write_registers (multi) even for single values
        for consistent handling of 32-bit (2-register) writes.

        Wrapped in a hard timeout: if a write doesn't get a confirmed
        Modbus-level response within 3s, we treat it as a failure rather
        than trust an ambiguous in-flight state (e.g. a half-closed TCP
        connection where the local write briefly appears to succeed)."""
        if not self._client.connected:
            log.error("Cannot write — Modbus not connected")
            return False
        try:
            result = await asyncio.wait_for(
                self._client.write_registers(addr, values), timeout=3
            )
            if result is None or result.isError():
                log.error(f"Register write error at addr {addr}: {result}")
                return False
            return True
        except asyncio.TimeoutError:
            log.error(f"Modbus write to addr {addr} timed out — treating as failure")
            return False
        except Exception as e:
            log.error(f"Modbus exception during write: {e}")
            return False