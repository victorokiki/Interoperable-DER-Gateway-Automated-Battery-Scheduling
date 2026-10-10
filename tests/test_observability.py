"""
tests/test_observability.py
Phase 5 regression tests — audit completeness (H7) and dashboard truth (H8).

The audit log alone must let an engineer reconstruct a command: what the
utility sent, what the gateway acknowledged, who took authority, which
register was written with which value, whether the BMS confirmed it, what
failed and what recovered, and when the gateway itself started and
stopped. The dashboard must keep apart what the gateway WANTS (requested),
what the BMS CONFIRMED (verified) and what it MEASURES, and say how old
the BMS data is.
"""

import asyncio
import contextlib
import importlib.util
import json
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp

import capstone.__main__ as main_mod
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient
from capstone.modbus_driver import ModbusDriver
from capstone.reliability import ReliabilityManager
from capstone.scheduler import Scheduler
from fakes import STANDBY, FakeBMS, Utility, audit_rows, derc, make_driver

ROOT = Path(__file__).resolve().parents[1]
T = 1_800_000_000                                # 2027-01-15 08:00:00 UTC
TRAIL = ("schedule_received", "ieee_response", "schedule_status", "authority", "modbus_write", "dispatch")


async def rig(tmp_path, util, fake=None):
    """capstone/__main__.py's wiring around one FakeBMS and one utility."""
    audit = AuditLog(db_path=tmp_path / "audit.db")
    driver, state = await make_driver(fake or FakeBMS(), audit_log=audit)
    rm = ReliabilityManager({"offline_timeout_sec": 30}, state, audit_log=audit)
    sched = Scheduler({}, state, driver, audit_log=audit, reliability_manager=rm)
    client = IEEEClient({"ieee_server_url": util.url, "ieee_poll_interval_sec": 5, "offline_timeout_sec": 30},
                        state, sched, reliability_manager=rm, audit_log=audit)
    return SimpleNamespace(fake=driver._client, driver=driver, state=state, rm=rm, sched=sched,
                           client=client, audit=audit, http=aiohttp.ClientSession())


def trail(audit, types=TRAIL):
    return [(r["event_type"], r["outcome"], r["detail"]) for r in audit_rows(audit) if r["event_type"] in types]


def dashboard_battery(tmp_path, monkeypatch, state, rm):
    """What the dashboard's /api/status serves for this gateway state."""
    status_file = tmp_path / "live_status.json"
    status_file.write_text(json.dumps(main_mod.status_snapshot(state, rm)))
    spec = importlib.util.spec_from_file_location("grid_server_under_test",
                                                  ROOT / "simulators" / "fake_grid_server.py")
    grid = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grid)
    monkeypatch.setattr(grid, "BATTERY_STATUS_FILE", status_file)
    client = grid.app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["username"] = "operator"
    return client.get("/api/status").get_json()["battery"], grid


# ── Audit completeness (H7) ──────────────────────────────────────────────

def test_audit_reconstructs_a_successful_command(tmp_path):
    """Mandatory scenario 19. (Since step 2 the event asks for responses,
    as IEEE 2030.5 requires before any are sent, and they are sent by the
    response loop, so the test delivers them after the poll.)"""
    async def run():
        util = await Utility(derc(dict(mrid="E", start=T, duration=600, value=300_000,
                                       response_required="01"))).start()
        g = await rig(tmp_path, util)
        await g.client._poll(g.http)
        await g.client.send_responses(g.http)
        await g.sched.step(T)
        await g.http.close()
        await util.stop()
        return g
    g = asyncio.run(run())
    rows = trail(g.audit)
    assert [(t, o) for t, o, _ in rows] == [
        ("schedule_received", "new"), ("ieee_response", "sent"), ("schedule_status", "active"),
        ("authority", "decided"), ("modbus_write", "confirmed"), ("dispatch", "success")]
    received, ack, active, authority, write, dispatch = [d for _, _, d in rows]
    assert "mrid=E power_kw=+300.0 start=2027-01-15 08:00:00 UTC end=2027-01-15 08:10:00 UTC" in received
    assert "EventReceived for E: HTTP 201" in ack
    assert "approved_schedule power_kw=+300.0" in authority and "event E" in authority
    assert "requested=+300.0 kW written=+300.0 kW register=41009" in write
    assert "verified: read back unchanged, BMS On, Normal control mode" in write
    assert "setpoint +300.0 kW written (approved_schedule)" in dispatch


