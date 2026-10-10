"""
tests/test_ieee_responses.py
Step 2 regression tests — the DERControlResponses the gateway sends back.

IEEE 2030.5: a DERControl asks for responses with replyTo (where) and
responseRequired (bit 0: received; bit 1: specific responses). Each status
goes out once, when it happens (received, started, completed, cancelled,
superseded, or a refusal: 252 not applicable, 253 invalid, 254 received
after it expired), as a DERControlResponse carrying endDeviceLFDI, status
and subject, never again on later polls (audit-harness H10a, H10b, M5d;
D3). Also: opModConnect / opModEnergize false mean 0 kW, and an event with
a control the gateway cannot execute is refused as a whole.

The real IEEEClient, Scheduler and ModbusDriver run against FakeBMS and an
in-process utility; responses are delivered with send_responses(), the
step response_loop() repeats.
"""

import asyncio
import base64
import hashlib
import importlib.util
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

import capstone.__main__ as main_mod
import capstone.ieee_client as client_mod
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient, _response_body, device_lfdi
from capstone.modbus_driver import _decode_int32
from capstone.scheduler import Scheduler
from fakes import AC_SETPOINT, FakeBMS, Utility, audit_rows, make_driver, target_w, until

ROOT = Path(__file__).resolve().parents[1]
T = 1_800_000_000                                  # 2027-01-15 08:00:00 UTC
SETTINGS = ("IEEE_SERVER_URL", "IEEE_POLL_INTERVAL_SEC", "OFFLINE_TIMEOUT_SEC", "MODBUS_HOST", "MODBUS_PORT",
            "TLS_CA_CERT", "TLS_CLIENT_CERT", "TLS_CLIENT_KEY", "ALLOW_INSECURE_TRANSPORT", "DEVICE_LFDI",
            "DER_SET_MAX_W", "DER_SET_MAX_CHARGE_RATE_W", "DER_SET_MAX_DISCHARGE_RATE_W")


def lfdi_of(path):
    """The LFDI of a PEM certificate, computed here independently of the
    gateway: SHA-256 of its DER bytes, first 160 bits, as hex."""
    lines = Path(path).read_text().splitlines()
    der = base64.b64decode("".join(line for line in lines if line and not line.startswith("-----")))
    return hashlib.sha256(der).hexdigest()[:40].upper()


DEV_LFDI = lfdi_of(ROOT / "certs" / "client.crt")


def event(mrid, base=None, start=T, duration=600, respond="03", reply_to="/rsps/1/rsp", status=None):
    """One DERControl. respond=None: no replyTo / responseRequired at all."""
    attributes = ("" if respond is None else
                  f' replyTo="{reply_to}" responseRequired="{respond}"' if reply_to is not None else
                  f' responseRequired="{respond}"')
    event_status = ("" if status is None else
                    f"<EventStatus><currentStatus>{status}</currentStatus><dateTime>{start}</dateTime>"
                    f"<potentiallySuperseded>false</potentiallySuperseded></EventStatus>")
    return (f"<DERControl{attributes}><mRID>{mrid}</mRID>{event_status}<interval><duration>{duration}"
            f"</duration><start>{start}</start></interval><DERControlBase>"
            f"{target_w(20_000) if base is None else base}</DERControlBase></DERControl>")


def listing(*events):
    return (f'<DERControlList xmlns="urn:ieee:std:2030.5:ns" all="{len(events)}" results="{len(events)}">'
            f'{"".join(events)}</DERControlList>')


async def rig(tmp_path, util, fake=None, **config):
    audit = AuditLog(db_path=tmp_path / "audit.db")
    driver, state = await make_driver(fake or FakeBMS(), audit_log=audit)
    sched = Scheduler({}, state, driver, audit_log=audit)
    config = {"ieee_server_url": util.url, "ieee_poll_interval_sec": 5, "offline_timeout_sec": 30, **config}
    client = IEEEClient(config, state, sched, audit_log=audit)
    return SimpleNamespace(fake=driver._client, driver=driver, state=state, sched=sched, client=client,
                           audit=audit, config=config, http=aiohttp.ClientSession())


async def exchange(g, now=None):
    """One poll, then deliver the responses that are due."""
    await g.client._poll(g.http)
    await g.client.send_responses(g.http, now)


