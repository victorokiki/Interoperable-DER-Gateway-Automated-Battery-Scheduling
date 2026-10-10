"""
tests/test_bms.py
Phase 3 regression tests — Endurium BMS integration (SPC100644 Rev 03).

The real ModbusDriver (and, where dispatch matters, the real Scheduler)
runs against FakeBMS, which follows the spec where it is explicit: the
Off/Standby/On state machine (p.16), User-Controlled mode (§6.1), the
power limits 31019/31021 (§6.2.6) and a Fault setting them to 0 (§6.2.8).
Audit findings S3, S4a, S4b, S5, S6, H3a, H3b, H3c, H4a and N6.
"""

import asyncio
from types import SimpleNamespace

import pytest

from capstone.audit_log import AuditLog
from capstone.modbus_driver import _encode_int32
from capstone.scheduler import Scheduler
from fakes import OFF, ON, STANDBY, FakeBMS, audit_rows, make_driver

T = 1_800_000_000


async def rig(tmp_path, fake):
    audit = AuditLog(db_path=tmp_path / "audit.db")
    driver, state = await make_driver(fake, audit_log=audit)
    sched = Scheduler({}, state, driver, audit_log=audit)
    return SimpleNamespace(fake=fake, driver=driver, state=state, sched=sched, audit=audit)


def run_event(r, kw=300.0):
    r.sched.add_event({"mrid": "E", "start": T, "end": T + 3600, "power_kw": kw, "source": "t"})


def conditions(audit):
    return [row["detail"] for row in audit_rows(audit, "bms_condition")]


# ── Operating limits (§6.2.6) ────────────────────────────────────────────

def test_setpoints_are_held_inside_the_published_power_limits(tmp_path):
    """S3: the gateway wrote -300 kW against a 100 kW limit."""
    async def run():
        fake = FakeBMS()
        fake.set_limits(charge_kw=100, discharge_kw=150)
        r = await rig(tmp_path, fake)
        results = [await r.driver.set_power(300.0, "t"), await r.driver.set_power(-400.0, "t")]
        return r, results
    r, results = asyncio.run(run())
    assert results == [True, True]
    assert r.fake.setpoint_history() == [100.0, -150.0]      # never beyond the limits
    written = [row["detail"] for row in audit_rows(r.audit, "modbus_write")]
    assert "requested=+300.0 kW written=+100.0 kW register=41009" in written[0]
    assert "limits charge<=100 discharge<=150 kW" in written[0]


