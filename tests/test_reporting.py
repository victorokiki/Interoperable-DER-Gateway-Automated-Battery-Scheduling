"""
tests/test_reporting.py
Step 5 regression tests — the CSIP pieces around dispatch: the IEEE 2030.5
TLS cipher suite, the utility's clock (Time), and what the gateway reports
back (DERCapability, DERSettings, DERStatus, metering through a
MirrorUsagePoint).

Element order follows the IEEE 2030.5 schema's sequences, as the BSGIP
envoy-schema models (sep2/der.py, metering.py, metering_mirror.py) and the
xsdata models generated from sep.xsd (GRIDAPPSD gridappsd-2030_5) give them.

The real IEEEClient, Reporter, Scheduler and ModbusDriver run against
FakeBMS and an in-process utility (ReportingUtility, below).
"""

import asyncio
import re
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

import capstone.__main__ as main_mod
import capstone.reporting as reporting
from capstone.audit_log import AuditLog
from capstone.ieee_client import (DEV_CERTIFICATE, IEEE_2030_5_SUITE, IEEEClient, build_mtls_context,
                                  certificate_key)
from capstone.reporting import Reporter, active_power, der_capability_xml, der_settings_xml, der_status, device_pen, mrid
from capstone.responses import lfdi_of_certificate, sfdi_of_lfdi
from capstone.scheduler import Scheduler
from fakes import OFF, ON, STANDBY, FAULT, FakeBMS, audit_rows, derc, make_driver

ROOT = Path(__file__).resolve().parents[1]
CERTS = ROOT / "certs"
NS = "{urn:ieee:std:2030.5:ns}"
LFDI = lfdi_of_certificate(DEV_CERTIFICATE)       # what the gateway presents to a local utility

# The schema's sequences (every element, optional ones included).
CAPABILITY_ORDER = ["modesSupported", "rtgAbnormalCategory", "rtgMaxA", "rtgMaxAh", "rtgMaxChargeRateVA",
                    "rtgMaxChargeRateW", "rtgMaxDischargeRateVA", "rtgMaxDischargeRateW", "rtgMaxV", "rtgMaxVA",
                    "rtgMaxVar", "rtgMaxVarNeg", "rtgMaxW", "rtgMaxWh", "rtgMinPFOverExcited",
                    "rtgMinPFUnderExcited", "rtgMinV", "rtgNormalCategory", "rtgOverExcitedPF", "rtgOverExcitedW",
                    "rtgReactiveSusceptance", "rtgUnderExcitedPF", "rtgUnderExcitedW", "rtgVNom", "type"]
SETTINGS_ORDER = ["modesEnabled", "setESDelay", "setESHighFreq", "setESHighVolt", "setESLowFreq", "setESLowVolt",
                  "setESRampTms", "setESRandomDelay", "setGradW", "setMaxA", "setMaxAh", "setMaxChargeRateVA",
                  "setMaxChargeRateW", "setMaxDischargeRateVA", "setMaxDischargeRateW", "setMaxV", "setMaxVA",
                  "setMaxVar", "setMaxVarNeg", "setMaxW", "setMaxWh", "setMinPFOverExcited", "setMinPFUnderExcited",
                  "setMinV", "setSoftGradW", "setVNom", "setVRef", "setVRefOfs", "updatedTime"]
STATUS_ORDER = ["alarmStatus", "genConnectStatus", "inverterStatus", "localControlModeStatus", "manufacturerStatus",
                "operationalModeStatus", "readingTime", "stateOfChargeStatus", "storageModeStatus",
                "storConnectStatus"]
USAGE_POINT_ORDER = ["mRID", "description", "version", "roleFlags", "serviceCategoryKind", "status", "deviceLFDI",
                     "MirrorMeterReading", "postRate"]
METER_READING_ORDER = ["mRID", "description", "version", "lastUpdateTime", "MirrorReadingSet", "nextUpdateTime",
                       "Reading", "ReadingType"]
READING_ORDER = ["consumptionBlock", "qualityFlags", "timePeriod", "touTier", "value", "localID"]
READING_TYPE_ORDER = ["accumulationBehaviour", "calorificValue", "commodity", "conversionFactor", "dataQualifier",
                      "flowDirection", "intervalLength", "kind", "maxNumberOfIntervals", "numberOfConsumptionBlocks",
                      "numberOfTouTiers", "phase", "powerOfTenMultiplier", "subIntervalLength", "supplyLimit",
                      "tieredConsumptionBlocks", "uom"]

