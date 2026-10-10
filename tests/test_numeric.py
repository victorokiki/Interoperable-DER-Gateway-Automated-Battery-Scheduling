"""
tests/test_numeric.py
Phase 4 regression tests — numeric safety from the utility to the register.

A DERControl's numbers pass through three representations: the IEEE
2030.5 text (value x 10^multiplier W, start, duration), the stored event
(kW), and the Endurium setpoint register (int32 kW, x1 scaling, spec
p.31). These tests drive the real IEEEClient parser, Scheduler and
ModbusDriver against FakeBMS and check what reaches the battery, what is
refused, and what the state and audit trail report. Audit finding H4b and
the Phase 4 numeric checks: scaling, rounding, int32 range, invalid input.
"""

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

import capstone.scheduler as sched_mod
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient
from capstone.modbus_driver import INT32_MAX, _decode_int32
from capstone.scheduler import Scheduler
from capstone.state import BatteryMode
from fakes import AC_SETPOINT, FakeBMS, audit_rows, derc, make_driver

T = 1_800_000_000
CLIENT_CFG = {"ieee_server_url": "http://127.0.0.1:9", "ieee_poll_interval_sec": 5,
              "offline_timeout_sec": 30}


async def rig(tmp_path, fake=None):
    audit = AuditLog(db_path=tmp_path / "audit.db")
    driver, state = await make_driver(fake or FakeBMS(), audit_log=audit)
    sched = Scheduler({}, state, driver, audit_log=audit)
    client = IEEEClient(CLIENT_CFG, state, sched, audit_log=audit)
    return SimpleNamespace(fake=driver._client, driver=driver, state=state, sched=sched,
                           client=client, audit=audit)


def receive(r, *events):
    """The gateway receives one DERControlList; returns the mRIDs it accepted."""
    return [e["mrid"] for e in r.client._parse_and_dispatch(derc(*events))]


def stored_mrids():
    with sqlite3.connect(sched_mod.DB_PATH) as conn:
        return [row[0] for row in conn.execute("SELECT mrid FROM events ORDER BY mrid")]


def hardware_register(fake):
    """The raw int32 in 41009, in the BMS's own sign (P > 0 = discharge)."""
    return _decode_int32([fake.hr.get(AC_SETPOINT, 0), fake.hr.get(AC_SETPOINT + 1, 0)])


# ── Scaling and rounding, utility text -> register ───────────────────────

@pytest.mark.parametrize("value, mult, bms_kw", [
    (400_000, 0, 400),
    (4, 5, 400),               # 4 x 10^5 W
    (-15, 3, -15),             # -15 x 10^3 W
    (1_499, 0, 1),             # 1.499 kW -> nearest whole kW
    (1_500, 0, 2),             # a half rounds away from zero
    (-1_500, 0, -2),
    (-1_499, 0, -1),
    (15, 2, 2),                # 1.5 kW through the multiplier
    (499, 0, 0),               # under half a kW: the register can only hold 0
    (25, -1, 0),               # 2.5 W
])
def test_utility_setpoints_reach_the_bms_scaled_exactly_in_whole_kw(tmp_path, value, mult, bms_kw):
    async def run():
        r = await rig(tmp_path)
        accepted = receive(r, dict(mrid="E", start=T, duration=600, value=value, mult=mult))
        for now in (T, T + 1, T + 2):
            await r.sched.step(now)
        return r, accepted
    r, accepted = asyncio.run(run())
    assert accepted == ["E"]
    assert r.fake.setpoint_history() == [bms_kw]              # written once, not every tick
    assert r.state.authority["confirmed"] is True
    assert r.state.power_kw == bms_kw


# ── Range: what the int32 kW register can hold ───────────────────────────

