"""
tests/test_authority.py
Phase 1 regression tests — deterministic dispatch authority.

Each test drives the real Scheduler and the real ModbusDriver against a
FakeBMS register map (tests/fakes.py) and checks what the battery was
actually told, who held authority, and what the audit log recorded.
Findings C1a-d, C2a/b/c, H2 and M2 of the audit are covered here.
"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

import capstone.scheduler as sched_mod
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient
from capstone.modbus_driver import _decode_int32
from capstone.scheduler import (
    ACTIVE, AUTH_FORCE_IDLE, AUTH_SAFE_IDLE, AUTH_SCHEDULE, CANCELLED, COMPLETED,
    EXPIRED, REJECTED, SUPERSEDED, Scheduler, set_force_idle,
)
from fakes import FakeBMS, audit_rows, derc, make_driver, until

T = 1_800_000_000          # a fixed "now" so every tick is deterministic
CLIENT_CFG = {"ieee_server_url": "http://127.0.0.1:9", "ieee_poll_interval_sec": 5,
              "offline_timeout_sec": 30}


@pytest.fixture(autouse=True)
def schedule_db(tmp_path, monkeypatch):
    path = tmp_path / "schedule.db"
    monkeypatch.setattr(sched_mod, "DB_PATH", path)
    return path


async def rig(tmp_path, fake=None):
    """Gateway pieces wired as __main__ does, around one FakeBMS. Calling it
    again with the same fake is a gateway restart against the same BMS
    (same schedule database and audit log)."""
    audit = AuditLog(db_path=tmp_path / "audit.db")
    fake = fake if fake is not None else FakeBMS()
    driver, state = await make_driver(fake, audit_log=audit)
    sched = Scheduler({"offline_timeout_sec": 30}, state, driver, audit_log=audit)
    return SimpleNamespace(fake=fake, driver=driver, state=state, sched=sched, audit=audit)


def ev(mrid, start, end, kw):
    return {"mrid": mrid, "start": start, "end": end, "power_kw": kw, "source": "ieee2030.5"}


def status(mrid):
    conn = sqlite3.connect(sched_mod.DB_PATH)
    try:
        return conn.execute("SELECT status FROM events WHERE mrid=?", (mrid,)).fetchone()[0]
    finally:
        conn.close()


def status_details(audit):
    return [row["detail"] for row in audit_rows(audit, "schedule_status")]


# ── 1. Replacement and supersession ──────────────────────────────────────

def test_new_event_replaces_older_event(tmp_path):
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("A", T, T + 1000, 300.0))
        await r.sched.tick(T)
        r.sched.add_event(ev("B", T + 500, T + 1500, -200.0))
        await r.sched.tick(T + 499)
        assert r.fake.setpoint_kw() == 300.0            # B has not started: A keeps control
        decision = await r.sched.tick(T + 500)
        assert (decision.authority, decision.mrid) == (AUTH_SCHEDULE, "B")
        for now in (T + 999, T + 1000, T + 1200):
            await r.sched.tick(now)
            assert r.fake.setpoint_kw() == -200.0       # A never regains control
        await r.sched.tick(T + 1500)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [300.0, -200.0, 0.0]
    assert (status("A"), status("B")) == (SUPERSEDED, COMPLETED)
    assert "mrid=A -> superseded (superseded by B)" in status_details(r.audit)
    assert any(row["source"] == "schedule:B" and "power_kw=-200.0" in row["detail"]
               for row in audit_rows(r.audit, "authority"))


def test_newer_revision_with_the_same_start_replaces_the_older_one_before_it_runs(tmp_path):
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("C", T, T + 100, 100.0))
        r.sched.add_event(ev("D", T, T + 100, 50.0))     # same window, received later
        await r.sched.tick(T)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [50.0]           # C's setpoint was never sent
    assert status("C") == SUPERSEDED


def test_superseded_event_does_not_resume_after_the_superseding_event_ends(tmp_path):
    """Sprint 2 F-03 ("superseded events do not fire") and the Phase 1
    requirement that stale events never regain authority. The previous
    scheduler resumed A here."""
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("A", T, T + 1000, 300.0))
        r.sched.add_event(ev("B", T + 200, T + 300, -100.0))
        for now in (T, T + 200, T + 300, T + 500):
            await r.sched.tick(now)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [300.0, -100.0, 0.0]
    assert r.fake.setpoint_kw() == 0.0
    assert status("A") == SUPERSEDED
    assert r.state.authority["authority"] == AUTH_SAFE_IDLE


def test_superseded_event_is_never_written_when_both_arrive_late(tmp_path):
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("A", T - 60, T + 600, 300.0))
        r.sched.add_event(ev("B", T - 30, T + 600, -100.0))
        await r.sched.tick(T)
        await r.sched.tick(T + 1)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [-100.0]
    assert status("A") == SUPERSEDED


def test_event_with_an_earlier_start_arriving_later_does_not_take_over(tmp_path):
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("A", T, T + 1000, 300.0))
        await r.sched.tick(T)
        r.sched.add_event(ev("B", T - 100, T + 500, -50.0))
        await r.sched.tick(T + 1)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [300.0]
    assert status("B") == SUPERSEDED


def test_back_to_back_events_hand_over_without_an_idle_gap(tmp_path):
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("A", T, T + 100, 100.0))
        r.sched.add_event(ev("B", T + 100, T + 200, -100.0))
        for now in (T, T + 100, T + 200):
            await r.sched.tick(now)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [100.0, -100.0, 0.0]
    assert (status("A"), status("B")) == (COMPLETED, COMPLETED)


# ── 2. Cancellation ──────────────────────────────────────────────────────

def test_cancelled_event_does_not_execute(tmp_path):
    async def run():
        r = await rig(tmp_path)
        client = IEEEClient(CLIENT_CFG, r.state, r.sched)
        client._parse_and_dispatch(derc(dict(mrid="X", start=T - 5, duration=600, value=300000, status=2)))
        await r.sched.tick(T)
        return r
    r = asyncio.run(run())
    assert 300.0 not in r.fake.setpoint_history()
    assert r.fake.setpoint_kw() == 0.0
    assert status("X") == CANCELLED
    assert audit_rows(r.audit, "schedule_received")[-1]["outcome"] == "cancelled"


def test_event_cancelled_while_running_idles_and_the_superseded_event_stays_out(tmp_path):
    async def run():
        r = await rig(tmp_path)
        client = IEEEClient(CLIENT_CFG, r.state, r.sched)
        client._parse_and_dispatch(derc(dict(mrid="A", start=T, duration=1000, value=300000),
                                        dict(mrid="B", start=T + 100, duration=800, value=-200000)))
        await r.sched.tick(T)
        await r.sched.tick(T + 100)
        assert r.fake.setpoint_kw() == -200.0
        client._parse_and_dispatch(derc(dict(mrid="A", start=T, duration=1000, value=300000),
                                        dict(mrid="B", start=T + 100, duration=800, value=-200000,
                                             status=2)))
        await r.sched.tick(T + 101)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_kw() == 0.0                      # not back to A's +300
    assert (status("A"), status("B")) == (SUPERSEDED, CANCELLED)


def test_server_marked_superseded_or_unrecognised_status_never_executes(tmp_path):
    async def run():
        r = await rig(tmp_path)
        client = IEEEClient(CLIENT_CFG, r.state, r.sched)
        client._parse_and_dispatch(derc(dict(mrid="S", start=T - 5, duration=60, value=10000, status=4),
                                        dict(mrid="U", start=T - 5, duration=60, value=20000, status=9)))
        await r.sched.tick(T)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [0.0]
    assert (status("S"), status("U")) == (SUPERSEDED, REJECTED)


def test_unchanged_reserve_never_revives_a_superseded_or_finished_event(tmp_path):
    async def run():
        r = await rig(tmp_path)
        a, b = ev("A", T, T + 1000, 300.0), ev("B", T + 100, T + 200, -100.0)
        r.sched.add_event(a)
        r.sched.add_event(b)
        for now in (T, T + 100, T + 200):
            await r.sched.tick(now)
        outcomes = [r.sched.add_event(a), r.sched.add_event(b)]     # the server re-serves both
        await r.sched.tick(T + 300)
        return r, outcomes
    r, outcomes = asyncio.run(run())
    assert outcomes == ["unchanged", "unchanged"]
    assert r.fake.setpoint_kw() == 0.0
    assert (status("A"), status("B")) == (SUPERSEDED, COMPLETED)


def test_changed_instruction_under_the_same_mrid_is_evaluated_afresh(tmp_path):
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("A", T, T + 100, 100.0))
        await r.sched.tick(T)
        await r.sched.tick(T + 100)
        outcome = r.sched.add_event(ev("A", T + 300, T + 400, 80.0))
        await r.sched.tick(T + 300)
        return r, outcome
    r, outcome = asyncio.run(run())
    assert outcome == "updated"
    assert r.fake.setpoint_history() == [100.0, 0.0, 80.0]


# ── 3. Retries and in-flight updates ─────────────────────────────────────

def test_older_event_cannot_regain_control_through_a_retry(tmp_path):
    """H2: the old scheduler retried A's failed start after B had taken
    control, and A overrode B."""
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("A", T, T + 3600, 500.0))
        r.sched.add_event(ev("B", T + 5, T + 1800, -200.0))
        r.fake.link_down = True
        for now in range(T, T + 5):
            await r.sched.tick(now)                          # A's writes all fail
        r.fake.link_down = False
        for now in range(T + 5, T + 11):
            await r.sched.tick(now)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [-200.0]             # A's +500 was never sent
    assert status("A") == SUPERSEDED


def test_update_arriving_during_an_in_flight_write_is_applied(tmp_path):
    """M2: the old scheduler dropped this update."""
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("E", T - 5, T + 600, 100.0))
        r.fake.write_gate = asyncio.Event()
        write = asyncio.create_task(r.sched.tick(T))
        await until(lambda: 1009 in r.fake.gated)            # the +100 write is now in flight
        r.sched.add_event(ev("E", T - 5, T + 600, 200.0))    # the utility updates E meanwhile
        r.fake.write_gate.set()
        await write
        await r.sched.tick(T + 1)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [100.0, 200.0]
    assert r.fake.setpoint_kw() == 200.0


# ── 4. Stale setpoints ───────────────────────────────────────────────────

def test_rejected_setpoint_does_not_strand_the_previous_one(tmp_path):
    """C1b: the BMS refuses B (Modbus exception 03). The old scheduler left
    A's +300 kW on the battery after every event had ended."""
    async def run():
        r = await rig(tmp_path)
        r.fake.reject = lambda address, words: 3 if address == 1009 and _decode_int32(words) == 900 else None
        r.sched.add_event(ev("A", T, T + 100, 300.0))
        r.sched.add_event(ev("B", T + 100, T + 200, -900.0))
        for now in (T, T + 100, T + 101, T + 200):
            await r.sched.tick(now)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [300.0, 0.0]
    assert r.fake.setpoint_kw() == 0.0
    assert (status("A"), status("B")) == (COMPLETED, REJECTED)
    rejected = [row for row in audit_rows(r.audit, "dispatch") if row["outcome"] == "rejected"]
    assert rejected and "exception 3" in rejected[0]["detail"]


