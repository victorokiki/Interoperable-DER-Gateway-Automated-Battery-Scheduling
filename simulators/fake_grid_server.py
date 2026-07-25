"""
simulators/fake_grid_server.py
Fake IEEE 2030.5 Grid Server

Run this INSTEAD of a real utility server during testing.
It serves DERControl XML at the endpoint the EMS Bridge polls.

Usage:
    python simulators/fake_grid_server.py

Then use the web UI at http://localhost:5000 to send commands,
OR use the command-line shortcuts below.

Endpoints:
    GET  /derp/1/derc          ← EMS Bridge polls this
    POST /simulate/charge      ← you call this to inject a charge command
    POST /simulate/discharge   ← inject a discharge command
    POST /simulate/batch       ← queue several future-dated events at once
    POST /simulate/raw         ← paste a hand-written DERControl XML body directly
    POST /simulate/idle        ← clear all events
    GET  /status               ← see what's currently queued

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
"""

from flask import Flask, request, jsonify
import os
import ssl
import time
import uuid
import xml.etree.ElementTree as ET

app = Flask(__name__)

# Active event store (in-memory for simulation)
active_events: list[dict] = []

# DERControlResponse acknowledgements received from the EMS Bridge (F-01),
# keyed by mrid — in-memory for simulation.
acknowledgements: dict = {}


def make_derc_xml(events: list[dict]) -> str:
    """Build an IEEE 2030.5 DERControlList XML response."""
    controls = ""
    for ev in events:
        # Positive kW = charge, negative = discharge
        # We encode as watts with multiplier=0
        power_w = int(ev["power_kw"] * 1000)
        controls += f"""
  <DERControl>
    <mRID>{ev['mrid']}</mRID>
    <interval>
      <start>{ev['start']}</start>
      <duration>{ev['duration']}</duration>
    </interval>
    <DERControlBase>
      <opModFixedW>
        <value>{power_w}</value>
        <multiplier>0</multiplier>
      </opModFixedW>
    </DERControlBase>
  </DERControl>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DERControlList xmlns="urn:ieee:std:2030.5:ns" all="{len(events)}" results="{len(events)}">
{controls}
</DERControlList>"""


@app.route("/derp/1/derc")
def get_derc():
    """The EMS Bridge polls this endpoint."""
    # Only serve events that haven't expired
    now = int(time.time())
    valid = [e for e in active_events if e["start"] + e["duration"] > now]
    return app.response_class(make_derc_xml(valid), mimetype="application/xml")


@app.route("/simulate/charge", methods=["POST"])
def sim_charge():
    """Inject a CHARGE command. Body: {"power_kw": 400, "duration_sec": 3600}"""
    data = request.get_json(silent=True) or {}
    power_kw    = float(data.get("power_kw", 400))
    duration    = int(data.get("duration_sec", 3600))
    event = {
        "mrid":     str(uuid.uuid4())[:8].upper(),
        "start":    int(time.time()),
        "duration": duration,
        "power_kw": abs(power_kw),     # positive = charge
    }
    active_events.clear()
    active_events.append(event)
    print(f"[SIM] Charge event queued: {power_kw} kW for {duration}s")
    return jsonify({"status": "ok", "event": event})


@app.route("/simulate/discharge", methods=["POST"])
def sim_discharge():
    """Inject a DISCHARGE command. Body: {"power_kw": 300, "duration_sec": 1800}"""
    data = request.get_json(silent=True) or {}
    power_kw    = float(data.get("power_kw", 300))
    duration    = int(data.get("duration_sec", 1800))
    event = {
        "mrid":     str(uuid.uuid4())[:8].upper(),
        "start":    int(time.time()),
        "duration": duration,
        "power_kw": -abs(power_kw),    # negative = discharge
    }
    active_events.clear()
    active_events.append(event)
    print(f"[SIM] Discharge event queued: {power_kw} kW for {duration}s")
    return jsonify({"status": "ok", "event": event})


