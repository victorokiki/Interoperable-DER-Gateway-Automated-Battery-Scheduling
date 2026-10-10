"""
tests/test_security.py
Phase 6 regression tests — security and robustness.

Configuration is checked before anything connects (L2), and the utility
transport never silently ends up weaker than configured (M7b; NF-04:
"connection established only with valid mutual certificates; rejected
otherwise"). No cipher or certificate profile is imposed: the utility's
is still unknown.
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import pytest

import capstone.__main__ as main_mod
import capstone.scheduler as sched_mod
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient, build_mtls_context, transport_security
from capstone.scheduler import Scheduler
from capstone.state import GatewayState
from fakes import FakeBMS, Utility, audit_rows, derc, make_driver

ROOT = Path(__file__).resolve().parents[1]
T = 1_800_000_000
CERTS = ROOT / "certs"
SETTINGS = ("IEEE_SERVER_URL", "IEEE_POLL_INTERVAL_SEC", "OFFLINE_TIMEOUT_SEC", "MODBUS_HOST", "MODBUS_PORT",
            "TLS_CA_CERT", "TLS_CLIENT_CERT", "TLS_CLIENT_KEY", "ALLOW_INSECURE_TRANSPORT")


@pytest.fixture
def env(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ── Configuration (L2) ───────────────────────────────────────────────────

@pytest.mark.parametrize("name, value, message", [
    ("IEEE_POLL_INTERVAL_SEC", "0", "IEEE_POLL_INTERVAL_SEC must be at least 1, got 0"),
    ("OFFLINE_TIMEOUT_SEC", "-5", "OFFLINE_TIMEOUT_SEC must be at least 1, got -5"),
    ("MODBUS_PORT", "70000", "MODBUS_PORT must be 1-65535, got 70000"),
    ("MODBUS_PORT", "0", "MODBUS_PORT must be 1-65535, got 0"),
    ("IEEE_POLL_INTERVAL_SEC", "5s", "IEEE_POLL_INTERVAL_SEC must be a whole number, got '5s'"),
    ("MODBUS_HOST", " ", "MODBUS_HOST is empty"),
    ("IEEE_SERVER_URL", "localhost:5001", "IEEE_SERVER_URL must be an http:// or https:// URL"),
])
def test_unusable_configuration_is_refused(env, name, value, message):
    env.setenv(name, value)
    with pytest.raises(main_mod.ConfigError) as refused:
        main_mod.load_config()
    assert message in str(refused.value)


def test_default_configuration_still_reaches_the_local_simulator(env):
    config = main_mod.load_config()
    assert config["ieee_server_url"] == "http://localhost:5001"
    assert config["transport"] == "plain HTTP (no TLS) to the local simulator: not NF-04 mutual TLS"


def test_gateway_does_not_start_with_an_unsafe_configuration(tmp_path):
    """The real entry point: a clear error and exit code 2, before any
    connection to the utility or the BMS (nothing is listening on either)."""
    environ = {k: v for k, v in os.environ.items() if k not in SETTINGS}
    environ.update(IEEE_SERVER_URL="http://utility.example:5001", MODBUS_PORT="1", PYTHONPATH=str(ROOT))
    done = subprocess.run([sys.executable, "-m", "capstone"], cwd=tmp_path, env=environ,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2
    assert "Configuration error: plain HTTP (no TLS) to utility.example is refused" in done.stderr
    assert not (tmp_path / "schedule_store").exists()          # nothing was started


# ── Utility transport (M7b, NF-04) ───────────────────────────────────────

@pytest.mark.parametrize("settings, refusal", [
    (dict(ieee_server_url="http://utility.example"), "plain HTTP (no TLS) to utility.example is refused"),
    (dict(ieee_server_url="https://utility.example", tls_ca_cert="ca.crt"),
     "server-only TLS 1.2+ (no client certificate) to utility.example is refused"),
    (dict(ieee_server_url="https://utility.example"),
     "server-only TLS against the system trust store (no client certificate) to utility.example is refused"),
    (dict(ieee_server_url="http://localhost:5001", tls_ca_cert="ca.crt"),
     "TLS certificates are configured but IEEE_SERVER_URL is http://"),
    (dict(ieee_server_url="https://utility.example", tls_client_cert="c.crt"),
     "TLS_CLIENT_CERT and TLS_CLIENT_KEY must be set together"),
    (dict(ieee_server_url="https://utility.example", tls_client_cert="c.crt", tls_client_key="c.key"),
     "a client certificate is configured without TLS_CA_CERT"),
    (dict(ieee_server_url=""), "IEEE_SERVER_URL must be an http:// or https:// URL"),
])
def test_transport_is_never_silently_weaker_than_configured(settings, refusal):
    with pytest.raises(ValueError) as refused:
        transport_security(settings)
    assert refusal in str(refused.value)


@pytest.mark.parametrize("settings", [
    dict(ieee_server_url="http://localhost:5001", tls_ca_cert="ca.crt"),
    dict(tls_client_cert="c.crt"),
    dict(tls_client_key="c.key"),
    dict(tls_client_cert="c.crt", tls_client_key="c.key"),
])
def test_no_caller_can_have_tls_settings_silently_dropped(settings):
    """build_mtls_context() returned None (plain HTTP / no client
    certificate) for a client certificate without a CA, and built an unused
    context for an http:// URL."""
    with pytest.raises(ValueError):
        build_mtls_context(settings)


