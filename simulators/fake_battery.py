"""
simulators/fake_battery.py
Fake Endurium BMS — Modbus TCP Server

Emulates the REAL register map from SPC100644 Endurium BMS Modbus
Interface Specification, Rev. 03 (see ICD_Endurium_BMS.docx), replacing
the earlier placeholder simulator that used an invented flat 4-register
layout.

Listens on port 5020 (the spec's default BMS Control and Monitoring
Interface port) and:
  - implements the Off/Standby/On operating-command state machine as
    documented (spec p.16): Standby from Off or On, On only from
    Standby, Off only from Standby
  - accepts DC Power Target (DC-configured) or Active Power Setpoint
    (AC-configured) writes and logs them in human-readable form
  - publishes Charge / Discharge Power Limits (31019 / 31021, spec
    §6.2.6) and the measured power (31009 AC / 31007 DC), which follows
    the setpoint while On and, AC-configured, while the dispatch modes
    41018-41021 select System-Level, Grid-Feeding, Fixed Active Power
    (§6.3; they power up as 0 = Invalid unless SIM_DISPATCH_MODES says
    otherwise)
  - acts on the Operating Command only when 41000 changes (the ICD does not
    say; the gateway handles either, working answer E2)
  - simulates a slowly drifting State of Charge based on commanded power
  - increments its own heartbeat register every second (spec Sec 4.5.1)
  - configurable as DC- or AC-configured via BMS_TYPE env var
  - SIMULATOR ONLY, not in the ICD: holding register 41090 injects
    conditions for tests (1 Fault, 2 the Fault clears to Standby, 3 freeze
    the heartbeat, 4 resume it, 5 Off as another controller would leave it)

Usage:
    python simulators/fake_battery.py
    BMS_TYPE=dc python simulators/fake_battery.py   # simulate a DC-configured unit
    CHARGE_LIMIT_KW=250 DISCHARGE_LIMIT_KW=300 python simulators/fake_battery.py
    SIM_DISPATCH_MODES=0,1,2,1 python simulators/fake_battery.py   # 41018-41021 at power-up (here P-f droop)
"""

import os
import time
import threading
import logging

from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.datastore import ModbusSequentialDataBlock
from pymodbus.device import ModbusDeviceIdentification

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("fake_battery")

# ── Register addresses (0-based, matching the ICD) ──────────────────────
REG_OPERATING_COMMAND   = 1000  # Holding: 0=Off, 1=Standby, 2=On
REG_CLEAR_ALERTS        = 1001  # Holding
REG_OPERATOR_HEARTBEAT  = 1002  # Holding
REG_DC_POWER_TARGET     = 1007  # Holding, int32 (2 regs), DC-configured only
REG_ACTIVE_POWER_SETPT  = 1009  # Holding, int32 (2 regs), AC-configured only
REG_DISPATCH_MODES      = 1018  # Holding: 41018 scope, 41019 grid mode, 41020 active, 41021 reactive
REG_SIM_CONTROL         = 1090  # Holding, SIMULATOR ONLY (not in the ICD): test hooks

REG_OPERATING_STATE     = 1000  # Input: enum
REG_OPERATING_STATUS    = 1001  # Input: enum
REG_HEARTBEAT           = 1003  # Input: BMS's own heartbeat, incr 1/s
REG_STATE_OF_CHARGE     = 1004  # Input: uint16, x10
REG_DC_POWER            = 1007  # Input: DC Power (measured), int32 kW
REG_ACTIVE_POWER        = 1009  # Input: Active Power (measured, AC), int32 kW
REG_CHARGE_POWER_LIMIT  = 1019  # Input: int32 kW, P < 0
REG_DISCHARGE_POWER_LIMIT = 1021  # Input: int32 kW, P > 0
REG_CONTROL_MODE        = 1036  # Input: 0=Normal, 1=User-Controlled
REG_BMS_TYPE            = 1038  # Input: 0=DC, 1=AC
REG_BMS_ALERTS          = 1039  # Input: bitfield, uint32 (2 regs)

BMS_TYPE_DC, BMS_TYPE_AC = 0, 1

OPERATING_STATE_NAMES = {
    0: "Init", 1: "Off", 2: "Standby", 3: "Starting up", 4: "On",
    5: "Shutting Down", 6: "Transitioning to Standby", 7: "Turning Off",
    8: "Waiting for Connection", 9: "Waiting for Disconnection", 10: "Fault",
}
DISPATCH_MODE_NAMES = (
    ("41018 Grid Control Scope", {0: "System-Level", 1: "Per-Array"}),
    ("41019 Grid Mode", {0: "Invalid", 1: "Grid-Feeding"}),
    ("41020 Active Power Control Mode", {0: "Invalid", 1: "Fixed Active Power", 2: "P-f Droop"}),
    ("41021 Reactive Power Control Mode", {0: "Invalid", 1: "Fixed Q", 2: "Fixed PF"}),
)

BLOCK_SIZE = 1100  # comfortably covers all addresses used above


