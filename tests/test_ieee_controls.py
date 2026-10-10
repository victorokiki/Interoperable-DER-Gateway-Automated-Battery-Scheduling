"""
tests/test_ieee_controls.py
Step 1 regression tests — the active-power setpoint as IEEE 2030.5 sends it.

opModFixedW is a SignedPerCent: hundredths of a percent, -10000..10000, of
the DER's setMaxChargeRateW when negative and of setMaxDischargeRateW (or
setMaxW) when positive. opModTargetW is an ActivePower in watts, the
"target output power". Both count the power the DER puts out, so + =
discharge: the opposite of the gateway's own convention (+ = charge).

These tests drive the real IEEEClient, Scheduler and ModbusDriver against
FakeBMS and read the Endurium setpoint register itself (41009, P > 0 =
discharge, SPC100644 p.31), so a sign error anywhere between the utility
and the battery shows up.
"""

import asyncio
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest

import capstone.__main__ as main_mod
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient
from capstone.modbus_driver import INT32_MAX, _decode_int32
from capstone.scheduler import Scheduler
from capstone.state import BatteryMode, GatewayState
from fakes import AC_SETPOINT, FakeBMS, Utility, audit_rows, make_driver, target_w

ROOT = Path(__file__).resolve().parents[1]
T = 1_800_000_000
CLIENT_CFG = {"ieee_server_url": "http://127.0.0.1:9", "ieee_poll_interval_sec": 5,
              "offline_timeout_sec": 30}
RATINGS = {"der_set_max_w": 200_000, "der_set_max_charge_rate_w": 80_000,
           "der_set_max_discharge_rate_w": 150_000}
SET_MAX_W_ONLY = {"der_set_max_w": 200_000}
DER_SETTINGS = ("DER_SET_MAX_W", "DER_SET_MAX_CHARGE_RATE_W", "DER_SET_MAX_DISCHARGE_RATE_W")
SETTINGS = ("IEEE_SERVER_URL", "IEEE_POLL_INTERVAL_SEC", "OFFLINE_TIMEOUT_SEC", "MODBUS_HOST", "MODBUS_PORT",
            "TLS_CA_CERT", "TLS_CLIENT_CERT", "TLS_CLIENT_KEY", "ALLOW_INSECURE_TRANSPORT",
            "SIM_POWER_CONTROL") + DER_SETTINGS


