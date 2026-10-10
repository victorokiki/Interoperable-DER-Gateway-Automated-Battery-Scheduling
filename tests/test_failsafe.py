"""
tests/test_failsafe.py
Phase 2 regression tests — communication loss, reliability state and
fail-safe behaviour (Sprint 2 Table 1, F-05, F-07; audit C3a-c, H1a/b, H6a-c).

The real IEEEClient polls an in-process IEEE 2030.5 server; the real
Scheduler, ReliabilityManager and ModbusDriver run against a FakeBMS,
wired as capstone/__main__.py wires them. Each check covers the battery
setpoint, the reliability state, the dispatch authority and the audit log.
"""

import asyncio
import sqlite3
import time

import aiohttp
import pytest

from capstone.__main__ import supervise
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient
from capstone.reliability import ReliabilityManager, ReliabilityState
from capstone.scheduler import AUTH_FAILSAFE, AUTH_SCHEDULE, SUPERSEDED, Scheduler
from fakes import FakeBMS, Utility, audit_rows, derc, make_driver

CONNECTED = ReliabilityState.CONNECTED
DERATED = ReliabilityState.DERATED
DISCONNECTED = ReliabilityState.DISCONNECTED
AUTONOMOUS = ReliabilityState.AUTONOMOUS
SAFE_IDLE = ReliabilityState.SAFE_IDLE

T = 1_800_000_000


class Gateway:
    """capstone/__main__.py's wiring around one FakeBMS and one utility."""

    @classmethod
    async def create(cls, tmp_path, util, fake=None, offline_timeout=30, initial_poll=True):
        """initial_poll: poll the utility once, so the schedule is known
        before the first scheduler step (as when the gateway has been up
        for a while). Leave it off to model a gateway just (re)started."""
        g = cls()
        g.audit = AuditLog(db_path=tmp_path / "audit.db")
        g.fake = fake if fake is not None else FakeBMS()
        g.driver, g.state = await make_driver(g.fake, audit_log=g.audit)
        g.rm = ReliabilityManager({"offline_timeout_sec": offline_timeout}, g.state, audit_log=g.audit)
        g.sched = Scheduler({}, g.state, g.driver, audit_log=g.audit, reliability_manager=g.rm)
        g.client = IEEEClient({"ieee_server_url": util.url, "ieee_poll_interval_sec": 5,
                               "offline_timeout_sec": offline_timeout},
                              g.state, g.sched, reliability_manager=g.rm, audit_log=g.audit)
        g.http = aiohttp.ClientSession()
        if initial_poll:
            await g.client._poll(g.http)
        return g

    async def cycle(self, now):
        """One scheduler step, then one utility poll (the FSM sees the
        arbiter's latest decision, as it does between real polls)."""
        await self.sched.step(now)
        await self.client._poll(self.http)
        return self.rm.reliability_state

    def offline_for(self, seconds):
        self.state.last_grid_contact = time.time() - seconds

    def transitions(self):
        return [row["detail"] for row in audit_rows(self.audit, "state_transition")]

    async def close(self):
        await self.http.close()


def e_list(*events):
    return derc(*[dict(mrid=m, start=s, duration=d, value=w) for m, s, d, w in events])


# ── Utility communication loss (Sprint 2 Table 1) ────────────────────────

def test_approved_schedule_keeps_running_through_a_prolonged_utility_outage(tmp_path):
    """Table 1 row 2 / C3a / H1a: the old gateway zeroed the battery once the
    utility had been unreachable for 30 s and reported SAFE_IDLE."""
    async def run():
        util = await Utility(e_list(("E", T, 3600, 300000))).start()
        g = await Gateway.create(tmp_path, util)
        first = await g.cycle(T)
        await util.stop()                                       # utility unreachable
        labels = [await g.cycle(T + 5 * i) for i in range(1, 4)]
        g.offline_for(600)                                      # ten minutes into the outage
        labels.append(await g.cycle(T + 600))
        during = g.fake.setpoint_kw()
        after = (await g.cycle(T + 3600), g.fake.setpoint_kw())  # E expires
        await g.close()
        return g, first, labels, during, after
    g, first, labels, during, after = asyncio.run(run())
    assert first is CONNECTED
    assert labels == [DERATED, DERATED, AUTONOMOUS, AUTONOMOUS]
    assert during == 300.0
    assert after == (SAFE_IDLE, 0.0)
    assert g.fake.setpoint_history() == [300.0, 0.0]
    assert g.transitions() == ["CONNECTED -> DERATED", "DERATED -> AUTONOMOUS", "AUTONOMOUS -> SAFE_IDLE"]