def encode_int32(value: int):
    if value < 0:
        value += 1 << 32
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def decode_int32(regs) -> int:
    value = (regs[0] << 16) | regs[1]
    if value >= (1 << 31):
        value -= (1 << 32)
    return value


# Shared register stores
hr_store = ModbusSequentialDataBlock(0, [0] * BLOCK_SIZE)  # holding (control)
ir_store = ModbusSequentialDataBlock(0, [0] * BLOCK_SIZE)  # input (status)

bms_type = BMS_TYPE_AC if os.environ.get("BMS_TYPE", "ac").lower() == "ac" else BMS_TYPE_DC
charge_limit_kw = int(os.environ.get("CHARGE_LIMIT_KW", "1000"))
discharge_limit_kw = int(os.environ.get("DISCHARGE_LIMIT_KW", "1000"))
ir_store.setValues(REG_BMS_TYPE, [bms_type])
ir_store.setValues(REG_OPERATING_STATE, [1])   # start Off
ir_store.setValues(REG_OPERATING_STATUS, [1])  # Normal
ir_store.setValues(REG_STATE_OF_CHARGE, [500])  # 50.0%
ir_store.setValues(REG_BMS_ALERTS, [0, 0])
ir_store.setValues(REG_CONTROL_MODE, [0])      # Normal control mode
ir_store.setValues(REG_CHARGE_POWER_LIMIT, encode_int32(-charge_limit_kw))
ir_store.setValues(REG_DISCHARGE_POWER_LIMIT, encode_int32(discharge_limit_kw))
hr_store.setValues(REG_DISPATCH_MODES,
                   [int(v) for v in os.environ.get("SIM_DISPATCH_MODES", "0,0,0,0").split(",")])

last_values = {
    "operating_command": -1,
    "power_setpoint": None,
    "clear_alerts": -1,
    "dispatch_modes": None,
}
_soc_x10 = 500.0
_heartbeat = 0
_heartbeat_frozen = False
_last_hw_power_kw = 0.0


def sim_control():
    """SIMULATOR ONLY: act on a test hook written to 41090, then clear it."""
    global _heartbeat_frozen
    action = hr_store.getValues(REG_SIM_CONTROL, 1)[0]
    if not action:
        return
    hr_store.setValues(REG_SIM_CONTROL, [0])
    state = ir_store.getValues(REG_OPERATING_STATE, 1)[0]
    if action == 1:
        ir_store.setValues(REG_OPERATING_STATE, [10])
        print(f"\n{'='*55}\n  [BATTERY SIM] (test hook) FAULT: charge and discharge prohibited\n{'='*55}\n")
    elif action == 2 and state == 10:
        ir_store.setValues(REG_OPERATING_STATE, [2])
        print(f"\n{'='*55}\n  [BATTERY SIM] (test hook) the Fault cleared -> Standby\n{'='*55}\n")
    elif action == 3:
        _heartbeat_frozen = True
        print("  [BATTERY SIM] (test hook) heartbeat 31003 frozen\n")
    elif action == 4:
        _heartbeat_frozen = False
        print("  [BATTERY SIM] (test hook) heartbeat 31003 incrementing again\n")
    elif action == 5:
        hr_store.setValues(REG_OPERATING_COMMAND, [0])
        last_values["operating_command"] = 0
        ir_store.setValues(REG_OPERATING_STATE, [1])
        print(f"\n{'='*55}\n  [BATTERY SIM] (test hook) another controller: Standby, then Off\n{'='*55}\n")
    else:
        print(f"  [BATTERY SIM] (test hook) {action} ignored in state {OPERATING_STATE_NAMES.get(state, state)}")


