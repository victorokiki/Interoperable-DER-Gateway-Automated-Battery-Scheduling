"""
tests/test_ieee_discovery.py
Step 3 regression tests — finding the utility's events the standard way.

The gateway used to read one fixed URL (/derp/1/derc) and one page of it.
IEEE 2030.5 / CSIP §5.2.3.1: a DER client starts at DeviceCapability and
"SHALL traverse all these links and lists": its EndDevice (matched by LFDI
or SFDI), that device's FunctionSetAssignments, their DERProgramLists (up
to 15 programs, CSIP §5.2.3), each program's DERControlList, read a page
at a time until `all` items are read, and its DefaultDERControl, which
applies between events. Overlapping events are ranked by program primacy,
then creationTime (IEEE 2030.5-2018 §10.2.3.3), and the client polls at
the utility's pollRate.

The real IEEEClient, Scheduler, ModbusDriver and ReliabilityManager run
against FakeBMS and an in-process server built per test.
"""

import asyncio
import importlib.util
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

import capstone.__main__ as main_mod
import capstone.scheduler as sched_mod
from capstone.audit_log import AuditLog
from capstone.ieee_client import IEEEClient
from capstone.modbus_driver import _decode_int32
from capstone.reliability import ReliabilityManager, ReliabilityState
from capstone.responses import lfdi_of_certificate, sfdi_of_lfdi
from capstone.scheduler import AUTH_DEFAULT, AUTH_SAFE_IDLE, AUTH_SCHEDULE, Scheduler
from fakes import AC_SETPOINT, FakeBMS, audit_rows, make_driver

ROOT = Path(__file__).resolve().parents[1]
T = 1_800_000_000
NS = 'xmlns="urn:ieee:std:2030.5:ns"'
DEV_LFDI = lfdi_of_certificate(ROOT / "certs" / "client.crt")
OTHER_LFDI = "0" * 40


class Server:
    """In-process IEEE 2030.5 server built by each test. A plain resource
    serves its XML; a list resource is served a page at a time, as `s` and
    `l` ask, and never more than page_size items. Every GET is recorded
    (path and query), and every DERControlResponse (subject, status)."""

    def __init__(self, page_size=1000):
        self.page_size = page_size
        self.resources = {}
        self.fail = {}                   # path -> HTTP status served instead
        self.gets = []
        self.responses = []

    def resource(self, path, xml):
        self.resources[path] = xml

    def list(self, path, name, items, **attributes):
        self.resources[path] = (name, list(items), attributes)

    async def start(self):
        app = web.Application()
        app.router.add_get("/{path:.*}", self._get)
        app.router.add_post("/{path:.*}", self._post)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def stop(self):
        await self.runner.cleanup()

    async def _get(self, request):
        self.gets.append(request.path_qs)
        if request.path in self.fail:
            return web.Response(status=self.fail[request.path])
        found = self.resources.get(request.path)
        if found is None:
            return web.Response(status=404)
        if isinstance(found, str):
            return web.Response(text=found, content_type="application/sep+xml")
        name, items, attributes = found
        start = int(request.query.get("s", 0))
        page = items[start:start + min(int(request.query.get("l", len(items))), self.page_size)]
        extra = "".join(f' {key}="{value}"' for key, value in attributes.items())
        return web.Response(text=f'<{name} {NS} href="{request.path}" all="{len(items)}" results="{len(page)}"'
                                 f'{extra}>{"".join(page)}</{name}>', content_type="application/sep+xml")

    async def _post(self, request):
        body = ET.fromstring(await request.text())
        self.responses.append((body.findtext("{*}subject"), int(body.findtext("{*}status"))))
        return web.Response(status=201)

    def statuses(self, subject):
        return [status for s, status in self.responses if s == subject]


def dcap(**links):
    return f'<DeviceCapability {NS} href="/dcap">' + "".join(
        f'<{name} href="{href}"/>' for name, href in links.items()) + "</DeviceCapability>"