@pytest.mark.parametrize("kw", [INT32_MAX, -INT32_MAX])
def test_int32_extremes_from_the_utility_reach_the_register_unwrapped(tmp_path, kw):
    async def run():
        fake = FakeBMS()
        fake.set_limits(charge_kw=INT32_MAX, discharge_kw=INT32_MAX)
        r = await rig(tmp_path, fake)
        accepted = receive(r, dict(mrid="E", start=T, duration=600, value=kw * 1000))
        await r.sched.step(T)
        return r, accepted
    r, accepted = asyncio.run(run())
    assert accepted == ["E"]
    assert r.fake.setpoint_history() == [kw]
    assert hardware_register(r.fake) == -kw                    # sign inverted, never wrapped


@pytest.mark.parametrize("value, mult", [
    ((INT32_MAX + 1) * 1000, 0),            # just past +int32 kW
    (-(INT32_MAX + 1) * 1000, 0),           # just past -int32 kW
    (INT32_MAX * 1000 + 1, 0),              # 2,147,483,647.001 kW
    (2**32 * 1000, 0),                      # would wrap to 0 kW in 32 bits
    (1, 13),                                # 10^10 kW
    (-(10**30), 0),
])
def test_setpoints_beyond_the_int32_kw_range_are_rejected_at_the_boundary(tmp_path, value, mult):
    async def run():
        r = await rig(tmp_path)
        accepted = receive(r, dict(mrid="HUGE", start=T, duration=600, value=value, mult=mult),
                           dict(mrid="OK", start=T, duration=600, value=20_000))
        await r.sched.step(T + 1)
        return r, accepted
    r, accepted = asyncio.run(run())
    assert accepted == ["OK"]
    assert stored_mrids() == ["OK"]
    assert r.fake.setpoint_history() == [20]                   # nothing limited, wrapped or guessed
    [row] = audit_rows(r.audit, "schedule_rejected")
    assert "HUGE" in row["detail"] and "int32 kW setpoint range" in row["detail"]
    assert row["outcome"] == "invalid"


# ── Invalid numbers ──────────────────────────────────────────────────────

@pytest.mark.parametrize("field, text", [
    ("value", "nan"), ("value", "inf"), ("value", "-inf"), ("value", "1.5"),
    ("value", ""), ("value", "1e3"), ("value", "0x10"),
    ("mult", "nan"), ("mult", "1.5"), ("mult", "101"), ("mult", "-101"),
    ("duration", "-1"), ("duration", "nan"), ("duration", "1e3"),
    ("start", "inf"), ("start", "1.8e9"),
    ("start", str(253_402_300_800)),        # after 9999-12-31 23:59:59 UTC
    ("duration", str(10**12)),              # ends after year 9999
])
def test_invalid_numbers_are_rejected_audited_and_other_events_still_run(tmp_path, field, text):
    bad = dict(mrid="BAD", start=T, duration=600, value=50_000, mult=0)
    bad[field] = text

    async def run():
        r = await rig(tmp_path)
        accepted = receive(r, bad, dict(mrid="OK", start=T, duration=600, value=20_000))
        for now in (T + 1, T + 2):
            await r.sched.step(now)
        return r, accepted
    r, accepted = asyncio.run(run())
    assert accepted == ["OK"]                                  # BAD is not acknowledged
    assert stored_mrids() == ["OK"]
    assert r.fake.setpoint_history() == [20]
    assert audit_rows(r.audit, "scheduler") == []                # no failed tick
    [row] = audit_rows(r.audit, "schedule_rejected")
    assert "BAD" in row["detail"] and row["outcome"] == "invalid"


def test_a_huge_multiplier_is_rejected_without_stalling_the_gateway(tmp_path):
    """10**30000000 takes ~30 s to compute, on the event loop that also
    runs dispatch and the BMS heartbeat."""
    async def run():
        r = await rig(tmp_path)
        started = time.perf_counter()
        accepted = receive(r, dict(mrid="BIG", start=T, duration=600, value=1, mult=30_000_000),
                           dict(mrid="OK", start=T, duration=600, value=20_000))
        return r, accepted, time.perf_counter() - started
    r, accepted, elapsed = asyncio.run(run())
    assert elapsed < 1.0
    assert accepted == ["OK"]
    [row] = audit_rows(r.audit, "schedule_rejected")
    assert "BIG" in row["detail"] and "multiplier" in row["detail"]


