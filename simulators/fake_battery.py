"""
simulators/fake_battery.py
Fake Endurium BMS — Modbus TCP Server

Emulates the REAL register map from SPC100644 Endurium BMS Modbus
Interface Specification, Rev. 03 (see ICD_Endurium_BMS.docx), replacing
the earlier placeholder simulator that used an invented flat 4-register
layout.

Listens on port 5020 (the spec's default BMS Control and Monitoring
Interface port) and:
  - implements the Off/Standby/On operating-command state machine
  - accepts DC Power Target (DC-configured) or Active Power Setpoint
    (AC-configured) writes and logs them in human-readable form
  - simulates a slowly drifting State of Charge based on commanded power
  - increments its own heartbeat register every second (spec Sec 4.5.1)
  - configurable as DC- or AC-configured via BMS_TYPE env var

Usage:
    python simulators/fake_battery.py
    BMS_TYPE=dc python simulators/fake_battery.py   # simulate a DC-configured unit
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

REG_OPERATING_STATE     = 1000  # Input: enum
REG_OPERATING_STATUS    = 1001  # Input: enum
REG_HEARTBEAT           = 1003  # Input: BMS's own heartbeat, incr 1/s
REG_STATE_OF_CHARGE     = 1004  # Input: uint16, x10
REG_BMS_TYPE            = 1038  # Input: 0=DC, 1=AC
REG_BMS_ALERTS          = 1039  # Input: bitfield, uint32 (2 regs)

BMS_TYPE_DC, BMS_TYPE_AC = 0, 1

OPERATING_STATE_NAMES = {
    0: "Init", 1: "Off", 2: "Standby", 3: "Starting up", 4: "On",
    5: "Shutting Down", 6: "Transitioning to Standby", 7: "Turning Off",
    8: "Waiting for Connection", 9: "Waiting for Disconnection", 10: "Fault",
}

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
ir_store.setValues(REG_BMS_TYPE, [bms_type])
ir_store.setValues(REG_OPERATING_STATE, [1])   # start Off
ir_store.setValues(REG_OPERATING_STATUS, [1])  # Normal
ir_store.setValues(REG_STATE_OF_CHARGE, [500])  # 50.0%
ir_store.setValues(REG_BMS_ALERTS, [0, 0])

last_values = {
    "operating_command": -1,
    "power_setpoint": None,
    "clear_alerts": -1,
}
_soc_x10 = 500.0
_heartbeat = 0
_last_hw_power_kw = 0.0


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
        # --- Operating Command -> Operating State transition ---
        cmd = hr_store.getValues(REG_OPERATING_COMMAND, 1)[0]
        if cmd != last_values["operating_command"]:
            if cmd == 2:
                ir_store.setValues(REG_OPERATING_STATE, [4])  # On
                print(f"\n{'='*55}\n  [BATTERY SIM] Operating Command -> On\n{'='*55}\n")
            elif cmd == 1:
                ir_store.setValues(REG_OPERATING_STATE, [2])  # Standby
                print(f"\n{'='*55}\n  [BATTERY SIM] Operating Command -> Standby "
                      f"(power flow halted)\n{'='*55}\n")
                hr_store.setValues(power_reg, encode_int32(0))
            elif cmd == 0:
                ir_store.setValues(REG_OPERATING_STATE, [1])  # Off
                print(f"\n{'='*55}\n  [BATTERY SIM] Operating Command -> Off\n{'='*55}\n")
            last_values["operating_command"] = cmd

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
        if tick % 10 == 0:  # every ~5s at 0.5s tick
            if _last_hw_power_kw < 0:      # charging
                _soc_x10 = min(1000.0, _soc_x10 + 1)
            elif _last_hw_power_kw > 0:     # discharging
                _soc_x10 = max(0.0, _soc_x10 - 1)
            ir_store.setValues(REG_STATE_OF_CHARGE, [int(_soc_x10)])

        # --- BMS heartbeat, increments every ~1s ---
        if tick % 2 == 0:
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
    print(f"Fake Endurium BMS ({type_label}) listening on port 5020...")
    print("Set BMS_TYPE=dc to simulate a DC-configured unit instead.")
    print("Press Ctrl+C to stop.\n")

    StartTcpServer(context=context, identity=identity, address=("0.0.0.0", 5020))