def control(mrid, start=T, duration=600, output_w=-20_000, created=None, respond=None, extra=""):
    """A DERControl asking for output_w (IEEE 2030.5 sign: + = discharge) as opModTargetW."""
    attributes = "" if respond is None else f' replyTo="/rsps/1/rsp" responseRequired="{respond}"'
    creation = "" if created is None else f"<creationTime>{created}</creationTime>"
    return (f'<DERControl{attributes}><mRID>{mrid}</mRID>{creation}<interval><duration>{duration}</duration>'
            f'<start>{start}</start></interval>{extra}<DERControlBase><opModTargetW><multiplier>0</multiplier>'
            f'<value>{output_w}</value></opModTargetW></DERControlBase></DERControl>')


def default_control(base, **settings):
    extra = "".join(f"<{name}>{value}</{name}>" for name, value in settings.items())
    return (f'<DefaultDERControl {NS}><mRID>D1</mRID><DERControlBase>{base}</DERControlBase>{extra}'
            f'</DefaultDERControl>')


def target_w(output_w):
    return f"<opModTargetW><multiplier>0</multiplier><value>{output_w}</value></opModTargetW>"


def program(server, href, primacy=1, events=None, default=None):
    """Register a DERProgram's DERControlList and DefaultDERControl on the server; returns its XML."""
    parts = [f'<DERProgram href="{href}"><mRID>{href.strip("/").replace("/", "")}</mRID>']
    if default is not None:
        server.resource(f"{href}/dderc", default)
        parts.append(f'<DefaultDERControlLink href="{href}/dderc"/>')
    if events is not None:
        server.list(f"{href}/derc", "DERControlList", events)
        parts.append(f'<DERControlListLink href="{href}/derc"/>')
    parts.append(f"<primacy>{primacy}</primacy></DERProgram>")
    return "".join(parts)


def one_program_server(events=(), default=None, page_size=1000, **list_attributes):
    """A server whose DeviceCapability lists one program (primacy 1) for all devices."""
    server = Server(page_size)
    server.resource("/dcap", dcap(DERProgramListLink="/derp"))
    server.list("/derp", "DERProgramList", [program(server, "/derp/1", 1, list(events), default)],
                **list_attributes)
    return server


async def rig(tmp_path, server, fake=None, **config):
    audit = AuditLog(db_path=tmp_path / "audit.db")
    driver, state = await make_driver(fake or FakeBMS(), audit_log=audit)
    rm = ReliabilityManager({"offline_timeout_sec": 30}, state, audit_log=audit)
    sched = Scheduler({}, state, driver, audit_log=audit, reliability_manager=rm)
    config = {"ieee_server_url": server.url, "ieee_poll_interval_sec": 5, "offline_timeout_sec": 30, **config}
    client = IEEEClient(config, state, sched, reliability_manager=rm, audit_log=audit)
    return SimpleNamespace(fake=driver._client, state=state, rm=rm, sched=sched, client=client, audit=audit,
                           http=aiohttp.ClientSession())


def run_with(tmp_path, server, scenario, fake=None, **config):
    async def run():
        await server.start()
        g = await rig(tmp_path, server, fake, **config)
        try:
            await scenario(g, server)
        finally:
            await g.http.close()
            await server.stop()
        return g
    return asyncio.run(run())


async def poll(g, server):
    assert await g.client._poll(g.http) is True


def stored():
    with sqlite3.connect(sched_mod.DB_PATH) as conn:
        return {row[0]: row[1:] for row in conn.execute(
            "SELECT mrid, status, program, primacy, creation_time FROM events ORDER BY mrid")}


def register(fake):
    """41009 as the BMS holds it: P > 0 = discharge."""
    return _decode_int32([fake.hr.get(AC_SETPOINT, 0), fake.hr.get(AC_SETPOINT + 1, 0)])


# ── Discovery ────────────────────────────────────────────────────────────