RSA_CERTIFICATE = """-----BEGIN CERTIFICATE-----
MIICsjCCAZoCCQCAB8HyG+9vYjANBgkqhkiG9w0BAQsFADAaMRgwFgYDVQQDDA9D
YXBzdG9uZSBEZXYgQ0EwHhcNMjYwNzI1MDQzMDExWhcNMjgxMDI3MDQzMDExWjAc
MRowGAYDVQQDDBFlbXMtYnJpZGdlLWNsaWVudDCCASIwDQYJKoZIhvcNAQEBBQAD
ggEPADCCAQoCggEBALbg2COoOkNHJe63qwa99ynzMlhLJubA9Oj8aWC9EeAYH27y
E/xfCzT516RMzmr44K8F9WHpj9paeZeF3E6aV8R8IwgPRAwW2QgZWmUs+Jy3qmVr
7RMD4ffasPN4G/2iUBySjQcbnnResDH75Mf8FDidJoQvVfe9bFTPA3Oz4Po/nklM
p/sr9EnTf+bGZm3vdW8m8C2QFxW+9u2aBw+RlXoFwR4R6ixdcOqIr7HGl8CZ8ykZ
7QJWIafqA69LHDKFUo4Mtz2OIN8GOhjZ1ANc2Cxd8gNMVr837tZvY1YtJjGpreMW
vtmaiz1zP3F5XDlidrBSV1BJ/qt+003yzsLFAdUCAwEAATANBgkqhkiG9w0BAQsF
AAOCAQEAIlI6PfOdo+vKh4HNMVwbkjg8+cJ96AdhI8nDPcYAOACUKbViZVDeeaCm
6AcIVZLAXrvZQLfOsbjGPWf9bURBbx2YRhtG/1wmrVfHtMskMhnXMYxb9PUwYWhO
4LKKJT1HJQJuXcSa7uJaccfRAgmrE9aeT3B0L8kHQAaIUnlayKpIFW8VLqpxg2nP
gWwSPCL7bh6mCbMlAPK+2fuBerAxcmC+E+lUi0xs/ZW9VX6PBcbYsTyCAxVT+pyp
4iLj+I8B/ICb0s4v4w2omwU1OlWocZTwL0/dCt6Q3gamyablcQj51vx7dTM1RJq8
jZLatRfYN7r1tKevuSCDQQebSItu3A==
-----END CERTIFICATE-----
"""


