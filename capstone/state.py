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
        # Set by the Scheduler's dispatch arbiter every tick: who has
        # authority over the setpoint, what it wants and whether the BMS
        # confirmed it ({"authority", "mrid", "desired_kw", "reason",
        # "until", "confirmed"}).
        self.authority = None
        # Set by ModbusDriver from what the BMS reports (SPC100644 Rev 03).
        self.commanded_kw = None        # setpoint the BMS last confirmed holding (app sign)
        self.limits_kw = None           # {"max_charge_kw", "max_discharge_kw"} from 31019 / 31021
        self.measured_kw = None         # measured power 31009 (AC) / 31007 (DC), app sign
        self.control_mode = None        # 31036: "Normal" or "User-Controlled"
        self.dispatch_kind = None       # "ac_active_power" (41009) or "dc_power_target" (41007)
        self.bms_heartbeat_stale = False
        self.last_status_poll_at = None
        self.operating_state_code = None  # 31000 as read at the last status poll
        self.measured_kvar = None       # measured reactive power 31011 (AC), kVAR, app sign
        # Set by IEEEClient from the utility server's Time resource: seconds to
        # add to this machine's clock to get the utility's (event windows).
        self.clock_offset_sec = 0.0

    def mark_grid_lost(self):
        self.grid_connected = False

    def mark_grid_contact(self):
        self.grid_connected = True
        self.last_grid_contact = time.time()

    def seconds_since_grid_contact(self) -> float:
        return time.time() - self.last_grid_contact