async def one_exchange(g, util):
    await exchange(g)


async def poll_then_clock(g):
    """One poll; returns the time right after it, so the responses it
    queued are due at that time (they are stamped when queued)."""
    await g.client._poll(g.http)
    return time.time()


def run_with(tmp_path, xml, scenario, fake=None, **config):
    """Run `scenario(g, util)` against a utility serving `xml`."""
    async def run():
        util = await Utility(xml).start()
        g = await rig(tmp_path, util, fake, **config)
        try:
            await scenario(g, util)
        finally:
            await g.http.close()
            await util.stop()
        return g, util
    return asyncio.run(run())


def register(fake):
    """41009 as the BMS holds it: P > 0 = discharge."""
    return _decode_int32([fake.hr.get(AC_SETPOINT, 0), fake.hr.get(AC_SETPOINT + 1, 0)])


# ── The lifecycle, once each ─────────────────────────────────────────────

def test_the_lifecycle_is_reported_once_each_in_order(tmp_path):
    async def scenario(g, util):
        await exchange(g)                                   # received
        for _ in range(3):
            await exchange(g)                               # listed again: nothing new
        await g.sched.step(T)                               # the BMS confirms the setpoint: started
        await g.client.send_responses(g.http)
        await g.sched.step(T + 300)
        await exchange(g)
        await g.sched.step(T + 600)                         # the window ends: completed
        await exchange(g)
    g, util = run_with(tmp_path, listing(event("E")), scenario)
    assert util.statuses() == [1, 2, 3]
    assert util.response_paths == ["/rsps/1/rsp"] * 3
    assert util.response_types == ["application/sep+xml"] * 3
    body = ET.fromstring(util.responses[0][1])
    assert body.tag == "{urn:ieee:std:2030.5:ns}DERControlResponse"
    assert [child.tag.split("}")[1] for child in body] == ["createdDateTime", "endDeviceLFDI", "status", "subject"]
    assert body.findtext("{*}endDeviceLFDI") == DEV_LFDI
    assert body.findtext("{*}subject") == "E"
    assert abs(int(body.findtext("{*}createdDateTime")) - time.time()) < 60
    assert [r["outcome"] for r in audit_rows(g.audit, "ieee_response")] == ["sent"] * 3


@pytest.mark.parametrize("respond, reply_to, statuses", [
    (None, None, []),                   # no responseRequired, no replyTo
    ("00", "/rsps/1/rsp", []),
    ("03", None, []),                   # nowhere to send them
    ("01", "/rsps/1/rsp", [1]),         # bit 0 only: received
    ("02", "/rsps/1/rsp", [2, 3]),      # bit 1 only: the specific responses
    ("zz", "/rsps/1/rsp", []),          # not hex: asks for nothing
])
def test_only_what_the_event_asks_for_is_sent(tmp_path, respond, reply_to, statuses):
    async def scenario(g, util):
        await exchange(g)
        await g.sched.step(T)
        await g.sched.step(T + 600)
        await exchange(g)
    g, util = run_with(tmp_path, listing(event("E", respond=respond, reply_to=reply_to)), scenario)
    assert util.statuses() == statuses


def test_superseded_and_cancelled_events_are_reported(tmp_path):
    async def scenario(g, util):
        await exchange(g)
        await g.sched.step(T)                               # A takes authority
        await g.sched.step(T + 100)                         # B starts later and overlaps: A superseded
        util.xml = listing(event("A"), event("B", start=T + 100), event("C", start=T + 5000, status=2))
        await exchange(g)                                   # the server cancels C
    xml = listing(event("A"), event("B", start=T + 100), event("C", start=T + 5000))
    g, util = run_with(tmp_path, xml, scenario)
    assert util.statuses("A") == [1, 2, 7]
    assert util.statuses("B") == [1, 2]
    assert util.statuses("C") == [1, 6]


def test_an_event_already_cancelled_on_first_sight_is_received_and_cancelled(tmp_path):
    g, util = run_with(tmp_path, listing(event("C", status=2)), one_exchange)
    assert util.statuses("C") == [1, 6]


# ── Refusals: 252, 253, 254 ──────────────────────────────────────────────

