"""
simulators/fake_grid_server.py
Fake IEEE 2030.5 Grid Server

Run this INSTEAD of a real utility server during testing.
It serves DERControl XML at the endpoint the EMS Bridge polls, AND hosts
a browser dashboard (/dashboard) so a non-technical grid operator can log
in and send charge/discharge commands without touching a terminal.

Usage:
    python simulators/fake_grid_server.py
    Then open http://localhost:5001/dashboard in a browser.
    Demo login: operator / invinity2026

The JSON/XML API below is unauthenticated and unchanged — the EMS Bridge
gateway and the automated test suite talk to it directly, exactly as
before. Only the human-facing /dashboard is behind the login.

Endpoints:
    GET  /dashboard            ← operator web UI (login required)
    GET  /login, POST /login   ← demo session login
    POST /logout               ← end the session
    GET  /dcap                 ← DeviceCapability: where the EMS Bridge starts
    GET  /edev                 ← EndDeviceList: the gateway, by its LFDI / SFDI
    GET  /edev/1/fsa           ← its FunctionSetAssignments: the DER programs to follow
    GET  /derp                 ← DERProgramList: one program, primacy 1
    GET  /derp/1/derc          ← the program's DERControlList (the queued events)
    GET  /derp/1/dderc         ← the program's DefaultDERControl, when one is set
    POST /rsps/1/rsp           ← EMS Bridge's DERControlResponses (each event's replyTo)
    GET  /tm                   ← Time: the utility's clock (SIM_CLOCK_OFFSET_SEC shifts it)
    GET  /edev/1/der           ← the gateway's DERList; its DER's reports are PUT to
    PUT  /edev/1/der/1/dercap  ←   DERCapability
    PUT  /edev/1/der/1/derg    ←   DERSettings
    PUT  /edev/1/der/1/ders    ←   DERStatus
    POST /mup                  ← create a MirrorUsagePoint (metering); 201 + Location /mup/N
    POST /mup/<n>              ← its MirrorMeterReadings
    POST /simulate/charge      ← inject a charge command
    POST /simulate/discharge   ← inject a discharge command
    POST /simulate/batch       ← queue several future-dated events at once
    POST /simulate/raw         ← paste a hand-written DERControl XML body directly
    POST /simulate/idle        ← clear all events
    POST /simulate/default     ← set ({"power_kw": 50}) or clear ({"power_kw": null}) the DefaultDERControl
    GET  /status               ← see what's currently queued (JSON)

Lists are served a page at a time, as the `s` (start) and `l` (limit)
query parameters ask, and never more than SIM_PAGE_SIZE items (default
100) per page. SIM_POLL_RATE, when set, is the program list's pollRate
(seconds); by default there is none, so the gateway uses its own
IEEE_POLL_INTERVAL_SEC. SIM_DEVICE_LFDI is the EndDevice's LFDI (default:
the development client certificate's, the one the gateway presents to a
local simulator). SIM_DER_POLL_RATE, when set, is the DERList's pollRate:
how often the gateway sends DERStatus (IEEE 2030.5 default 900 s).
SIM_CLOCK_OFFSET_SEC makes this utility's clock run that many seconds from
the machine's: /tm tells the gateway, and every event time is on it. What
the gateway reports shows under "reports" in /status.

Batch example — schedule a charge window tomorrow morning and a
discharge window tomorrow evening in one call:

    curl -X POST http://localhost:5001/simulate/batch \\
         -H "Content-Type: application/json" \\
         -d '{"events": [
               {"power_kw": 500,  "start_offset_sec": 60,   "duration_sec": 3600},
               {"power_kw": -300, "start_offset_sec": 7200, "duration_sec": 1800}
             ]}'

Each event's "start_offset_sec" is relative to now, so you can push
events minutes, hours, or days into the future to simulate a
multi-day calendar of charge/discharge windows.

power_kw here is in the gateway's convention (+ = charge). On the wire
each event is an IEEE 2030.5 active-power control, which counts the
power the DER puts out (+ = discharge). SIM_POWER_CONTROL picks the form:
    opModTargetW  (default) watts, a 16-bit value x 10^multiplier
    opModFixedW   hundredths of a percent of the DER's power ratings,
                  taken from the gateway's DER_SET_MAX_W,
                  DER_SET_MAX_CHARGE_RATE_W and DER_SET_MAX_DISCHARGE_RATE_W
                  (a real utility has them on file); needs DER_SET_MAX_W

Every event asks for responses as CSIP's example DERControl does
(replyTo="/rsps/1/rsp", responseRequired="03": received, plus the
specific ones such as started and completed).
"""

from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string, abort
from functools import wraps

from capstone.ieee_client import DER_RATING_SETTINGS, DEV_CERTIFICATE, PERCENT_SCALE, der_ratings, fixed_w_reference
from capstone.responses import lfdi_of_certificate, sfdi_of_lfdi
from capstone.scheduler import set_force_idle
from pathlib import Path
import importlib.util
import json
import math
import os
import secrets
import ssl
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

# The plain-English dashboard pages live beside this file (loaded by path so this works
# however the simulator is started: as a script, or imported by the tests).
_spec = importlib.util.spec_from_file_location("dashboard_pages", Path(__file__).with_name("dashboard_pages.py"))
dashboard_pages = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dashboard_pages)

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", secrets.token_hex(32))

# The gateway (capstone/__main__.py) publishes a live status snapshot here
# every ~2s. We only ever READ this file — the grid-operator side never
# talks Modbus directly, preserving the system boundary (see Sprint 2
# report Fig. 1): the middleware is the sole authority on battery state,
# and the dashboard just displays what it chooses to publish.
BATTERY_STATUS_FILE = Path("schedule_store/live_status.json")
# The gateway reads the BMS every 10 s (F-04); BMS data older than two
# missed reads and some margin is shown as stale, not as current.
DEVICE_STALE_SEC = 25

# Demo-only operator account. Not production security — just enough to
# make /dashboard behave like a real gated login for demo purposes.
DEMO_USERNAME = os.environ.get("DASHBOARD_USERNAME", "operator")
DEMO_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "invinity2026")
DEFAULT_DEMO_PASSWORD = "invinity2026"

# PUBLIC_MODE=1 is for a copy that is reachable from the internet. Only the dashboard, its
# APIs and its static files answer to the world; the machine-to-machine endpoints (the IEEE
# 2030.5 server, /simulate/*, /status) answer only to the gateway on the same server, and
# the server refuses to start with the published demo password.
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "0") == "1"
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "5001"))
MIN_PUBLIC_PASSWORD_LEN = 12
LOGIN_MAX_FAILURES, LOGIN_WINDOW_SEC = 5, 900
_PUBLIC_PREFIXES = ("/login", "/logout", "/dashboard", "/api/", "/reports/", "/static/")
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_login_failures: dict = {}


def check_public_config() -> None:
    """Refuse to run a public copy that anyone could log in to."""
    if not PUBLIC_MODE:
        return
    if DEMO_PASSWORD == DEFAULT_DEMO_PASSWORD or len(DEMO_PASSWORD) < MIN_PUBLIC_PASSWORD_LEN:
        raise SystemExit("PUBLIC_MODE needs DASHBOARD_PASSWORD set to a new password of at least "
                         f"{MIN_PUBLIC_PASSWORD_LEN} characters (the demo password is published in the repository).")
    if "DASHBOARD_SECRET_KEY" not in os.environ:
        raise SystemExit("PUBLIC_MODE needs DASHBOARD_SECRET_KEY set, so sessions survive a restart.")


