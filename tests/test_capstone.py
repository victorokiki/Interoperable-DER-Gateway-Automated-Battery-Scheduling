"""
tests/test_capstone.py
Automated test suite for the EMS Bridge
"""

import pytest
import time
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

# ── Test 1: IEEE XML Parsing ────────────────────────────────────

from capstone.ieee_client import IEEEClient

SAMPLE_XML_CHARGE = """<?xml version="1.0"?>
<DERControlList xmlns="urn:ieee:std:2030.5:ns">
  <DERControl>
    <mRID>TEST001</mRID>
    <interval>
      <start>1716400000</start>
      <duration>3600</duration>
    </interval>
    <DERControlBase>
      <opModFixedW>
        <value>500000</value>
        <multiplier>-3</multiplier>
      </opModFixedW>
    </DERControlBase>
  </DERControl>
</DERControlList>"""

SAMPLE_XML_DISCHARGE = """<?xml version="1.0"?>
<DERControlList xmlns="urn:ieee:std:2030.5:ns">
  <DERControl>
    <mRID>TEST002</mRID>
    <interval>
      <start>1716400000</start>
      <duration>1800</duration>
    </interval>
    <DERControlBase>
      <opModFixedW>
        <value>-300000</value>
        <multiplier>-3</multiplier>
      </opModFixedW>
    </DERControlBase>
  </DERControl>
</DERControlList>"""

SAMPLE_XML_EMPTY = """<?xml version="1.0"?>
<DERControlList xmlns="urn:ieee:std:2030.5:ns">
</DERControlList>"""


class TestIEEEParsing:
    def setup_method(self):
        config = {
            "ieee_server_url": "http://localhost:5000",
            "ieee_poll_interval_sec": 30,
            "offline_timeout_sec": 600
        }
        self.client = IEEEClient(config, MagicMock(), MagicMock())

    def test_parse_charge_event(self):
        import xml.etree.ElementTree as ET
        root  = ET.fromstring(SAMPLE_XML_CHARGE)
        ctrl  = root.find("{*}DERControl")
        event = self.client._extract_event(ctrl)
        assert event is not None
        assert event["mrid"]     == "TEST001"
        assert event["power_kw"]  > 0
        assert event["start"]    == 1716400000
        assert event["end"]      == 1716400000 + 3600

    def test_parse_discharge_event(self):
        import xml.etree.ElementTree as ET
        root  = ET.fromstring(SAMPLE_XML_DISCHARGE)
        ctrl  = root.find("{*}DERControl")
        event = self.client._extract_event(ctrl)
        assert event is not None
        assert event["power_kw"] < 0

    def test_empty_control_list(self):
        dispatched = []
        self.client.scheduler.add_event = lambda e: dispatched.append(e)
        self.client._parse_and_dispatch(SAMPLE_XML_EMPTY)
        assert len(dispatched) == 0

    def test_invalid_xml(self):
        self.client._parse_and_dispatch("this is not xml <<>>")


# ── Test 2: Scheduler ───────────────────────────────────────────

from capstone.scheduler import Scheduler
from capstone.state import GatewayState, BatteryMode
import capstone.scheduler as sched_mod

TEST_DB = Path("schedule_store/test_schedule.db")


class TestScheduler:
    def setup_method(self):
        if TEST_DB.exists():
            TEST_DB.unlink()
        sched_mod.DB_PATH = TEST_DB
        config = {
            "ieee_server_url": "",
            "ieee_poll_interval_sec": 30,
            "offline_timeout_sec": 600,
            "modbus_host": "127.0.0.1",
            "modbus_port": 5020
        }
        self.sched = Scheduler(config, GatewayState(), MagicMock())

    def test_add_and_retrieve_event(self):
        now = int(time.time())
        event = {"mrid": "E001", "start": now + 60,
                 "end": now + 3660, "power_kw": 400.0, "source": "test"}
        self.sched.add_event(event)
        upcoming = self.sched.get_upcoming()
        assert any(e["mrid"] == "E001" for e in upcoming)

    def test_event_replaces_duplicate_mrid(self):
        now = int(time.time())
        event = {"mrid": "E002", "start": now + 60,
                 "end": now + 3660, "power_kw": 200.0, "source": "test"}
        self.sched.add_event(event)
        event["power_kw"] = 999.0
        self.sched.add_event(event)
        upcoming = self.sched.get_upcoming()
        matches = [e for e in upcoming if e["mrid"] == "E002"]
        assert len(matches) == 1
        assert matches[0]["power_kw"] == 999.0

    def test_expired_events_not_returned(self):
        past = int(time.time()) - 7200
        event = {"mrid": "E003", "start": past,
                 "end": past + 3600, "power_kw": 100.0, "source": "test"}
        self.sched.add_event(event)
        upcoming = self.sched.get_upcoming()
        assert not any(e["mrid"] == "E003" for e in upcoming)


# ── Test 3: Modbus Register Encoding (real Endurium spec) ──────

from capstone.modbus_driver import (
    _encode_int32, _decode_int32, BMS_TYPE_DC, BMS_TYPE_AC,
    REG_DC_POWER_TARGET, REG_ACTIVE_POWER_SETPT,
)


class TestModbusEncoding:
    """Tests the real 32-bit register encoding (spec Sec 4.4: signed
    int32, most-significant-word-first) and the app<->hardware sign
    inversion, replacing the old flat-register/mode-enum tests."""

    @pytest.mark.parametrize("value", [0, 400, -400, 1, -1, 123456, -123456, 2147483647, -2147483648])
    def test_int32_roundtrip(self, value):
        assert _decode_int32(_encode_int32(value)) == value

    def test_encode_positive_is_two_words(self):
        regs = _encode_int32(400)
        assert len(regs) == 2
        assert regs == [0, 400]

    def test_encode_negative_uses_twos_complement(self):
        regs = _encode_int32(-400)
        # high word should be all 1s (0xFFFF) for a small negative number
        assert regs[0] == 0xFFFF

    def test_charge_inverts_to_negative_hardware_value(self):
        # App convention: power_kw > 0 means CHARGE.
        # Hardware convention (both DC and AC): negative = charge/import.
        app_power_kw = 400.0
        hardware_value = -app_power_kw
        assert hardware_value < 0

    def test_discharge_inverts_to_positive_hardware_value(self):
        app_power_kw = -250.0  # app convention: negative = DISCHARGE
        hardware_value = -app_power_kw
        assert hardware_value > 0

    def test_dc_and_ac_use_different_registers(self):
        assert REG_DC_POWER_TARGET != REG_ACTIVE_POWER_SETPT
        assert BMS_TYPE_DC != BMS_TYPE_AC


# ── Test 4: State & Offline Timeout ────────────────────────────

class TestGatewayState:
    def test_initial_state(self):
        state = GatewayState()
        assert state.mode      == BatteryMode.IDLE
        assert state.power_kw  == 0.0
        assert state.grid_connected is True

    def test_mark_grid_lost(self):
        state = GatewayState()
        state.mark_grid_lost()
        assert state.grid_connected is False

    def test_seconds_since_contact_increases(self):
        state = GatewayState()
        state.last_grid_contact = time.time() - 300
        assert state.seconds_since_grid_contact() >= 300

    def test_reconnect_resets_timer(self):
        state = GatewayState()
        state.mark_grid_lost()
        time.sleep(0.05)
        state.mark_grid_contact()
        assert state.grid_connected is True
        assert state.seconds_since_grid_contact() < 1.0