@pytest.mark.parametrize("settings, description", [
    (dict(ieee_server_url="http://127.0.0.1:5001"), "plain HTTP (no TLS) to the local simulator: not NF-04 mutual TLS"),
    (dict(ieee_server_url="https://localhost:5001", tls_ca_cert="ca.crt"),
     "server-only TLS 1.2+ (no client certificate) to the local simulator: not NF-04 mutual TLS"),
    (dict(ieee_server_url="http://utility.lab:5001", allow_insecure_transport=True),
     "plain HTTP (no TLS) to utility.lab (ALLOW_INSECURE_TRANSPORT set): not NF-04 mutual TLS"),
    (dict(ieee_server_url="https://utility.example", tls_ca_cert="ca.crt", tls_client_cert="c.crt",
          tls_client_key="c.key"), "mutual TLS 1.2+ (NF-04)"),
])
def test_every_accepted_transport_says_what_it_is(settings, description):
    assert transport_security(settings) == description


def test_mutual_tls_context_verifies_the_server_and_presents_the_client_certificate():
    context = build_mtls_context(dict(ieee_server_url="https://localhost:5001",
                                      tls_ca_cert=str(CERTS / "ca.crt"),
                                      tls_client_cert=str(CERTS / "client.crt"),
                                      tls_client_key=str(CERTS / "client.key")))
    assert context.verify_mode.name == "CERT_REQUIRED" and context.check_hostname is True
    assert context.minimum_version.name == "TLSv1_2"


def test_unit_tests_can_still_build_a_client_for_a_placeholder_url():
    """The deployment rule (no plain HTTP off this machine) is applied to
    the configuration at startup, not to every IEEEClient: tests build one
    for an address they never contact."""
    client = IEEEClient(dict(ieee_server_url="http://x", ieee_poll_interval_sec=5, offline_timeout_sec=30),
                        GatewayState(), scheduler=None)
    assert client.transport is None


# ── A locked schedule database (M3b, NF-01) ──────────────────────────────