def test_write_that_fails_verification_is_resent_and_stopped_at_the_end(tmp_path):
    """C1c: the write lands but the post-write check fails. The old
    scheduler never stopped this event."""
    async def run():
        r = await rig(tmp_path)
        r.fake.fail_reads.add(1000)                          # every post-write check read fails
        r.sched.add_event(ev("A", T, T + 3, 300.0))
        for now in (T, T + 1, T + 2, T + 3):
            await r.sched.tick(now)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_kw() == 0.0
    assert r.fake.setpoint_history()[-1] == 0.0
    assert status("A") == EXPIRED                            # its setpoint was never confirmed


# ── 5. Restart recovery ──────────────────────────────────────────────────

def test_graceful_restart_restores_the_active_event(tmp_path):
    """C1d: shutdown idles the battery; the old scheduler never re-applied
    an event that was still in its window."""
    async def run():
        fake = FakeBMS()
        first = await rig(tmp_path, fake)
        first.sched.add_event(ev("A", T, T + 1000, 300.0))
        await first.sched.tick(T)
        await first.driver.safe_idle(source="shutdown")     # what __main__ does on Ctrl+C
        second = await rig(tmp_path, fake)                   # restart against the same BMS
        decision = await second.sched.tick(T + 10)
        return fake, decision
    fake, decision = asyncio.run(run())
    assert (decision.authority, decision.mrid) == (AUTH_SCHEDULE, "A")
    assert fake.setpoint_history() == [300.0, 0.0, 300.0]