def _client_ip() -> str:
    """The caller's address. Behind the host's proxy the last X-Forwarded-For entry is the one
    the proxy itself added, so it cannot be forged by the caller."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if PUBLIC_MODE and forwarded:
        return forwarded.split(",")[-1].strip()
    return request.remote_addr or "unknown"


def _login_blocked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SEC]
    _login_failures[ip] = recent
    return len(recent) >= LOGIN_MAX_FAILURES


@app.before_request
def _public_mode_guard():
    if not PUBLIC_MODE:
        return None
    path = request.path
    if path == "/" or path.startswith(_PUBLIC_PREFIXES) or request.remote_addr in _LOOPBACK:
        return None
    abort(404)          # a machine endpoint, asked by someone other than the gateway


@app.after_request
def _security_headers(response):
    if PUBLIC_MODE:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.route("/")
def home():
    return redirect(url_for("dashboard"))

# Active event store. Saved to schedule_store/utility_queue.json when this runs as a script (the
# way a real utility server keeps its schedule), so restarting the simulator does not lose the queue.
active_events: list[dict] = []
QUEUE_FILE = Path("schedule_store/utility_queue.json")
_persist_queue = False      # set when run as a script; importing this module never writes files


def _persist() -> None:
    if not _persist_queue:
        return
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = QUEUE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(active_events), encoding="utf-8")
        for _ in range(5):          # os.replace can briefly fail on Windows while another process reads
            try:
                os.replace(tmp, QUEUE_FILE)
                break
            except PermissionError:
                time.sleep(0.05)
    except OSError as e:
        print(f"[SIM] could not save the event queue: {e}")


def _load_queue() -> None:
    try:
        saved = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    now = time.time() + float(os.environ.get("SIM_CLOCK_OFFSET_SEC", "0") or 0)
    need = {"mrid", "start", "duration", "power_kw"}
    active_events[:] = [e for e in saved if isinstance(e, dict) and need <= e.keys() and e["start"] + e["duration"] > now]
    print(f"[SIM] Restored {len(active_events)} event(s) from {QUEUE_FILE}")

# The program's DefaultDERControl ({"power_kw", "control"}), or None.
default_control: dict | None = None

# DERControlResponses received from the EMS Bridge (F-01), keyed by the
# event's mRID (the response's subject) — in-memory for simulation.
acknowledgements: dict = {}
REPLY_TO = "/rsps/1/rsp"
RESPONSE_REQUIRED = "03"

# What the gateway reported: its DER's capability, settings and status (the
# last PUT of each, as {element: text}) and its metering (MirrorUsagePoints
# and their latest readings). In-memory for simulation.
reports: dict = {"capability": None, "settings": None, "status": None, "usage_points": {}}


def _server_now() -> float:
    """This utility's clock: the machine's, shifted by SIM_CLOCK_OFFSET_SEC."""
    return time.time() + float(os.environ.get("SIM_CLOCK_OFFSET_SEC", "0") or 0)


# Last time the EMS Bridge polled /derp/1/derc — lets the dashboard show
# whether the gateway is actively checking in ("connected") or has gone
# quiet ("stale"), without the grid server needing any visibility into
# the gateway's internals.
last_poll_at: float | None = None


def _finite_number(value, name: str) -> float:
    """A number from a request. One NaN or infinity stored here made every
    later /derp/1/derc fail with HTTP 500, which the gateway (rightly)
    reads as the utility being down."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _whole_number(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, got {value!r}") from None


POWER_CONTROLS = ("opModTargetW", "opModFixedW")
INT16_MAX = 32767


def _power_control() -> str:
    """The active-power control this utility sends (SIM_POWER_CONTROL)."""
    control = os.environ.get("SIM_POWER_CONTROL", "opModTargetW")
    if control not in POWER_CONTROLS:
        raise ValueError(f"SIM_POWER_CONTROL must be opModTargetW or opModFixedW, got {control!r}")
    return control


def _der_ratings() -> dict:
    """The DER's power ratings, from the gateway's own settings."""
    config = {}
    for _, key in DER_RATING_SETTINGS:
        raw = os.environ.get(key.upper(), "").strip()
        try:
            config[key] = int(raw) if raw else None
        except ValueError:
            raise ValueError(f"{key.upper()} must be a whole number of watts, got {raw!r}") from None
    return der_ratings(config)


def _control_xml(power_kw: float) -> str:
    """The DERControlBase control asking the DER for power_kw (+ = charge)
    in the configured form. Raises ValueError if that form cannot carry it."""
    output_w = -power_kw * 1000               # IEEE 2030.5 counts output: + = discharge
    if _power_control() == "opModFixedW":
        direction = 1 if output_w > 0 else -1 if output_w < 0 else 0
        name, watts = fixed_w_reference(_der_ratings(), direction)
        hundredths = round(output_w / watts * PERCENT_SCALE)
        if abs(hundredths) > PERCENT_SCALE:
            raise ValueError(f"{abs(power_kw):g} kW is more than 100 % of {name} ({watts / 1000:g} kW)")
        return f"<opModFixedW>{hundredths}</opModFixedW>"
    for multiplier in range(10):              # the smallest power of ten whose value fits 16 bits
        value = round(output_w / 10 ** multiplier)
        if abs(value) <= INT16_MAX:
            return f"<opModTargetW><multiplier>{multiplier}</multiplier><value>{value}</value></opModTargetW>"
    raise ValueError(f"{abs(power_kw):g} kW is more than opModTargetW can carry")


def _power_kw_from(ctrl) -> float:
    """A DERControl's active-power setpoint in the gateway's convention
    (+ = charge), from its opModTargetW or opModFixedW."""
    base = ctrl.find("{*}DERControlBase")
    target = None if base is None else base.find("{*}opModTargetW")
    fixed = None if base is None else base.find("{*}opModFixedW")
    if target is not None and fixed is not None:
        raise ValueError("both opModTargetW and opModFixedW: which one applies is not defined")
    if target is not None:
        multiplier = int(target.findtext("{*}multiplier", default="0"))
        if abs(multiplier) > 9:
            raise ValueError(f"multiplier {multiplier} is outside -9..9")
        output_w = int(target.findtext("{*}value")) * 10 ** multiplier
    elif fixed is not None:
        hundredths = int((fixed.text or "").strip())
        if abs(hundredths) > PERCENT_SCALE:
            raise ValueError(f"opModFixedW {hundredths} is outside -10000..10000")
        _, watts = fixed_w_reference(_der_ratings(), hundredths)
        output_w = hundredths * watts / PERCENT_SCALE
    else:
        raise ValueError("no opModTargetW or opModFixedW setpoint")
    return -output_w / 1000


def _event_status_xml(ev: dict) -> str:
    """A cancelled event stays in the list, marked Cancelled (EventStatus.currentStatus 2):
    that is how an IEEE 2030.5 utility stops an event the EMS Bridge already holds. Merely
    dropping it from the list does not, by design: the Bridge keeps running approved events
    through a lost link."""
    if not ev.get("cancelled"):
        return ""
    return (f"\n    <EventStatus><currentStatus>2</currentStatus><dateTime>{int(ev.get('cancelled_at', ev['start']))}</dateTime>"
            "<potentiallySuperseded>false</potentiallySuperseded></EventStatus>")


def _der_control_xml(ev: dict) -> str:
    control = ev.get("control") or _control_xml(ev["power_kw"])
    return f"""
  <DERControl replyTo="{REPLY_TO}" responseRequired="{RESPONSE_REQUIRED}">
    <mRID>{xml_escape(str(ev['mrid']))}</mRID>
    <creationTime>{ev.get('created', ev['start'])}</creationTime>{_event_status_xml(ev)}
    <interval>
      <start>{ev['start']}</start>
      <duration>{ev['duration']}</duration>
    </interval>
    <DERControlBase>
      {control}
    </DERControlBase>
  </DERControl>"""


def make_derc_xml(events: list[dict]) -> str:
    """Build an IEEE 2030.5 DERControlList XML response holding all `events`."""
    return _list_xml("DERControlList", "/derp/1/derc", [_der_control_xml(ev) for ev in events], len(events))


def _page_size() -> int:
    return max(1, int(os.environ.get("SIM_PAGE_SIZE", "100")))


def _page(items: list) -> list:
    """The items the request's `s` (start) and `l` (limit) ask for, never
    more than SIM_PAGE_SIZE. Raises ValueError for a bad s or l."""
    start = max(0, _whole_number(request.args.get("s", 0), "s"))
    limit = request.args.get("l")
    limit = _page_size() if limit is None else max(0, min(_whole_number(limit, "l"), _page_size()))
    return items[start:start + limit]


def _list_xml(name: str, href: str, items: list[str], total: int, attributes: str = "") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<{name} xmlns="urn:ieee:std:2030.5:ns" href="{href}" '
            f'all="{total}" results="{len(items)}"{attributes}>{"".join(items)}\n</{name}>')


def _xml_response(text: str):
    return app.response_class(text, mimetype="application/sep+xml")


def _paged(name: str, href: str, items: list[str], attributes: str = ""):
    try:
        page = _page(items)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return _xml_response(_list_xml(name, href, page, len(items), attributes))


def _device_lfdi() -> str:
    return (os.environ.get("SIM_DEVICE_LFDI") or lfdi_of_certificate(DEV_CERTIFICATE)).upper()