def test_a_far_future_end_is_accepted_and_formatted_on_every_platform(tmp_path):
    """Windows' fromtimestamp() fails after the year 3000: an event ending
    in 3237 was stored, reported rejected, and then failed every tick."""
    end = 40_000_000_000

    async def run():
        r = await rig(tmp_path)
        accepted = receive(r, dict(mrid="LONG", start=T, duration=end - T, value=50_000))
        for now in (T, T + 1):
            await r.sched.step(now)
        return r, accepted
    r, accepted = asyncio.run(run())
    assert accepted == ["LONG"]
    assert r.fake.setpoint_history() == [50]
    assert audit_rows(r.audit, "scheduler") == []                # no failed tick
    assert audit_rows(r.audit, "schedule_rejected") == []
    [received] = audit_rows(r.audit, "schedule_received")
    assert "end=3237-07-19 23:06:40 UTC" in received["detail"]


# ── Rounding at the register ─────────────────────────────────────────────

@pytest.mark.parametrize("requested, written", [
    (0.4, 0), (0.5, 1), (-0.5, -1), (1.6, 2), (-1.6, -2), (2.5, 3), (-2.5, -3),
    (0.49999999999999994, 0),               # largest double below 0.5 (x + 0.5 would give 1)
    (1234.5, 1235), (-1234.5, -1235),
])
def test_setpoints_round_to_the_nearest_whole_kw_with_halves_away_from_zero(tmp_path, requested, written):
    async def run():
        r = await rig(tmp_path)
        return r, await r.driver.set_power(requested, "t")
    r, ok = asyncio.run(run())
    assert ok is True
    assert r.fake.setpoint_history() == [written]
    assert (r.state.power_kw, r.state.commanded_kw) == (written, written)
    assert r.state.mode == (BatteryMode.CHARGE if written > 0 else
                            BatteryMode.DISCHARGE if written < 0 else BatteryMode.IDLE)
    [row] = audit_rows(r.audit, "modbus_write")
    assert f"requested={requested:+} kW written={written:+.1f} kW" in row["detail"]


def test_rounding_never_takes_a_setpoint_beyond_the_bms_limits(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.set_limits(charge_kw=100, discharge_kw=150)
        r = await rig(tmp_path, fake)
        for kw in (100.4, 100.6, 99.5, -150.5, -149.5, -150.4):
            assert await r.driver.set_power(kw, "t")
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [100, 100, 100, -150, -150, -150]


# ── What the gateway reports ─────────────────────────────────────────────

def test_state_and_audit_report_what_the_bms_holds_not_what_was_asked(tmp_path):
    """H4b: state.power_kw showed the request (+300) while the BMS held +100."""
    async def run():
        fake = FakeBMS()
        fake.set_limits(charge_kw=100, discharge_kw=100)
        r = await rig(tmp_path, fake)
        receive(r, dict(mrid="E", start=T, duration=600, value=300_000))
        await r.sched.step(T)
        await r.sched.step(T + 1)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [100]
    assert (r.state.power_kw, r.state.commanded_kw) == (100, 100)
    assert (r.state.authority["desired_kw"], r.state.authority["confirmed"]) == (300.0, True)
    [dispatch] = audit_rows(r.audit, "dispatch")
    assert "setpoint +300.0 kW written" in dispatch["detail"]
    assert "BMS holds +100.0 kW" in dispatch["detail"]


def test_audit_records_the_exact_request_not_a_rounded_neighbour(tmp_path):
    async def run():
        r = await rig(tmp_path)
        receive(r, dict(mrid="E", start=T, duration=600, value=45, mult=1))   # 0.45 kW
        await r.sched.step(T)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [0]
    [received] = audit_rows(r.audit, "schedule_received")
    assert "power_kw=+0.45 " in received["detail"]
    [write] = audit_rows(r.audit, "modbus_write")
    assert "requested=+0.45 kW written=+0.0 kW" in write["detail"]
