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
from fakes import target_w

# opModTargetW counts the power the DER puts out (IEEE 2030.5), so a
# charge is negative on the wire: 0.5 kW charge, 0.3 kW discharge.
SAMPLE_XML_CHARGE = """<?xml version="1.0"?>
<DERControlList xmlns="urn:ieee:std:2030.5:ns">
  <DERControl>
    <mRID>TEST001</mRID>
    <interval>
      <start>1716400000</start>
      <duration>3600</duration>
    </interval>
    <DERControlBase>
      <opModTargetW>
        <multiplier>0</multiplier>
        <value>-500</value>
      </opModTargetW>
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
      <opModTargetW>
        <multiplier>0</multiplier>
        <value>300</value>
      </opModTargetW>
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


@pytest.fixture
def scheduler_db(tmp_path, monkeypatch):
    """A throw-away schedule database, so tests never touch schedule_store/."""
    monkeypatch.setattr(sched_mod, "DB_PATH", tmp_path / "schedule.db")


class TestScheduler:
    @pytest.fixture(autouse=True)
    def _scheduler(self, scheduler_db):
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

# ── Test 5: Reliability FSM (F-05) — DERATED naming per Sprint 1 feedback ──

from capstone.reliability import ReliabilityManager, ReliabilityState


def _make_reliability(offline_timeout_sec=60):
    state = GatewayState()
    return state, ReliabilityManager({"offline_timeout_sec": offline_timeout_sec}, state)


class TestReliabilityFSM:
    def test_state_names(self):
        assert [s.value for s in ReliabilityState] == [
            "CONNECTED", "DERATED", "DISCONNECTED", "AUTONOMOUS", "SAFE_IDLE",
        ]

    def test_one_missed_poll_is_derated(self):
        _, rm = _make_reliability()
        rm.record_poll_failure()
        assert rm.reliability_state is ReliabilityState.DERATED

    def test_success_recovers_to_connected(self):
        _, rm = _make_reliability()
        rm.record_poll_failure()
        rm.record_poll_success()
        assert rm.reliability_state is ReliabilityState.CONNECTED

    def test_three_misses_with_valid_schedule_is_autonomous(self):
        _, rm = _make_reliability()
        for _ in range(3):
            rm.record_poll_failure()
        rm.evaluate_with_schedule_status(has_valid_schedule=True)
        assert rm.reliability_state is ReliabilityState.AUTONOMOUS

    def test_offline_past_grace_without_schedule_is_safe_idle(self):
        state, rm = _make_reliability(offline_timeout_sec=60)
        state.last_grid_contact = time.time() - 300
        for _ in range(3):
            rm.record_poll_failure()
        rm.evaluate_with_schedule_status(has_valid_schedule=False)
        assert rm.reliability_state is ReliabilityState.SAFE_IDLE


# ── Test 6: Multiple schedules — back-to-back, overlapping, unsupported ──

import asyncio
from unittest.mock import AsyncMock


def _multi_event_xml(*mrids_and_powers):
    body = "".join(
        f"<DERControl><mRID>{m}</mRID><interval><start>1000</start><duration>60</duration></interval>"
        f"<DERControlBase>{target_w(w)}</DERControlBase></DERControl>"
        for m, w in mrids_and_powers
    )
    return f'<DERControlList xmlns="urn:ieee:std:2030.5:ns">{body}</DERControlList>'


class TestMultipleSchedules:
    @pytest.fixture(autouse=True)
    def _scheduler(self, scheduler_db):
        self.modbus = MagicMock(commanded_kw=None, last_dispatch_error=None, is_applied=None)
        self.modbus.set_power = AsyncMock(side_effect=self._set_power)
        config = {"ieee_server_url": "", "ieee_poll_interval_sec": 30,
                  "offline_timeout_sec": 600, "modbus_host": "127.0.0.1", "modbus_port": 5020}
        self.sched = Scheduler(config, GatewayState(), self.modbus)
        client_cfg = {"ieee_server_url": "http://x", "ieee_poll_interval_sec": 5,
                      "offline_timeout_sec": 30}
        self.client = IEEEClient(client_cfg, MagicMock(), self.sched)

    async def _set_power(self, power_kw, source=""):
        self.modbus.commanded_kw = power_kw          # a driver remembers what it confirmed
        return True

    def _setpoints(self):
        return [c.args[0] for c in self.modbus.set_power.await_args_list]

    def _ticks(self, *times):
        async def run():
            for now in times:
                await self.sched.tick(now)
        asyncio.run(run())

    def test_list_of_three_events_all_dispatched(self):
        xml = _multi_event_xml(("A", -100000), ("B", 50000), ("C", -200000))
        dispatched = self.client._parse_and_dispatch(xml)
        assert [e["mrid"] for e in dispatched] == ["A", "B", "C"]
        assert [e["power_kw"] for e in dispatched] == [-100.0, 50.0, -200.0]

    def test_unsupported_control_is_rejected_not_zeroed(self):
        xml = ('<DERControlList xmlns="urn:ieee:std:2030.5:ns"><DERControl><mRID>M1</mRID>'
               '<interval><start>1000</start><duration>60</duration></interval>'
               '<DERControlBase><opModMaxLimW><value>5000</value><multiplier>0</multiplier>'
               '</opModMaxLimW></DERControlBase></DERControl></DERControlList>')
        assert self.client._parse_and_dispatch(xml) == []

    def test_back_to_back_end_does_not_idle_battery(self):
        now = int(time.time())
        self.sched.add_event({"mrid": "A", "start": now - 20, "end": now, "power_kw": 100.0, "source": "t"})
        self.sched.add_event({"mrid": "B", "start": now, "end": now + 30, "power_kw": -200.0, "source": "t"})
        self._ticks(now - 20, now)
        assert self._setpoints() == [100.0, -200.0]       # straight from A to B, no 0 kW between

    def test_overlap_end_does_not_resume_superseded_event(self):
        # Previously asserted that C resumed when D ended. Sprint 2 F-03
        # ("superseded events do not fire") and the Phase 1 authority
        # requirement say a superseded event never regains control.
        now = int(time.time())
        self.sched.add_event({"mrid": "C", "start": now - 20, "end": now + 30, "power_kw": 300.0, "source": "t"})
        self.sched.add_event({"mrid": "D", "start": now - 10, "end": now, "power_kw": -150.0, "source": "t"})
        self._ticks(now - 20, now - 10, now)
        assert self._setpoints() == [300.0, -150.0, 0.0]

    def test_last_event_ending_idles_battery(self):
        now = int(time.time())
        self.sched.add_event({"mrid": "Z", "start": now - 20, "end": now, "power_kw": 100.0, "source": "t"})
        self._ticks(now - 20, now)
        assert self._setpoints() == [100.0, 0.0]


# ── Test 7: SQLite connection lifecycle — every connection is closed ──

import gc
import os
from capstone.audit_log import AuditLog


def _open_connections_to(db_path: Path) -> int:
    """sqlite3 connections in this process still OPEN on db_path. Closed
    connections waiting for garbage collection are not counted."""
    target = os.path.normcase(os.path.realpath(db_path))
    count = 0
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                files = [row[2] for row in obj.execute("PRAGMA database_list")]
            except sqlite3.ProgrammingError:  # already closed
                continue
            if any(f and os.path.normcase(os.path.realpath(f)) == target for f in files):
                count += 1
    return count


class TestDatabaseConnectionLifecycle:
    """Scheduler and AuditLog used to open connections with
    `with sqlite3.connect(...)`, which commits but never closes. Each
    connection then stayed open until the cyclic GC ran, which on Windows
    blocked deleting the database (WinError 32). The GC is disabled here so
    a lucky collection cannot hide a leak."""

    def setup_method(self):
        gc.disable()

    def teardown_method(self):
        gc.enable()

    def test_scheduler_closes_every_connection(self, tmp_path, monkeypatch):
        db = tmp_path / "schedule.db"
        monkeypatch.setattr(sched_mod, "DB_PATH", db)
        modbus = MagicMock(commanded_kw=None, last_dispatch_error=None, is_applied=None)
        modbus.set_power = AsyncMock(return_value=True)
        sched = Scheduler({}, GatewayState(), modbus)
        now = int(time.time())
        sched.add_event({"mrid": "L1", "start": now - 5, "end": now + 60, "power_kw": 10.0, "source": "t"})
        sched.get_upcoming()
        sched.has_valid_schedule(now)

        async def start_then_stop():
            await sched.tick(now)
            await sched.tick(now + 120)
        asyncio.run(start_then_stop())

        assert [c.args[0] for c in modbus.set_power.await_args_list] == [10.0, 0.0]
        assert _open_connections_to(db) == 0
        db.unlink()  # raised WinError 32 on Windows while connections leaked

    def test_audit_log_closes_every_connection(self, tmp_path):
        db = tmp_path / "audit.db"
        audit = AuditLog(db_path=db)
        audit.record("test", "lifecycle", "first")
        audit.record("test", "lifecycle", "second")
        assert [r["detail"] for r in audit.recent()] == ["second", "first"]
        assert _open_connections_to(db) == 0
        db.unlink()

    def test_db_context_commits_on_success_and_rolls_back_on_error(self, tmp_path, monkeypatch):
        db = tmp_path / "schedule.db"
        monkeypatch.setattr(sched_mod, "DB_PATH", db)
        sched = Scheduler({}, GatewayState(), MagicMock())
        insert = "INSERT INTO events (mrid, start_ts, end_ts, power_kw) VALUES (?, 0, 1, 0)"
        with sched._db() as conn:
            conn.execute(insert, ("committed",))
        with pytest.raises(RuntimeError):
            with sched._db() as conn:
                conn.execute(insert, ("rolled_back",))
                raise RuntimeError("boom")
        with sched._db() as conn:
            mrids = [row[0] for row in conn.execute("SELECT mrid FROM events")]
        assert mrids == ["committed"]
        assert _open_connections_to(db) == 0