@app.route("/dcap")
def get_dcap():
    """DeviceCapability: where an IEEE 2030.5 client starts (CSIP §5.2.3.1)."""
    return _xml_response('<?xml version="1.0" encoding="UTF-8"?>\n'
                         '<DeviceCapability xmlns="urn:ieee:std:2030.5:ns" href="/dcap">'
                         '<DERProgramListLink href="/derp" all="1"/><TimeLink href="/tm"/>'
                         '<EndDeviceListLink href="/edev" all="1"/>'
                         f'<MirrorUsagePointListLink href="/mup" all="{len(reports["usage_points"])}"/>'
                         '</DeviceCapability>')


@app.route("/edev")
def get_end_devices():
    lfdi = _device_lfdi()
    device = (f'<EndDevice href="/edev/1"><DERListLink href="/edev/1/der" all="1"/>'
              f'<lFDI>{lfdi}</lFDI><sFDI>{sfdi_of_lfdi(lfdi)}</sFDI>'
              f'<changedTime>{int(_server_now())}</changedTime>'
              f'<FunctionSetAssignmentsListLink href="/edev/1/fsa" all="1"/></EndDevice>')
    return _paged("EndDeviceList", "/edev", [device])


@app.route("/tm")
def get_time():
    """Time: the utility's clock (IEEE 2030.5 Time function set); no DST, UTC."""
    return _xml_response('<?xml version="1.0" encoding="UTF-8"?>\n'
                         '<Time xmlns="urn:ieee:std:2030.5:ns" href="/tm" pollRate="900">'
                         f'<currentTime>{int(_server_now())}</currentTime><dstEndTime>0</dstEndTime>'
                         '<dstOffset>0</dstOffset><dstStartTime>0</dstStartTime><quality>7</quality>'
                         '<tzOffset>0</tzOffset></Time>')


@app.route("/edev/1/der")
def get_ders():
    rate = os.environ.get("SIM_DER_POLL_RATE", "").strip()
    der = ('<DER href="/edev/1/der/1"><DERCapabilityLink href="/edev/1/der/1/dercap"/>'
           '<DERSettingsLink href="/edev/1/der/1/derg"/><DERStatusLink href="/edev/1/der/1/ders"/></DER>')
    return _paged("DERList", "/edev/1/der", [der], f' pollRate="{int(rate)}"' if rate else "")


DER_REPORTS = {"dercap": ("capability", "DERCapability"), "derg": ("settings", "DERSettings"),
               "ders": ("status", "DERStatus")}


def _flatten(element, prefix: str = "") -> dict:
    """{path: text} of an XML element's leaves, for /status."""
    found = {}
    for child in element:
        name = prefix + child.tag.split("}")[-1]
        if len(child):
            found.update(_flatten(child, name + "."))
        else:
            found[name] = (child.text or "").strip()
    return found


@app.route("/edev/1/der/1/<resource>", methods=["GET", "PUT"])
def der_report(resource):
    """The gateway PUTs its DER's capability, settings and status here."""
    if resource not in DER_REPORTS:
        return jsonify({"status": "error", "message": "no such resource"}), 404
    key, tag = DER_REPORTS[resource]
    if request.method == "GET":
        stored = reports[key]
        return _xml_response(stored["xml"]) if stored else (jsonify({"status": "error", "message": "none yet"}), 404)
    try:
        body = ET.fromstring(request.get_data(as_text=True))
    except ET.ParseError as e:
        return jsonify({"status": "error", "message": f"Invalid XML: {e}"}), 400
    if body.tag.split("}")[-1] != tag:
        return jsonify({"status": "error", "message": f"expected a {tag}"}), 400
    reports[key] = {"received_at": int(_server_now()), "values": _flatten(body),
                    "xml": request.get_data(as_text=True)}
    print(f"[SIM] {tag} received from the gateway")
    return "", 204


@app.route("/mup", methods=["GET", "POST"])
def mirror_usage_points():
    """The gateway creates its MirrorUsagePoint here (POST), once; its
    readings then go to the Location returned."""
    if request.method == "GET":
        items = [f'<MirrorUsagePoint href="/mup/{n}"><mRID>{p["mRID"]}</mRID></MirrorUsagePoint>'
                 for n, p in reports["usage_points"].items()]
        return _paged("MirrorUsagePointList", "/mup", items)
    try:
        body = ET.fromstring(request.get_data(as_text=True))
    except ET.ParseError as e:
        return jsonify({"status": "error", "message": f"Invalid XML: {e}"}), 400
    mrid, lfdi = body.findtext("{*}mRID"), body.findtext("{*}deviceLFDI")
    if body.tag.split("}")[-1] != "MirrorUsagePoint" or not mrid or not lfdi:
        return jsonify({"status": "error", "message": "a MirrorUsagePoint needs mRID and deviceLFDI"}), 400
    for n, point in reports["usage_points"].items():
        if point["mRID"] == mrid:                   # the same one again: where it already is
            response = app.response_class(status=204)
            response.headers["Location"] = f"/mup/{n}"
            return response
    n = str(len(reports["usage_points"]) + 1)
    reports["usage_points"][n] = {"mRID": mrid, "deviceLFDI": lfdi, "readings": {}}
    _store_readings(n, body)
    print(f"[SIM] MirrorUsagePoint /mup/{n} created by the gateway")
    response = app.response_class(status=201)
    response.headers["Location"] = f"/mup/{n}"
    return response


def _store_readings(n: str, element):
    for reading in element.iter("{urn:ieee:std:2030.5:ns}MirrorMeterReading"):
        reports["usage_points"][n]["readings"][reading.findtext("{*}description") or reading.findtext("{*}mRID")] = {
            "value": reading.findtext("{*}Reading/{*}value"),
            "powerOfTenMultiplier": reading.findtext("{*}ReadingType/{*}powerOfTenMultiplier"),
            "uom": reading.findtext("{*}ReadingType/{*}uom"),
            "time": reading.findtext("{*}Reading/{*}timePeriod/{*}start"),
            "received_at": int(_server_now()),
        }


@app.route("/mup/<n>", methods=["POST"])
def mirror_meter_readings(n):
    if n not in reports["usage_points"]:
        return jsonify({"status": "error", "message": "no such MirrorUsagePoint"}), 404
    try:
        body = ET.fromstring(request.get_data(as_text=True))
    except ET.ParseError as e:
        return jsonify({"status": "error", "message": f"Invalid XML: {e}"}), 400
    if body.tag.split("}")[-1] not in ("MirrorMeterReading", "MirrorMeterReadingList", "MirrorUsagePoint"):
        return jsonify({"status": "error", "message": "expected MirrorMeterReading(s)"}), 400
    _store_readings(n, body)
    return "", 204


@app.route("/edev/1/fsa")
def get_function_set_assignments():
    assignment = ('<FunctionSetAssignments href="/edev/1/fsa/1"><DERProgramListLink href="/derp" all="1"/>'
                  '<mRID>F0000000000000000000000000000001</mRID><description>Simulated assignment</description>'
                  '</FunctionSetAssignments>')
    return _paged("FunctionSetAssignmentsList", "/edev/1/fsa", [assignment])


@app.route("/derp")
def get_programs():
    rate = os.environ.get("SIM_POLL_RATE", "").strip()
    default = '<DefaultDERControlLink href="/derp/1/dderc"/>' if default_control else ""
    program = (f'<DERProgram href="/derp/1"><mRID>A0000000000000000000000000000001</mRID>'
               f'<description>Simulated DER program</description>{default}'
               f'<DERControlListLink href="/derp/1/derc" all="{len(_listed_events())}"/>'
               f'<primacy>1</primacy></DERProgram>')
    return _paged("DERProgramList", "/derp", [program], f' pollRate="{int(rate)}"' if rate else "")


def _listed_events() -> list[dict]:
    """The events served: only those that haven't expired."""
    now = int(_server_now())
    return [e for e in active_events if e["start"] + e["duration"] > now]


@app.route("/derp/1/derc")
def get_derc():
    """The program's events. Every poll of the EMS Bridge reads this."""
    global last_poll_at
    last_poll_at = time.time()
    return _paged("DERControlList", "/derp/1/derc", [_der_control_xml(ev) for ev in _listed_events()])