def test_refused_events_are_answered_with_the_reason_once(tmp_path):
    now = int(time.time())
    xml = listing(
        event("NA", "<opModMaxLimW>5000</opModMaxLimW>"),                              # not executable
        event("PCT", "<opModFixedW>5000</opModFixedW>"),                               # no DER ratings
        event("BAD", "<opModTargetW><multiplier>0</multiplier><value>nan</value></opModTargetW>"),
        event("TWO", target_w(1_000) + target_w(2_000)),                                # a control twice
        event("OLD", start=now - 7200),                                                 # already over
        event("OK"))

    async def scenario(g, util):
        for _ in range(3):
            await exchange(g)
    g, util = run_with(tmp_path, xml, scenario)
    assert {mrid: util.statuses(mrid) for mrid in ("NA", "PCT", "BAD", "TWO", "OLD", "OK")} == {
        "NA": [252], "PCT": [252], "BAD": [253], "TWO": [253], "OLD": [254], "OK": [1]}


def test_a_setpoint_the_bms_refuses_is_answered_not_applicable(tmp_path):
    fake = FakeBMS()
    fake.reject = lambda address, words: 3 if address == AC_SETPOINT and words != [0, 0] else None

    async def scenario(g, util):
        await exchange(g)
        await g.sched.step(T)
        await g.client.send_responses(g.http)
    g, util = run_with(tmp_path, listing(event("E")), scenario, fake)
    assert util.statuses() == [1, 252]


# ── Other controls in the event ──────────────────────────────────────────

@pytest.mark.parametrize("base, register_kw, control", [
    (target_w(-50_000) + "<opModConnect>false</opModConnect>", 0, "opModConnect false: 0 kW"),
    (target_w(-50_000) + "<opModEnergize>0</opModEnergize>", 0, "opModEnergize false: 0 kW"),
    ("<opModConnect>false</opModConnect>", 0, "opModConnect false: 0 kW"),          # no setpoint needed
    (target_w(-50_000) + "<opModConnect>true</opModConnect>", 50, "opModTargetW 50000 x 10^0 W"),
    (target_w(-50_000) + "<rampTms>500</rampTms>", 50,
     "opModTargetW 50000 x 10^0 W; rampTms not applied (the BMS ramps at its own rate)"),
])
def test_disconnect_and_de_energize_mean_0_kw(tmp_path, base, register_kw, control):
    async def scenario(g, util):
        await exchange(g)
        await g.sched.step(T)
    g, util = run_with(tmp_path, listing(event("E", base)), scenario)
    assert (g.state.authority["authority"], g.state.authority["mrid"]) == ("approved_schedule", "E")
    assert g.state.authority["desired_kw"] == -register_kw     # the event itself asks for it
    assert register(g.fake) == register_kw
    [received] = audit_rows(g.audit, "schedule_received")
    assert received["detail"].endswith(f" control={control}")


@pytest.mark.parametrize("base, status", [
    (target_w(-50_000) + '<opModVoltVar href="/derp/1/dc/1"/>', 252),
    (target_w(-50_000) + "<opModMaxLimW>5000</opModMaxLimW>", 252),
    (target_w(-50_000) + "<opModConnect>maybe</opModConnect>", 253),
])
def test_an_event_with_a_control_the_gateway_cannot_honour_is_refused_whole(tmp_path, base, status):
    async def scenario(g, util):
        await exchange(g)
        await g.sched.step(T)
    g, util = run_with(tmp_path, listing(event("E", base)), scenario)
    assert util.statuses() == [status]
    assert g.fake.setpoint_history() == [0]                     # its setpoint never reached the BMS
    [rejected] = audit_rows(g.audit, "schedule_rejected")
    assert "E" in rejected["detail"]


# ── Delivery ─────────────────────────────────────────────────────────────

def test_an_events_responses_are_delivered_in_order_after_a_failure(tmp_path):
    async def scenario(g, util):
        util.response_status = 500
        t0 = await poll_then_clock(g)
        await g.client.send_responses(g.http, t0)           # received: HTTP 500, retry at t0 + 5
        await g.sched.step(T)                               # started is queued behind it
        util.response_status = 201
        await g.client.send_responses(g.http, t0 + 1)       # neither may go yet
        assert util.statuses() == [1]
        await g.client.send_responses(g.http, t0 + 5)       # received, then started
        await g.client.send_responses(g.http, t0 + 6)       # nothing left
    g, util = run_with(tmp_path, listing(event("E")), scenario)
    assert util.statuses() == [1, 1, 2]