@app.route("/derp/1/derc/<mrid>/derresponse", methods=["POST"])
def der_control_response(mrid):
    """Receives the EMS Bridge's DERControlResponse acknowledgement
    (F-01) for a given event mrid. A real IEEE 2030.5 server would
    validate/store this per the spec; here we just record and log it
    so /status can show which events have been acknowledged."""
    acknowledgements[mrid] = {"received_at": int(time.time()), "body": request.get_data(as_text=True)}
    print(f"[SIM] DERControlResponse received for {mrid}")
    return jsonify({"status": "ok"})


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
        power_kw = float(ev["power_kw"])
        event = {
            "mrid":     str(uuid.uuid4())[:8].upper(),
            "start":    now + int(ev.get("start_offset_sec", 0)),
            "duration": int(ev.get("duration_sec", 3600)),
            "power_kw": power_kw,
        }
        queued.append(event)

    active_events.clear()
    active_events.extend(queued)
    print(f"[SIM] Batch queued: {len(queued)} event(s)")
    for ev in queued:
        print(f"      {ev['mrid']}: {ev['power_kw']:+.1f} kW, "
              f"starts in {ev['start'] - now}s, lasts {ev['duration']}s")
    return jsonify({"status": "ok", "events": queued})


@app.route("/simulate/raw", methods=["POST"])
def sim_raw():
    """Accept a hand-written IEEE 2030.5 DERControlList XML body directly
    (Content-Type: application/xml or text/plain) and serve it back
    verbatim at /derp/1/derc — for demonstrating that the EMS Bridge
    parses genuine standard-format XML, not just JSON dressed up as one.

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
            value    = int(ctrl.findtext("{*}DERControlBase/{*}opModFixedW/{*}value", default="0"))
            mult     = int(ctrl.findtext("{*}DERControlBase/{*}opModFixedW/{*}multiplier", default="0"))
            power_kw = (value * (10 ** mult)) / 1000.0
            parsed.append({"mrid": mrid, "start": start, "duration": duration, "power_kw": power_kw})
        except (ValueError, TypeError) as e:
            return jsonify({"status": "error", "message": f"Bad DERControl element: {e}"}), 400

    if not parsed:
        return jsonify({"status": "error", "message": "No <DERControl> elements found"}), 400

    active_events.clear()
    active_events.extend(parsed)
    print(f"[SIM] Raw XML accepted: {len(parsed)} DERControl event(s)")
    for ev in parsed:
        print(f"      {ev['mrid']}: {ev['power_kw']:+.1f} kW")
    return jsonify({"status": "ok", "events": parsed})


@app.route("/simulate/idle", methods=["POST"])
def sim_idle():
    """Clear all events — EMS Bridge should return battery to IDLE."""
    active_events.clear()
    print("[SIM] All events cleared → battery should go IDLE")
    return jsonify({"status": "ok", "message": "events cleared"})


@app.route("/status")
def status():
    now = int(time.time())
    return jsonify({
        "server_time": now,
        "active_events": active_events,
        "acknowledgements": acknowledgements,
    })


def _build_mtls_context() -> ssl.SSLContext | None:
    """If TLS_ENABLED=1, build a server-side TLS context requiring a
    client certificate signed by our dev CA (NF-04: TLS 1.2+ with
    mutual certificate authentication). Cert paths default to the
    dev certs generated by certs/generate_dev_certs.sh."""
    if os.environ.get("TLS_ENABLED", "0") != "1":
        return None

    certs_dir = os.path.join(os.path.dirname(__file__), "..", "certs")
    server_cert = os.environ.get("TLS_SERVER_CERT", os.path.join(certs_dir, "server.crt"))
    server_key  = os.environ.get("TLS_SERVER_KEY", os.path.join(certs_dir, "server.key"))
    ca_cert     = os.environ.get("TLS_CA_CERT", os.path.join(certs_dir, "ca.crt"))

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=server_cert, keyfile=server_key)
    context.load_verify_locations(cafile=ca_cert)
    context.verify_mode = ssl.CERT_REQUIRED  # mutual auth: client MUST present a cert we trust
    return context


if __name__ == "__main__":
    ssl_context = _build_mtls_context()
    scheme = "https" if ssl_context else "http"
    print(f"Fake IEEE 2030.5 Grid Server running at {scheme}://localhost:5001")
    if ssl_context:
        print("TLS 1.2+ mutual certificate authentication ENABLED "
              "(TLS_ENABLED=1) — clients must present a cert signed by certs/ca.crt")
    print()
    print("Inject commands with curl:")
    print('  curl -X POST http://localhost:5000/simulate/charge \\')
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"power_kw": 500, "duration_sec": 3600}\'')
    print()
    app.run(port=5001, debug=False, ssl_context=ssl_context)