def test_audit_records_a_failing_command_once_and_its_recovery(tmp_path):
    """Mandatory scenario 20: a Modbus outage makes the scheduler retry
    every second; the failure is recorded when it starts (not 60 times a
    minute) and the recovery says how many attempts failed."""
    async def run():
        util = await Utility(derc(dict(mrid="E", start=T, duration=600, value=300_000))).start()
        g = await rig(tmp_path, util)
        await g.client._poll(g.http)
        g.fake.link_down = True
        for i in range(30):
            await g.sched.step(T + i)
        g.fake.link_down = False
        await g.sched.step(T + 30)
        await g.http.close()
        await util.stop()
        return g
    g = asyncio.run(run())
    rows = trail(g.audit, ("modbus_write", "dispatch"))
    assert [(t, o) for t, o, _ in rows] == [
        ("modbus_write", "not written: limits unavailable"), ("dispatch", "failed_will_retry"),
        ("modbus_write", "confirmed"), ("dispatch", "success")]
    assert "setpoint +300.0 kW NOT written (approved_schedule): limits_unavailable" in rows[1][2]
    assert "after 30 failed attempt(s)" in rows[3][2]
    assert g.fake.setpoint_kw() == 300


def test_audit_records_a_command_the_bms_refuses(tmp_path):
    """Mandatory scenario 20 (refusal): a Modbus exception ends the event,
    and the trail shows why and what the gateway did instead."""
    async def run():
        fake = FakeBMS()
        fake.reject = lambda address, words: 3 if address == 1009 and words != [0, 0] else None
        util = await Utility(derc(dict(mrid="E", start=T, duration=600, value=300_000))).start()
        g = await rig(tmp_path, util, fake)
        await g.client._poll(g.http)
        await g.sched.step(T)
        await g.http.close()
        await util.stop()
        return g
    g = asyncio.run(run())
    rows = trail(g.audit, ("schedule_status", "authority", "modbus_write", "dispatch"))
    assert [(t, o) for t, o, _ in rows] == [
        ("schedule_status", "active"), ("authority", "decided"),
        ("modbus_write", "not confirmed: exception 3"), ("dispatch", "rejected"),
        ("schedule_status", "rejected"), ("authority", "decided"),
        ("modbus_write", "confirmed"), ("dispatch", "success")]
    assert "BMS rejected setpoint +300.0 kW (Modbus exception 3)" in rows[4][2]
    assert rows[5][2].startswith("safe_idle power_kw=+0.0")
    assert g.fake.setpoint_kw() == 0


def test_modbus_outage_is_audited_as_one_loss_and_one_recovery(tmp_path):
    """The watchdog retries every 10 s; 'connection lost' was recorded on
    every attempt (300 rows an hour during an outage)."""
    audit = AuditLog(db_path=tmp_path / "audit.db")

    async def run():
        fake = FakeBMS()
        driver, _ = await make_driver(fake, audit_log=audit)
        fake.connected = False
        reconnect, attempts = fake.connect, []

        async def unreachable_for_three_checks():
            attempts.append(1)
            if len(attempts) > 3:
                await reconnect()
        fake.connect = unreachable_for_three_checks
        for _ in range(4):
            await driver.ensure_connected()
    asyncio.run(run())
    assert [(r["outcome"], r["detail"]) for r in audit_rows(audit, "modbus_link")] == [
        ("reconnecting", "connection lost — reconnecting"), ("connected", "reconnected after 4 attempt(s)")]


def test_every_bms_link_loss_is_audited_even_when_the_client_reconnects_itself(tmp_path):
    """Functional validation: pymodbus reconnects on its own. After it restored
    the link the recovery was never audited and the watchdog's loss counter never
    reset, so the NEXT link loss had no audit row at all and its recovery row
    counted the attempts of both outages."""
    audit = AuditLog(db_path=tmp_path / "audit.db")

    async def run():
        fake = FakeBMS()
        driver, _ = await make_driver(fake, audit_log=audit)
        reconnect = fake.connect

        async def unreachable():
            return False
        for _ in range(2):                                      # two separate outages
            fake.connected = False                              # the link drops
            fake.connect = unreachable
            await driver.ensure_connected()                     # a watchdog check: still unreachable
            fake.connect = reconnect
            await fake.connect()                                # the Modbus client reconnects by itself
            await driver.heartbeat_once()                       # the next heartbeat sees the link back
            await driver.ensure_connected()                     # the next watchdog check: nothing to do
    asyncio.run(run())
    assert [(r["outcome"], r["detail"]) for r in audit_rows(audit, "modbus_link")] == [
        ("reconnecting", "connection lost — reconnecting"), ("connected", "reconnected after 1 attempt(s)"),
        ("reconnecting", "connection lost — reconnecting"), ("connected", "reconnected after 1 attempt(s)")]