@app.route("/derp/1/dderc")
def get_default_control():
    if not default_control:
        return jsonify({"status": "error", "message": "no DefaultDERControl is set"}), 404
    return _xml_response('<?xml version="1.0" encoding="UTF-8"?>\n'
                         '<DefaultDERControl xmlns="urn:ieee:std:2030.5:ns" href="/derp/1/dderc">'
                         '<mRID>D0000000000000000000000000000001</mRID><description>Simulated default</description>'
                         f'<DERControlBase>{default_control["control"]}</DERControlBase></DefaultDERControl>')


@app.route("/simulate/default", methods=["POST"])
def sim_default():
    """Set the program's DefaultDERControl: what the DER is to do between
    events. Body: {"power_kw": 50} (+ = charge), or {"power_kw": null} to
    remove it."""
    global default_control
    data = request.get_json(silent=True) or {}
    if data.get("power_kw") is None:
        default_control = None
        print("[SIM] DefaultDERControl removed")
        return jsonify({"status": "ok", "default": None})
    try:
        power_kw = _finite_number(data["power_kw"], "power_kw")
        default_control = {"power_kw": power_kw, "control": _control_xml(power_kw)}
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    print(f"[SIM] DefaultDERControl set: {power_kw:+.1f} kW")
    return jsonify({"status": "ok", "default": default_control})


@app.route("/simulate/charge", methods=["POST"])
def sim_charge():
    """Inject a CHARGE command. Body: {"power_kw": 400, "duration_sec": 3600}"""
    data = request.get_json(silent=True) or {}
    try:
        power_kw = _finite_number(data.get("power_kw", 400), "power_kw")
        duration = _whole_number(data.get("duration_sec", 3600), "duration_sec")
        control = _control_xml(abs(power_kw))
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    event = {
        "mrid":     str(uuid.uuid4())[:8].upper(),
        "start":    int(_server_now()),
        "created":  int(_server_now()),
        "duration": duration,
        "power_kw": abs(power_kw),     # positive = charge
        "control":  control,
    }
    active_events.clear()
    active_events.append(event)
    _persist()
    print(f"[SIM] Charge event queued: {power_kw} kW for {duration}s")
    return jsonify({"status": "ok", "event": event})


@app.route("/simulate/discharge", methods=["POST"])
def sim_discharge():
    """Inject a DISCHARGE command. Body: {"power_kw": 300, "duration_sec": 1800}"""
    data = request.get_json(silent=True) or {}
    try:
        power_kw = _finite_number(data.get("power_kw", 300), "power_kw")
        duration = _whole_number(data.get("duration_sec", 1800), "duration_sec")
        control = _control_xml(-abs(power_kw))
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    event = {
        "mrid":     str(uuid.uuid4())[:8].upper(),
        "start":    int(_server_now()),
        "created":  int(_server_now()),
        "duration": duration,
        "power_kw": -abs(power_kw),    # negative = discharge
        "control":  control,
    }
    active_events.clear()
    active_events.append(event)
    _persist()
    print(f"[SIM] Discharge event queued: {power_kw} kW for {duration}s")
    return jsonify({"status": "ok", "event": event})


@app.route(REPLY_TO, methods=["POST"])
def der_control_response():
    """Receives the EMS Bridge's DERControlResponses (F-01) at the replyTo
    address every event names. Like a validating server it refuses one
    without the fields IEEE 2030.5 requires (endDeviceLFDI, subject), and
    records the rest so /status and the dashboard show what each event
    was answered with."""
    try:
        body = ET.fromstring(request.get_data(as_text=True))
    except ET.ParseError as e:
        return jsonify({"status": "error", "message": f"Invalid XML: {e}"}), 400
    subject, lfdi = body.findtext("{*}subject"), body.findtext("{*}endDeviceLFDI")
    if body.tag.split("}")[-1] != "DERControlResponse" or not subject or not lfdi:
        return jsonify({"status": "error", "message": "a DERControlResponse needs endDeviceLFDI and subject"}), 400
    status = body.findtext("{*}status")
    record = acknowledgements.setdefault(subject, {"statuses": []})
    record.update(received_at=int(_server_now()), lfdi=lfdi)
    record["statuses"].append(status)
    print(f"[SIM] DERControlResponse {status} received for {subject}")
    response = jsonify({"status": "ok"})
    response.status_code = 201
    response.headers["Location"] = f"{REPLY_TO}/{sum(len(r['statuses']) for r in acknowledgements.values())}"
    return response


@app.route("/simulate/batch", methods=["POST"])
def sim_batch():
    """Queue multiple events at once, each with its own future start time.

    Body: {"events": [
        {"power_kw": 500, "start_offset_sec": 60, "duration_sec": 3600},
        {"power_kw": -300, "start_offset_sec": 7200, "duration_sec": 1800}
    ]}

    power_kw > 0 = charge, power_kw < 0 = discharge.
    start_offset_sec is relative to now, so this can simulate a
    calendar of windows spread across a single day or many days.
    Replaces whatever was previously queued.
    """
    data = request.get_json(silent=True) or {}
    now = int(time.time())
    queued = []
    for ev in data.get("events", []):
        try:
            event = {
                "mrid":     str(uuid.uuid4())[:8].upper(),
                "start":    now + _whole_number(ev.get("start_offset_sec", 0), "start_offset_sec"),
                "created":  now,
                "duration": _whole_number(ev.get("duration_sec", 3600), "duration_sec"),
                "power_kw": _finite_number(ev.get("power_kw"), "power_kw"),
            }
            event["control"] = _control_xml(event["power_kw"])
        except (AttributeError, ValueError) as e:
            return jsonify({"status": "error", "message": str(e)}), 400
        queued.append(event)

    active_events.clear()
    active_events.extend(queued)
    _persist()
    print(f"[SIM] Batch queued: {len(queued)} event(s)")
    for ev in queued:
        print(f"      {ev['mrid']}: {ev['power_kw']:+.1f} kW, "
              f"starts in {ev['start'] - now}s, lasts {ev['duration']}s")
    return jsonify({"status": "ok", "events": queued})


@app.route("/simulate/raw", methods=["POST"])
def sim_raw():
    """Accept a hand-written IEEE 2030.5 DERControlList XML body directly
    (Content-Type: application/xml or text/plain) and serve its events at
    /derp/1/derc — for demonstrating that the EMS Bridge parses genuine
    standard-format XML, not just JSON dressed up as one. Each DERControl
    needs an opModTargetW or opModFixedW (see the module docstring); its
    mRID, times and power are served again in the configured control form.

    Example:
        curl -X POST http://localhost:5001/simulate/raw \\
             -H "Content-Type: application/xml" \\
             --data-binary @my_dercontrol.xml
    """
    xml_text = request.get_data(as_text=True)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return jsonify({"status": "error", "message": f"Invalid XML: {e}"}), 400

    parsed = []
    for ctrl in root.findall("{*}DERControl"):
        try:
            mrid     = ctrl.findtext("{*}mRID", default="unknown")
            start    = int(ctrl.findtext("{*}interval/{*}start", default="0"))
            duration = int(ctrl.findtext("{*}interval/{*}duration", default="0"))
            created  = int(ctrl.findtext("{*}creationTime") or _server_now())
            power_kw = _power_kw_from(ctrl)
            parsed.append({"mrid": mrid, "start": start, "created": created, "duration": duration,
                           "power_kw": power_kw, "control": _control_xml(power_kw)})
        except (ValueError, TypeError) as e:
            return jsonify({"status": "error", "message": f"Bad DERControl element: {e}"}), 400

    if not parsed:
        return jsonify({"status": "error", "message": "No <DERControl> elements found"}), 400

    active_events.clear()
    active_events.extend(parsed)
    _persist()
    print(f"[SIM] Raw XML accepted: {len(parsed)} DERControl event(s)")
    for ev in parsed:
        print(f"      {ev['mrid']}: {ev['power_kw']:+.1f} kW")
    return jsonify({"status": "ok", "events": parsed})


@app.route("/simulate/idle", methods=["POST"])
def sim_idle():
    """Clear this server's event list. The EMS Bridge keeps the approved
    events it already stored (what removal from the list means is set by
    the utility's IEEE 2030.5 profile, which is not specified yet); to
    stop the battery, use the dashboard's Force Idle, which engages the
    gateway's operator override."""
    active_events.clear()
    _persist()
    print("[SIM] All events cleared from the utility list")
    return jsonify({"status": "ok", "message": "events cleared"})


@app.route("/status")
def status():
    now = int(_server_now())
    return jsonify({
        "server_time": now,
        "active_events": active_events,
        "acknowledgements": acknowledgements,
        "reports": reports,
    })