def test_a_response_the_server_refuses_outright_is_not_retried(tmp_path):
    async def scenario(g, util):
        util.response_status = 400
        await exchange(g)
        for later in (60, 600, 3600):
            await g.client.send_responses(g.http, time.time() + later)
    g, util = run_with(tmp_path, listing(event("E", respond="01")), scenario)
    assert util.statuses() == [1]
    [row] = audit_rows(g.audit, "ieee_response")
    assert row["outcome"] == "rejected"
    assert row["detail"] == "DERControlResponse EventReceived for E: HTTP 400 (status 1 to /rsps/1/rsp); not retried"


def test_an_undelivered_response_is_given_up_after_a_day(tmp_path):
    async def scenario(g, util):
        util.response_status = 503
        t0 = await poll_then_clock(g)
        await g.client.send_responses(g.http, t0)
        await g.client.send_responses(g.http, t0 + 24 * 3600 + 1)
        await g.client.send_responses(g.http, t0 + 48 * 3600)
    g, util = run_with(tmp_path, listing(event("E", respond="01")), scenario)
    assert util.statuses() == [1, 1]
    assert [r["outcome"] for r in audit_rows(g.audit, "ieee_response")] == ["rejected", "abandoned"]
    assert audit_rows(g.audit, "ieee_response")[-1]["detail"].endswith("undelivered for 24 h, given up")


@pytest.mark.parametrize("reply_to, delivered_to", [
    ("/rsps/9/rsp", "/rsps/9/rsp"),
    ("{url}/rsps/2/rsp", "/rsps/2/rsp"),                    # absolute, on the same server
    ("http://utility.example/rsps/1/rsp", None),            # another host
    ("http://127.0.0.1:9/rsps/1/rsp", None),                # this host, another port
    ("ftp://127.0.0.1/rsps", None),
])
def test_responses_only_ever_go_to_the_utility_server(tmp_path, reply_to, delivered_to):
    async def scenario(g, util):
        util.xml = listing(event("E", respond="01", reply_to=reply_to.format(url=util.url)))
        await exchange(g)
    g, util = run_with(tmp_path, listing(), scenario)
    assert util.response_paths == ([delivered_to] if delivered_to else [])
    if delivered_to is None:
        [row] = audit_rows(g.audit, "ieee_response")
        assert row["outcome"] == "abandoned" and "is not on the utility server" in row["detail"]


def test_responses_survive_a_restart_and_are_never_repeated(tmp_path):
    async def scenario(g, util):
        util.response_status = 500
        t0 = await poll_then_clock(g)
        await g.client.send_responses(g.http, t0)           # attempt 1 fails
        util.response_status = 201
        for later in (5, 200):                              # two restarts: a new scheduler and client
            sched = Scheduler({}, g.state, g.driver, audit_log=g.audit)
            client = IEEEClient(g.config, g.state, sched, audit_log=g.audit)
            await client._poll(g.http)                      # listed again: nothing new queued
            await client.send_responses(g.http, t0 + later)
    g, util = run_with(tmp_path, listing(event("E", respond="01")), scenario)
    assert util.statuses() == [1, 1]                        # the failed attempt, then one delivery


def test_steady_polls_send_nothing_however_many_events_are_listed(tmp_path):
    """D3: every listed event was acknowledged again, one POST after another,
    inside every poll. Now a poll makes no POST at all."""
    xml = listing(*(event(f"E{i}", start=T + 60 * i, duration=60, respond="01") for i in range(50)))

    async def scenario(g, util):
        await g.client._poll(g.http)
        assert util.responses == []                         # the poll itself sends nothing
        await g.client.send_responses(g.http)
        for _ in range(5):
            await exchange(g)
    g, util = run_with(tmp_path, xml, scenario)
    assert len(util.responses) == 50


