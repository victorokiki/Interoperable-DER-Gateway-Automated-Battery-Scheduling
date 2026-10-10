"""
tests/test_bms_settings.py
Step 4 regression tests — how the gateway treats the Endurium BMS where
SPC100644 Rev 03 is silent (working answers E1-E10), and the audit writer
that keeps audit writes off the event loop running the BMS heartbeat (E5).

The real ModbusDriver (and, where dispatch matters, the real Scheduler and
IEEEClient) runs against FakeBMS, which can play either answer where the
ICD is silent: 41000 acting on every write or only on a change, which unit
IDs it answers, state codes outside the ICD's table.
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
import pytest

import capstone.__main__ as main_mod
import capstone.audit_log as audit_mod
import capstone.modbus_driver as driver_mod
from capstone.audit_log import AuditLog
from capstone.event_bus import EventBus
from capstone.ieee_client import IEEEClient
from capstone.modbus_driver import _decode_int32, _encode_int32
from capstone.scheduler import Scheduler
from fakes import OFF, ON, STANDBY, FakeBMS, Utility, audit_rows, derc, make_driver

T = 1_800_000_000


async def rig(tmp_path, fake, **settings):
    audit = AuditLog(db_path=tmp_path / "audit.db")
    bus = EventBus()
    alerts = bus.subscribe()
    driver, state = await make_driver(fake, audit_log=audit, event_bus=bus, **settings)
    sched = Scheduler({}, state, driver, audit_log=audit)
    return SimpleNamespace(fake=fake, driver=driver, state=state, sched=sched, audit=audit, alerts=alerts)


def run_event(r, kw=300.0):
    r.sched.add_event({"mrid": "E", "start": T, "end": T + 3600, "power_kw": kw, "source": "t"})


def details(audit, event_type):
    return [row["detail"] for row in audit_rows(audit, event_type)]


def alarms(r):
    """(outcome, detail) of every bms_alarm row, and the alarms published on
    the event bus's alerts topic."""
    published = []
    while not r.alerts.empty():
        payload = r.alerts.get_nowait()["payload"]
        if "alarm" in payload:
            published.append(payload["alarm"])
    return [(row["outcome"], row["detail"]) for row in audit_rows(r.audit, "bms_alarm")], published


def measured_kw(fake):
    """31009 as the fake BMS measures it, in the app's sign (+ = charge)."""
    return -_decode_int32([fake.ir.get(1009, 0), fake.ir.get(1010, 0)])


def gateway_writes(fake, address):
    return [words for _, a, words in fake.writes if a == address]


# ── E1: authority to command On ─────────────────────────────────────────

def test_off_found_at_start_up_is_left_when_the_setting_says_so(tmp_path):
    r = asyncio.run(rig(tmp_path, FakeBMS(operating_state=OFF), bms_start_from_off=False))
    assert r.fake.commands() == [] and r.fake.state == OFF
    assert any("BMS_START_FROM_OFF is off" in d for d in details(r.audit, "bms_condition"))


def test_off_commanded_while_the_gateway_runs_is_left_alone(tmp_path, monkeypatch):
    """Spec p.16: Off is entered only by an Off command, and a restarted BMS
    resumes its stored state. An Off that appears while the gateway runs was
    commanded by someone; the gateway used to undo it within 10 s."""
    monkeypatch.setattr(driver_mod, "COMMAND_SETTLE_SEC", 0)

    async def run():
        r = await rig(tmp_path, FakeBMS())
        run_event(r)
        await r.sched.step(T)
        r.fake.ir[1000], r.fake.hr[1000] = OFF, 0            # someone else: Standby, then Off
        before = len(r.fake.commands())
        await r.driver.supervise()
        await r.sched.step(T + 1)
        left = (r.fake.state, r.fake.commands()[before:], r.state.authority["confirmed"])
        r.fake.ir[1000], r.fake.hr[1000] = STANDBY, 1        # an operator brings it back to Standby
        await r.driver.supervise()
        await r.sched.step(T + 2)
        return r, left
    r, left = asyncio.run(run())
    assert left == (OFF, [], False)
    assert r.fake.state == ON and r.fake.setpoint_kw() == 300 and r.state.authority["confirmed"] is True
    assert any("commanded while the gateway was running" in d for d in details(r.audit, "bms_condition"))


