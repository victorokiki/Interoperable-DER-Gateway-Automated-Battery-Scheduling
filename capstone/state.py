"""
core/state.py
Shared Gateway State

Holds the live state of the battery gateway: current operating mode,
power setpoint, and grid connectivity — shared between the Modbus
driver, scheduler, and IEEE client.
"""

import time
from enum import Enum


class BatteryMode(Enum):
    IDLE       = "IDLE"
    CHARGE     = "CHARGE"
    DISCHARGE  = "DISCHARGE"


class GatewayState:
    def __init__(self):
        self.mode = BatteryMode.IDLE
        self.power_kw = 0.0
        self.last_command_source = None
        self.grid_connected = True
        self.last_grid_contact = time.time()
        # Populated by ModbusDriver.status_poll_loop() (F-04) — last known
        # BMS status snapshot, read every <=10s per spec.
        self.soc_pct = None
        self.operating_state = None
        self.operating_status = None
        self.last_status_poll = None

    def mark_grid_lost(self):
        self.grid_connected = False

    def mark_grid_contact(self):
        self.grid_connected = True
        self.last_grid_contact = time.time()

    def seconds_since_grid_contact(self) -> float:
        return time.time() - self.last_grid_contact