def test_hard_kill_during_an_active_event_is_reconciled_on_restart(tmp_path):
    async def run():
        fake = FakeBMS()
        first = await rig(tmp_path, fake)
        first.sched.add_event(ev("A", T, T + 1000, 300.0))
        await first.sched.tick(T)
        fake.hr[1009] = fake.hr[1010] = 0                    # the BMS lost the setpoint meanwhile
        second = await rig(tmp_path, fake)                   # no shutdown ran
        await second.sched.tick(T + 30)
        return fake
    fake = asyncio.run(run())
    assert fake.setpoint_kw() == 300.0


def test_crash_while_a_write_was_in_flight_is_recovered(tmp_path):
    """C1a: the old scheduler never picked up an event whose start was in
    flight when the process died."""
    async def run():
        fake = FakeBMS()
        first = await rig(tmp_path, fake)
        first.sched.add_event(ev("A", T, T + 1000, 300.0))
        fake.write_gate = asyncio.Event()
        dying = asyncio.create_task(first.sched.tick(T))
        await until(lambda: 1009 in fake.gated)              # the write is in flight
        dying.cancel()                                       # the process dies mid-write
        await asyncio.gather(dying, return_exceptions=True)
        fake.write_gate = None
        assert fake.setpoint_history() == []
        second = await rig(tmp_path, fake)
        await second.sched.tick(T + 5)
        return fake
    fake = asyncio.run(run())
    assert fake.setpoint_history() == [300.0]