def test_reliability_state_follows_dispatch_between_polls(tmp_path):
    """Found by the post-Phase 3 comm-loss probe (scenario 4): with the
    utility down, a pending approved event started discharging while the
    FSM still said SAFE_IDLE until the next failed poll, 4.6 s later (it is
    re-evaluated only on polls, every 5 s)."""
    async def run():
        util = await Utility(e_list(("P", T + 100, 50, -30000))).start()   # -30 kW, T+100..T+150
        g = await Gateway.create(tmp_path, util)
        await g.cycle(T)
        await util.stop()
        for i in range(1, 4):
            await g.cycle(T + 5 * i)
        g.offline_for(60)                                       # past the 30 s grace period
        await g.cycle(T + 60)
        before = g.rm.reliability_state
        await g.sched.step(T + 100)                             # P starts; no poll in between
        at_start = (g.fake.setpoint_kw(), g.rm.reliability_state)
        await g.sched.step(T + 150)                             # P ends; no poll in between
        at_end = (g.fake.setpoint_kw(), g.rm.reliability_state)
        await g.close()
        return g, before, at_start, at_end
    g, before, at_start, at_end = asyncio.run(run())
    assert before is SAFE_IDLE
    assert at_start == (-30.0, AUTONOMOUS)
    assert at_end == (0.0, SAFE_IDLE)
    assert g.transitions()[-2:] == ["SAFE_IDLE -> AUTONOMOUS", "AUTONOMOUS -> SAFE_IDLE"]


def test_outage_with_no_valid_schedule_ends_in_safe_idle(tmp_path):
    async def run():
        util = await Utility(e_list()).start()
        g = await Gateway.create(tmp_path, util)
        await g.cycle(T)
        util.mode = "503"
        labels = [await g.cycle(T + 5 * i) for i in range(1, 4)]
        g.offline_for(60)                                       # past the 30 s grace period
        labels.append(await g.cycle(T + 60))
        await util.stop()
        await g.close()
        return g, labels
    g, labels = asyncio.run(run())
    assert labels == [DERATED, DERATED, DISCONNECTED, SAFE_IDLE]
    assert g.fake.setpoint_history() == [0.0]
    assert g.transitions() == ["CONNECTED -> DERATED", "DERATED -> DISCONNECTED", "DISCONNECTED -> SAFE_IDLE"]
    assert all(row["detail"] == "HTTP 503" for row in audit_rows(g.audit, "grid_poll"))


def test_schedule_completing_during_an_outage_then_safe_idle(tmp_path):
    async def run():
        util = await Utility(e_list(("E", T, 20, 40000))).start()
        g = await Gateway.create(tmp_path, util)
        await g.cycle(T)
        util.mode = "503"
        labels = [await g.cycle(T + 5 * i) for i in range(1, 4)]  # E still running
        g.offline_for(60)
        labels.append(await g.cycle(T + 20))                   # E completes
        await util.stop()
        await g.close()
        return g, labels
    g, labels = asyncio.run(run())
    assert labels == [DERATED, DERATED, AUTONOMOUS, SAFE_IDLE]
    assert g.fake.setpoint_history() == [40.0, 0.0]


def test_restored_utility_with_a_new_schedule_takes_over(tmp_path):
    async def run():
        util = await Utility(e_list(("E", T, 3600, 300000))).start()
        g = await Gateway.create(tmp_path, util)
        await g.cycle(T)
        util.mode = "503"
        for i in range(1, 4):
            await g.cycle(T + 5 * i)
        util.mode = "serve"
        util.xml = e_list(("E", T, 3600, 300000), ("N", T + 100, 600, -150000))
        restored = await g.cycle(T + 60)
        await g.cycle(T + 100)
        mid = g.fake.setpoint_kw()
        await g.cycle(T + 700)                                  # N ends; E must not come back
        await util.stop()
        await g.close()
        return g, restored, mid
    g, restored, mid = asyncio.run(run())
    assert restored is CONNECTED
    assert mid == -150.0
    assert g.fake.setpoint_history() == [300.0, -150.0, 0.0]
    with g.sched._db() as conn:
        assert conn.execute("SELECT status FROM events WHERE mrid='E'").fetchone()[0] == SUPERSEDED