def ieee_2030_5_server_context(suites="@SECLEVEL=0:ECDHE-ECDSA-AES128-CCM8") -> ssl.SSLContext:
    """A server as IEEE 2030.5 has it: TLS 1.2, the mandatory suite, client
    certificates required (the simulator's TLS_ENABLED mode, CCM_8 only)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(suites)
    context.load_cert_chain(CERTS / "server.crt", CERTS / "server.key")
    context.load_verify_locations(CERTS / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def gateway_context() -> ssl.SSLContext:
    return build_mtls_context(dict(ieee_server_url="https://127.0.0.1:1", tls_ca_cert=str(CERTS / "ca.crt"),
                                   tls_client_cert=str(CERTS / "client.crt"),
                                   tls_client_key=str(CERTS / "client.key")))


def children(element) -> list:
    return [child.tag.split("}")[-1] for child in element]


def in_schema_order(element, order, required=()):
    tags = children(element)
    positions = [order.index(tag) for tag in tags]            # an unknown element raises here
    assert positions == sorted(positions), f"{element.tag}: {tags} is not in schema order"
    assert set(required) <= set(tags), f"{element.tag}: missing {set(required) - set(tags)}"
    return tags


class ReportingUtility:
    """In-process IEEE 2030.5 server with what reporting uses: a Time
    resource (its clock .clock_offset seconds from this machine's), this
    device's EndDevice (by LFDI) and DERList (.der_poll_rate), the DER's
    report targets and a MirrorUsagePointList. Records every PUT and POST as
    (method, path, body); .fail maps a path to the HTTP status answered
    instead. One DER program whose DERControlList is .xml."""

    def __init__(self, xml=None):
        self.xml = xml or derc()
        self.clock_offset = 0.0
        self.time_poll_rate = 900
        self.der_poll_rate = None
        self.der_list_fails = False
        self.fail = {}
        self.writes = []
        self.gets = []
        self.usage_points = {}

    async def start(self, ssl_context=None):
        app = web.Application()
        for path, handler in (("/dcap", self._dcap), ("/tm", self._time), ("/edev", self._edev),
                              ("/edev/1/der", self._ders), ("/derp", self._programs), ("/derp/1/derc", self._derc)):
            app.router.add_get(path, handler)
        app.router.add_route("*", "/{path:.*}", self._write)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=ssl_context).start()
        self.url = f"{'https' if ssl_context else 'http'}://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def stop(self):
        await self.runner.cleanup()

    def _xml(self, request, text):
        self.gets.append(request.path)
        return web.Response(text=text, content_type="application/sep+xml")

    async def _dcap(self, request):
        return self._xml(request, f'<DeviceCapability xmlns="urn:ieee:std:2030.5:ns" href="/dcap">'
                                  f'<DERProgramListLink href="/derp" all="1"/><TimeLink href="/tm"/>'
                                  f'<EndDeviceListLink href="/edev" all="2"/>'
                                  f'<MirrorUsagePointListLink href="/mup" all="{len(self.usage_points)}"/>'
                                  f'</DeviceCapability>')

    async def _time(self, request):
        return self._xml(request, f'<Time xmlns="urn:ieee:std:2030.5:ns" href="/tm" pollRate="{self.time_poll_rate}">'
                                  f'<currentTime>{int(time.time() + self.clock_offset)}</currentTime>'
                                  f'<dstEndTime>0</dstEndTime><dstOffset>0</dstOffset><dstStartTime>0</dstStartTime>'
                                  f'<quality>7</quality><tzOffset>0</tzOffset></Time>')

    async def _edev(self, request):
        other = "0123456789ABCDEF0123456789ABCDEF01234567"
        devices = "".join(f'<EndDevice href="/edev/{n}"><DERListLink href="/edev/{n}/der" all="1"/>'
                          f'<lFDI>{lfdi}</lFDI><sFDI>{sfdi_of_lfdi(lfdi)}</sFDI><changedTime>0</changedTime>'
                          f'</EndDevice>' for n, lfdi in ((2, other), (1, LFDI)))
        return self._xml(request, f'<EndDeviceList xmlns="urn:ieee:std:2030.5:ns" href="/edev" all="2" '
                                  f'results="2">{devices}</EndDeviceList>')

    async def _ders(self, request):
        if self.der_list_fails:
            return web.Response(status=503)
        rate = "" if self.der_poll_rate is None else f' pollRate="{self.der_poll_rate}"'
        return self._xml(request, f'<DERList xmlns="urn:ieee:std:2030.5:ns" href="/edev/1/der" all="1" results="1"'
                                  f'{rate}><DER href="/edev/1/der/1">'
                                  f'<DERAvailabilityLink href="/edev/1/der/1/dera"/>'
                                  f'<DERCapabilityLink href="/edev/1/der/1/dercap"/>'
                                  f'<DERSettingsLink href="/edev/1/der/1/derg"/>'
                                  f'<DERStatusLink href="/edev/1/der/1/ders"/></DER></DERList>')

    async def _programs(self, request):
        return self._xml(request, '<DERProgramList xmlns="urn:ieee:std:2030.5:ns" href="/derp" all="1" results="1">'
                                  '<DERProgram href="/derp/1"><mRID>A1</mRID>'
                                  '<DERControlListLink href="/derp/1/derc"/><primacy>1</primacy></DERProgram>'
                                  '</DERProgramList>')

    async def _derc(self, request):
        return self._xml(request, self.xml)

    async def _write(self, request):
        if request.method == "GET":
            return web.Response(status=404)
        body = await request.text()
        self.writes.append((request.method, request.path, body))
        if request.path in self.fail:
            return web.Response(status=self.fail[request.path])
        if request.method == "POST" and request.path == "/mup":
            n = str(len(self.usage_points) + 1)
            self.usage_points[n] = ET.fromstring(body).findtext(f"{NS}mRID")
            return web.Response(status=201, headers={"Location": f"/mup/{n}"})
        if request.method == "POST" and request.path.startswith("/mup/"):
            return web.Response(status=204 if request.path[5:] in self.usage_points else 404)
        return web.Response(status=204 if request.method == "PUT" else 201)

    def sent(self, path, method=None):
        return [ET.fromstring(body) for m, p, body in self.writes if p == path and (method is None or m == method)]


async def rig(tmp_path, util, fake=None, **config):
    audit = AuditLog(db_path=tmp_path / "audit.db")
    driver, state = await make_driver(fake or FakeBMS(), audit_log=audit)
    sched = Scheduler({}, state, driver, audit_log=audit)
    config = {"ieee_server_url": util.url, "ieee_poll_interval_sec": 5, "offline_timeout_sec": 30,
              "der_set_max_w": 250_000, "der_set_max_charge_rate_w": 200_000,
              "der_set_max_discharge_rate_w": 250_000, **config}
    client = IEEEClient(config, state, sched, audit_log=audit)
    pen, source = device_pen(config)
    reporter = Reporter(client, state, audit_log=audit, pen=pen, pen_source=source)
    return SimpleNamespace(fake=driver._client, driver=driver, state=state, sched=sched, client=client,
                           reporter=reporter, audit=audit, http=client._session())


def run_with(tmp_path, util, scenario, fake=None, ssl_context=None, **config):
    async def run():
        await util.start(ssl_context)
        g = await rig(tmp_path, util, fake, **config)
        try:
            await scenario(g)
        finally:
            await g.http.close()
            await util.stop()
        return g
    return asyncio.run(run())


def reporting_rows(audit):
    return [(row["outcome"], row["detail"]) for row in audit_rows(audit, "ieee_reporting")]


# ── TLS: the IEEE 2030.5 cipher suite ────────────────────────────────────

def handshake(server_context, client_context):
    """One TLS handshake over a socket pair; (version, cipher) or the error."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    errors = []

    def serve():
        connection, _ = listener.accept()
        try:
            with server_context.wrap_socket(connection, server_side=True) as tls:
                tls.recv(1)
        except (ssl.SSLError, OSError) as e:
            errors.append(e)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(listener.getsockname()) as raw, \
                client_context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
            result = (tls.version(), tls.cipher()[0])
            tls.sendall(b"x")
    except ssl.SSLError as e:
        result = ("failed", str(e))
    thread.join(5)
    listener.close()
    return result


def test_the_ieee_2030_5_cipher_suite_is_negotiated_with_a_server_offering_only_it():
    """Harness M7a only checks that the suite is offered. OpenSSL 3 allows
    CCM_8 suites only at security level 0: offered at the default level, it
    never takes part in a handshake ("no ciphers available")."""
    assert handshake(ieee_2030_5_server_context(), gateway_context()) == ("TLSv1.2", IEEE_2030_5_SUITE)


def test_the_ieee_2030_5_suite_is_preferred_where_the_server_offers_others_too():
    server = ieee_2030_5_server_context("@SECLEVEL=0:ECDHE-ECDSA-AES128-CCM8:ECDHE+AESGCM")
    server.options &= ~ssl.OP_CIPHER_SERVER_PREFERENCE          # the client's order decides
    assert handshake(server, gateway_context()) == ("TLSv1.2", IEEE_2030_5_SUITE)


def test_the_gateway_still_reaches_a_server_without_the_ieee_2030_5_suite():
    server = ieee_2030_5_server_context("ECDHE-ECDSA-AES128-GCM-SHA256")
    assert handshake(server, gateway_context()) == ("TLSv1.2", "ECDHE-ECDSA-AES128-GCM-SHA256")


def test_only_forward_secret_aead_or_sha2_suites_are_offered_besides():
    names = [c["name"] for c in gateway_context().get_ciphers() if c["protocol"] == "TLSv1.2"]
    assert names[0] == IEEE_2030_5_SUITE
    assert all(name.startswith("ECDHE-") for name in names)
    assert not [name for name in names if name.endswith("-SHA") or "NULL" in name or "RC4" in name or "DES" in name]


def test_the_suite_the_utility_chose_is_audited(tmp_path):
    async def scenario(g):
        await g.client._poll(g.http)
        await g.client._poll(g.http)
    util = ReportingUtility()
    g = run_with(tmp_path, util, scenario, ssl_context=ieee_2030_5_server_context(),
                 tls_ca_cert=str(CERTS / "ca.crt"), tls_client_cert=str(CERTS / "client.crt"),
                 tls_client_key=str(CERTS / "client.key"))
    assert [row["detail"] for row in audit_rows(g.audit, "ieee_transport")] == [
        "utility connection uses TLSv1.2 ECDHE-ECDSA-AES128-CCM8 (the IEEE 2030.5 cipher suite)"]


@pytest.mark.parametrize("certificate, key", [
    (CERTS / "client.crt", "EC P-256"), (CERTS / "server.crt", "EC P-256"), (CERTS / "ca.crt", "EC P-256")])
def test_the_development_certificates_are_ecdsa_p256(certificate, key):
    assert certificate_key(certificate) == key


def test_the_key_of_an_rsa_or_unreadable_certificate_is_told(tmp_path):
    (tmp_path / "rsa.crt").write_text(RSA_CERTIFICATE)
    (tmp_path / "junk.crt").write_text("not a certificate")
    assert certificate_key(tmp_path / "rsa.crt") == "RSA 2048"
    assert certificate_key(tmp_path / "junk.crt") == "unknown"
    assert certificate_key(tmp_path / "missing.crt") == "unknown"


# ── The utility's clock ──────────────────────────────────────────────────

def test_event_windows_are_taken_on_the_utility_clock(tmp_path):
    """An event the utility starts now, on a clock 120 s ahead of this
    machine's, runs now; it used to wait 120 s."""
    now = int(time.time())
    util = ReportingUtility(derc(dict(mrid="E", start=now + 120, duration=600, value=50_000)))
    util.clock_offset = 120

    async def scenario(g):
        await g.client._poll(g.http)
        await g.sched.step()
    g = run_with(tmp_path, util, scenario)
    assert 119 <= g.state.clock_offset_sec <= 121
    assert (g.state.authority["authority"], g.state.authority["mrid"]) == ("approved_schedule", "E")
    assert g.fake.setpoint_kw() == 50
    [row] = audit_rows(g.audit, "ieee_time")
    assert re.search(r"the utility's clock is \+1(19|20)\.\d s from this machine's; event times are taken on "
                     r"the utility's$", row["detail"])


def test_a_clock_difference_within_a_second_counts_as_none(tmp_path):
    util = ReportingUtility()
    util.clock_offset = 0.4

    async def scenario(g):
        await g.client._poll(g.http)
    g = run_with(tmp_path, util, scenario)
    assert g.state.clock_offset_sec == 0
    [row] = audit_rows(g.audit, "ieee_time")
    assert "agrees with the utility's within 1 s" in row["detail"]


def test_the_time_is_read_again_only_at_its_poll_rate(tmp_path):
    util = ReportingUtility()
    util.time_poll_rate = 60

    async def scenario(g):
        await g.client._poll(g.http)
        await g.client._poll(g.http)
        g.client._time_due -= 60                                  # its pollRate has passed
        util.clock_offset = -30
        await g.client._poll(g.http)
    g = run_with(tmp_path, util, scenario)
    assert util.gets.count("/tm") == 2
    assert -31 <= g.state.clock_offset_sec <= -29
    assert len(audit_rows(g.audit, "ieee_time")) == 2


def test_an_event_already_over_on_the_utility_clock_is_answered_expired(tmp_path):
    async def run():
        g = await rig(tmp_path, SimpleNamespace(url="http://127.0.0.1:9"))
        g.state.clock_offset_sec = 3600
        now = int(time.time())
        g.sched.add_event({"mrid": "OLD", "start": now, "end": now + 600, "power_kw": 10.0, "source": "t",
                           "reply_to": "/rsps/1/rsp", "response_required": 3})
        await g.http.close()
        return g
    g = asyncio.run(run())
    assert [row[1] for row in g.sched.due_responses(time.time() + 1, 10)] == [254]


def test_response_times_are_on_the_utility_clock(tmp_path):
    now = int(time.time())
    util = ReportingUtility(derc(dict(mrid="E", start=now + 3600, duration=600, value=50_000,
                                      response_required="01")))
    util.clock_offset = 3600

    async def scenario(g):
        await g.client._poll(g.http)
        await g.client.send_responses(g.http, time.time() + 1)
    g = run_with(tmp_path, util, scenario)
    [response] = util.sent("/rsps/1/rsp", "POST")
    assert abs(int(response.findtext(f"{NS}createdDateTime")) - (now + 3600)) <= 2


# ── Where reports go ─────────────────────────────────────────────────────

def test_reports_go_to_this_devices_der(tmp_path):
    util = ReportingUtility()
    util.der_poll_rate = 60

    async def scenario(g):
        await g.client._poll(g.http)
    g = run_with(tmp_path, util, scenario)
    links = g.client.links
    assert (links.time, links.mirror_usage_points, links.der) == ("/tm", "/mup", "/edev/1/der/1")
    assert (links.capability, links.settings, links.status, links.status_rate) == (
        "/edev/1/der/1/dercap", "/edev/1/der/1/derg", "/edev/1/der/1/ders", 60)
    assert "/edev/2/der" not in util.gets                        # another device's DERs are not read


def test_a_der_list_that_cannot_be_read_keeps_the_links_and_the_poll(tmp_path):
    util = ReportingUtility()

    async def scenario(g):
        await g.client._poll(g.http)
        util.der_list_fails = True
        g.polled = await g.client._poll(g.http)
    g = run_with(tmp_path, util, scenario)
    assert g.polled is True and g.state.grid_connected is True
    assert g.client.links.status == "/edev/1/der/1/ders"


# ── The reports ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("watts, multiplier, value", [
    (250_000, 1, 25_000), (32_767, 0, 32_767), (-32_768, 0, -32_768), (32_768, 1, 3_277), (5, 0, 5),
    (2_147_483_647_000, 8, 21_475)])