def test_restart_with_pending_events_runs_each_at_its_time(tmp_path):
    async def run():
        fake = FakeBMS()
        first = await rig(tmp_path, fake)
        for event in (ev("A", T, T + 100, 100.0), ev("B", T + 200, T + 300, -50.0),
                      ev("C", T + 400, T + 500, 70.0)):
            first.sched.add_event(event)
        await first.sched.tick(T)
        second = await rig(tmp_path, fake)                   # restart during the gap after A
        for now in (T + 150, T + 200, T + 300, T + 400, T + 500):
            await second.sched.tick(now)
        return fake
    fake = asyncio.run(run())
    assert fake.setpoint_history() == [100.0, 0.0, -50.0, 0.0, 70.0, 0.0]
    assert (status("A"), status("B"), status("C")) == (COMPLETED, COMPLETED, COMPLETED)


def test_database_from_the_previous_scheduler_is_migrated(tmp_path, schedule_db):
    conn = sqlite3.connect(schedule_db)
    conn.execute("""CREATE TABLE events (mrid TEXT PRIMARY KEY, start_ts INTEGER NOT NULL,
                    end_ts INTEGER NOT NULL, power_kw REAL NOT NULL, source TEXT,
                    executed INTEGER DEFAULT 0)""")
    conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, 't', ?)",
                     [("RUNNING", T - 60, T + 600, 250.0, -1),     # killed mid-start
                      ("DONE", T - 600, T - 10, 50.0, 2),
                      ("NEXT", T + 60, T + 120, -80.0, 0)])
    conn.commit()
    conn.close()

    async def run():
        r = await rig(tmp_path)
        await r.sched.tick(T)
        await r.sched.tick(T + 60)
        return r
    r = asyncio.run(run())
    assert r.fake.setpoint_history() == [250.0, -80.0]
    assert (status("RUNNING"), status("DONE"), status("NEXT")) == (SUPERSEDED, COMPLETED, ACTIVE)