def test_gateway_restart_during_an_outage_keeps_the_approved_schedule(tmp_path):
    async def run():
        util = await Utility(e_list(("E", T, 3600, 300000))).start()
        fake = FakeBMS()
        first = await Gateway.create(tmp_path, util, fake)
        await first.cycle(T)
        util.mode = "503"
        await first.cycle(T + 5)
        await first.close()
        second = await Gateway.create(tmp_path, util, fake, initial_poll=False)  # restart, utility down
        labels = [await second.cycle(T + 30 + 5 * i) for i in range(3)]
        await util.stop()
        await second.close()
        return second, labels
    g, labels = asyncio.run(run())
    assert labels == [DERATED, DERATED, AUTONOMOUS]
    assert g.fake.setpoint_kw() == 300.0
    assert g.state.authority["authority"] == AUTH_SCHEDULE


def test_http_errors_and_non_xml_answers_count_as_link_failures(tmp_path):
    """C3b / C3c: an HTML page counted as a healthy link, and during HTTP
    503s the FSM said SAFE_IDLE while the battery kept running."""
    async def run():
        util = await Utility(e_list(("E", T, 3600, 300000))).start()
        g = await Gateway.create(tmp_path, util)
        await g.cycle(T)
        util.mode = "html"
        html = (await g.cycle(T + 5), g.state.grid_connected)
        util.mode = "503"
        errors = [await g.cycle(T + 10), await g.cycle(T + 15)]
        battery = g.fake.setpoint_kw()
        util.mode = "serve"
        back = (await g.cycle(T + 20), g.state.grid_connected)
        await util.stop()
        await g.close()
        return html, errors, battery, back
    html, errors, battery, back = asyncio.run(run())
    assert html == (DERATED, False)
    assert errors == [DERATED, AUTONOMOUS]
    assert battery == 300.0
    assert back == (CONNECTED, True)


def test_one_bad_event_is_rejected_without_dropping_the_others_or_faking_grid_loss(tmp_path):
    """H6a / H6b: a multiplier of 400 aborted the whole list and was
    reported as a lost utility link."""
    async def run():
        util = await Utility(derc(dict(mrid="A", start=T, duration=60, value=1000),
                                  dict(mrid="BAD", start=T, duration=60, value=1, mult=400),
                                  dict(mrid="B", start=T + 60, duration=60, value=2000))).start()
        g = await Gateway.create(tmp_path, util)
        label = await g.cycle(T)
        await util.stop()
        await g.close()
        with g.sched._db() as conn:
            stored = [row[0] for row in conn.execute("SELECT mrid FROM events ORDER BY mrid")]
        return g, label, stored
    g, label, stored = asyncio.run(run())
    assert stored == ["A", "B"]
    assert (label, g.state.grid_connected) == (CONNECTED, True)
    assert any("BAD" in row["detail"] for row in audit_rows(g.audit, "schedule_rejected"))


# ── Scheduler and background-task failures ───────────────────────────────