def test_the_programs_are_found_through_this_devices_end_device(tmp_path):
    server = Server()
    server.resource("/dcap", dcap(EndDeviceListLink="/edev"))
    server.list("/edev", "EndDeviceList", [
        f'<EndDevice href="/edev/1"><lFDI>{OTHER_LFDI}</lFDI><FunctionSetAssignmentsListLink href="/edev/1/fsa"/>'
        f'</EndDevice>',
        f'<EndDevice href="/edev/2"><lFDI>{DEV_LFDI.lower()}</lFDI>'
        f'<FunctionSetAssignmentsListLink href="/edev/2/fsa"/></EndDevice>'])
    server.list("/edev/1/fsa", "FunctionSetAssignmentsList",
                ['<FunctionSetAssignments href="/edev/1/fsa/1"><DERProgramListLink href="/other"/>'
                 '</FunctionSetAssignments>'])
    server.list("/edev/2/fsa", "FunctionSetAssignmentsList",
                ['<FunctionSetAssignments href="/edev/2/fsa/1"><DERProgramListLink href="/mine"/>'
                 '</FunctionSetAssignments>'])
    server.list("/mine", "DERProgramList", [program(server, "/mine/1", 3, [control("E")])])
    server.list("/other", "DERProgramList", [program(server, "/other/1", 1, [control("NOT_MINE")])])
    run_with(tmp_path, server, poll)
    assert stored() == {"E": ("scheduled", "/mine/1", 3, None)}
    assert not any(get.startswith(("/edev/1/fsa", "/other")) for get in server.gets)   # another device's


def test_the_end_device_is_also_matched_by_its_sfdi(tmp_path):
    server = Server()
    server.resource("/dcap", dcap(EndDeviceListLink="/edev"))
    server.list("/edev", "EndDeviceList", [
        f'<EndDevice href="/edev/7"><sFDI>{sfdi_of_lfdi(DEV_LFDI)}</sFDI>'
        f'<FunctionSetAssignmentsListLink href="/edev/7/fsa"/></EndDevice>'])
    server.list("/edev/7/fsa", "FunctionSetAssignmentsList",
                ['<FunctionSetAssignments><DERProgramListLink href="/derp"/></FunctionSetAssignments>'])
    server.list("/derp", "DERProgramList", [program(server, "/derp/1", 1, [control("E")])])
    run_with(tmp_path, server, poll)
    assert list(stored()) == ["E"]


def test_the_sfdi_is_derived_as_ieee_2030_5_defines_it():
    """IEEE 2030.5's worked example: the LFDI truncated to 36 bits,
    0x3E4F45AB3 = 16726121139, plus its check digit 1."""
    assert sfdi_of_lfdi("3E4F45AB31EDFE5B67E343E5E4562E31984E23E5") == 167261211391


def test_programs_listed_both_for_all_devices_and_for_this_one_are_followed_once(tmp_path):
    server = Server()
    server.resource("/dcap", dcap(DERProgramListLink="/derp", EndDeviceListLink="/edev"))
    server.list("/edev", "EndDeviceList", [
        f'<EndDevice><lFDI>{DEV_LFDI}</lFDI><FunctionSetAssignmentsListLink href="/fsa"/></EndDevice>'])
    server.list("/fsa", "FunctionSetAssignmentsList",
                ['<FunctionSetAssignments><DERProgramListLink href="/derp"/></FunctionSetAssignments>'])
    server.list("/derp", "DERProgramList", [program(server, "/derp/1", 1, [control("E")])])
    run_with(tmp_path, server, poll)
    assert list(stored()) == ["E"]
    assert sum(get.startswith("/derp/1/derc") for get in server.gets) == 1


def test_every_page_of_a_list_is_read(tmp_path):
    server = one_program_server([control(f"E{i:02d}", start=T + 60 * i, duration=60) for i in range(10)],
                                page_size=3)
    run_with(tmp_path, server, poll)
    assert list(stored()) == [f"E{i:02d}" for i in range(10)]
    assert [get for get in server.gets if get.startswith("/derp/1/derc")] == [
        f"/derp/1/derc?s={start}&l=255" for start in (0, 3, 6, 9)]