def test_locked_schedule_database_never_stalls_the_bms_heartbeat(tmp_path):
    """Another process holding the schedule database's write lock (a second
    instance, a DB browser) froze the whole event loop for SQLite's 5 s busy
    timeout whenever the gateway had to write: no heartbeat, no polling.
    The waits now happen in worker threads; the approved event keeps running
    (failsafe) and everything recovers when the lock is released."""
    async def run():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        driver, state = await make_driver(FakeBMS(), audit_log=audit)
        sched = Scheduler({}, state, driver, audit_log=audit)
        util = await Utility(derc(dict(mrid="E", start=T, duration=600, value=300_000),
                                  dict(mrid="F", start=T + 1, duration=600, value=-100_000))).start()
        client = IEEEClient({"ieee_server_url": util.url, "ieee_poll_interval_sec": 5, "offline_timeout_sec": 30},
                            state, sched, audit_log=audit)
        http = aiohttp.ClientSession()
        await client._poll(http)
        await sched.step(T)                                      # E running at +300 kW
        util.xml = derc(dict(mrid="E", start=T, duration=600, value=300_000),
                        dict(mrid="F", start=T + 1, duration=600, value=-100_000),
                        dict(mrid="G", start=T + 900, duration=60, value=50_000))
        heartbeat = asyncio.create_task(driver.heartbeat_loop())
        blocker = sqlite3.connect(sched_mod.DB_PATH, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")                       # the write lock, held elsewhere
        started = time.monotonic()
        # F must supersede E (a write) and G must be stored (a write): both wait on the lock.
        await asyncio.gather(sched.step(T + 1), client._poll(http))
        waited = time.monotonic() - started
        during = driver._client.setpoint_kw()
        blocker.rollback()
        blocker.close()
        await client._poll(http)                                 # G is stored now
        await sched.step(T + 2)                                  # F takes over
        heartbeat.cancel()
        await http.close()
        await util.stop()
        beats = [t for t, address, _ in driver._client.writes if address == 1002]
        stored = [e["mrid"] for e in sched.get_upcoming(10)]
        return audit, driver, waited, during, beats, stored
    audit, driver, waited, during, beats, stored = asyncio.run(run())
    assert waited > 3                                            # the lock really was held for the busy timeout
    assert max(b - a for a, b in zip(beats, beats[1:])) < 2.0    # the heartbeat kept its ~1 s cadence
    assert during == 300                                         # the approved event kept running
    assert [r["outcome"] for r in audit_rows(audit, "scheduler")] == ["failsafe", "recovered"]
    assert driver._client.setpoint_kw() == -100                  # F took over once the lock was gone
    assert "G" in stored                                         # nothing the utility sent was lost


# ── Utility input: the mRID (M5a) ────────────────────────────────────────

def _missing_mrid_list():
    """derc() cannot omit <mRID>: two DERControls without one, plus a good one."""
    body = ('<DERControl><interval><duration>60</duration><start>{start}</start></interval>'
            '<DERControlBase><opModFixedW><value>{value}</value><multiplier>0</multiplier>'
            '</opModFixedW></DERControlBase></DERControl>')
    good = derc(dict(mrid="OK", start=T, duration=60, value=20_000))
    return good.replace("<DERControl>", body.format(start=T + 100, value=1000)
                        + body.format(start=T + 500, value=-2000) + "<DERControl>", 1)


def test_events_without_an_mrid_are_rejected_not_merged(tmp_path):
    """M5a: two DERControls without an mRID were both stored as 'unknown',
    so the second overwrote the first; neither can be acknowledged or
    cancelled by the server. They are now refused and audited."""
    audit = AuditLog(db_path=tmp_path / "audit.db")

    async def run():
        driver, state = await make_driver(FakeBMS(), audit_log=audit)
        sched = Scheduler({}, state, driver, audit_log=audit)
        client = IEEEClient({"ieee_server_url": "http://127.0.0.1:9", "ieee_poll_interval_sec": 5,
                             "offline_timeout_sec": 30}, state, sched, audit_log=audit)
        return sched, [e["mrid"] for e in client._parse_and_dispatch(_missing_mrid_list())]
    sched, accepted = asyncio.run(run())
    assert accepted == ["OK"]
    assert [e["mrid"] for e in sched.get_upcoming(10)] == ["OK"]
    rejected = audit_rows(audit, "schedule_rejected")
    assert [(r["outcome"], "no mRID" in r["detail"]) for r in rejected] == [("invalid", True)] * 2


def test_the_mrid_cannot_redirect_the_acknowledgement(tmp_path):
    """The mRID comes from the utility's data and was pasted into the
    DERControlResponse URL: 'a/../../admin?x=1' sent the POST to /derp/admin.
    Since step 2 the response goes to the event's replyTo, and the mRID
    travels only in the body, as its subject."""
    mrid = "a/../../admin?x=1"

    async def run():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        driver, state = await make_driver(FakeBMS(), audit_log=audit)
        sched = Scheduler({}, state, driver, audit_log=audit)
        util = await Utility(derc(dict(mrid=mrid, start=T, duration=600, value=20_000,
                                       response_required="01"))).start()
        client = IEEEClient({"ieee_server_url": util.url, "ieee_poll_interval_sec": 5,
                             "offline_timeout_sec": 30}, state, sched, audit_log=audit)
        async with aiohttp.ClientSession() as http:
            await client._poll(http)
            await client.send_responses(http)
        await util.stop()
        return util, audit
    util, audit = asyncio.run(run())
    assert util.response_paths == ["/rsps/1/rsp"]                # the replyTo, whatever the mRID
    assert [m for m, _ in util.responses] == [mrid]              # the subject, intact
    assert [r["outcome"] for r in audit_rows(audit, "ieee_response")] == ["sent"]


# ── Stopping (N1) ────────────────────────────────────────────────────────

@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGBREAK"), reason="SIGBREAK is Windows-only")
def test_ctrl_break_stops_the_gateway_through_safe_idle(tmp_path, monkeypatch):
    """N1: Ctrl+Break (the console stop a Windows service wrapper can send
    to one process group) killed the gateway at once with the battery still
    at its setpoint (probe: +200 kW, exit 0xC000013A). Runs the real
    capstone.__main__.run() and delivers SIGBREAK through the handler it
    registered, from the main thread, as Windows does."""
    import signal
    from capstone.modbus_driver import ModbusDriver

    monkeypatch.chdir(tmp_path)
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    fake = FakeBMS()

    class Driver(ModbusDriver):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._client = fake
    monkeypatch.setattr(main_mod, "ModbusDriver", Driver)

    async def run():
        util = await Utility(derc(dict(mrid="E", start=int(time.time()) - 5, duration=600, value=200_000))).start()
        monkeypatch.setenv("IEEE_SERVER_URL", util.url)
        gateway = asyncio.create_task(main_mod.run())
        for _ in range(100):
            await asyncio.sleep(0.1)
            if fake.setpoint_kw() == 200:
                break
        running = fake.setpoint_kw()
        handlers[signal.SIGBREAK](signal.SIGBREAK, None)         # what the OS does on Ctrl+Break
        await asyncio.wait_for(gateway, timeout=10)              # returns: a graceful stop, not a cancellation
        await util.stop()
        return running
    running = asyncio.run(run())
    assert running == 200
    assert fake.setpoint_kw() == 0 and fake.connected is False   # Safe Idle, then the link closed
    stopped = audit_rows(AuditLog(db_path=tmp_path / "schedule_store" / "audit_log.db"), "gateway")[-1]
    assert (stopped["outcome"], stopped["detail"]) == ("stopped", "stopped: battery at Safe Idle (0 kW), confirmed by the BMS")


# ── The utility simulator (H9b, M8) ──────────────────────────────────────

def _grid_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location("grid_server_under_test", ROOT / "simulators" / "fake_grid_server.py")
    grid = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grid)
    return grid