def _broken_store(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


def test_scheduler_failure_keeps_the_approved_event_only_until_its_end(tmp_path):
    async def run():
        util = await Utility(e_list()).start()
        g = await Gateway.create(tmp_path, util)
        g.sched.add_event({"mrid": "E", "start": T, "end": T + 100, "power_kw": 300.0, "source": "t"})
        await g.sched.step(T)
        g.sched._decide_and_record = _broken_store              # the schedule store fails
        await g.sched.step(T + 50)
        inside = (g.fake.setpoint_kw(), dict(g.state.authority))
        await g.sched.step(T + 100)
        after = (g.fake.setpoint_kw(), dict(g.state.authority))
        del g.sched._decide_and_record                          # the store recovers
        await g.sched.step(T + 101)
        await util.stop()
        await g.close()
        return g, inside, after
    g, inside, after = asyncio.run(run())
    assert inside[0] == 300.0 and "continuing approved event E" in inside[1]["reason"]
    assert after[0] == 0.0 and after[1]["authority"] == AUTH_FAILSAFE
    outcomes = [row["outcome"] for row in audit_rows(g.audit, "scheduler")]
    assert outcomes == ["failsafe", "recovered"]


def test_scheduler_failure_before_any_decision_holds_zero(tmp_path):
    async def run():
        util = await Utility(e_list()).start()
        g = await Gateway.create(tmp_path, util)
        g.fake.hr[1009], g.fake.hr[1010] = 0xFFFF, 0xFED4        # the BMS still holds +300 kW
        g.sched._decide_and_record = _broken_store
        await g.sched.step(T)
        await util.stop()
        await g.close()
        return g
    g = asyncio.run(run())
    assert g.fake.setpoint_kw() == 0.0
    assert g.state.authority["authority"] == AUTH_FAILSAFE


def test_crashed_background_task_is_logged_audited_and_restarted(tmp_path):
    audit = AuditLog(db_path=tmp_path / "audit.db")

    async def run():
        runs = []

        async def flaky():
            runs.append(len(runs))
            if len(runs) == 1:
                raise RuntimeError("boom")
            await asyncio.Event().wait()                        # the restarted loop keeps running

        task = asyncio.create_task(supervise("flaky_loop", flaky, audit, restart_delay=0))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return runs
    runs = asyncio.run(run())
    assert runs == [0, 1]
    rows = audit_rows(audit, "task_supervision")
    assert [(row["source"], row["detail"]) for row in rows] == [("flaky_loop", "crashed: RuntimeError: boom")]


# ── Control that is claimed must be confirmed ────────────────────────────

def test_fsm_does_not_claim_control_that_the_bms_has_not_confirmed(tmp_path):
    async def run():
        util = await Utility(e_list(("E", T, 3600, 300000))).start()
        g = await Gateway.create(tmp_path, util)                # E received
        g.fake.link_down = True                                 # then the BMS link goes down too
        util.mode = "503"
        labels = [await g.cycle(T + 5 * i) for i in range(3)]
        unconfirmed = dict(g.state.authority)
        g.fake.link_down = False
        labels.append(await g.cycle(T + 20))
        await util.stop()
        await g.close()
        return g, labels, unconfirmed
    g, labels, unconfirmed = asyncio.run(run())
    assert labels == [DERATED, DERATED, DISCONNECTED, AUTONOMOUS]
    assert unconfirmed["authority"] == AUTH_SCHEDULE and unconfirmed["confirmed"] is False
    assert g.state.authority["confirmed"] is True


def test_lost_bms_link_ends_the_autonomous_claim_before_the_watchdog_runs(tmp_path):
    """Functional validation: with the utility down and the approved event
    confirmed (AUTONOMOUS), the Modbus link to the BMS dropped. The FSM kept
    AUTONOMOUS, and the dashboard the BMS-verified setpoint, for 9.2 s, until
    the 10 s watchdog noticed, although pymodbus reported the link down at once."""
    async def run():
        util = await Utility(e_list(("E", T, 3600, 300000))).start()
        g = await Gateway.create(tmp_path, util)
        await g.cycle(T)
        await util.stop()                                       # utility unreachable
        labels = [await g.cycle(T + 5 * i) for i in range(1, 4)]
        g.fake.connected = False                                # the BMS link drops (as pymodbus reports it)
        lost = (await g.cycle(T + 20), dict(g.state.authority), g.state.commanded_kw)
        await g.driver.ensure_connected()                       # the watchdog reconnects
        back = (await g.cycle(T + 25), dict(g.state.authority))
        await g.close()
        return labels, lost, back
    labels, lost, back = asyncio.run(run())
    assert labels == [DERATED, DERATED, AUTONOMOUS]
    assert lost[0] is DISCONNECTED and lost[1]["confirmed"] is False and lost[2] is None
    assert back[0] is AUTONOMOUS and back[1]["confirmed"] is True


def test_lost_modbus_link_is_reconnected_and_the_setpoint_reapplied(tmp_path):
    async def run():
        util = await Utility(e_list(("E", T, 3600, 300000))).start()
        g = await Gateway.create(tmp_path, util)
        await g.cycle(T)
        g.fake.connected = False                                # the link dropped ...
        g.fake.hr[1009] = g.fake.hr[1010] = 0                   # ... and the BMS lost the setpoint
        await g.driver.ensure_connected()                       # one watchdog check
        await g.cycle(T + 10)
        await util.stop()
        await g.close()
        return g
    g = asyncio.run(run())
    assert g.fake.setpoint_kw() == 300.0
    assert g.fake.setpoint_history() == [300.0, 300.0]
    assert g.fake.commands() == []       # the BMS stayed On: no Operating Command was needed


def test_modbus_write_failure_is_retried_with_authority_kept_and_audited(tmp_path):
    async def run():
        util = await Utility(e_list(("E", T, 3600, 300000))).start()
        g = await Gateway.create(tmp_path, util)
        g.fake.link_down = True
        failing = (await g.cycle(T), dict(g.state.authority))
        g.fake.link_down = False
        recovered = (await g.cycle(T + 5), dict(g.state.authority))
        await util.stop()
        await g.close()
        return g, failing, recovered
    g, failing, recovered = asyncio.run(run())
    assert failing[0] is CONNECTED and failing[1]["confirmed"] is False
    assert recovered[0] is CONNECTED and recovered[1]["confirmed"] is True
    assert [row["outcome"] for row in audit_rows(g.audit, "dispatch")] == ["failed_will_retry", "success"]