def test_automatic_on_after_a_fault_is_limited_to_three_an_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(driver_mod, "COMMAND_SETTLE_SEC", 0)

    async def fault_cleared(r):
        r.fake.fault()
        await r.driver.supervise()
        r.fake.clear_fault()                                         # a non-latching fault clears to Standby
        await r.driver.supervise()
        return r.fake.state == ON

    async def run():
        r = await rig(tmp_path, FakeBMS(operating_state=STANDBY))   # On at start-up: not counted
        back_on = [await fault_cleared(r) for _ in range(4)]
        r.fake.user_controlled(True)
        await r.driver.supervise()
        r.fake.user_controlled(False)                                # an operator hands control back
        await r.driver.supervise()
        return r, back_on, await fault_cleared(r)                   # counting starts again
    r, back_on, after_release = asyncio.run(run())
    assert back_on == [True, True, True, False]
    assert r.fake.state == ON and after_release is True
    assert r.fake.commands().count(2) == 1 + 3 + 1 + 1               # start-up, three recoveries, the release, one more
    rows, published = alarms(r)
    assert [outcome for outcome, _ in rows] == ["alarm", "cleared"] and published == ["bms_auto_on"]
    assert "after 3 automatic On commands within 60 min" in rows[0][1]


def test_a_take_over_that_finds_the_bms_running_ends_there(tmp_path, monkeypatch):
    """Control handed back while the BMS stays On: the take-over ends with
    nothing to command, so a later Fault is counted like any other."""
    monkeypatch.setattr(driver_mod, "COMMAND_SETTLE_SEC", 0)

    async def run():
        r = await rig(tmp_path, FakeBMS())
        r.fake.user_controlled(True)
        await r.driver.supervise()
        r.fake.user_controlled(False)
        await r.driver.supervise()                                   # found On: the take-over ends
        back_on = []
        for _ in range(4):
            r.fake.fault()
            await r.driver.supervise()
            r.fake.clear_fault()
            await r.driver.supervise()
            back_on.append(r.fake.state == ON)
        return back_on
    assert asyncio.run(run()) == [True, True, True, False]


def test_bms_restart_into_standby_is_taken_over_without_counting(tmp_path, monkeypatch):
    monkeypatch.setattr(driver_mod, "COMMAND_SETTLE_SEC", 0)

    async def run():
        fake = FakeBMS()
        fake.heartbeat_live = False
        fake.ir[1003] = 500
        r = await rig(tmp_path, fake)
        run_event(r)
        await r.sched.step(T)
        r.driver._auto_on_times = [time.monotonic()] * 3              # the hourly limit already used up
        await r.driver.heartbeat_once()
        fake.reboot(into=STANDBY)                                    # dispatch modes lost with it
        await r.driver.heartbeat_once()                              # 500 -> 0: a restart (E3)
        await r.driver.supervise()
        await r.sched.step(T + 10)
        return r
    r = asyncio.run(run())
    assert r.fake.commands() == [2] and r.fake.state == ON
    assert [r.fake.hr.get(a, 0) for a in (1018, 1019, 1020, 1021)] == [0, 1, 1, 1]   # set again (E7)
    assert r.fake.setpoint_kw() == 300 and r.state.authority["confirmed"] is True


# ── E2: repeated On command ──────────────────────────────────────────────