@pytest.fixture
def env(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def control_list(*controls):
    """A DERControlList with one event per DERControlBase body: mRIDs E0,
    E1, ..., all running from T for 600 s."""
    parts = "".join(f"<DERControl><mRID>E{i}</mRID><interval><duration>600</duration><start>{T}</start>"
                    f"</interval><DERControlBase>{control}</DERControlBase></DERControl>"
                    for i, control in enumerate(controls))
    return (f'<DERControlList xmlns="urn:ieee:std:2030.5:ns" all="{len(controls)}" results="{len(controls)}">'
            f'{parts}</DERControlList>')


async def rig(tmp_path, ratings, fake=None, url="http://127.0.0.1:9"):
    audit = AuditLog(db_path=tmp_path / "audit.db")
    driver, state = await make_driver(fake or FakeBMS(), audit_log=audit)
    sched = Scheduler({}, state, driver, audit_log=audit)
    client = IEEEClient({**CLIENT_CFG, "ieee_server_url": url, **ratings}, state, sched, audit_log=audit)
    return SimpleNamespace(fake=driver._client, state=state, sched=sched, client=client, audit=audit)


def receive_and_run(tmp_path, ratings, xml, fake=None):
    """The gateway receives one DERControlList and runs it for two ticks."""
    async def run():
        r = await rig(tmp_path, ratings, fake)
        r.accepted = [e["mrid"] for e in r.client._parse_and_dispatch(xml)]
        for now in (T, T + 1):
            await r.sched.step(now)
        return r
    return asyncio.run(run())


def register(fake):
    """41009 as the BMS holds it: P > 0 = discharge."""
    return _decode_int32([fake.hr.get(AC_SETPOINT, 0), fake.hr.get(AC_SETPOINT + 1, 0)])


# ── From the utility's number to the BMS register ────────────────────────

@pytest.mark.parametrize("ratings, control, register_kw", [
    (RATINGS, "<opModFixedW>10000</opModFixedW>", 150),     # 100 % of setMaxDischargeRateW
    (RATINGS, "<opModFixedW>8800</opModFixedW>", 132),      # CSIP's example value: 88 %
    (RATINGS, "<opModFixedW>-5000</opModFixedW>", -40),     # 50 % of setMaxChargeRateW: charging
    (RATINGS, "<opModFixedW>-10000</opModFixedW>", -80),
    (RATINGS, "<opModFixedW>0</opModFixedW>", 0),
    (RATINGS, "<opModFixedW>1</opModFixedW>", 0),           # 0.01 % = 15 W, below the register's 1 kW step
    (SET_MAX_W_ONLY, "<opModFixedW>2500</opModFixedW>", 50),    # without the storage rates: % of setMaxW
    (SET_MAX_W_ONLY, "<opModFixedW>-2500</opModFixedW>", -50),
    (RATINGS, "<opModTargetW><multiplier>3</multiplier><value>120</value></opModTargetW>", 120),
    (RATINGS, "<opModTargetW><multiplier>3</multiplier><value>-45</value></opModTargetW>", -45),
    ({}, "<opModTargetW><multiplier>0</multiplier><value>-30000</value></opModTargetW>", -30),
])
def test_the_utility_setpoint_reaches_the_bms_with_the_ieee_sign(tmp_path, ratings, control, register_kw):
    r = receive_and_run(tmp_path, ratings, control_list(control))
    assert r.accepted == ["E0"]
    assert register(r.fake) == register_kw
    assert r.state.mode == (BatteryMode.DISCHARGE if register_kw > 0 else
                            BatteryMode.CHARGE if register_kw < 0 else BatteryMode.IDLE)
    assert audit_rows(r.audit, "schedule_rejected") == []


def test_the_largest_rating_at_100_percent_reaches_the_register_unwrapped(tmp_path):
    fake = FakeBMS()
    fake.set_limits(charge_kw=INT32_MAX, discharge_kw=INT32_MAX)
    r = receive_and_run(tmp_path, {"der_set_max_w": INT32_MAX * 1000},
                        control_list("<opModFixedW>-10000</opModFixedW>"), fake)
    assert register(r.fake) == -INT32_MAX


def test_the_audit_trail_records_the_percentage_and_the_rating_it_refers_to(tmp_path):
    r = receive_and_run(tmp_path, RATINGS, control_list("<opModFixedW>-5000</opModFixedW>",
                                                        "<opModFixedW>8800</opModFixedW>"))
    first, second = audit_rows(r.audit, "schedule_received")
    assert "mrid=E0 power_kw=+40.0 " in first["detail"]
    assert first["detail"].endswith(" control=opModFixedW -50.00% of setMaxChargeRateW 80000 W")
    assert "mrid=E1 power_kw=-132.0 " in second["detail"]
    assert second["detail"].endswith(" control=opModFixedW +88.00% of setMaxDischargeRateW 150000 W")


def test_without_its_own_rate_a_percentage_is_audited_against_setMaxW(tmp_path):
    r = receive_and_run(tmp_path, SET_MAX_W_ONLY, control_list("<opModFixedW>-2500</opModFixedW>"))
    [received] = audit_rows(r.audit, "schedule_received")
    assert received["detail"].endswith(" control=opModFixedW -25.00% of setMaxW 200000 W")


def test_a_csip_percentage_event_discharges_the_battery_through_a_real_poll(tmp_path):
    async def run():
        util = await Utility(control_list("<opModFixedW>8800</opModFixedW>")).start()
        r = await rig(tmp_path, {"der_set_max_w": 250_000}, url=util.url)
        async with aiohttp.ClientSession() as http:
            await r.client._poll(http)
        await r.sched.step(T)
        await util.stop()
        return r
    r = asyncio.run(run())
    assert register(r.fake) == 220                              # 88 % of 250 kW, discharging


# ── Refused, audited, and the rest of the list still runs ────────────────

def test_without_der_ratings_a_percentage_is_refused_and_watts_still_run(tmp_path):
    r = receive_and_run(tmp_path, {}, control_list("<opModFixedW>5000</opModFixedW>", target_w(20_000)))
    assert r.accepted == ["E1"]
    assert register(r.fake) == -20                               # E1: 20 kW charge
    [row] = audit_rows(r.audit, "schedule_rejected")
    assert row["outcome"] == "unsupported"
    assert "E0" in row["detail"] and "DER_SET_MAX_W (setMaxW) is not configured" in row["detail"]


@pytest.mark.parametrize("control", [
    "<opModFixedW>10001</opModFixedW>",
    "<opModFixedW>-10001</opModFixedW>",
    "<opModFixedW>12.5</opModFixedW>",
    "<opModFixedW>nan</opModFixedW>",
    "<opModFixedW></opModFixedW>",
    "<opModFixedW><value>5000</value><multiplier>0</multiplier></opModFixedW>",   # the pre-standard form
    "<opModTargetW><multiplier>0</multiplier></opModTargetW>",                     # no value: not 0 kW
    "<opModFixedW>5000</opModFixedW>" + target_w(1_000),                           # both: ambiguous
])
def test_a_malformed_or_ambiguous_setpoint_is_refused_and_the_rest_still_runs(tmp_path, control):
    r = receive_and_run(tmp_path, RATINGS, control_list(control, target_w(20_000)))
    assert r.accepted == ["E1"]
    assert register(r.fake) == -20
    [row] = audit_rows(r.audit, "schedule_rejected")
    assert row["outcome"] == "invalid" and "E0" in row["detail"]


@pytest.mark.parametrize("control", [
    "<opModFixedW><value>5000</value><multiplier>0</multiplier></opModFixedW>",
    "<opModFixedW>5000<value>5000</value></opModFixedW>",       # a number AND the old elements
])
def test_the_pre_standard_watts_form_is_refused_saying_why(tmp_path, control):
    """This project's simulator used to send <opModFixedW><value>W</value>
    <multiplier>..</multiplier></opModFixedW>, which is not IEEE 2030.5."""
    r = receive_and_run(tmp_path, RATINGS, control_list(control))
    assert r.accepted == []
    [row] = audit_rows(r.audit, "schedule_rejected")
    assert "not <value>/<multiplier> elements" in row["detail"]


# ── Configuration ────────────────────────────────────────────────────────

def test_der_ratings_are_read_from_the_environment(env):
    env.setenv("DER_SET_MAX_W", "200000")
    env.setenv("DER_SET_MAX_CHARGE_RATE_W", "80000")
    config = main_mod.load_config()
    assert [config[key] for key in ("der_set_max_w", "der_set_max_charge_rate_w",
                                    "der_set_max_discharge_rate_w")] == [200_000, 80_000, None]
    assert config["der_ratings_summary"] == ("setMaxW 200 kW; opModFixedW charges against setMaxChargeRateW "
                                             "80 kW, discharges against setMaxW 200 kW")


def test_without_der_ratings_the_gateway_still_starts(env):
    config = main_mod.load_config()
    assert config["der_ratings_summary"] == "not configured (DER_SET_MAX_W): opModFixedW percentages are refused"


@pytest.mark.parametrize("settings, message", [
    ({"DER_SET_MAX_W": "0"}, "DER_SET_MAX_W (setMaxW) must be a whole number of watts from 1 to"),
    ({"DER_SET_MAX_W": "-5"}, "DER_SET_MAX_W (setMaxW) must be a whole number of watts from 1 to"),
    ({"DER_SET_MAX_W": str(INT32_MAX * 1000 + 1)}, "must be a whole number of watts from 1 to 2147483647000"),
    ({"DER_SET_MAX_W": "250kW"}, "DER_SET_MAX_W must be a whole number, got '250kW'"),
    ({"DER_SET_MAX_W": "1.5"}, "DER_SET_MAX_W must be a whole number, got '1.5'"),
    ({"DER_SET_MAX_W": "200000", "DER_SET_MAX_DISCHARGE_RATE_W": "0"},
     "DER_SET_MAX_DISCHARGE_RATE_W (setMaxDischargeRateW) must be a whole number of watts"),
    ({"DER_SET_MAX_CHARGE_RATE_W": "80000"}, "need DER_SET_MAX_W"),
])
def test_unusable_der_ratings_are_refused_at_startup(env, settings, message):
    for name, value in settings.items():
        env.setenv(name, value)
    with pytest.raises(main_mod.ConfigError) as refused:
        main_mod.load_config()
    assert message in str(refused.value)


# ── The utility simulator speaks the same standard ───────────────────────

def _grid_server(env, **settings):
    for name, value in settings.items():
        env.setenv(name, value)
    spec = importlib.util.spec_from_file_location("grid_server_under_test", ROOT / "simulators" / "fake_grid_server.py")
    grid = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grid)
    return grid