@pytest.mark.parametrize("body", [
    {"power_kw": "nan"}, {"power_kw": "inf"}, {"power_kw": "-Infinity"}, {"power_kw": "lots"},
    {"power_kw": 100, "duration_sec": "an hour"},
])
def test_a_bad_simulator_request_is_refused_and_the_feed_stays_up(body):
    """H9b: one {"power_kw": "nan"} made every later /derp/1/derc fail with
    HTTP 500, which the gateway reads as the utility being down."""
    client = _grid_server().app.test_client()
    refused = client.post("/simulate/charge", json=body)
    assert refused.status_code == 400
    assert [client.get("/derp/1/derc").status_code for _ in range(3)] == [200, 200, 200]


def test_markup_in_an_mrid_neither_breaks_the_feed_nor_reaches_the_dashboard_unescaped():
    """M8: an mRID such as <img onerror=...> was served inside the XML feed
    unescaped (so every poll failed to parse) and concatenated into the
    dashboard's innerHTML (script in the operator's logged-in session)."""
    from xml.sax.saxutils import escape
    import xml.etree.ElementTree as ET
    grid = _grid_server()
    client = grid.app.test_client()
    mrid = "<img src=x onerror=\"fetch('/dashboard/send',{method:'POST'})\">"
    posted = client.post("/simulate/raw", data=derc(dict(mrid=escape(mrid), start=int(time.time()), duration=600,
                                                         value=1000)), content_type="application/xml")
    assert posted.status_code == 200
    feed = client.get("/derp/1/derc").get_data(as_text=True)
    assert ET.fromstring(feed).findtext("{*}DERControl/{*}mRID") == mrid      # valid XML, value intact
    assert "esc(e.mrid)" in grid.DASHBOARD_TEMPLATE and "+ e.mrid +" not in grid.DASHBOARD_TEMPLATE


def test_the_acknowledgement_is_well_formed_whatever_the_mrid(tmp_path):
    """The mRID was pasted into the DERControlResponse XML unescaped: an
    mRID containing '&' or '<' (sent escaped by the utility) made the
    acknowledgement malformed."""
    import xml.etree.ElementTree as ET
    from xml.sax.saxutils import escape
    mrid = "E&<1>"

    async def run():
        driver, state = await make_driver(FakeBMS())
        sched = Scheduler({}, state, driver)
        util = await Utility(derc(dict(mrid=escape(mrid), start=T, duration=600, value=20_000,
                                       response_required="01"))).start()
        client = IEEEClient({"ieee_server_url": util.url, "ieee_poll_interval_sec": 5,
                             "offline_timeout_sec": 30}, state, sched)
        async with aiohttp.ClientSession() as http:
            await client._poll(http)
            await client.send_responses(http)
        await util.stop()
        return util
    util = asyncio.run(run())
    [(subject, body)] = util.responses
    assert subject == mrid
    assert ET.fromstring(body).findtext("{*}subject") == mrid      # since step 2: the schema's subject