def test_active_power_fits_an_int16_value(watts, multiplier, value):
    element = ET.fromstring(active_power("x", watts))
    assert children(element) == ["multiplier", "value"]
    assert (int(element.findtext("multiplier")), int(element.findtext("value"))) == (multiplier, value)


RATINGS = {"setMaxW": 250_000, "setMaxChargeRateW": 200_000, "setMaxDischargeRateW": 250_000}


def test_capability_and_settings_follow_the_schema():
    capability = ET.fromstring(der_capability_xml(RATINGS))
    assert in_schema_order(capability, CAPABILITY_ORDER, ("modesSupported", "rtgMaxW", "type")) == [
        "modesSupported", "rtgMaxChargeRateW", "rtgMaxDischargeRateW", "rtgMaxW", "type"]
    assert capability.findtext(f"{NS}modesSupported") == "0040008F"      # charge, discharge, Connect, Energize, FixedW, TargetW
    assert capability.findtext(f"{NS}type") == "80"
    assert capability.findtext(f"{NS}rtgMaxChargeRateW/{NS}value") == "20000"
    settings = ET.fromstring(der_settings_xml(RATINGS, 1_800_000_000))
    assert in_schema_order(settings, SETTINGS_ORDER, ("setGradW", "setMaxW", "updatedTime")) == [
        "modesEnabled", "setGradW", "setMaxChargeRateW", "setMaxDischargeRateW", "setMaxW", "updatedTime"]
    assert settings.findtext(f"{NS}setGradW") == "0" and settings.findtext(f"{NS}updatedTime") == "1800000000"
    assert der_capability_xml({"setMaxW": None}) is None and der_settings_xml({"setMaxW": None}, 0) is None