def test_on_is_made_a_change_when_41000_already_holds_on(tmp_path, monkeypatch):
    """A BMS that acts on 41000 only when it changes would ignore a second
    On after a fault cleared to Standby."""
    monkeypatch.setattr(driver_mod, "COMMAND_SETTLE_SEC", 0.3)

    async def run():
        fake = FakeBMS(operating_state=STANDBY)
        fake.change_triggered = True
        fake.hr[1000] = 2                                            # On, from before the fault
        return await rig(tmp_path, fake)
    r = asyncio.run(run())
    assert r.fake.commands() == [1, 2] and r.fake.state == ON
    standby_at, on_at = [t for t, a, _ in r.fake.writes if a == 1000]
    assert on_at - standby_at >= 0.3                                 # time for a BMS that scans for changes


def test_on_is_written_once_when_41000_does_not_hold_on(tmp_path):
    async def run():
        fake = FakeBMS(operating_state=STANDBY)
        fake.change_triggered = True
        fake.hr[1000] = 1
        return await rig(tmp_path, fake)
    r = asyncio.run(run())
    assert r.fake.commands() == [2] and r.fake.state == ON


# ── E4: the BMS heartbeat stops incrementing ─────────────────────────────

def test_frozen_bms_heartbeat_stops_dispatch_until_it_has_run_again_for_10_s(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.heartbeat_live = False
        fake.ir[1003] = 40
        r = await rig(tmp_path, fake)
        run_event(r)
        await r.sched.step(T)
        seen = {"running": (fake.setpoint_kw(), r.state.authority["confirmed"])}
        await r.driver.heartbeat_once()
        r.driver._heartbeat_changed_at -= 6                          # 31003 unchanged for 6 s
        await r.driver.heartbeat_once()
        await r.sched.step(T + 1)
        seen["frozen"] = (fake.setpoint_kw(), r.state.authority["confirmed"], r.driver.last_dispatch_error[0])
        for _ in range(2):                                           # incrementing again...
            fake.beat()
            await r.driver.heartbeat_once()
        await r.sched.step(T + 2)
        seen["recovering"] = (fake.setpoint_kw(), r.state.authority["confirmed"])
        r.driver._resumed_at -= 10                                   # ...for 10 s
        fake.beat()
        await r.driver.heartbeat_once()
        await r.sched.step(T + 3)
        seen["resumed"] = (fake.setpoint_kw(), r.state.authority["confirmed"])
        return r, seen
    r, seen = asyncio.run(run())
    assert seen == {"running": (300, True), "frozen": (0, False, "bms_unhealthy"),
                    "recovering": (0, False), "resumed": (300, True)}
    assert r.fake.heartbeats() == [40, 40, 41, 42, 43]              # echoed throughout (§4.5.1)
    rows, published = alarms(r)
    assert [outcome for outcome, _ in rows] == ["alarm", "cleared"] and published == ["bms_heartbeat"]
    assert "stuck at 40" in rows[0][1]
    assert "src=bms_heartbeat_frozen requested=+0.0 kW written=+0.0 kW" in details(r.audit, "modbus_write")[1]


# ── E5: loss of the controller ───────────────────────────────────────────

@pytest.mark.parametrize("low_word, reading", [(0x0040, "set"), (0x0020, "clear")])
def test_no_active_controller_alert_is_audited_at_connect(tmp_path, low_word, reading):
    async def run():
        fake = FakeBMS()
        fake.ir[1039], fake.ir[1040] = 0, low_word                   # 31039 bit 6, or another bit only
        return await rig(tmp_path, fake)
    r = asyncio.run(run())
    [row] = [d for d in details(r.audit, "bms_condition") if "No Active Controller" in d]
    assert f"31039 bit 6 (No Active Controller) {reading}" in row


def test_audit_record_never_waits_for_a_locked_database(tmp_path):
    """record() ran SQLite on the event loop that also runs the 1 s BMS
    heartbeat: a locked audit database (a second instance, a DB browser)
    held the loop for up to the 10 s busy timeout."""
    audit = AuditLog(db_path=tmp_path / "audit.db")
    blocker = sqlite3.connect(tmp_path / "audit.db", timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        audit.record("test", "t", "while locked")
        took = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
    assert took < 0.1
    assert [row["detail"] for row in audit.recent(5)] == ["while locked"]


def test_audit_rows_keep_their_order_and_the_time_they_were_recorded(tmp_path):
    audit = AuditLog(db_path=tmp_path / "audit.db")
    blocker = sqlite3.connect(tmp_path / "audit.db", timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        recorded_at = datetime.now(timezone.utc)
        for n in range(50):
            audit.record("test", "t", f"row {n}")
        time.sleep(0.5)                                              # written only after this
    finally:
        blocker.rollback()
        blocker.close()
    rows = list(reversed(audit.recent(100)))
    assert [row["detail"] for row in rows] == [f"row {n}" for n in range(50)]
    assert abs((datetime.fromisoformat(rows[0]["ts_utc"]) - recorded_at).total_seconds()) < 0.2


def test_a_relative_audit_path_is_fixed_when_the_row_is_recorded(tmp_path, monkeypatch):
    """The gateway's audit log is schedule_store/audit_log.db, relative. A row
    the writer thread writes later must land where it was recorded, even if
    the working directory changed in between."""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    audit = AuditLog()
    blocker = sqlite3.connect(first / "schedule_store" / "audit_log.db", timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        audit.record("test", "t", "recorded in first")
        monkeypatch.chdir(second)
    finally:
        blocker.rollback()
        blocker.close()
    assert audit_mod.flush()
    assert not (second / "schedule_store").exists()
    assert [row["detail"] for row in AuditLog(db_path=first / "schedule_store" / "audit_log.db").recent()] \
        == ["recorded in first"]


# ── E6: values of 31000 while the BMS is On ──────────────────────────────

def test_an_undocumented_state_confirms_while_the_power_follows(tmp_path):
    """Spec §6.2.2 names Idle, Charging and Discharging On sub-states the
    table (0-10) has no codes for."""
    async def run():
        fake = FakeBMS(operating_state=11)
        fake.running_states = {ON, 11}
        r = await rig(tmp_path, fake)
        ok = await r.driver.set_power(300.0, "t")
        await r.driver.supervise()                                   # the 10 s status poll keeps it
        return r, ok
    r, ok = asyncio.run(run())
    assert ok is True and r.driver.commanded_kw == 300 and r.fake.setpoint_history() == [300]
    assert any("operating state 11 is not in the ICD's table" in d for d in details(r.audit, "bms_condition"))


def test_an_undocumented_state_confirms_a_setpoint_limited_to_0_while_nothing_flows(tmp_path):
    async def run():
        fake = FakeBMS(operating_state=11)
        fake.running_states = {ON, 11}
        fake.set_limits(charge_kw=0, discharge_kw=0)                 # no power permitted right now
        r = await rig(tmp_path, fake)
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert ok is True and r.driver.commanded_kw == 0


@pytest.mark.parametrize("measured, setpoint, follows", [
    (300, 300, True), (5, 300, True), (0, 300, False), (-5, 300, False), (None, 300, False),
    (-300, -300, True), (5, -300, False), (0, 0, True), (5, 0, False), (None, 0, False)])
def test_what_following_the_setpoint_means(measured, setpoint, follows):
    assert driver_mod._follows(measured, setpoint) is follows


def test_an_undocumented_state_without_power_flow_does_not_confirm(tmp_path):
    async def run():
        r = await rig(tmp_path, FakeBMS(operating_state=11))
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert ok is False and r.driver.last_dispatch_error[0] == "bms_state"
    assert "measured power (0 kW) does not follow" in r.driver.last_dispatch_error[1]


@pytest.mark.parametrize("state", [0, 3, 5, 6, 7, 8, 9])
def test_documented_states_other_than_on_never_confirm(tmp_path, state):
    async def run():
        fake = FakeBMS(operating_state=state)
        fake.running_states = {ON, state}                            # even with power flowing
        r = await rig(tmp_path, fake)
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert ok is False and r.driver.last_dispatch_error[0] == "bms_state"
    assert r.driver.last_dispatch_error[1].endswith(", not On")


# ── E7: dispatch modes (AC-configured) ───────────────────────────────────

def test_invalid_dispatch_modes_are_set_and_read_back_before_on(tmp_path):
    """Audit finding S1: 41019-41021 power up as 0 (Invalid) and were never
    set, so a real BMS would not have followed 41009."""
    async def run():
        r = await rig(tmp_path, FakeBMS(operating_state=STANDBY))
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert [a for _, a, _ in r.fake.writes][:5] == [1019, 1020, 1011, 1021, 1000]   # Q = 0 before Fixed Q; then On
    assert [r.fake.hr.get(a, 0) for a in (1018, 1019, 1020, 1021, 1011, 1012)] == [0, 1, 1, 1, 0, 0]
    assert ok is True and measured_kw(r.fake) == 300
    assert any(d.startswith("dispatch modes in place: 41018 System-Level, 41019 Grid-Feeding, 41020 Fixed "
                            "Active Power, 41021 Fixed Reactive Power (Q)") for d in details(r.audit, "bms_condition"))


@pytest.mark.parametrize("register, value, problem", [
    (1020, 2, "41020 Active Power Control Mode is P-f Droop"),
    (1018, 1, "41018 Grid Control Scope is Per-Array"),
])
def test_dispatch_modes_set_by_someone_else_are_left_and_block_confirmation(tmp_path, register, value, problem):
    async def run():
        fake = FakeBMS()
        fake.hr[register] = value
        r = await rig(tmp_path, fake)
        blocked = (await r.driver.set_power(300.0, "t"), r.driver.last_dispatch_error, fake.setpoint_history())
        idled = await r.driver.safe_idle("t")                        # 0 kW written; the modes can still move power
        fake.hr[register] = 1 if register == 1020 else 0             # an operator selects fixed dispatch
        await r.driver.supervise()
        return r, blocked, idled, await r.driver.set_power(300.0, "t")
    r, blocked, idled, ok = asyncio.run(run())
    assert blocked[0] is False and blocked[1][0] == "grid_modes" and problem in blocked[1][1]
    assert blocked[2] == []                                          # no setpoint while blocked
    assert idled is False
    assert gateway_writes(r.fake, register) == []                    # left as it was
    assert ok is True and r.fake.setpoint_history() == [0, 300]
    rows, published = alarms(r)
    assert [outcome for outcome, _ in rows] == ["alarm", "cleared"] and published == ["grid_modes"]


def test_power_factor_mode_set_by_someone_else_is_left_and_does_not_block(tmp_path):
    """Changed from the working answer: fixed-PF reactive control leaves
    the active power on 41009, so it does not stop active-power dispatch."""
    async def run():
        fake = FakeBMS()
        fake.hr[1021] = 2
        r = await rig(tmp_path, fake)
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert ok is True
    assert gateway_writes(r.fake, 1021) == [] and gateway_writes(r.fake, 1011) == []


def test_dispatch_modes_changed_while_running_withdraw_the_confirmation(tmp_path):
    async def run():
        r = await rig(tmp_path, FakeBMS())
        run_event(r)
        await r.sched.step(T)
        confirmed = r.state.authority["confirmed"]
        r.fake.hr[1018] = 1                                          # someone selects Per-Array scope
        await r.driver.supervise()
        await r.sched.step(T + 1)
        return r, confirmed
    r, confirmed = asyncio.run(run())
    assert confirmed is True and r.state.authority["confirmed"] is False
    assert r.driver.last_dispatch_error[0] == "grid_modes"


def test_unreadable_dispatch_modes_raise_the_alarm_only_when_the_bms_refuses_the_read(tmp_path):
    """Seen in the end-to-end run: the BMS process was killed during a
    status poll, the mode read found no one there, and a dispatch-modes
    alarm went out for what was a link loss (reported on its own)."""
    async def run():
        r = await rig(tmp_path, FakeBMS())
        r.fake.holding_read_errors[1018] = None                      # no answer: the link is failing
        await r.driver.supervise()
        quiet = alarms(r)
        r.fake.holding_read_errors[1018] = 2                         # the BMS answers: Illegal Data Address
        await r.driver.supervise()
        refused = alarms(r)
        del r.fake.holding_read_errors[1018]
        await r.driver.supervise()
        return r, quiet, refused, await r.driver.set_power(300.0, "t")
    r, quiet, refused, ok = asyncio.run(run())
    assert quiet == ([], [])
    assert [outcome for outcome, _ in refused[0]] == ["alarm"] and refused[1] == ["grid_modes"]
    assert "41018-41021 unreadable (exception 2)" in refused[0][0][1]
    assert ok is True and [outcome for outcome, _ in alarms(r)[0]] == ["alarm", "cleared"]


def test_a_refused_mode_write_blocks_dispatch(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.reject = lambda address, words: 2 if address == 1020 else None
        r = await rig(tmp_path, fake)
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert ok is False and r.driver.last_dispatch_error == (
        "grid_modes", "41020 Active Power Control Mode := 1 (Fixed Active Power) failed: exception 2")
    assert r.fake.setpoint_history() == []


# ── E8: DC-configured systems ────────────────────────────────────────────

def test_power_events_on_a_dc_configured_bms_are_answered_not_applicable(tmp_path):
    async def run():
        util = await Utility(derc(dict(mrid="E", start=T, duration=600, value=50_000,
                                       response_required="03"))).start()
        audit = AuditLog(db_path=tmp_path / "audit.db")
        driver, state = await make_driver(FakeBMS(bms_type=0), audit_log=audit)
        sched = Scheduler({}, state, driver, audit_log=audit)
        client = IEEEClient({"ieee_server_url": util.url, "ieee_poll_interval_sec": 5,
                             "offline_timeout_sec": 30}, state, sched, audit_log=audit)
        async with aiohttp.ClientSession() as http:
            await client._poll(http)
            await sched.step(T)
            await client.send_responses(http)
        await util.stop()
        return util, driver, audit
    util, driver, audit = asyncio.run(run())
    assert util.statuses() == [1, 252]
    assert driver._client.setpoint_history(1007) == [] and driver._client.setpoint_history(1009) == []
    [rejected] = [d for d in details(audit, "schedule_status") if "-> rejected" in d]
    assert "mrid=E -> rejected (setpoint +50.0 kW not applicable: DC-configured BMS" in rejected


# ── E9: Modbus TCP unit ID ───────────────────────────────────────────────

def test_requests_use_unit_id_255_by_default(tmp_path):
    async def run():
        r = await rig(tmp_path, FakeBMS())
        await r.driver.set_power(300.0, "t")
        await r.driver.heartbeat_once()
        return r
    r = asyncio.run(run())
    assert set(r.fake.unit_log) == {255}
    assert "Modbus unit ID 255 answers" in details(r.audit, "bms_condition")


@pytest.mark.parametrize("answers, others, used", [({1}, "exception", 1), ({0}, "exception", 0),
                                                   ({1}, "silent", 1)])
def test_unit_id_probe_tries_1_then_0(tmp_path, monkeypatch, answers, others, used):
    monkeypatch.setattr(driver_mod, "PROBE_TIMEOUT_SEC", 0.05)

    async def run():
        fake = FakeBMS()
        fake.answers_units, fake.other_units = answers, others
        r = await rig(tmp_path, fake)
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    probe = r.fake.unit_log[:r.fake.unit_log.index(used) + 1]
    assert probe == [255, 1, 0][:len(probe)] and probe[-1] == used
    assert set(r.fake.unit_log[len(probe):]) == {used}
    assert ok is True
    [answered] = [d for d in details(r.audit, "bms_condition") if d.startswith("Modbus unit ID")]
    assert answered.startswith(f"Modbus unit ID {used} answers (unit 255: ")


def test_the_configured_unit_id_is_used(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.answers_units = {7}
        r = await rig(tmp_path, fake, modbus_unit_id=7)
        await r.driver.set_power(300.0, "t")
        return r
    r = asyncio.run(run())
    assert set(r.fake.unit_log) == {7}


def test_no_unit_id_answering_blocks_dispatch_and_probes_only_by_reading(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.answers_units = set()
        r = await rig(tmp_path, fake)
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert ok is False and r.driver.last_dispatch_error[0] == "not_ready"
    assert r.fake.writes == []
    assert ("BMS type unknown: unit 255: exception 11; unit 1: exception 11; unit 0: exception 11"
            in details(r.audit, "bms_condition"))


# ── E10: power limits and operator caps ──────────────────────────────────

def test_power_limits_are_used_by_magnitude_whatever_their_sign(tmp_path):
    """A charge limit published as a positive number used to read as 0,
    blocking all charging."""
    async def run():
        fake = FakeBMS()
        fake.ir[1019], fake.ir[1020] = _encode_int32(100)
        fake.ir[1021], fake.ir[1022] = _encode_int32(-150)
        r = await rig(tmp_path, fake)
        return r, [await r.driver.set_power(300.0, "t"), await r.driver.set_power(-400.0, "t")]
    r, results = asyncio.run(run())
    assert results == [True, True] and r.fake.setpoint_history() == [100, -150]
    assert any("not the signs the ICD's table gives" in d for d in details(r.audit, "bms_condition"))


def test_operator_caps_hold_setpoints_and_are_never_written(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.set_limits(charge_kw=200, discharge_kw=250)
        fake.hr[1014], fake.hr[1015] = _encode_int32(80)             # Operating Charge Power Limit Permitted
        fake.hr[1016], fake.hr[1017] = _encode_int32(0)              # no discharge cap
        r = await rig(tmp_path, fake)
        return r, [await r.driver.set_power(300.0, "t"), await r.driver.set_power(-300.0, "t")]
    r, results = asyncio.run(run())
    assert results == [True, True] and r.fake.setpoint_history() == [80, -250]
    assert not [a for _, a, _ in r.fake.writes if 1014 <= a <= 1017]
    assert "limits charge<=80 discharge<=250 kW" in details(r.audit, "modbus_write")[0]
    assert "operator caps 41014 / 41016: charge <= 80 kW, discharge <= - kW (0 = no cap)" \
        in details(r.audit, "bms_condition")


# ── Settings ─────────────────────────────────────────────────────────────

def test_unit_id_and_start_from_off_settings(monkeypatch):
    for name in ("IEEE_SERVER_URL", "TLS_CA_CERT", "TLS_CLIENT_CERT", "TLS_CLIENT_KEY", "MODBUS_UNIT_ID",
                 "BMS_START_FROM_OFF", "DER_SET_MAX_W", "DER_SET_MAX_CHARGE_RATE_W",
                 "DER_SET_MAX_DISCHARGE_RATE_W", "DEVICE_LFDI"):
        monkeypatch.delenv(name, raising=False)
    config = main_mod.load_config()
    assert (config["modbus_unit_id"], config["bms_start_from_off"]) == (255, True)
    monkeypatch.setenv("MODBUS_UNIT_ID", "1")
    monkeypatch.setenv("BMS_START_FROM_OFF", "0")
    config = main_mod.load_config()
    assert (config["modbus_unit_id"], config["bms_start_from_off"]) == (1, False)
    for bad in ("256", "-1", "one"):
        monkeypatch.setenv("MODBUS_UNIT_ID", bad)
        with pytest.raises(main_mod.ConfigError, match="MODBUS_UNIT_ID"):
            main_mod.load_config()
    monkeypatch.setenv("MODBUS_UNIT_ID", "255")
    monkeypatch.setenv("BMS_START_FROM_OFF", "maybe")
    with pytest.raises(main_mod.ConfigError, match="BMS_START_FROM_OFF"):
        main_mod.load_config()