def test_limit_changes_are_followed_while_an_event_runs(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.set_limits(charge_kw=100, discharge_kw=100)
        r = await rig(tmp_path, fake)
        run_event(r)
        await r.sched.step(T)
        await r.sched.step(T + 1)                              # confirmed: nothing re-sent
        confirmed = r.state.authority["confirmed"]
        fake.set_limits(charge_kw=250, discharge_kw=250)
        await r.driver.supervise()                             # the 10 s status poll reads them
        await r.sched.step(T + 2)
        fake.set_limits(charge_kw=400, discharge_kw=400)
        await r.driver.supervise()
        await r.sched.step(T + 3)
        return r, confirmed
    r, confirmed = asyncio.run(run())
    assert confirmed is True
    assert r.fake.setpoint_history() == [100.0, 250.0, 300.0]


def test_unreadable_limits_block_non_zero_setpoints(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.fail_reads.add(1019)
        r = await rig(tmp_path, fake)
        charged = (await r.driver.set_power(300.0, "t"), r.driver.last_dispatch_error)
        return r, charged, await r.driver.safe_idle("t")
    r, charged, idled = asyncio.run(run())
    assert charged[0] is False and charged[1][0] == "limits_unavailable"
    assert idled is True                                       # 0 kW is always within the limits
    assert r.fake.setpoint_history() == [0.0]


# ── State machine (p.16) ─────────────────────────────────────────────────

def test_startup_from_off_goes_through_standby_before_on(tmp_path):
    """S4a: the gateway wrote On straight from Off."""
    async def run():
        fake = FakeBMS(operating_state=OFF)
        return await rig(tmp_path, fake)
    r = asyncio.run(run())
    assert r.fake.commands() == [1, 2]                        # Standby, then On
    assert r.fake.state == ON


def test_fault_during_dispatch_then_recovery_turns_the_bms_back_on(tmp_path):
    """S4b: after Fault -> Standby the gateway never re-sent On."""
    async def run():
        r = await rig(tmp_path, FakeBMS())
        run_event(r)
        await r.sched.step(T)
        r.fake.fault()                                         # limits drop to 0 (§6.2.8)
        await r.driver.supervise()
        await r.sched.step(T + 1)
        during = (r.fake.setpoint_kw(), dict(r.state.authority))
        r.fake.clear_fault()                                   # the fault clears to Standby
        await r.driver.supervise()
        await r.sched.step(T + 2)
        return r, during
    r, during = asyncio.run(run())
    assert during[0] == 0.0 and during[1]["confirmed"] is False
    assert r.fake.commands() == [2]                           # On, once the BMS was in Standby
    assert r.fake.setpoint_kw() == 300.0 and r.state.authority["confirmed"] is True
    assert any("operating state Fault" in c for c in conditions(r.audit))


def test_setpoint_is_not_reported_as_dispatched_while_the_bms_is_not_on(tmp_path):
    """H3a: set_power reported success while the BMS was Off / Standby."""
    async def run():
        fake = FakeBMS(operating_state=STANDBY)
        fake.accept_on = False                                 # the BMS never comes On
        r = await rig(tmp_path, fake)
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert ok is False
    assert r.driver.last_dispatch_error == ("bms_state", "BMS operating state is Standby, not On")
    assert r.driver.commanded_kw is None and r.state.mode.value == "IDLE"


def test_user_controlled_mode_is_detected_and_never_reported_as_success(tmp_path):
    """S5: writes are accepted but not stored in User-Controlled mode (§6.1)."""
    async def run():
        fake = FakeBMS(operating_state=STANDBY)
        fake.user_controlled()
        r = await rig(tmp_path, fake)
        return r, await r.driver.set_power(300.0, "t")
    r, ok = asyncio.run(run())
    assert ok is False and r.driver.last_dispatch_error[0] == "user_controlled"
    assert r.fake.commands() == []                            # no On while an operator has control
    assert r.fake.setpoint_history() == [] and r.fake.ignored
    assert any("User-Controlled" in c for c in conditions(r.audit))


def test_bms_type_other_than_dc_or_ac_blocks_setpoint_writes(tmp_path):
    """H3b: an unknown type was logged as DC but written to the AC register."""
    async def run():
        r = await rig(tmp_path, FakeBMS(bms_type=2))
        return r, await r.driver.set_power(300.0, "t"), await r.driver.safe_idle("t")
    r, charged, idled = asyncio.run(run())
    assert (charged, idled) == (False, False)
    assert r.fake.setpoint_history(1009) == [] and r.fake.setpoint_history(1007) == []
    assert any("BMS type unknown: unrecognised value 2" in c for c in conditions(r.audit))


def test_dc_configured_bms_is_not_dispatched(tmp_path):
    """Requirement change, step 4 (working answer E8; audit finding S2): on
    a DC-configured BMS the plant controller drives the PCS (spec §2) and
    41007 is only a target, so writing it and reporting the dispatch as done
    claimed power the gateway cannot command. It used to write 41007 and
    return True."""
    async def run():
        r = await rig(tmp_path, FakeBMS(bms_type=0))
        return r, await r.driver.set_power(300.0, "t"), await r.driver.safe_idle("t")
    r, ok, idled = asyncio.run(run())
    assert (ok, idled) == (False, False)
    assert r.driver.last_dispatch_error[0] == "not_dispatchable"
    assert r.fake.setpoint_history(1007) == [] and r.fake.setpoint_history(1009) == []
    assert r.state.dispatch_kind == "dc_not_dispatchable" and r.state.mode.value == "IDLE"


# ── Heartbeat (§4.5.1) ───────────────────────────────────────────────────

def test_heartbeat_echoes_the_bms_heartbeat(tmp_path):
    """S6: the gateway wrote its own counter starting at 1."""
    async def run():
        fake = FakeBMS()
        fake.ir[1003] = 5000
        r = await rig(tmp_path, fake)
        for _ in range(3):
            await r.driver.heartbeat_once()
            fake.beat()
        return r
    r = asyncio.run(run())
    assert r.fake.heartbeats() == [5000, 5001, 5002]


def test_frozen_bms_heartbeat_is_reported(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.ir[1003] = 7
        r = await rig(tmp_path, fake)
        await r.driver.heartbeat_once()
        r.driver._heartbeat_changed_at -= 10                  # 10 s without a change
        await r.driver.heartbeat_once()
        stale = r.state.bms_heartbeat_stale
        fake.beat()
        await r.driver.heartbeat_once()
        return r, stale
    r, stale = asyncio.run(run())
    assert stale is True and r.state.bms_heartbeat_stale is False
    assert any("stuck at 7" in c for c in conditions(r.audit))


def test_bms_restart_behind_a_transparent_reconnect_is_recovered(tmp_path):
    """N6: after a quick BMS restart the gateway kept reporting CHARGE while
    the battery sat Off at 0 kW."""
    async def run():
        fake = FakeBMS()
        fake.ir[1003] = 500
        r = await rig(tmp_path, fake)
        run_event(r)
        await r.sched.step(T)
        await r.driver.heartbeat_once()
        fake.reboot(into=OFF)                                  # setpoint lost, heartbeat restarts
        await r.driver.heartbeat_once()                        # 500 -> 0: a restart
        await r.driver.supervise()                             # Off -> Standby -> On
        await r.sched.step(T + 10)
        return r
    r = asyncio.run(run())
    assert r.fake.commands() == [1, 2]
    assert r.fake.setpoint_kw() == 300.0 and r.state.authority["confirmed"] is True
    assert any("heartbeat restarted" in c for c in conditions(r.audit))


def test_setpoint_changed_behind_the_gateways_back_is_resent(tmp_path):
    async def run():
        r = await rig(tmp_path, FakeBMS())
        run_event(r)
        await r.sched.step(T)
        r.fake.hr[1009] = r.fake.hr[1010] = 0                  # the BMS no longer holds it
        await r.driver.supervise()
        await r.sched.step(T + 1)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [300.0, 300.0]
    assert any("setpoint register 41009 changed" in c for c in conditions(r.audit))


# ── int32 bounds (§4.4) ──────────────────────────────────────────────────

@pytest.mark.parametrize("value", [2**31, -2**31 - 1, 3_000_000_000, 1.5, True])
def test_values_int32_cannot_hold_are_refused_not_wrapped(value):
    """H4a: 3,000,000,000 kW used to wrap into a +1,294,967,296 kW command."""
    with pytest.raises(ValueError):
        _encode_int32(value)


def test_int32_boundaries_encode_exactly():
    assert _encode_int32(2**31 - 1) == [0x7FFF, 0xFFFF]
    assert _encode_int32(-2**31) == [0x8000, 0x0000]


def test_request_the_register_cannot_hold_is_refused_not_wrapped(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.set_limits(charge_kw=2**31 - 1, discharge_kw=2**31 - 1)
        r = await rig(tmp_path, fake)
        refused = []
        for value in (3e9, -3e9, 2.0**31, float("nan"), float("inf"), float("-inf")):
            refused.append((await r.driver.set_power(value, "t"), r.driver.last_dispatch_error[0]))
        boundary = [await r.driver.set_power(2**31 - 1, "t"), await r.driver.set_power(-(2**31 - 1), "t")]
        return r, refused, boundary
    r, refused, boundary = asyncio.run(run())
    assert refused == [(False, "out_of_range")] * 6
    assert boundary == [True, True]
    assert r.fake.setpoint_history() == [2**31 - 1, -(2**31 - 1)]   # only the representable ones


# ── Audit of the actual writes ───────────────────────────────────────────

def test_modbus_writes_are_audited_with_register_and_outcome(tmp_path):
    async def run():
        fake = FakeBMS()
        r = await rig(tmp_path, fake)
        await r.driver.set_power(300.0, "schedule:A")
        fake.reject = lambda address, words: 3 if address == 1009 else None
        await r.driver.set_power(-50.0, "schedule:B")
        return r
    r = asyncio.run(run())
    rows = audit_rows(r.audit, "modbus_write")
    assert [row["outcome"] for row in rows] == ["confirmed", "not confirmed: exception 3"]
    assert "src=schedule:A requested=+300.0 kW written=+300.0 kW register=41009" in rows[0]["detail"]