def test_at_most_15_programs_are_followed_the_lowest_primacy_first(tmp_path):
    server = Server()
    server.resource("/dcap", dcap(DERProgramListLink="/derp"))
    server.list("/derp", "DERProgramList",
                [program(server, f"/derp/{p}", p, [control(f"P{p:02d}", start=T + p)]) for p in range(16, -1, -1)])
    g = run_with(tmp_path, server, poll)
    assert sorted(stored()) == [f"P{p:02d}" for p in range(15)]
    [found] = audit_rows(g.audit, "ieee_discovery")
    assert found["detail"].startswith("15 DER program(s): /derp/0 (primacy 0), /derp/1 (primacy 1), ")


def test_discovery_is_audited_when_it_changes_not_on_every_poll(tmp_path):
    async def scenario(g, server):
        for _ in range(3):
            await poll(g, server)
        server.list("/derp", "DERProgramList", [program(server, "/derp/1", 1, []), program(server, "/derp/2", 2, [])],
                    pollRate=600)
        await poll(g, server)
    g = run_with(tmp_path, one_program_server(), scenario)
    assert [row["detail"] for row in audit_rows(g.audit, "ieee_discovery")] == [
        "1 DER program(s): /derp/1 (primacy 1); no pollRate: polling every 5 s",
        "2 DER program(s): /derp/1 (primacy 1), /derp/2 (primacy 2); utility pollRate 600 s"]


# ── The poll rate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("rates, after_success, after_failure", [
    ((), 5, 5),                          # no pollRate: IEEE_POLL_INTERVAL_SEC
    ((600,), 600, 5),                    # the utility's pollRate; retried sooner after a failure
    ((600, 300), 300, 5),                # several program lists: the smallest
    ((2,), 2, 2),
    ((0,), 1, 1),                        # kept within 1 s ...
    ((10 ** 6,), 86_400, 5),             # ... and one day
])
def test_the_utilitys_poll_rate_decides_when_to_poll_again(tmp_path, rates, after_success, after_failure):
    server = Server()
    server.resource("/dcap", dcap(EndDeviceListLink="/edev"))
    lists = [f"/derp{i}" for i in range(len(rates))] or ["/derp"]
    server.list("/edev", "EndDeviceList", [
        f'<EndDevice><lFDI>{DEV_LFDI}</lFDI><FunctionSetAssignmentsListLink href="/fsa"/></EndDevice>'])
    server.list("/fsa", "FunctionSetAssignmentsList",
                [f'<FunctionSetAssignments><DERProgramListLink href="{href}"/></FunctionSetAssignments>'
                 for href in lists])
    for i, href in enumerate(lists):
        attributes = {} if not rates else {"pollRate": rates[i]}
        server.list(href, "DERProgramList", [program(server, f"{href}/1", 1, [])], **attributes)
    g = run_with(tmp_path, server, poll)
    assert (g.client.next_poll_in(True), g.client.next_poll_in(False)) == (after_success, after_failure)


# ── Which event wins ─────────────────────────────────────────────────────

def two_programs(events_1, events_2):
    """Programs /p1 (primacy 1) and /p2 (primacy 2), for all devices."""
    server = Server()
    server.resource("/dcap", dcap(DERProgramListLink="/derp"))
    server.list("/derp", "DERProgramList", [program(server, "/p2", 2, events_2), program(server, "/p1", 1, events_1)])
    return server


async def poll_and_step(g, server, *times):
    await poll(g, server)
    for now in times:
        await g.sched.step(now)
    await g.client.send_responses(g.http)