FIXED_W = {"SIM_POWER_CONTROL": "opModFixedW", "DER_SET_MAX_W": "500000", "DER_SET_MAX_CHARGE_RATE_W": "400000"}


@pytest.mark.parametrize("settings, path, power_kw, wire, gateway_kw", [
    ({}, "/simulate/charge", 300,
     "<opModTargetW><multiplier>1</multiplier><value>-30000</value></opModTargetW>", 300),
    ({}, "/simulate/discharge", 125,
     "<opModTargetW><multiplier>1</multiplier><value>12500</value></opModTargetW>", -125),
    (FIXED_W, "/simulate/charge", 300, "<opModFixedW>-7500</opModFixedW>", 300),        # 75 % of 400 kW
    (FIXED_W, "/simulate/discharge", 125, "<opModFixedW>2500</opModFixedW>", -125),     # 25 % of 500 kW
])
def test_the_simulator_sends_standard_controls_that_the_gateway_reads_back(env, settings, path, power_kw,
                                                                            wire, gateway_kw):
    client = _grid_server(env, **settings).app.test_client()
    assert client.post(path, json={"power_kw": power_kw, "duration_sec": 600}).status_code == 200
    feed = client.get("/derp/1/derc").get_data(as_text=True)
    assert wire in feed
    ratings = {key.lower(): int(value) for key, value in settings.items() if key in DER_SETTINGS}
    gateway = IEEEClient({**CLIENT_CFG, **ratings}, GatewayState(), MagicMock())
    event = gateway._extract_event(ET.fromstring(feed).find("{*}DERControl"))
    assert event["power_kw"] == gateway_kw