def simulate_and_log():
    """Background tick: advances simulated state, echoes heartbeat, and
    prints human-readable log lines when commands change — the
    equivalent of a real BMS reacting to the Modbus interface."""
    global _soc_x10, _heartbeat, _last_hw_power_kw

    power_reg = REG_DC_POWER_TARGET if bms_type == BMS_TYPE_DC else REG_ACTIVE_POWER_SETPT
    power_label = "DC Power Target" if bms_type == BMS_TYPE_DC else "Active Power Setpoint"
    charge_word, discharge_word = (("Charging", "Discharging"))

    tick = 0
    while True:
        sim_control()

        # --- Dispatch modes (AC-configured, spec §6.3) ---
        modes = tuple(hr_store.getValues(REG_DISPATCH_MODES, 4))
        if modes != last_values["dispatch_modes"]:
            last_values["dispatch_modes"] = modes
            print("  [BATTERY SIM] dispatch modes: " + ", ".join(
                f"{name} {labels.get(value, value)}" for (name, labels), value in zip(DISPATCH_MODE_NAMES, modes)))

        # --- Operating Command -> Operating State transition ---
        cmd = hr_store.getValues(REG_OPERATING_COMMAND, 1)[0]
        if cmd != last_values["operating_command"]:
            state = ir_store.getValues(REG_OPERATING_STATE, 1)[0]
            name = OPERATING_STATE_NAMES.get(state, state)
            if cmd == 2 and state == 2:
                ir_store.setValues(REG_OPERATING_STATE, [4])  # Standby -> On
                print(f"\n{'='*55}\n  [BATTERY SIM] Operating Command -> On\n{'='*55}\n")
            elif cmd == 1 and state in (1, 4):
                ir_store.setValues(REG_OPERATING_STATE, [2])  # Off/On -> Standby
                print(f"\n{'='*55}\n  [BATTERY SIM] Operating Command -> Standby "
                      f"(power flow halted)\n{'='*55}\n")
            elif cmd == 0 and state == 2:
                ir_store.setValues(REG_OPERATING_STATE, [1])  # Standby -> Off
                print(f"\n{'='*55}\n  [BATTERY SIM] Operating Command -> Off\n{'='*55}\n")
            elif cmd in (0, 1, 2):
                print(f"  [BATTERY SIM] Operating Command {cmd} ignored in state {name} "
                      f"(spec p.16: Off -> Standby -> On)")
            last_values["operating_command"] = cmd

        # Power flows only while On (and, AC-configured, in fixed active-power
        # dispatch); the measured power follows the setpoint. Outside On the
        # limits read 0 (spec §6.2.4: set to 0 to halt power flow).
        on = ir_store.getValues(REG_OPERATING_STATE, 1)[0] == 4
        flowing = on and (bms_type == BMS_TYPE_DC or modes[:3] == (0, 1, 1))
        measured_reg = REG_DC_POWER if bms_type == BMS_TYPE_DC else REG_ACTIVE_POWER
        ir_store.setValues(measured_reg, hr_store.getValues(power_reg, 2) if flowing else [0, 0])
        ir_store.setValues(REG_CHARGE_POWER_LIMIT, encode_int32(-charge_limit_kw) if on else [0, 0])
        ir_store.setValues(REG_DISCHARGE_POWER_LIMIT, encode_int32(discharge_limit_kw) if on else [0, 0])

        # --- Power setpoint writes (hardware sign convention) ---
        raw = hr_store.getValues(power_reg, 2)
        hw_power_kw = decode_int32(raw)
        if hw_power_kw != last_values["power_setpoint"]:
            last_values["power_setpoint"] = hw_power_kw
            _last_hw_power_kw = hw_power_kw
            if hw_power_kw > 0:
                action = f"{discharge_word} (exporting) at {hw_power_kw:.1f} kW"
            elif hw_power_kw < 0:
                action = f"{charge_word} (importing) at {abs(hw_power_kw):.1f} kW"
            else:
                action = "Idle (0 kW)"
            print(f"\n{'='*55}")
            print(f"  [BATTERY SIM] NEW COMMAND: {power_label} = {hw_power_kw:.1f} kW")
            print(f"  -> {action}")
            print(f"{'='*55}\n")

        # --- Clear Alerts ---
        clear = hr_store.getValues(REG_CLEAR_ALERTS, 1)[0]
        if clear == 1 and last_values["clear_alerts"] != 1:
            ir_store.setValues(REG_BMS_ALERTS, [0, 0])
            hr_store.setValues(REG_CLEAR_ALERTS, [0])
            print("  [BATTERY SIM] Alerts cleared.\n")
        last_values["clear_alerts"] = clear

        # --- Drift SoC based on last known power direction (cosmetic) ---
        if tick % 10 == 0 and flowing:  # every ~5s at 0.5s tick, only while power flows
            if _last_hw_power_kw < 0:      # charging
                _soc_x10 = min(1000.0, _soc_x10 + 1)
            elif _last_hw_power_kw > 0:     # discharging
                _soc_x10 = max(0.0, _soc_x10 - 1)
            ir_store.setValues(REG_STATE_OF_CHARGE, [int(_soc_x10)])

        # --- BMS heartbeat, increments every ~1s ---
        if tick % 2 == 0 and not _heartbeat_frozen:
            _heartbeat = (_heartbeat + 1) & 0xFFFF
            ir_store.setValues(REG_HEARTBEAT, [_heartbeat])

        tick += 1
        time.sleep(0.5)


if __name__ == "__main__":
    slave = ModbusSlaveContext(hr=hr_store, ir=ir_store, zero_mode=True)
    context = ModbusServerContext(slaves=slave, single=True)

    identity = ModbusDeviceIdentification()
    identity.VendorName  = "Invinity Sim"
    identity.ProductName = "Fake Endurium BMS"

    t = threading.Thread(target=simulate_and_log, daemon=True)
    t.start()

    type_label = "AC-configured" if bms_type == BMS_TYPE_AC else "DC-configured"
    BIND_HOST = os.environ.get("BATTERY_HOST", "0.0.0.0")
    BIND_PORT = int(os.environ.get("BATTERY_PORT", "5020"))
    print(f"Fake Endurium BMS ({type_label}) listening on port {BIND_PORT}...")
    print("Set BMS_TYPE=dc to simulate a DC-configured unit instead.")
    print("Press Ctrl+C to stop.\n")

    StartTcpServer(context=context, identity=identity, address=(BIND_HOST, BIND_PORT))