def test_the_program_with_the_lower_primacy_wins_even_when_its_event_starts_first(tmp_path):
    """The previous rule (the latest start wins) would have let B take over."""
    server = two_programs([control("A", output_w=30_000, respond="03")],                       # primacy 1
                          [control("B", start=T + 100, output_w=-50_000, respond="03")])      # primacy 2
    g = run_with(tmp_path, server, lambda g, s: poll_and_step(g, s, T, T + 100))
    assert stored()["A"][0] == "active" and stored()["B"][0] == "superseded"
    assert register(g.fake) == 30                                   # A still discharging
    assert server.statuses("B") == [1, 14]                          # aborted for another program's event


def test_an_event_of_a_higher_priority_program_supersedes_a_running_one(tmp_path):
    server = two_programs([control("B", start=T + 100, output_w=-50_000, respond="03")],      # primacy 1
                          [control("A", output_w=30_000, respond="03")])                      # primacy 2
    g = run_with(tmp_path, server, lambda g, s: poll_and_step(g, s, T, T + 100))
    assert (stored()["A"][0], stored()["B"][0]) == ("superseded", "active")
    assert register(g.fake) == -50
    assert server.statuses("A") == [1, 2, 14]


def test_within_a_program_the_newer_event_wins_even_when_it_starts_first(tmp_path):
    """IEEE 2030.5-2018 §10.2.3.3: of two overlapping events of equal
    primacy, the one with the newer creationTime supersedes the other."""
    server = one_program_server([control("NEW", created=2000, output_w=30_000, respond="03"),
                                 control("OLD", start=T + 100, created=1000, output_w=-50_000, respond="03")])
    g = run_with(tmp_path, server, lambda g, s: poll_and_step(g, s, T, T + 100))
    assert (stored()["NEW"], stored()["OLD"]) == (("active", "/derp/1", 1, 2000), ("superseded", "/derp/1", 1, 1000))
    assert register(g.fake) == 30
    assert server.statuses("OLD") == [1, 7]                         # superseded within its own program


def test_without_creation_times_the_later_start_still_wins(tmp_path):
    server = one_program_server([control("A", output_w=30_000), control("B", start=T + 100, output_w=-50_000)])
    g = run_with(tmp_path, server, lambda g, s: poll_and_step(g, s, T, T + 100))
    assert (stored()["A"][0], stored()["B"][0]) == ("superseded", "active")


# ── DefaultDERControl ────────────────────────────────────────────────────

def test_the_default_control_applies_between_events(tmp_path):
    async def scenario(g, server):
        g.seen = []
        await poll(g, server)
        for now in (T - 100, T, T + 600):                   # before, during and after event E
            await g.sched.step(now)
            g.seen.append((g.state.authority["authority"], register(g.fake)))
        server.list("/derp", "DERProgramList", [program(server, "/derp/1", 1, [control("E")])])  # no default now
        await poll(g, server)
        await g.sched.step(T + 700)
        g.seen.append((g.state.authority["authority"], register(g.fake)))
    g = run_with(tmp_path, one_program_server([control("E", output_w=-20_000)],
                                              default_control(target_w(30_000))), scenario)
    assert g.seen == [(AUTH_DEFAULT, 30), (AUTH_SCHEDULE, -20), (AUTH_DEFAULT, 30), (AUTH_SAFE_IDLE, 0)]
    assert [(r["outcome"], r["detail"]) for r in audit_rows(g.audit, "default_control")] == [
        ("updated", "program=/derp/1 primacy=1: power_kw=-30.0 control=opModTargetW 30000 x 10^0 W"),
        ("removed", "program=/derp/1: DefaultDERControl no longer listed")]