# =====================================================================
# OPERATOR DASHBOARD — browser UI for non-technical grid operators.
# Everything below is additive: the JSON/XML API above is untouched and
# still unauthenticated, so the EMS Bridge and the test suite are
# unaffected. Only /dashboard and its supporting routes require login.
# =====================================================================

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


PAGE_STYLE = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700;800&display=swap');
  :root {
    --ink: #0B0B0B; --paper: #FBFBF9; --red: #E42626; --red-dark: #B81C1C; --red-text: #E42626;
    --line: #D6D6D0; --muted: #6B6B66; --faint: #9A9A94; --panel: rgba(255, 255, 255, 0.82);
    --green: #2F7D4F; --amber: #A8760A; --dis: #B4530A; --field: #FFFFFF;
    --ok-bg: #EAF4EE; --bad-bg: #FCEDED; --warn-bg: #FBF3DD; --dis-bg: #FDF0E4;
    --track: #E4E4DE; --grid-line: rgba(11, 11, 11, 0.045); --zebra: rgba(11, 11, 11, 0.03);
    --btn-dark: #0B0B0B; --btn-dark-hover: #2A2A2A; --on-dark: #FFFFFF;
    --mono: 'JetBrains Mono', ui-monospace, 'Cascadia Mono', Consolas, 'Courier New', monospace;
    color-scheme: light;
  }
  html[data-theme="dark"] {
    --ink: #F2F2EE; --paper: #0E1217; --red-text: #FF5A5A;
    --line: #2A313B; --muted: #9AA1AB; --faint: #6F7782; --panel: #171C23;
    --green: #3DDC84; --amber: #E0A93B; --dis: #FF9A4D; --field: #0E1217;
    --ok-bg: rgba(61, 220, 132, 0.12); --bad-bg: rgba(255, 90, 90, 0.14);
    --warn-bg: rgba(224, 169, 59, 0.14); --dis-bg: rgba(255, 154, 77, 0.14);
    --track: #2A313B; --grid-line: rgba(255, 255, 255, 0.035); --zebra: rgba(255, 255, 255, 0.03);
    --btn-dark: #F2F2EE; --btn-dark-hover: #FFFFFF; --on-dark: #0B0B0B;
    color-scheme: dark;
  }
  * { box-sizing: border-box; border-radius: 0; }
  body {
    font-family: var(--mono); margin: 0; color: var(--ink); font-size: 14px;
    background-color: var(--paper);
    background-image:
      linear-gradient(var(--grid-line) 1px, transparent 1px),
      linear-gradient(90deg, var(--grid-line) 1px, transparent 1px);
    background-size: 28px 28px;
  }
  .ico { display: inline-block; width: 1.15em; height: 1.15em; vertical-align: -0.2em; margin-right: 0.45em; background-color: currentColor; -webkit-mask: var(--i) center / contain no-repeat; mask: var(--i) center / contain no-repeat; }
  .btn .ico { margin-right: 0.55em; }
  .tls-pill .ico { margin-right: 0.5em; width: 1.1em; height: 1.1em; }
  .ico-lock { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Crect x='3' y='11' width='18' height='11'/%3E%3Cpath d='M7 11V7a5 5 0 0 1 10 0v4'/%3E%3C/svg%3E"); }
  .ico-unlock { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Crect x='3' y='11' width='18' height='11'/%3E%3Cpath d='M7 11V7a5 5 0 0 1 9.9-1'/%3E%3C/svg%3E"); }
  .ico-activity { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Cpolyline points='22 12 18 12 15 21 9 3 6 12 2 12'/%3E%3C/svg%3E"); }
  .ico-battery { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Crect x='2' y='7' width='16' height='10'/%3E%3Cpath d='M22 11v2'/%3E%3Cpath d='M11 9l-2.5 3H12l-2.5 3'/%3E%3C/svg%3E"); }
  .ico-zap { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Cpolygon points='13 2 3 14 12 14 11 22 21 10 12 10 13 2'/%3E%3C/svg%3E"); }
  .ico-list { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Cpath d='M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01'/%3E%3C/svg%3E"); }
  .ico-logout { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Cpath d='M9 21H5V3h4'/%3E%3Cpolyline points='16 17 21 12 16 7'/%3E%3Cpath d='M21 12H9'/%3E%3C/svg%3E"); }
  .logo-dark { display: none !important; }
  html[data-theme="dark"] .logo-light.swap { display: none !important; }
  html[data-theme="dark"] .logo-dark { display: block !important; }
  .theme-toggle { padding: 8px 10px; line-height: 0; }
  .theme-toggle .ico { margin: 0; width: 1.3em; height: 1.3em; }
  .theme-toggle .ico-sun { display: none; }
  html[data-theme="dark"] .theme-toggle .ico-sun { display: inline-block; }
  html[data-theme="dark"] .theme-toggle .ico-moon { display: none; }
  .ico-sun { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square'%3E%3Crect x='8' y='8' width='8' height='8'/%3E%3Cpath d='M12 1v3M12 20v3M1 12h3M20 12h3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1'/%3E%3C/svg%3E"); }
  .ico-moon { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Cpath d='M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z'/%3E%3C/svg%3E"); }
  .topbar {
    background: var(--paper); border-bottom: 1px solid var(--ink); padding: 14px 32px;
    display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;
  }
  .topbar .brand { display: flex; align-items: center; gap: 18px; }
  .topbar .brand img { height: 30px; display: block; }
  .topbar .brand .title { font-size: 12px; font-weight: 700; letter-spacing: 0.22em; color: var(--red-text); text-transform: uppercase; }
  .topbar .who { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .topbar .who b { color: var(--ink); }
  .topbar form { display: inline; margin: 0; }
  .btn {
    background: var(--red); color: white; border: 1px solid var(--red); padding: 10px 18px;
    font-family: var(--mono); font-size: 12px; font-weight: 700; letter-spacing: 0.14em;
    text-transform: uppercase; cursor: pointer;
  }
  .btn:hover { background: var(--red-dark); border-color: var(--red-dark); }
  .btn.secondary { background: var(--btn-dark); border-color: var(--btn-dark); color: var(--on-dark); }
  .btn.secondary:hover { background: var(--btn-dark-hover); border-color: var(--btn-dark-hover); }
  .btn.grey { background: transparent; color: var(--ink); border-color: var(--ink); }
  .btn.grey:hover { background: var(--ink); color: var(--paper); }
  .wrap { max-width: 1120px; margin: 32px auto; padding: 0 24px; }
  .grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr); gap: 24px; }
  .card { background: var(--panel); border: 1px solid var(--line); padding: 22px 24px; margin-bottom: 24px; }
  .card h2 {
    font-size: 12px; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase;
    margin: 0 0 16px 0; color: var(--red-text);
  }
  .card h2::before, .kicker::before { content: '// '; }
  .kicker {
    font-size: 12px; font-weight: 700; letter-spacing: 0.2em; color: var(--red-text);
    text-transform: uppercase; margin-bottom: 12px;
  }
  .status-pill {
    display: inline-flex; align-items: center; gap: 10px; font-weight: 700;
    padding: 7px 14px; font-size: 12px; letter-spacing: 0.04em; border: 1px solid currentColor;
  }
  .status-pill.connected { color: var(--green); background: var(--ok-bg); }
  .status-pill.stale { color: var(--red-text); background: var(--bad-bg); }
  .dot { width: 8px; height: 8px; background: currentColor; }
  label {
    display: block; font-size: 11px; font-weight: 700; margin: 14px 0 6px;
    letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted);
  }
  input[type=number], input[type=text], input[type=password] {
    width: 100%; padding: 10px 12px; border: 1px solid var(--line); background: var(--field);
    font-family: var(--mono); font-size: 14px; color: var(--ink);
  }
  input:focus { outline: 2px solid var(--red); outline-offset: -1px; border-color: var(--red-text); }
  .action-row { display: flex; gap: 10px; margin-top: 18px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; overflow-wrap: normal; }
  th {
    text-align: left; background: var(--btn-dark); color: var(--on-dark); padding: 9px 10px;
    font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase;
  }
  td { padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  td.id { overflow-wrap: anywhere; }
  tr:nth-child(even) td { background: var(--zebra); }
  .tag { padding: 3px 8px; font-size: 10px; font-weight: 700; letter-spacing: 0.1em; border: 1px solid currentColor; }
  .tag.charge { color: var(--green); }
  .tag.discharge { color: var(--dis); }
  .ack-yes { color: var(--green); font-weight: 700; }
  .ack-no { color: var(--amber); font-weight: 700; }
  .empty { color: var(--faint); padding: 8px 0; margin: 0; }
  .battery-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 24px; align-items: start;
  }
  p.warn { color: var(--red-text); font-size: 12px; font-weight: 700; margin: 14px 0 0; }
  p.note { color: var(--amber); font-size: 12px; margin: 14px 0 0; }
  .meta { color: var(--faint); font-size: 11px; margin: 10px 0 0; line-height: 1.6; }
  span.bad { color: var(--red-text); font-weight: 700; }
  .stale-tag { color: var(--red-text); border: 1px solid var(--red); padding: 0 5px; text-transform: none; letter-spacing: 0; }
  .stat-label {
    font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.16em; margin-bottom: 8px;
  }
  .stat-value { font-size: 22px; font-weight: 800; color: var(--ink); letter-spacing: -0.02em; }
  .mode-badge, .rel-badge {
    display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 700;
    letter-spacing: 0.12em; border: 1px solid currentColor; margin-top: 8px;
  }
  .mode-badge.unconfirmed { color: var(--red-text); background: var(--bad-bg); }
  .mode-badge.idle { color: var(--muted); }
  .mode-badge.charge { color: var(--green); background: var(--ok-bg); }
  .mode-badge.discharge { color: var(--dis); background: var(--dis-bg); }
  .rel-badge { margin-top: 0; }
  .rel-badge.connected { color: var(--green); background: var(--ok-bg); }
  .rel-badge.derated { color: var(--amber); background: var(--warn-bg); }
  .rel-badge.disconnected { color: var(--red-text); background: var(--bad-bg); }
  .rel-badge.autonomous { color: var(--ink); }
  .rel-badge.safe_idle { color: var(--red-text); background: var(--bad-bg); }
  .soc-bar-outer { background: var(--track); height: 10px; margin-top: 10px; width: 100%; }
  .soc-bar-inner { background: var(--red); height: 100%; }
  .error {
    background: var(--bad-bg); color: var(--red-text); border: 1px solid var(--red); padding: 10px 14px;
    font-size: 12px; font-weight: 700; margin-bottom: 16px;
  }
  .hint { font-size: 11px; color: var(--faint); margin-top: 18px; }
  .tls-pill {
    display: inline-block; font-size: 11px; font-weight: 700; padding: 5px 12px;
    margin: 4px 0 18px; border: 1px solid currentColor; letter-spacing: 0.04em;
  }
  .tls-pill.secure { color: var(--green); background: var(--ok-bg); }
  .tls-pill.insecure { color: var(--amber); background: var(--warn-bg); }
  .tls-pill.topbar-pill { margin: 0; }
  @media (max-width: 1180px) { .grid { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 820px) { .topbar { padding: 14px 16px; } .wrap { padding: 0 16px; } }
</style>
<script>
(function () {
  var t = null;
  try { t = localStorage.getItem('theme'); } catch (e) {}
  if (!t) t = (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', t);
  window.toggleTheme = function () {
    var n = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', n);
    try { localStorage.setItem('theme', n); } catch (e) {}
  };
})();
</script>
"""

LOGIN_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grid Operator Portal — Login</title>{{ style|safe }}
<style>
  .login-split {
    display: flex; max-width: 1040px; min-height: 560px; margin: 6vh auto;
    border: 1px solid var(--ink); background: var(--paper);
  }
  .login-art {
    flex: 1 1 50%; display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 32px; gap: 28px; border-right: 1px solid var(--ink);
    background-color: white;
    background-image:
      linear-gradient(rgba(11, 11, 11, 0.06) 1px, transparent 1px),
      linear-gradient(90deg, rgba(11, 11, 11, 0.06) 1px, transparent 1px);
    background-size: 28px 28px;
  }
  .login-art .logo { width: 70%; max-width: 280px; }
  .login-art .battery { width: 100%; max-width: 440px; mix-blend-mode: multiply; }
  .login-split { position: relative; }
  .login-panel { flex: 1 1 50%; padding: 44px 40px; display: flex; flex-direction: column; justify-content: center; }
  .login-panel h1 {
    font-size: 52px; font-weight: 800; letter-spacing: -0.03em; line-height: 1;
    margin: 0 0 6px; text-transform: uppercase;
  }
  .login-panel h1 span { color: var(--red); }
  .login-panel .lede { color: var(--muted); font-size: 13px; line-height: 1.7; margin: 14px 0 0; }
  .login-card { margin-top: 24px; border: 1px solid var(--line); background: var(--panel); padding: 22px 24px 26px; }
  .login-card label:first-of-type { margin-top: 0; }
  .login-card .btn { width: 100%; margin-top: 24px; padding: 13px; }
  .login-panel .tls-pill { align-self: flex-start; margin: 18px 0 0; }
  @media (max-width: 760px) {
    .login-split { flex-direction: column; margin: 0; min-height: 100vh; border: none; }
    .login-art { padding: 24px; border-right: none; border-bottom: 1px solid var(--ink); }
    .login-art .battery { max-width: 320px; }
    .login-panel { padding: 28px 20px; }
    .login-panel h1 { font-size: 40px; }
  }
</style></head>
<body>
  <div class="login-split">
    <div class="login-art">
      <img class="logo" src="{{ url_for('static', filename='invinity-logo.svg') }}" alt="Invinity Energy Systems">
      <img class="battery" src="{{ url_for('static', filename='invinity-battery.png') }}" alt="Invinity battery container">
    </div>
    <div class="login-panel">
      <div style="position:absolute;top:14px;right:14px"><button type="button" class="btn grey theme-toggle" onclick="toggleTheme()" aria-label="Switch between light and dark mode" title="Switch light / dark"><i class="ico ico-moon"></i><i class="ico ico-sun"></i></button></div>
      <div class="kicker">Interoperable DER Gateway</div>
      <h1>Log in<span>.</span></h1>
      <p class="lede">Grid Operator Portal. Sign in to schedule battery dispatch commands.</p>
      {% if error %}<div class="error" style="margin:18px 0 0">{{ error }}</div>{% endif %}
      <form method="post" class="login-card">
        <label for="username">Username *</label>
        <input type="text" id="username" name="username" placeholder="Enter your Username" required autofocus>
        <label for="password">Password *</label>
        <input type="password" id="password" name="password" placeholder="Enter your Password" required>
        <button class="btn" type="submit">Login</button>
      </form>
      {% if secure %}
        <div class="tls-pill secure"><i class="ico ico-lock"></i>Secure connection (TLS)</div>
      {% else %}
        <div class="tls-pill insecure"><i class="ico ico-unlock"></i>Unencrypted connection — for local demo use only</div>
      {% endif %}
      {% if show_hint %}<div class="hint">Demo account — operator / invinity2026</div>{% endif %}
    </div>
  </div>
</body></html>
"""

# The Overview page (sidebar shell + page + script), kept under this name for the audit tests.
DASHBOARD_TEMPLATE = dashboard_pages.SHELL + dashboard_pages.OVERVIEW_BODY + dashboard_pages.OVERVIEW_JS


@app.route("/login", methods=["GET", "POST"])
def login():
    show_hint = not PUBLIC_MODE
    if request.method == "POST":
        ip = _client_ip()
        if PUBLIC_MODE and _login_blocked(ip):
            return render_template_string(LOGIN_TEMPLATE, style=PAGE_STYLE, secure=request.is_secure, show_hint=show_hint,
                                           error="Too many failed attempts. Try again in 15 minutes."), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (secrets.compare_digest(username.encode(), DEMO_USERNAME.encode())
                and secrets.compare_digest(password.encode(), DEMO_PASSWORD.encode())):
            _login_failures.pop(ip, None)
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("dashboard"))
        _login_failures.setdefault(ip, []).append(time.time())
        return render_template_string(LOGIN_TEMPLATE, style=PAGE_STYLE,
                                       secure=request.is_secure, show_hint=show_hint,
                                       error="Invalid username or password."), 401
    return render_template_string(LOGIN_TEMPLATE, style=PAGE_STYLE,
                                   secure=request.is_secure, show_hint=show_hint, error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard/send", methods=["POST"])
@login_required
def dashboard_send():
    action = request.form.get("action")
    if action == "idle":
        # Clear the utility-side list AND engage the gateway's operator
        # Force Idle override, which takes precedence over every schedule
        # until it is released.
        _cancel_events()
        set_force_idle(True, by=session.get("username"), reason="dashboard Clear / Force Idle")
        print(f"[SIM] Operator '{session.get('username')}' cleared all events and engaged Force Idle")
        session["flash"] = ["warn", "Force Idle is on. Utility events were cleared and the battery is held at 0 kW. "
                                    "Press Release Force Idle to let the utility control it again."]
        return redirect(_back_to_page())
    if action == "release":
        set_force_idle(False, by=session.get("username"), reason="dashboard Release Force Idle")
        print(f"[SIM] Operator '{session.get('username')}' released Force Idle")
        session["flash"] = ["ok", "Force Idle released. The battery will follow the utility schedule again within a few seconds."]
        return redirect(_back_to_page())

    try:
        power_kw = _finite_number(request.form.get("power_kw", 0), "power_kw")
        duration = _whole_number(request.form.get("duration_sec", 3600), "duration_sec")
        signed_power = abs(power_kw) if action == "charge" else -abs(power_kw)
        _control_xml(signed_power)
    except ValueError as e:
        return f"Invalid command: {e}", 400

    result = _schedule([{"power_kw": signed_power, "start": None, "duration_sec": duration}],
                       replace=request.form.get("replace") == "1")
    if result["errors"]:
        session["flash"] = ["warn", "Not sent. " + result["errors"][0]["message"]]
        return redirect(_back_to_page())
    event = result["created"][0]
    print(f"[SIM] Operator '{session.get('username')}' queued {action} event "
          f"{event['mrid']}: {signed_power:+.1f} kW for {duration}s")
    in_control = ((_dashboard_state().get("battery") or {}).get("authority") or {}).get("authority")
    cancelled = (f" It replaced {len(result['removed'])} overlapping event(s)." if result["removed"] else "")
    if in_control == "operator_force_idle":
        session["flash"] = ["warn", f"Event {event['mrid']} ({action} {abs(signed_power):g} kW) was queued, but Force Idle is on, "
                                    "so the battery will not act on it until you press Release Force Idle." + cancelled]
    else:
        session["flash"] = ["ok", f"Event {event['mrid']} queued: {action} {abs(signed_power):g} kW for {duration} seconds. "
                                  "The gateway picks it up within about 5 seconds." + cancelled]
    return redirect(_back_to_page())


# ── Scheduling: a queue of events with their own start times ────────────
# Several events can wait in the queue. A new event is refused if its window overlaps
# one already there, unless the operator asks for the overlapping ones to be replaced:
# silently cancelling a running event is not something an operator should get by accident.
SCHEDULE_MIN_SEC, SCHEDULE_MAX_SEC = 60, 7 * 86400
SCHEDULE_MAX_AHEAD_SEC, SCHEDULE_MAX_QUEUE = 366 * 86400, 200
SCHEDULE_MAX_KW = 100_000       # far above any real battery; stops a typo like 99999999
_schedule_lock = threading.Lock()


def _new_event(raw, now: int) -> dict:
    """One validated event from {power_kw (+ charge / - discharge), start (epoch seconds, or
    None for now), duration_sec}. Raises ValueError with a sentence an operator can read."""
    if not isinstance(raw, dict):
        raise ValueError("this is not an event")
    power_kw = _finite_number(raw.get("power_kw"), "power")
    if power_kw == 0:
        raise ValueError("power must not be 0 kW")
    if abs(power_kw) > SCHEDULE_MAX_KW:
        raise ValueError(f"power must be at most {SCHEDULE_MAX_KW:,} kW")
    duration = _whole_number(raw.get("duration_sec"), "length")
    if not SCHEDULE_MIN_SEC <= duration <= SCHEDULE_MAX_SEC:
        raise ValueError("the length must be between 1 minute and 7 days")
    start = raw.get("start")
    start = now if start in (None, "") else _whole_number(start, "start time")
    if start < now - 60:
        raise ValueError("the start time is in the past")
    if start > now + SCHEDULE_MAX_AHEAD_SEC:
        raise ValueError("the start time is more than a year away")
    return {"mrid": str(uuid.uuid4())[:8].upper(), "start": max(start, now), "created": now, "duration": duration,
            "power_kw": power_kw, "control": _control_xml(power_kw), "issued_by": session.get("username")}


def _cancel_events(mrid: str | None = None) -> int:
    """Mark the queued events Cancelled (all of them, or just `mrid`). They stay in the list so the
    EMS Bridge hears the cancellation. Returns how many were cancelled."""
    now = int(_server_now())
    count = 0
    with _schedule_lock:
        for e in _listed_events():
            if not e.get("cancelled") and (mrid is None or e["mrid"] == mrid):
                e["cancelled"], e["cancelled_at"] = True, now
                count += 1
        _persist()
    return count


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["start"] + b["duration"] and b["start"] < a["start"] + a["duration"]


def _describe(ev: dict) -> str:
    kind = "charge" if ev["power_kw"] > 0 else "discharge"
    mrid = ev["mrid"] if len(ev["mrid"]) <= 12 else f"{ev['mrid'][:8]}…{ev['mrid'][-4:]}"
    return f"event {mrid} ({kind} {abs(ev['power_kw']):g} kW, @@t={ev['start']}@@ to @@t={ev['start'] + ev['duration']}@@)"


def _schedule(rows, replace: bool = False, dry_run: bool = False) -> dict:
    """Validate rows and, unless dry_run, add them all (or none). Returns
    {"created": [...], "removed": [mrids], "errors": [{"row", "message"}]}."""
    now = int(_server_now())
    errors, new = [], []
    if not isinstance(rows, list) or not rows:
        return {"created": [], "removed": [], "errors": [{"row": 0, "message": "There are no events to add."}]}
    if len(rows) > SCHEDULE_MAX_QUEUE:
        return {"created": [], "removed": [], "errors": [{"row": 0, "message": f"Add at most {SCHEDULE_MAX_QUEUE} events at a time."}]}
    for i, raw in enumerate(rows, 1):
        try:
            new.append((i, _new_event(raw, now)))
        except ValueError as e:
            text = str(e)
            errors.append({"row": i, "message": text[:1].upper() + text[1:] + "."})
    with _schedule_lock:
        active_events[:] = [e for e in active_events if e["start"] + e["duration"] > now]
        for n, (i, ev) in enumerate(new):
            for _, other in new[:n]:
                if _overlaps(ev, other):
                    errors.append({"row": i, "message": f"This overlaps another row you are adding ({_describe(other)})."})
                    break
        removed: list = []
        for i, ev in new:
            for e in active_events:
                if not e.get("cancelled") and _overlaps(ev, e):
                    if replace:
                        if e["mrid"] not in removed:
                            removed.append(e["mrid"])
                    else:
                        errors.append({"row": i, "message": f"This overlaps {_describe(e)}. Tick Replace to swap it, "
                                                           "or choose another time."})
                        break
        if sum(1 for e in active_events if not e.get("cancelled")) - len(removed) + len(new) > SCHEDULE_MAX_QUEUE:
            errors.append({"row": 0, "message": f"The queue would hold more than {SCHEDULE_MAX_QUEUE} events."})
        if errors or dry_run:
            return {"created": [], "removed": [], "errors": errors, "ok": not errors}
        for e in active_events:
            if e["mrid"] in removed:
                e["cancelled"], e["cancelled_at"] = True, now
        active_events.extend(ev for _, ev in new)
        _persist()
    return {"created": [ev for _, ev in new], "removed": removed, "errors": [], "ok": True}


def _public(ev: dict) -> dict:
    return {"mrid": ev["mrid"], "power_kw": ev["power_kw"], "start": ev["start"], "duration": ev["duration"]}


@app.route("/api/schedule", methods=["POST"])
def api_schedule_add():
    if not session.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "errors": [{"row": 0, "message": "Send a JSON body."}]}), 400
    result = _schedule(data.get("events"), replace=bool(data.get("replace")), dry_run=bool(data.get("dry_run")))
    if result["errors"]:
        return jsonify({"ok": False, "errors": result["errors"]}), 400
    if not data.get("dry_run"):
        for ev in result["created"]:
            print(f"[SIM] Operator '{session.get('username')}' scheduled event {ev['mrid']}: "
                  f"{ev['power_kw']:+.1f} kW at {ev['start']} for {ev['duration']}s")
    return jsonify({"ok": True, "created": [_public(e) for e in result["created"]], "removed": result["removed"]})


@app.route("/api/schedule/cancel", methods=["POST"])
def api_schedule_cancel():
    if not session.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(silent=True) or {}
    removed = _cancel_events(None if data.get("all") is True else str(data.get("mrid", "")))
    print(f"[SIM] Operator '{session.get('username')}' cancelled {removed} event(s)")
    return jsonify({"ok": True, "removed": removed})


_DASHBOARD_PAGES = {"/dashboard", "/dashboard/battery", "/dashboard/events", "/dashboard/activity",
                    "/dashboard/alerts", "/dashboard/reports"}


def _back_to_page() -> str:
    """Return to the dashboard page the operator was on (only our own pages, never an outside address)."""
    nxt = request.form.get("next", "")
    return nxt if nxt in _DASHBOARD_PAGES else url_for("dashboard")


@app.route("/api/status")
def api_status():
    """Polled every few seconds by the dashboard's browser JS. Returns a
    real 401 JSON (rather than redirecting) so a fetch() call can detect
    an expired session cleanly instead of silently receiving the login
    page's HTML."""
    if not session.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401

    return jsonify(_dashboard_state())


def _dashboard_state() -> dict:
    """What the dashboard shows about the utility link, the event list and the battery."""
    now = time.time()
    seconds_since_poll = round(now - last_poll_at) if last_poll_at else None
    # "connected" if the gateway has checked in within the last ~10s —
    # generous relative to the default 5s poll interval, to tolerate jitter.
    gateway_connected = seconds_since_poll is not None and seconds_since_poll <= 10

    events_view = []
    server_now = _server_now()
    for e in sorted((e for e in _listed_events() if not e.get("cancelled")), key=lambda ev: ev["start"]):
        end = e["start"] + e["duration"]
        events_view.append({
            "mrid": e["mrid"],
            "power_kw": e["power_kw"],
            "starts_in": f"{e['start'] - int(server_now)}s" if e["start"] > server_now else "now",
            "duration": e["duration"],
            "start": e["start"],
            "end": end,
            "running": e["start"] <= server_now < end,
        })

    return {
        "gateway_connected": gateway_connected,
        "seconds_since_poll": seconds_since_poll,
        "active_events": events_view,
        "acknowledgements": acknowledgements,
        "battery": _read_battery_status(now),
    }


def _read_battery_status(now: float) -> dict:
    """Reads the gateway's published status snapshot (schedule_store/
    live_status.json — see capstone/__main__.py:publish_status_loop).
    Treated as stale if it hasn't been refreshed in the last ~5s (the
    gateway writes it every 2s), so a killed/crashed gateway is reflected
    within a few seconds rather than freezing on the last known value."""
    if not BATTERY_STATUS_FILE.exists():
        return {"available": False, "reason": "Gateway has not published any status yet."}
    try:
        snapshot = json.loads(BATTERY_STATUS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"available": False, "reason": "Status file unreadable."}

    age = now - snapshot.get("published_at", 0)
    if age > 5:
        return {"available": False, "reason": f"Last update {round(age)}s ago — gateway may be offline."}

    snapshot["available"] = True
    snapshot["age_sec"] = round(age)
    # The snapshot can be fresh while the BMS data in it is old (the
    # gateway keeps publishing through a Modbus outage).
    read_at = snapshot.get("device_read_at")
    snapshot["device_age_sec"] = None if read_at is None else round(now - read_at)
    snapshot["device_stale"] = read_at is None or now - read_at > DEVICE_STALE_SEC
    return snapshot


dashboard_pages.register(app, page_style=PAGE_STYLE, login_required=login_required, state_provider=_dashboard_state)


def _certs_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "certs")


def _build_mtls_context() -> ssl.SSLContext | None:
    """If TLS_ENABLED=1, build a server-side TLS context requiring a
    client certificate signed by our dev CA (NF-04: TLS 1.2+ with
    mutual certificate authentication). Cert paths default to the
    dev certs generated by certs/generate_dev_certs.sh.

    This is the strict, machine-to-machine mode — it's what the real
    IEEE 2030.5 client (capstone/ieee_client.py) uses, since a service
    account can be handed a client certificate ahead of time. It is NOT
    what a human logging into /dashboard should be asked to do (a
    browser has no client cert to present), so it is a separate,
    independent flag from DASHBOARD_TLS below.

    Like an IEEE 2030.5 server it speaks TLS 1.2 and prefers the cipher
    suite IEEE 2030.5 makes mandatory, TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8
    (OpenSSL 3 allows it only at security level 0), falling back to
    ECDHE with AES-GCM for clients without it, such as browsers."""
    if os.environ.get("TLS_ENABLED", "0") != "1":
        return None

    certs_dir = _certs_dir()
    server_cert = os.environ.get("TLS_SERVER_CERT", os.path.join(certs_dir, "server.crt"))
    server_key  = os.environ.get("TLS_SERVER_KEY", os.path.join(certs_dir, "server.key"))
    ca_cert     = os.environ.get("TLS_CA_CERT", os.path.join(certs_dir, "ca.crt"))

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("@SECLEVEL=0:ECDHE-ECDSA-AES128-CCM8:ECDHE+AESGCM:!aNULL:!eNULL")
    context.load_cert_chain(certfile=server_cert, keyfile=server_key)
    context.load_verify_locations(cafile=ca_cert)
    context.verify_mode = ssl.CERT_REQUIRED  # mutual auth: client MUST present a cert we trust
    return context


def _build_dashboard_tls_context() -> ssl.SSLContext | None:
    """If DASHBOARD_TLS=1, build a standard server-only HTTPS context —
    TLS 1.2+, using the same dev server certificate as the mutual-TLS
    mode, but WITHOUT requiring the browser to present a client
    certificate. This is what actually makes sense for a human operator
    logging in: the login form, password, and session cookie are
    encrypted in transit, exactly like any HTTPS website — without
    requiring every operator's browser to have a certificate installed."""
    if os.environ.get("DASHBOARD_TLS", "0") != "1":
        return None

    certs_dir = _certs_dir()
    server_cert = os.environ.get("TLS_SERVER_CERT", os.path.join(certs_dir, "server.crt"))
    server_key  = os.environ.get("TLS_SERVER_KEY", os.path.join(certs_dir, "server.key"))

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=server_cert, keyfile=server_key)
    context.verify_mode = ssl.CERT_NONE  # server-only auth — no client cert required
    return context


if __name__ == "__main__":
    check_public_config()
    _persist_queue = True
    _load_queue()
    dashboard_pages.start_history(_dashboard_state)
    try:
        _control_xml(0.0)       # SIM_POWER_CONTROL, and for opModFixedW the DER ratings
    except ValueError as e:
        raise SystemExit(f"Configuration error: {e}")
    mtls_context = _build_mtls_context()
    dashboard_tls_context = None if mtls_context else _build_dashboard_tls_context()
    ssl_context = mtls_context or dashboard_tls_context
    scheme = "https" if ssl_context else "http"

    # Never let the session cookie travel unencrypted once TLS is on.
    app.config["SESSION_COOKIE_SECURE"] = bool(ssl_context) or PUBLIC_MODE

    print(f"Fake IEEE 2030.5 Grid Server running at {scheme}://localhost:5001")
    print(f"Active-power control sent: {_power_control()} (SIM_POWER_CONTROL)")
    if mtls_context:
        print("TLS 1.2 MUTUAL certificate authentication ENABLED (TLS_ENABLED=1), "
              "preferring the IEEE 2030.5 suite TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8 "
              "— machine clients (and browsers) must present a cert signed by "
              "certs/ca.crt. To reach /dashboard from a browser in this mode, "
              "import certs/client.crt + certs/client.key into it first.")
    elif dashboard_tls_context:
        print("TLS 1.2+ enabled for the dashboard (DASHBOARD_TLS=1) — server-only "
              "HTTPS, no client certificate required. Your browser will warn about "
              "the dev certificate being untrusted/self-signed; that is expected — "
              "proceed past the warning.")
    print()
    print(f"Operator dashboard: {scheme}://localhost:5001/dashboard")
    print(f"  Demo login: {DEMO_USERNAME} / {DEMO_PASSWORD}")
    print()
    print("Or inject commands with curl:")
    print('  curl -X POST http://localhost:5001/simulate/charge \\')
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"power_kw": 500, "duration_sec": 3600}\'')
    print()
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False, ssl_context=ssl_context)