def test_a_refused_response_is_retried_with_backoff_and_audited_once_each_way(tmp_path):
    """Requirement change (step 2; audit-harness H10a): EventReceived used
    to be re-sent on every poll that listed the event. Each response is now
    sent once; a POST the utility refuses is retried after 5 s, then 10 s,
    ...; the audit records when it starts failing and when it is delivered,
    and nothing is sent again after that, however often the event is listed."""
    async def run():
        util = await Utility(derc(dict(mrid="E", start=T, duration=600, value=300_000,
                                       response_required="01"))).start()
        g = await rig(tmp_path, util)
        util.response_status = 500
        await g.client._poll(g.http)
        t0 = time.time()                                   # the response was queued (stamped) by the poll
        await g.client.send_responses(g.http, t0)          # attempt 1: HTTP 500
        await g.client._poll(g.http)
        await g.client.send_responses(g.http, t0 + 4)      # not due before t0 + 5
        await g.client.send_responses(g.http, t0 + 5)      # attempt 2: HTTP 500
        util.response_status = 201
        await g.client.send_responses(g.http, t0 + 14)     # not due before t0 + 15
        await g.client.send_responses(g.http, t0 + 15)     # attempt 3: delivered
        for _ in range(3):
            await g.client._poll(g.http)
            await g.client.send_responses(g.http, t0 + 100)
        await g.http.close()
        await util.stop()
        return g, util
    g, util = asyncio.run(run())
    assert util.statuses() == [1, 1, 1]                     # three attempts, one response
    assert [(o, d) for _, o, d in trail(g.audit, ("ieee_response",))] == [
        ("rejected", "DERControlResponse EventReceived for E: HTTP 500 (status 1 to /rsps/1/rsp); "
                     "retrying with backoff"),
        ("sent", "DERControlResponse EventReceived for E: HTTP 201 (status 1 to /rsps/1/rsp) "
                 "after 2 failed attempt(s)")]


def test_gateway_start_and_stop_are_audited(tmp_path, monkeypatch):
    """A restart was visible only as a repeated authority row, and the
    shutdown Safe Idle was not audited. Runs the real capstone.__main__.run()
    against a FakeBMS and an in-process utility, then shuts it down the way
    Ctrl+C does (the same finally block)."""
    monkeypatch.chdir(tmp_path)                               # audit log, status file: relative paths
    monkeypatch.setattr(signal, "signal", lambda *args: None)  # keep pytest's own Ctrl+C handler
    for name in ("IEEE_POLL_INTERVAL_SEC", "OFFLINE_TIMEOUT_SEC", "TLS_CA_CERT", "TLS_CLIENT_CERT",
                 "TLS_CLIENT_KEY"):
        monkeypatch.delenv(name, raising=False)
    fake = FakeBMS()

    class Driver(ModbusDriver):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._client = fake
    monkeypatch.setattr(main_mod, "ModbusDriver", Driver)

    async def run():
        util = await Utility(derc(dict(mrid="E", start=int(time.time()) - 5, duration=600, value=50_000))).start()
        monkeypatch.setenv("IEEE_SERVER_URL", util.url)
        gateway = asyncio.create_task(main_mod.run())
        for _ in range(100):
            await asyncio.sleep(0.1)
            if fake.setpoint_kw() == 50:
                break
        running = fake.setpoint_kw()
        gateway.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gateway
        await util.stop()
        return util, running
    util, running = asyncio.run(run())
    assert running == 50
    assert fake.setpoint_kw() == 0
    rows = audit_rows(AuditLog(db_path=tmp_path / "schedule_store" / "audit_log.db"))
    types = [r["event_type"] for r in rows]
    assert types[0] == "gateway" and types[-1] == "gateway"
    started, stopped = rows[0], rows[-1]
    assert started["outcome"] == "started"
    assert f"started: utility {util.url} (poll 5 s, offline timeout 30 s, plain HTTP (no TLS) to the " \
           f"local simulator: not NF-04 mutual TLS); BMS 127.0.0.1:5020; " \
           f"reliability state CONNECTED until the first utility poll" == started["detail"]
    assert (stopped["outcome"], stopped["detail"]) == (
        "stopped", "stopped: battery at Safe Idle (0 kW), confirmed by the BMS")
    assert "src=shutdown requested=+0.0 kW written=+0.0 kW" in rows[-2]["detail"]