@pytest.mark.parametrize("base, settings, authority, register_kw, outcome, detail", [
    ('<opModVoltVar href="/derp/1/dc/1"/>', {}, AUTH_SAFE_IDLE, 0, "not_applied",
     "program=/derp/1 primacy=1: not applied: opModVoltVar cannot be executed by this gateway"),
    (target_w(30_000) + "<opModMaxLimW>5000</opModMaxLimW>", {}, AUTH_SAFE_IDLE, 0, "not_applied",
     "program=/derp/1 primacy=1: not applied: opModMaxLimW cannot be executed by this gateway"),
    ("<opModConnect>true</opModConnect>", {}, AUTH_SAFE_IDLE, 0, "no_setpoint",
     "program=/derp/1 primacy=1: no active-power setpoint"),
    ("<opModConnect>false</opModConnect>", {}, AUTH_DEFAULT, 0, "updated",
     "program=/derp/1 primacy=1: power_kw=+0.0 control=opModConnect false: 0 kW"),
    (target_w(-10_000), {"setGradW": 100}, AUTH_DEFAULT, -10, "updated",
     "program=/derp/1 primacy=1: power_kw=+10.0 control=opModTargetW -10000 x 10^0 W; setGradW not applied"),
])
def test_a_default_control_is_applied_only_as_a_whole(tmp_path, base, settings, authority, register_kw,
                                                      outcome, detail):
    async def scenario(g, server):
        await poll(g, server)
        await g.sched.step(T)
    g = run_with(tmp_path, one_program_server(default=default_control(base, **settings)), scenario)
    assert (g.state.authority["authority"], register(g.fake)) == (authority, register_kw)
    [row] = audit_rows(g.audit, "default_control")
    assert (row["outcome"], row["detail"]) == (outcome, detail)


def test_the_scheduler_never_applies_a_default_marked_with_a_problem(tmp_path):
    """The scheduler's own contract, whatever power such a default carries."""
    async def run():
        driver, state = await make_driver(FakeBMS())
        sched = Scheduler({}, state, driver)
        sched.set_default_controls([
            {"program": "/p1", "primacy": 1, "power_kw": -30.0, "control": "x", "problem": "cannot be executed"},
            {"program": "/p2", "primacy": 2, "power_kw": -10.0, "control": "opModTargetW", "problem": None}])
        first = sched.decide(T)
        sched.set_default_controls([
            {"program": "/p1", "primacy": 1, "power_kw": -30.0, "control": "x", "problem": "cannot be executed"}])
        return first, sched.decide(T)
    first, second = asyncio.run(run())
    assert (first.authority, first.power_kw) == (AUTH_DEFAULT, -10.0)     # the next program's default
    assert (second.authority, second.power_kw) == (AUTH_SAFE_IDLE, 0.0)


def test_a_default_that_cannot_be_read_this_time_is_kept(tmp_path):
    async def scenario(g, server):
        await poll(g, server)
        server.fail["/derp/1/dderc"] = 500
        await poll(g, server)                                  # the events were read: the poll succeeds
        await g.sched.step(T)
    g = run_with(tmp_path, one_program_server(default=default_control(target_w(30_000))), scenario)
    assert (g.state.authority["authority"], register(g.fake)) == (AUTH_DEFAULT, 30)
    assert len(audit_rows(g.audit, "default_control")) == 1


def test_the_default_keeps_running_through_a_utility_outage(tmp_path):
    async def scenario(g, server):
        await poll(g, server)
        await g.sched.step(T)
        server.fail["/dcap"] = 503
        for i in range(1, 4):
            assert await g.client._poll(g.http) is False
            await g.sched.step(T + i)
    g = run_with(tmp_path, one_program_server(default=default_control(target_w(30_000))), scenario)
    assert g.rm.reliability_state is ReliabilityState.AUTONOMOUS    # the utility's standing instruction
    assert register(g.fake) == 30


# ── When the utility cannot be read ──────────────────────────────────────

@pytest.mark.parametrize("path, detail", [
    ("/dcap", "HTTP 503"),
    ("/derp", "HTTP 503"),
    ("/derp/1/derc", "HTTP 503"),
])
def test_any_list_that_cannot_be_read_fails_the_poll_and_nothing_is_lost(tmp_path, path, detail):
    async def scenario(g, server):
        await poll(g, server)
        server.fail[path] = 503
        assert await g.client._poll(g.http) is False
    g = run_with(tmp_path, one_program_server([control("E")]), scenario)
    assert list(stored()) == ["E"]
    assert [row["detail"] for row in audit_rows(g.audit, "grid_poll")] == [detail]
    assert g.state.grid_connected is False