@pytest.mark.parametrize("settings, message", [
    ({"SIM_POWER_CONTROL": "opModFixedW", "DER_SET_MAX_W": "100000"}, "150 kW is more than 100 % of setMaxW (100 kW)"),
    ({"SIM_POWER_CONTROL": "opModFixedW"}, "DER_SET_MAX_W (setMaxW) is not configured"),
    ({"SIM_POWER_CONTROL": "watts"}, "SIM_POWER_CONTROL must be opModTargetW or opModFixedW"),
])
def test_the_simulator_refuses_what_its_control_cannot_express(env, settings, message):
    client = _grid_server(env, **settings).app.test_client()
    refused = client.post("/simulate/discharge", json={"power_kw": 150, "duration_sec": 60})
    assert refused.status_code == 400 and message in refused.get_json()["message"]
    assert client.get("/derp/1/derc").status_code == 200               # the feed stays up


def test_the_simulator_takes_csip_percentage_xml_and_refuses_xml_without_a_setpoint(env):
    client = _grid_server(env, DER_SET_MAX_W="250000").app.test_client()
    posted = client.post("/simulate/raw", data=control_list("<opModFixedW>8800</opModFixedW>"),
                         content_type="application/xml")
    assert posted.status_code == 200
    [event] = client.get("/status").get_json()["active_events"]
    assert event["power_kw"] == -220.0                                 # 88 % of 250 kW, discharging
    refused = client.post("/simulate/raw", data=control_list("<opModMaxLimW>5000</opModMaxLimW>"),
                          content_type="application/xml")
    assert refused.status_code == 400
    assert "no opModTargetW or opModFixedW setpoint" in refused.get_json()["message"]