@pytest.mark.parametrize("code, measured, connect, inverter, operational, storage", [
    (ON, 50, "07", "0", "2", "0"), (ON, -50, "07", "0", "2", "1"), (ON, 0, "07", "0", "2", "2"),
    (STANDBY, 0, "02", "8", "1", "2"), (OFF, 0, "00", "1", "1", "2"), (FAULT, 0, "10", "7", "1", "2"),
    (3, 0, "02", "3", "1", "2"), (11, 20, "07", "0", "2", "0"), (11, 0, "02", "0", "1", "2")])
def test_status_follows_the_bms(code, measured, connect, inverter, operational, storage):
    xml, _ = der_status(1_800_000_000, code, "Normal", 51.25, measured)
    status = ET.fromstring(xml)
    assert in_schema_order(status, STATUS_ORDER, ("readingTime",))
    values = {tag: status.findtext(f"{NS}{tag}/{NS}value") for tag in children(status) if tag != "readingTime"}
    assert values == {"genConnectStatus": connect, "inverterStatus": inverter, "localControlModeStatus": "1",
                      "operationalModeStatus": operational, "stateOfChargeStatus": "5125",
                      "storageModeStatus": storage, "storConnectStatus": connect}
    assert status.findtext(f"{NS}readingTime") == "1800000000"