@pytest.mark.parametrize("dcap_xml, detail", [
    ("<html><body>Please log in</body></html>", "response is not an IEEE 2030.5 DeviceCapability"),
    (dcap(DERProgramListLink="http://utility.example/derp"), "http://utility.example/derp is not on the utility server"),
])
def test_what_is_not_the_utilitys_ieee_2030_5_is_not_followed(tmp_path, dcap_xml, detail):
    server = one_program_server([control("E")])
    server.resource("/dcap", dcap_xml)

    async def scenario(g, server):
        assert await g.client._poll(g.http) is False
    g = run_with(tmp_path, server, scenario)
    assert stored() == {}
    assert [row["detail"] for row in audit_rows(g.audit, "grid_poll")] == [detail]


def test_randomization_is_recorded_as_not_applied(tmp_path):
    g = run_with(tmp_path, one_program_server([control("E", extra="<randomizeStart>60</randomizeStart>")]), poll)
    [received] = audit_rows(g.audit, "schedule_received")
    assert received["detail"].endswith("randomizeStart 60 s not applied (no randomization)")


def test_the_entry_point_must_be_a_path(monkeypatch):
    for name in ("IEEE_SERVER_URL", "IEEE_POLL_INTERVAL_SEC", "TLS_CA_CERT", "TLS_CLIENT_CERT", "TLS_CLIENT_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IEEE_DCAP_PATH", "dcap")
    with pytest.raises(main_mod.ConfigError, match="IEEE_DCAP_PATH must be a path on the utility server"):
        main_mod.load_config()


# ── The utility simulator serves the same tree ───────────────────────────

def test_the_simulator_serves_discovery_paging_and_a_default(monkeypatch):
    for name in ("SIM_POWER_CONTROL", "SIM_POLL_RATE", "SIM_DEVICE_LFDI", "DER_SET_MAX_W"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SIM_PAGE_SIZE", "2")
    monkeypatch.setenv("SIM_POLL_RATE", "600")
    spec = importlib.util.spec_from_file_location("grid_server_under_test", ROOT / "simulators" / "fake_grid_server.py")
    grid = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grid)
    client = grid.app.test_client()

    def get(path):
        return ET.fromstring(client.get(path).get_data(as_text=True))
    root = get("/dcap")
    device = get(root.find("{*}EndDeviceListLink").get("href")).find("{*}EndDevice")
    assert (device.findtext("{*}lFDI"), device.findtext("{*}sFDI")) == (DEV_LFDI, str(sfdi_of_lfdi(DEV_LFDI)))
    assignments = get(device.find("{*}FunctionSetAssignmentsListLink").get("href"))
    programs = get(assignments.find("{*}FunctionSetAssignments/{*}DERProgramListLink").get("href"))
    assert programs.get("pollRate") == "600"

    client.post("/simulate/batch", json={"events": [{"power_kw": 10, "start_offset_sec": 60 * (j + 1),
                                                     "duration_sec": 30} for j in range(5)]})
    derc = programs.find("{*}DERProgram/{*}DERControlListLink").get("href")
    pages = [get(f"{derc}?s={start}&l=255") for start in (0, 2, 4)]
    assert [(page.get("all"), page.get("results")) for page in pages] == [("5", "2"), ("5", "2"), ("5", "1")]
    assert pages[0].find("{*}DERControl/{*}creationTime") is not None

    assert programs.find("{*}DERProgram/{*}DefaultDERControlLink") is None
    client.post("/simulate/default", json={"power_kw": -30})
    program = get("/derp").find("{*}DERProgram")
    default = get(program.find("{*}DefaultDERControlLink").get("href"))
    assert default.find("{*}DERControlBase/{*}opModTargetW/{*}value").text == "30000"   # 30 kW out (discharge)
    client.post("/simulate/default", json={"power_kw": None})
    assert get("/derp").find("{*}DERProgram/{*}DefaultDERControlLink") is None