# ── 6. Operator Force Idle ───────────────────────────────────────────────

def test_force_idle_overrides_every_schedule_until_released(tmp_path):
    """C2c: Force Idle used to clear only the utility-side list, and the
    running event kept going."""
    async def run():
        fake = FakeBMS()
        r = await rig(tmp_path, fake)
        r.sched.add_event(ev("A", T, T + 10_000, 300.0))
        await r.sched.tick(T)
        set_force_idle(True, "alice", "maintenance")
        d1 = await r.sched.tick(T + 1)
        r2 = await rig(tmp_path, fake)                       # survives a restart
        d2 = await r2.sched.tick(T + 2)
        r2.sched.add_event(ev("B", T + 3, T + 10_000, -150.0))
        d3 = await r2.sched.tick(T + 3)                      # a new event does not lift it
        set_force_idle(False, "alice", "work finished")
        d4 = await r2.sched.tick(T + 4)
        return fake, r2, (d1, d2, d3, d4)
    fake, r2, (d1, d2, d3, d4) = asyncio.run(run())
    assert [d.authority for d in (d1, d2, d3)] == [AUTH_FORCE_IDLE] * 3
    assert (d4.authority, d4.mrid) == (AUTH_SCHEDULE, "B")
    assert fake.setpoint_history() == [300.0, 0.0, 0.0, -150.0]
    assert status("A") == SUPERSEDED                         # B took over underneath; A never returns
    commands = [(row["outcome"], row["source"]) for row in audit_rows(r2.audit, "operator_command")]
    assert ("engaged", "operator:alice") in commands and ("released", "operator:alice") in commands


def test_authority_is_always_reported(tmp_path):
    async def run():
        r = await rig(tmp_path)
        seen = [(await r.sched.tick(T)).authority]
        r.sched.add_event(ev("A", T + 1, T + 100, 40.0))
        seen.append((await r.sched.tick(T + 1)).authority)
        set_force_idle(True, "bob", "test")
        seen.append((await r.sched.tick(T + 2)).authority)
        return r, seen
    r, seen = asyncio.run(run())
    assert seen == [AUTH_SAFE_IDLE, AUTH_SCHEDULE, AUTH_FORCE_IDLE]
    assert r.state.authority == {"authority": AUTH_FORCE_IDLE, "mrid": None, "desired_kw": 0.0,
                                 "reason": "Force Idle engaged by bob: test", "until": None,
                                 "confirmed": True}


# ── 7. Audit of dispatch outcomes ────────────────────────────────────────

def test_dispatch_success_and_failure_are_audited_once_each(tmp_path):
    async def run():
        r = await rig(tmp_path)
        r.sched.add_event(ev("A", T, T + 1000, 300.0))
        r.fake.link_down = True
        for now in (T, T + 1, T + 2):
            await r.sched.tick(now)
        r.fake.link_down = False
        await r.sched.tick(T + 3)
        return r
    r = asyncio.run(run())
    rows = [(row["outcome"], row["source"]) for row in audit_rows(r.audit, "dispatch")]
    assert rows == [("failed_will_retry", "schedule:A"), ("success", "schedule:A")]
    assert "after 3 failed attempt(s)" in audit_rows(r.audit, "dispatch")[-1]["detail"]