def test_user_controlled_mode_is_local_control():
    xml, _ = der_status(0, ON, "User-Controlled", None, None)
    status = ET.fromstring(xml)
    assert status.findtext(f"{NS}localControlModeStatus/{NS}value") == "0"
    assert "stateOfChargeStatus" not in children(status) and "storageModeStatus" not in children(status)


def test_mrids_carry_the_pen_in_their_low_32_bits():
    assert mrid("usage point", LFDI, 12345).endswith("00003039") and len(mrid("usage point", LFDI, 12345)) == 32
    assert mrid("usage point", LFDI, 1) != mrid("reading:active_power", LFDI, 1)
    assert mrid("usage point", LFDI, 1) == mrid("usage point", LFDI, 1)      # the same after a restart


async def fresh(g):
    """A BMS status read the reports can use (the driver's 10 s status poll)."""
    await g.driver.supervise()


def test_capability_and_settings_are_sent_once_and_again_when_they_change(tmp_path):
    util = ReportingUtility()

    async def scenario(g):
        await g.client._poll(g.http)
        await fresh(g)
        await g.reporter.report_once(g.http)
        await g.reporter.report_once(g.http)
        g.client.der_ratings = dict(g.client.der_ratings, setMaxW=300_000)
        await g.reporter.report_once(g.http)
    g = run_with(tmp_path, util, scenario)
    assert [c.findtext(f"{NS}rtgMaxW/{NS}value") for c in util.sent("/edev/1/der/1/dercap", "PUT")] == ["25000", "30000"]
    settings = util.sent("/edev/1/der/1/derg", "PUT")
    assert [s.findtext(f"{NS}setMaxW/{NS}value") for s in settings] == ["25000", "30000"]
    first, second = (int(s.findtext(f"{NS}updatedTime")) for s in settings)
    assert abs(first - time.time()) <= 5 and second >= first    # when these settings took effect