def test_the_response_loop_delivers_without_being_asked(tmp_path):
    async def scenario(g, util):
        loop = asyncio.create_task(g.client.response_loop())
        try:
            await g.client._poll(g.http)
            await until(lambda: util.responses, timeout=5)
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
    g, util = run_with(tmp_path, listing(event("E", respond="01")), scenario)
    assert util.statuses() == [1]


# ── The LFDI ─────────────────────────────────────────────────────────────

def test_the_lfdi_is_derived_from_the_client_certificate():
    config = {"ieee_server_url": "https://utility.example", "tls_client_cert": str(ROOT / "certs" / "client.crt")}
    assert device_lfdi(config) == (DEV_LFDI, "TLS client certificate")
    assert device_lfdi({**config, "device_lfdi": DEV_LFDI.lower()})[0] == DEV_LFDI
    with pytest.raises(ValueError, match="is not the LFDI of TLS_CLIENT_CERT"):
        device_lfdi({**config, "device_lfdi": "0" * 40})


def test_the_lfdi_comes_from_device_lfdi_or_for_the_local_simulator_from_the_development_certificate():
    assert device_lfdi({"ieee_server_url": "http://utility.lab", "device_lfdi": "ab" * 20}) == \
        ("AB" * 20, "DEVICE_LFDI")
    assert device_lfdi({"ieee_server_url": "http://127.0.0.1:5001"})[0] == DEV_LFDI
    assert device_lfdi({"ieee_server_url": "http://utility.lab"}) == \
        (None, "none: set DEVICE_LFDI or a TLS client certificate")


@pytest.mark.parametrize("value", ["abc", "G" * 40, "A" * 39, "A" * 41])
def test_a_malformed_device_lfdi_is_refused_at_startup(monkeypatch, value):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEVICE_LFDI", value)
    with pytest.raises(main_mod.ConfigError, match="DEVICE_LFDI must be 40 hex digits"):
        main_mod.load_config()


def test_the_startup_banner_says_which_lfdi_responses_carry(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    assert main_mod.load_config()["lfdi_summary"] == f"{DEV_LFDI} (development certificate (local simulator only))"


def test_without_an_lfdi_responses_are_given_up_not_sent_without_one(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "DEV_CERTIFICATE", tmp_path / "missing.crt")
    g, util = run_with(tmp_path, listing(event("E", respond="01")), one_exchange)
    assert util.responses == []
    [row] = audit_rows(g.audit, "ieee_response")
    assert row["outcome"] == "abandoned" and "no LFDI" in row["detail"]


# ── The utility simulator ────────────────────────────────────────────────

def _grid_server(monkeypatch):
    for name in ("SIM_POWER_CONTROL",) + SETTINGS:
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("grid_server_under_test", ROOT / "simulators" / "fake_grid_server.py")
    grid = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grid)
    return grid


def test_the_simulator_asks_for_responses_and_records_them(monkeypatch):
    client = _grid_server(monkeypatch).app.test_client()
    posted = client.post("/simulate/charge", json={"power_kw": 100, "duration_sec": 600}).get_json()
    mrid = posted["event"]["mrid"]
    control = ET.fromstring(client.get("/derp/1/derc").get_data(as_text=True)).find("{*}DERControl")
    assert (control.get("replyTo"), control.get("responseRequired")) == ("/rsps/1/rsp", "03")
    for status in (1, 2):
        answered = client.post("/rsps/1/rsp", data=_response_body(mrid, status, DEV_LFDI, time.time()),
                               content_type="application/sep+xml")
        assert answered.status_code == 201 and answered.headers["Location"].startswith("/rsps/1/rsp/")
    assert client.get("/status").get_json()["acknowledgements"][mrid]["statuses"] == ["1", "2"]


def test_the_simulator_refuses_a_response_without_the_required_fields(monkeypatch):
    client = _grid_server(monkeypatch).app.test_client()
    refused = client.post("/rsps/1/rsp", content_type="application/sep+xml",
                          data='<DERControlResponse xmlns="urn:ieee:std:2030.5:ns"><status>1</status>'
                               '<subject>E</subject></DERControlResponse>')
    assert refused.status_code == 400 and "endDeviceLFDI" in refused.get_json()["message"]
    assert client.get("/status").get_json()["acknowledgements"] == {}