# ── Dashboard truth (H8) ─────────────────────────────────────────────────

def test_dashboard_keeps_requested_verified_and_measured_apart(tmp_path, monkeypatch):
    """Mandatory scenario 21: the utility asks for +300 kW, the BMS permits
    100 kW. The dashboard showed one 'Power' figure: the last value written."""
    async def run():
        fake = FakeBMS()
        fake.set_limits(charge_kw=100, discharge_kw=100)
        util = await Utility(derc(dict(mrid="E", start=T, duration=600, value=300_000))).start()
        g = await rig(tmp_path, util, fake)
        await g.client._poll(g.http)
        await g.sched.step(T)
        await g.driver.supervise()                              # the 10 s BMS status read
        await g.http.close()
        await util.stop()
        return g
    g = asyncio.run(run())
    battery, grid = dashboard_battery(tmp_path, monkeypatch, g.state, g.rm)
    assert battery["available"] is True
    assert (battery["authority"]["authority"], battery["authority"]["desired_kw"]) == ("approved_schedule", 300.0)
    assert battery["authority"]["confirmed"] is True
    assert battery["verified_kw"] == 100.0
    assert battery["measured_kw"] == 100
    assert battery["limits_kw"] == {"max_charge_kw": 100, "max_discharge_kw": 100}
    assert (battery["device_stale"], battery["device_age_sec"]) == (False, 0)
    for field in ("desired_kw", "verified_kw", "measured_kw", "device_stale"):
        assert field in grid.DASHBOARD_TEMPLATE


def test_dashboard_shows_an_unconfirmed_setpoint_as_unconfirmed(tmp_path, monkeypatch):
    """Mandatory scenario 21 (not confirmed): the BMS stays in Standby, so
    the setpoint is requested but never verified."""
    async def run():
        fake = FakeBMS(operating_state=STANDBY)
        fake.accept_on = False
        util = await Utility(derc(dict(mrid="E", start=T, duration=600, value=300_000))).start()
        g = await rig(tmp_path, util, fake)
        await g.client._poll(g.http)
        await g.sched.step(T)
        await g.driver.supervise()
        await g.http.close()
        await util.stop()
        return g
    g = asyncio.run(run())
    battery, _ = dashboard_battery(tmp_path, monkeypatch, g.state, g.rm)
    assert (battery["authority"]["desired_kw"], battery["authority"]["confirmed"]) == (300.0, False)
    assert battery["verified_kw"] is None
    assert battery["measured_kw"] == 0
    assert battery["operating_state"] == "Standby"


def test_dashboard_flags_stale_bms_data_and_shows_bms_status(tmp_path, monkeypatch):
    """H8a: BMS data of any age was shown as 'updated 0s ago' (the age of
    the snapshot, not of the data). H8c: operating status (Fault) was never
    shown."""
    async def run():
        fake = FakeBMS()
        fake.ir[1001] = 3                                       # 31001 Operating Status: Fault
        driver, state = await make_driver(fake)
        await driver.supervise()
        return state
    state = asyncio.run(run())
    rm = ReliabilityManager({"offline_timeout_sec": 30}, state)
    fresh, grid = dashboard_battery(tmp_path, monkeypatch, state, rm)
    state.last_status_poll_at = time.time() - 60                # no BMS read for a minute
    stale, _ = dashboard_battery(tmp_path, monkeypatch, state, rm)
    state.last_status_poll_at = None                            # never read
    never, _ = dashboard_battery(tmp_path, monkeypatch, state, rm)
    assert (fresh["device_stale"], fresh["operating_status"]) == (False, "Fault")
    assert (stale["device_stale"], stale["device_age_sec"], stale["age_sec"]) == (True, 60, 0)
    assert (never["device_stale"], never["device_age_sec"]) == (True, None)
    assert "b.operating_status" in grid.DASHBOARD_TEMPLATE