def test_status_is_sent_when_it_changes_and_at_the_der_list_poll_rate(tmp_path):
    util = ReportingUtility()
    util.der_poll_rate = 60

    async def scenario(g):
        await g.client._poll(g.http)
        await fresh(g)
        await g.reporter.report_once(g.http)
        await g.reporter.report_once(g.http)                    # nothing changed: not sent again yet
        g.fake.fault()
        await fresh(g)
        await g.reporter.report_once(g.http)                    # a change: sent at once
        g.reporter._sent_at["status"] -= 60                      # its pollRate has passed
        await g.reporter.report_once(g.http)
    g = run_with(tmp_path, util, scenario)
    sent = [s.findtext(f"{NS}operationalModeStatus/{NS}value") for s in util.sent("/edev/1/der/1/ders", "PUT")]
    assert sent == ["2", "1", "1"]
    assert [outcome for outcome, detail in reporting_rows(g.audit) if detail.startswith("DERStatus")] == ["sent", "sent"]


def test_nothing_is_reported_from_a_stale_bms_reading(tmp_path):
    util = ReportingUtility()

    async def scenario(g):
        await g.client._poll(g.http)
        await fresh(g)
        g.state.last_status_poll_at -= 60
        await g.reporter.report_once(g.http)
    g = run_with(tmp_path, util, scenario)
    assert util.sent("/edev/1/der/1/ders") == [] and util.sent("/mup") == []
    assert ("not_sent", f"DERStatus not sent: no BMS reading in the last 30 s") in reporting_rows(g.audit)


def test_metering_creates_a_mirror_usage_point_then_posts_every_5_minutes(tmp_path):
    util = ReportingUtility()

    async def scenario(g):
        await g.client._poll(g.http)
        await g.driver.set_power(60.0, "t")                      # charging 60 kW
        await fresh(g)
        await g.reporter.report_once(g.http)
        await g.reporter.report_once(g.http)                    # not due yet
        g.reporter._sent_at["metering"] -= 300
        await g.reporter.report_once(g.http)
    g = run_with(tmp_path, util, scenario)
    [point] = util.sent("/mup", "POST")
    assert in_schema_order(point, USAGE_POINT_ORDER, ("mRID", "roleFlags", "serviceCategoryKind", "status",
                                                      "deviceLFDI"))
    assert (point.findtext(f"{NS}deviceLFDI"), point.findtext(f"{NS}roleFlags"), point.findtext(f"{NS}postRate")) == (
        LFDI, "0049", "300")
    assert point.findtext(f"{NS}mRID").endswith("00000000")                 # the local simulator's placeholder PEN
    readings = point.findall(f"{NS}MirrorMeterReading")
    for reading in readings:
        in_schema_order(reading, METER_READING_ORDER, ("mRID",))
        in_schema_order(reading.find(f"{NS}Reading"), READING_ORDER)
        in_schema_order(reading.find(f"{NS}ReadingType"), READING_TYPE_ORDER)
    values = {r.findtext(f"{NS}ReadingType/{NS}uom"): r.findtext(f"{NS}Reading/{NS}value") for r in readings}
    assert values == {"38": "-60", "63": "0"}                   # delivered = -60 kW (it charges); no reactive power
    assert [r.findtext(f"{NS}ReadingType/{NS}flowDirection") for r in readings] == ["19", "19"]
    assert [r.findtext(f"{NS}ReadingType/{NS}powerOfTenMultiplier") for r in readings] == ["3", "3"]
    later = util.sent("/mup/1", "POST")
    assert [r.findtext(f"{NS}Reading/{NS}value") for r in later] == ["-60", "0"]


def test_a_mirror_usage_point_the_utility_lost_is_created_again(tmp_path):
    util = ReportingUtility()

    async def scenario(g):
        await g.client._poll(g.http)
        await fresh(g)
        await g.reporter.report_once(g.http)
        util.usage_points.clear()                                 # the utility no longer has /mup/1
        g.reporter._sent_at["metering"] -= 300
        await g.reporter.report_once(g.http)                    # 404: forgotten
        g.reporter._retry.clear()
        await g.reporter.report_once(g.http)                    # created again
    g = run_with(tmp_path, util, scenario)
    assert len(util.sent("/mup", "POST")) == 2 and g.reporter.usage_point == "/mup/1"


def test_metering_needs_a_pen_away_from_the_local_simulator():
    assert device_pen({"ieee_server_url": "https://utility.example"}) == (
        None, "set DEVICE_PEN to the IANA PEN of the organisation responsible for this gateway")
    assert device_pen({"ieee_server_url": "https://utility.example", "device_pen": 26_188}) == (26_188, "DEVICE_PEN")
    assert device_pen({"ieee_server_url": "http://127.0.0.1:5001"}) == (0, "placeholder 0, local simulator only")
    with pytest.raises(ValueError, match="DEVICE_PEN"):
        device_pen({"ieee_server_url": "https://utility.example", "device_pen": 1 << 32})


def test_without_a_pen_metering_is_not_sent_and_says_why(tmp_path):
    util = ReportingUtility()

    async def scenario(g):
        await g.client._poll(g.http)
        await fresh(g)
        g.reporter.pen, g.reporter.pen_source = None, "set DEVICE_PEN to ..."
        await g.reporter.report_once(g.http)
    g = run_with(tmp_path, util, scenario)
    assert util.sent("/mup") == []
    assert ("not_sent", "metering not sent: no IANA PEN for the mRIDs this gateway creates (set DEVICE_PEN to ...)"
            ) in reporting_rows(g.audit)
    assert util.sent("/edev/1/der/1/ders")                      # the other reports still go


def test_a_refused_report_backs_off_is_audited_once_and_goes_through_later(tmp_path):
    util = ReportingUtility()
    util.fail["/edev/1/der/1/ders"] = 500

    async def scenario(g):
        await g.client._poll(g.http)
        await fresh(g)
        for _ in range(3):
            await g.reporter.report_once(g.http)                # one try: the retry waits 5 s
        g.reporter._retry["status"] = (0.0, 5)
        await g.reporter.report_once(g.http)                    # tried again: still refused
        del util.fail["/edev/1/der/1/ders"]
        g.reporter._retry["status"] = (0.0, 10)
        await g.reporter.report_once(g.http)
    g = run_with(tmp_path, util, scenario)
    assert len(util.sent("/edev/1/der/1/ders")) == 3
    rows = [row for row in reporting_rows(g.audit) if "DERStatus" in row[1]]
    assert rows[0] == ("failed", "DERStatus to /edev/1/der/1/ders: HTTP 500; retrying with backoff")
    assert rows[1] == ("sent", "DERStatus sent to /edev/1/der/1/ders again (it failed: DERStatus to "
                               "/edev/1/der/1/ders: HTTP 500)")
    assert len(rows) == 3                                       # and the status itself


def test_nothing_is_reported_for_a_dc_configured_bms(tmp_path):
    util = ReportingUtility()

    async def scenario(g):
        await g.client._poll(g.http)
        await fresh(g)
        await g.reporter.report_once(g.http)
    g = run_with(tmp_path, util, scenario, fake=FakeBMS(bms_type=0))
    assert util.writes == []
    assert reporting_rows(g.audit)[-1] == (
        "not_sent", "DC-configured BMS: not dispatched by this gateway, so nothing is reported for it")


def test_reports_only_go_to_the_utility_server(tmp_path):
    util = ReportingUtility()

    async def scenario(g):
        await g.client._poll(g.http)
        await fresh(g)
        g.client.links.status = "https://elsewhere.example/ders"
        await g.reporter.report_once(g.http)
    g = run_with(tmp_path, util, scenario)
    assert ("failed", "DERStatus to https://elsewhere.example/ders: https://elsewhere.example/ders is not on the "
                      "utility server; retrying with backoff") in reporting_rows(g.audit)


@pytest.mark.parametrize("bms_type", [1, 0])
def test_a_utility_that_asks_for_no_reports_gets_none_and_no_notes(tmp_path, bms_type):
    util = ReportingUtility()

    async def scenario(g):
        await fresh(g)
        await g.reporter.report_once(g.http)                    # before any poll: no links known
    g = run_with(tmp_path, util, scenario, fake=FakeBMS(bms_type=bms_type))
    assert util.writes == [] and reporting_rows(g.audit) == []


# ── The driver's part and the settings ───────────────────────────────────

def test_the_status_poll_reads_reactive_power_as_the_bms_reports_it(tmp_path):
    async def run():
        fake = FakeBMS()
        fake.ir[1011], fake.ir[1012] = 0xFFFF, 0xFFF6            # -10 kVAR
        driver, state = await make_driver(fake)
        await driver.supervise()
        return state
    state = asyncio.run(run())
    assert state.measured_kvar == -10 and state.operating_state_code == ON


def test_device_pen_setting(monkeypatch):
    for name in ("IEEE_SERVER_URL", "TLS_CA_CERT", "TLS_CLIENT_CERT", "TLS_CLIENT_KEY", "DEVICE_PEN",
                 "DER_SET_MAX_W", "DER_SET_MAX_CHARGE_RATE_W", "DER_SET_MAX_DISCHARGE_RATE_W", "DEVICE_LFDI",
                 "MODBUS_UNIT_ID", "BMS_START_FROM_OFF"):
        monkeypatch.delenv(name, raising=False)
    assert main_mod.load_config()["pen_summary"] == "0 (placeholder 0, local simulator only)"
    monkeypatch.setenv("DEVICE_PEN", "26188")
    assert main_mod.load_config()["pen_summary"] == "26188 (DEVICE_PEN)"
    monkeypatch.setenv("DEVICE_PEN", "0")
    with pytest.raises(main_mod.ConfigError, match="DEVICE_PEN"):
        main_mod.load_config()
