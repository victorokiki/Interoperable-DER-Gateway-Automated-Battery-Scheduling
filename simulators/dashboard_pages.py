"""
simulators/dashboard_pages.py
The Grid Operator dashboard pages that explain the gateway in plain English:
Overview and Alerts & failures, behind a sidebar. Registered onto the Flask app
in fake_grid_server.py.

Everything shown here is read from what the gateway already records, relative
to the working directory (like live_status.json): the audit log
(schedule_store/audit_log.db), the schedule store (schedule_store/schedule.db)
and the live status snapshot. Nothing is written, and the databases are opened
read-only, so the dashboard can never disturb the gateway.
"""

import csv
import io
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Response, jsonify, render_template_string, request, session, url_for

AUDIT_DB = Path("schedule_store/audit_log.db")
SCHEDULE_DB = Path("schedule_store/schedule.db")

HISTORY_SAMPLE_SEC = 5
HISTORY = deque(maxlen=2160)            # 3 hours at one sample per 5 s; fills while the server runs
_history_started = False

# Outcomes in the audit log that mean something went wrong / is degraded.
BAD_OUTCOMES = {"failed", "error", "rejected", "refused", "lost", "unconfirmed", "fault", "mismatch"}
WARN_OUTCOMES = {"not_sent", "stale", "timeout", "retrying", "degraded", "skipped", "gave_up"}

EVENT_STATUS_WORDS = {
    "scheduled": "Waiting to start", "active": "Running now", "completed": "Finished",
    "superseded": "Replaced by a newer event", "cancelled": "Cancelled by the utility",
    "canceled": "Cancelled by the utility", "expired": "Expired before it could run",
}
RESPONSE_WORDS = {1: "received", 2: "started", 3: "completed", 4: "opted out", 5: "opted in",
                  6: "cancelled", 7: "superseded"}
REPORT_KINDS = {"DERStatus": "battery status", "DERCapability": "battery capabilities",
                "DERSettings": "battery settings", "MirrorMeterReading": "meter readings"}

# What the gateway does by itself for a given kind of problem (shown next to failures).
GATEWAY_ACTION = {
    ("ieee_reporting", "failed"): "Tries again automatically, waiting a little longer each time.",
    ("ieee_reporting", "not_sent"): "Skips this report and sends the next one once battery readings are fresh again.",
    ("modbus_write", "failed"): "Does not assume the battery changed; the battery is held at a safe setting.",
    ("bms_condition", "stale"): "Holds the battery safe until fresh readings arrive.",
    ("ieee_response", "failed"): "Keeps the reply queued and tries again.",
}


def _query(db: Path, sql: str, args: tuple = ()) -> list:
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _short(ident: str) -> str:
    """Long utility event IDs read badly in a sentence; the full ID stays in the technical details."""
    return ident if len(ident) <= 12 else f"{ident[:8]}…{ident[-4:]}"


def _utc_token(text: str) -> str:
    """'2026-10-06 18:23:30 UTC' as a marker the browser shows in Pacific time."""
    try:
        when = datetime.strptime(text.replace("UTC", "").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return text
    return f"@@t={int(when.timestamp())}@@"


def _ago_text(sec: float) -> str:
    sec = max(0, round(sec))
    return f"{sec}s ago" if sec < 120 else f"{round(sec / 60)} min ago" if sec < 7200 else f"{round(sec / 3600)} h ago"


def _kw(v) -> str:
    v = float(v)
    if v == 0:
        return "idle (0 kW)"
    return f"{'charge' if v > 0 else 'discharge'} {abs(v):g} kW"


def _severity(outcome: str) -> str:
    o = (outcome or "").lower()
    if o in BAD_OUTCOMES:
        return "bad"
    if o in WARN_OUTCOMES:
        return "warn"
    return "ok"


def explain(ts_utc: str, event_type: str, source: str, detail: str, outcome: str) -> dict:
    """One audit-log row in plain English, with the raw text kept for engineers."""
    detail = detail or ""
    title = None
    m = None
    if event_type == "gateway":
        title = "Gateway started" if outcome == "started" else "Gateway stopped safely" if outcome == "stopped" else f"Gateway {outcome}"
    elif event_type == "bms_condition":
        title = {"configured": "Battery controller is answering", "detected": "Battery type recognised",
                 "observed": "Battery checked at start-up", "confirmed": "Battery control modes confirmed",
                 "live": "Battery heartbeat is running"}.get(outcome, f"Battery condition: {outcome}")
    elif event_type == "bms_command":
        title = "Battery mode set up" if outcome == "success" else f"Battery mode change {outcome}"
    elif event_type == "authority":
        if source == "safe_idle":
            title = "Decision: hold the battery at idle (no valid utility schedule)"
        elif source == "operator_force_idle":
            title = "Decision: operator force idle, utility events ignored"
        elif source.startswith("schedule:"):
            m = re.search(r"power_kw=([+-][\d.]+)", detail)
            title = f"Decision: follow utility event {_short(source.split(':', 1)[1])}" + (f" ({_kw(m.group(1))})" if m else "")
        else:
            title = f"Decision: {source}"
    elif event_type == "dispatch":
        m = re.search(r"setpoint ([+-][\d.]+) kW", detail)
        title = ("Power setting sent to the battery" + (f": {_kw(m.group(1))}" if m else "")) if outcome == "success" \
            else "Power setting could not be sent to the battery"
    elif event_type == "modbus_write":
        title = "Battery confirmed the new power setting" if outcome == "confirmed" else f"Battery power setting not confirmed ({outcome})"
    elif event_type == "ieee_time":
        title = "Clock agrees with the utility's clock" if outcome == "synchronized" else f"Clock check: {outcome}"
    elif event_type == "ieee_discovery":
        title = "Found the utility's control program"
    elif event_type == "ieee_reporting":
        first = detail.split(" ", 1)[0] if detail else ""
        kind = REPORT_KINDS.get(first, "meter readings" if first.startswith("metering") else "report")
        if outcome == "sent":
            title = ("Meter reporting set up with the utility" if first == "MirrorUsagePoint"
                     else f"Sent {kind} to the utility" + (" (after an earlier failure)" if " again" in detail else ""))
        elif outcome == "not_sent":
            title = f"{kind.capitalize()} not sent: no recent battery reading"
        elif outcome == "failed":
            code = re.search(r"HTTP (\d+)", detail)
            refused = re.search(r"Cannot connect|refused|ClientConnector|timed out", detail)
            title = (f"Report of {kind} to the utility failed" +
                     (f" (error {code.group(1)})" if code else " (could not connect to the utility)" if refused else ""))
        else:
            title = "Found where to send reports"
    elif event_type == "ieee_response":
        m = re.search(r"DERControlResponse (\w+) for (\w+)", detail)
        word = {"EventReceived": "received", "EventStarted": "started", "EventCompleted": "completed",
                "EventSuperseded": "replaced by a newer one", "EventCancelled": "cancelled"}.get(m.group(1), m.group(1)) if m else ""
        title = f"Told the utility event {_short(m.group(2))} was {word}" if m else "Reply sent to the utility"
        if outcome != "sent":
            title = "Reply to the utility " + outcome + (f" ({_short(m.group(2))})" if m else "")
    elif event_type == "schedule_received":
        m = re.search(r"mrid=(\w+) power_kw=([+-][\d.]+) start=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC) end=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC)", detail)
        if m and re.search(r"status=cancelled", detail):
            return {"ts": _epoch(ts_utc), "sev": "ok", "title": f"The utility cancelled event {_short(m.group(1))} ({_kw(m.group(2))})", "action":
                    "The gateway will not run it, and stops it if it was running.", "kind": event_type, "source": source,
                    "outcome": outcome, "detail": detail}
        title = (f"New event {_short(m.group(1))} from the utility: {_kw(m.group(2))}, {_utc_token(m.group(3))} to {_utc_token(m.group(4))}"
                 if m else "New event from the utility")
    elif event_type == "schedule_status":
        m = re.search(r"mrid=(\w+) -> (\w+)", detail)
        title = f"Event {_short(m.group(1))} is now: {EVENT_STATUS_WORDS.get(m.group(2), m.group(2)).lower()}" if m else "Event status changed"
    elif event_type == "operator_command":
        who = source.split(":", 1)[-1]
        title = f"Operator {who} switched Force Idle {'on' if outcome == 'engaged' else 'off'}"
    if title is None:
        title = event_type.replace("_", " ").capitalize() + (f": {outcome}" if outcome else "")
    return {
        "ts": _epoch(ts_utc), "sev": _severity(outcome), "title": title,
        "action": GATEWAY_ACTION.get((event_type, outcome), ""),
        "kind": event_type, "source": source, "outcome": outcome, "detail": detail,
    }


def _recent_audit(limit: int, since: float = 0.0, outcomes: set | None = None) -> list:
    sql = "SELECT ts_utc, event_type, source, detail, outcome FROM audit_log"
    args: list = []
    where = []
    if since:
        where.append("ts_utc >= ?")
        args.append(datetime.fromtimestamp(since, timezone.utc).isoformat())
    if outcomes:
        where.append("outcome IN (%s)" % ",".join("?" * len(outcomes)))
        args += sorted(outcomes)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [explain(*r) for r in _query(AUDIT_DB, sql, tuple(args))]


def _control_text(auth) -> tuple[str, str]:
    if not auth:
        return "Unknown", "warn"
    kind = auth.get("authority", "")
    if kind == "safe_idle":
        return "Safe idle: no valid utility schedule, battery held at 0 kW", "info"
    if kind == "operator_force_idle":
        return "Operator force idle: utility events are ignored until released", "info"
    if kind in ("approved_schedule",) or kind.startswith("schedule"):
        return f"Following utility event {auth.get('mrid') or ''}".strip(), "ok"
    return kind or "Unknown", "info"


def live_alerts(state: dict) -> list:
    """What needs attention right now, from the live status. Each item says what it means and what to do."""
    out = []
    secs = state.get("seconds_since_poll")
    if not state.get("gateway_connected"):
        out.append(("bad", "The gateway is not checking in with the utility",
                    "No contact yet." if secs is None else f"Last contact was {secs} seconds ago.",
                    "Check the gateway is running and can reach the utility server."))
    b = state.get("battery") or {}
    if not b.get("available"):
        out.append(("bad", "No live battery data", b.get("reason", ""),
                    "Check the gateway is running and connected to the battery."))
        return out
    if b.get("device_stale"):
        age = b.get("device_age_sec")
        out.append(("warn", "Battery readings are out of date",
                    "The battery has not been read yet." if age is None else f"Last reading was {age} seconds ago.",
                    "Check the connection between the gateway and the battery controller."))
    if b.get("bms_heartbeat_stale"):
        out.append(("bad", "The battery controller's heartbeat has stopped",
                    "The battery controller is no longer showing signs of life.", "Check the battery controller; the gateway holds the battery safe."))
    if b.get("operating_status") in ("Fault", "Critical Fault"):
        out.append(("bad", f"Battery reports a {b['operating_status'].lower()}",
                    "The battery's own controller has flagged a problem.", "An engineer should inspect the battery."))
    rel = (b.get("reliability_state") or "").upper()
    if rel in ("DERATED", "AUTONOMOUS"):
        out.append(("warn", f"Reliability state: {rel.title()}", "The gateway is running in a reduced mode.", "Watch for the state returning to Connected."))
    elif rel in ("DISCONNECTED", "SAFE_IDLE"):
        out.append(("bad", f"Reliability state: {rel.replace('_', ' ').title()}", "The gateway has stopped following the utility.", "Check the utility link."))
    a = b.get("authority") or {}
    if a and not a.get("confirmed"):
        out.append(("warn", "The battery has not confirmed the requested power", "The last setting sent has not been read back as accepted.", "Wait a few seconds; if it persists check the battery controller."))
    if a.get("authority") == "operator_force_idle":
        out.append(("warn", "Force Idle is on", "The battery is held at zero and every utility event is being ignored, including any you send from this dashboard.", "Press Release Force Idle (button at the top of every page) when you want utility events to run again."))
    return out


def _events_summary(now: float) -> dict:
    rows = _query(SCHEDULE_DB, "SELECT status, COUNT(*) FROM events WHERE received_at >= ? GROUP BY status", (now - 86400,))
    return {r[0]: r[1] for r in rows}


def _recent_events(limit: int = 8) -> list:
    rows = _query(SCHEDULE_DB, "SELECT mrid, start_ts, end_ts, power_kw, status, received_at FROM events ORDER BY received_at DESC LIMIT ?", (limit,))
    told = {r[0]: (r[1], r[2], r[3]) for r in _query(
        SCHEDULE_DB, "SELECT mrid, SUM(sent_at IS NOT NULL), COUNT(*), SUM(gave_up_at IS NOT NULL) FROM responses GROUP BY mrid")}
    out = []
    for mrid, start, end, kw, status, _ in rows:
        sent, total, gave_up = told.get(mrid, (0, 0, 0))
        out.append({"mrid": mrid, "start": start, "end": end, "what": _kw(kw), "status": EVENT_STATUS_WORDS.get(status, status),
                    "raw_status": status, "told": f"{sent or 0} of {total} replies sent" if total else "No replies needed",
                    "told_tone": "bad" if gave_up else ("warn" if total and (sent or 0) < total else "ok")})
    return out


def _undelivered() -> list:
    rows = _query(SCHEDULE_DB, "SELECT mrid, status, attempts, gave_up_at, last_error FROM responses WHERE sent_at IS NULL OR gave_up_at IS NOT NULL ORDER BY created_at DESC LIMIT 50")
    return [{"mrid": r[0], "reply": RESPONSE_WORDS.get(r[1], f"status {r[1]}"), "attempts": r[2],
             "gave_up": bool(r[3]), "error": r[4] or ""} for r in rows]


def _tiles(state: dict, now: float) -> tuple[list, int]:
    b = state.get("battery") or {}
    alerts = live_alerts(state)
    attention = sum(1 for a in alerts if a[0] in ("bad", "warn"))
    day = datetime.fromtimestamp(now - 86400, timezone.utc).isoformat()
    failures = _query(AUDIT_DB, "SELECT COUNT(*) FROM audit_log WHERE ts_utc >= ? AND outcome IN (%s)" % ",".join("?" * len(BAD_OUTCOMES)),
                      (day, *sorted(BAD_OUTCOMES)))
    failures = failures[0][0] if failures else 0
    ev = _events_summary(now)
    last = _query(AUDIT_DB, "SELECT ts_utc FROM audit_log WHERE event_type='ieee_reporting' AND outcome='sent' ORDER BY id DESC LIMIT 1")
    rep_age = None if not last else now - _epoch(last[0][0])

    if not b.get("available"):
        battery = ("No data", "The gateway is not publishing", "bad")
    else:
        v = b.get("verified_kw")
        battery = (("Unconfirmed", "Battery has not confirmed its setting", "bad") if v is None else
                   ("Idle", "Not charging or discharging", "ok") if v == 0 else
                   ("Charging", f"{abs(v):g} kW going in", "ok") if v > 0 else
                   ("Discharging", f"{abs(v):g} kW going out", "ok"))
    soc = b.get("soc_pct") if b.get("available") else None
    secs = state.get("seconds_since_poll")
    auth = (b.get("authority") or {}) if b.get("available") else {}
    kind = auth.get("authority", "")
    if not b.get("available"):
        control = ("Unknown", "No battery data", "warn")
    elif kind == "operator_force_idle":
        control = ("Operator", "Force Idle is on: utility events ignored", "warn")
    elif kind in ("approved_schedule",) or kind.startswith("schedule"):
        control = ("Utility schedule", "Following event " + (auth.get("mrid") or kind.split(":", 1)[-1]), "ok")
    elif kind == "safe_idle":
        control = ("Safe idle", "No valid utility schedule", "ok")
    else:
        control = (kind or "Unknown", "", "warn")
    tiles = [
        {"label": "Battery now", "value": battery[0], "sub": battery[1], "tone": battery[2]},
        {"label": "Who is in control", "value": control[0], "sub": control[1], "tone": control[2]},
        {"label": "Charge level", "value": "Unknown" if soc is None else f"{soc:.1f}%", "sub": "of battery capacity",
         "tone": "warn" if soc is None else "bad" if soc < 15 else "warn" if soc < 25 else "ok"},
        {"label": "Utility link", "value": "Connected" if state.get("gateway_connected") else "Lost",
         "sub": "No contact yet" if secs is None else f"Last check-in {secs}s ago", "tone": "ok" if state.get("gateway_connected") else "bad"},
        {"label": "Needs attention", "value": str(attention), "sub": "problems right now" if attention != 1 else "problem right now",
         "tone": "ok" if attention == 0 else "bad" if any(a[0] == "bad" for a in alerts) else "warn"},
        {"label": "Failures (24 h)", "value": str(failures), "sub": "recorded in the audit log", "tone": "ok" if failures == 0 else "warn"},
        {"label": "Utility events (24 h)", "value": str(sum(ev.values())),
         "sub": f"{ev.get('completed', 0)} finished, {ev.get('active', 0)} running", "tone": "ok"},
        {"label": "Last report to utility", "value": "None yet" if rep_age is None else _ago_text(rep_age),
         "sub": "battery status sent to the utility", "tone": "ok" if rep_age is not None and rep_age <= 900 else "warn"},
    ]
    return tiles, attention


def start_history(state_provider) -> None:
    """Starts the background sampler behind the charts (once). Called when the simulator runs, not on import."""
    global _history_started
    if not _history_started:
        _history_started = True
        threading.Thread(target=_sample_forever, args=(state_provider,), name="dashboard-history", daemon=True).start()


def _sample_forever(state_provider):
    while True:
        try:
            b = state_provider().get("battery") or {}
            if b.get("available"):
                a = b.get("authority") or {}
                HISTORY.append((time.time(), b.get("soc_pct"), a.get("desired_kw"), b.get("measured_kw")))
        except Exception:
            pass
        time.sleep(HISTORY_SAMPLE_SEC)


COMMON_CSS = """
<style>
  .role-tag { border: 1px solid var(--amber); color: var(--amber); font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; padding: 3px 8px; margin-left: 8px; }
  .viewer-note { display: none; border: 1px dashed var(--muted); padding: 12px 14px; font-size: 13px; color: var(--muted); }
  body[data-role="viewer"] .viewer-note { display: block; }
  body[data-role="viewer"] .ops-only { display: none !important; }
  .ico-grid { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square'%3E%3Crect x='3' y='3' width='7' height='7'/%3E%3Crect x='14' y='3' width='7' height='7'/%3E%3Crect x='3' y='14' width='7' height='7'/%3E%3Crect x='14' y='14' width='7' height='7'/%3E%3C/svg%3E"); }
  .ico-alert { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square' stroke-linejoin='miter'%3E%3Cpath d='M12 3L2 21h20L12 3z'/%3E%3Cpath d='M12 10v5M12 18h.01'/%3E%3C/svg%3E"); }
  .shell { display: flex; align-items: stretch; min-height: calc(100vh - 62px); }
  .side { width: 224px; flex: none; border-right: 1px solid var(--line); padding: 20px 0; background: var(--panel); }
  .side .side-h { font-size: 11px; font-weight: 700; letter-spacing: 0.2em; color: var(--red-text); padding: 0 20px 12px; }
  .nav { display: flex; align-items: center; padding: 11px 20px; color: var(--muted); text-decoration: none; font-size: 12px;
         font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; border-left: 3px solid transparent; }
  .nav:hover { color: var(--ink); background: var(--zebra); }
  .nav.on { color: var(--ink); border-left-color: var(--red); background: var(--zebra); }
  .nav .badge { margin-left: auto; background: var(--red); color: #fff; font-size: 11px; padding: 1px 7px; letter-spacing: 0; }
  .content { flex: 1; min-width: 0; padding: 24px; max-width: 1240px; overflow-wrap: anywhere; }
  .page-h { font-size: 12px; font-weight: 700; letter-spacing: 0.2em; color: var(--red-text); text-transform: uppercase; margin: 0 0 4px; }
  .page-h::before { content: '// '; }
  .page-sub { color: var(--muted); font-size: 13px; margin: 0 0 20px; }
  .tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }
  @media (max-width: 820px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  .tile { background: var(--panel); border: 1px solid var(--line); border-top: 3px solid var(--tc, var(--line)); padding: 14px 16px; container-type: inline-size; }
  .tile .l { font-size: 10px; font-weight: 700; letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted); }
  .tile .v { font-size: clamp(14px, 10cqw, 24px); font-weight: 800; letter-spacing: -0.02em; margin: 8px 0 4px; color: var(--vc, var(--ink)); }
  .tile .s { font-size: 11px; color: var(--muted); }
  .tile.ok { --tc: var(--green); --vc: var(--green); }
  .tile.warn { --tc: var(--amber); --vc: var(--amber); }
  .tile.bad { --tc: var(--red); --vc: var(--red-text); }
  .two { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 24px; }
  .chart { width: 100%; min-height: 180px; }
  .legend { font-size: 11px; color: var(--muted); display: flex; gap: 16px; margin-top: 6px; flex-wrap: wrap; }
  .legend i { display: inline-block; width: 12px; height: 3px; margin-right: 6px; vertical-align: middle; }
  .stat-hint { font-size: 11px; color: var(--faint); margin: 4px 0 0; }
  .sev-ok { color: var(--green); } .sev-warn { color: var(--amber); } .sev-bad { color: var(--red-text); } .sev-info { color: var(--muted); }
  .act { display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
  .act:last-child { border-bottom: 0; }
  .act > div { min-width: 0; }
  .act .when { flex: none; width: 74px; color: var(--faint); font-size: 11px; padding-top: 2px; }
  .act .pip { flex: none; width: 8px; height: 8px; margin-top: 5px; background: var(--muted); }
  .act.ok .pip { background: var(--green); } .act.warn .pip { background: var(--amber); } .act.bad .pip { background: var(--red); }
  .act .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .act.bad .ttl { color: var(--red-text); font-weight: 700; } .act.warn .ttl { color: var(--amber); font-weight: 700; }
  details { margin-top: 4px; } summary { cursor: pointer; font-size: 11px; color: var(--faint); }
  details code { display: block; white-space: pre-wrap; word-break: break-word; font-size: 11px; color: var(--muted); margin-top: 4px; font-family: var(--mono); }
  .alert { border: 1px solid var(--line); border-left: 4px solid var(--muted); background: var(--panel); padding: 12px 16px; margin-bottom: 10px; }
  .alert.bad { border-left-color: var(--red); } .alert.warn { border-left-color: var(--amber); } .alert.info { border-left-color: var(--muted); }
  .alert .t { font-weight: 700; font-size: 14px; } .alert .d { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .alert .todo { font-size: 12px; margin-top: 6px; } .alert .todo b { letter-spacing: .1em; text-transform: uppercase; font-size: 10px; color: var(--red-text); }
  .allclear { border: 1px solid var(--green); background: var(--ok-bg); color: var(--green); padding: 14px 16px; font-weight: 700; }
  select { font-family: var(--mono); font-size: 12px; padding: 6px 8px; border: 1px solid var(--line); background: var(--field); color: var(--ink); }
  .toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }
  .tablewrap { overflow-x: auto; }
  .idle-banner { display: flex; gap: 16px; align-items: center; justify-content: space-between; flex-wrap: wrap; padding: 12px 32px;
                 background: var(--bad-bg); color: var(--red-text); border-bottom: 2px solid var(--red); font-size: 13px; }
  .idle-banner[hidden] { display: none; } .idle-banner form { margin: 0; }
  .notice { padding: 12px 16px; margin-bottom: 20px; border: 1px solid; font-size: 13px; font-weight: 700; }
  .notice.ok { color: var(--green); background: var(--ok-bg); } .notice.warn { color: var(--amber); background: var(--warn-bg); }
  @media (max-width: 900px) {
    .shell { flex-direction: column; } .side { width: auto; border-right: 0; border-bottom: 1px solid var(--line); padding: 8px 0; display: flex; overflow-x: auto; }
    .side .side-h { display: none; } .nav { border-left: 0; border-bottom: 3px solid transparent; white-space: nowrap; }
    .nav.on { border-bottom-color: var(--red); } .two { grid-template-columns: minmax(0, 1fr); } .content { padding: 16px; }
  }
</style>
"""

COMMON_JS = """
<script>
function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; });
}
function cssv(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
const TZ = 'America/Vancouver';
function clock(ts) { return ts ? new Intl.DateTimeFormat('en-US', {timeZone: TZ, hour: 'numeric', minute: '2-digit', second: '2-digit'}).format(new Date(ts * 1000)) : '—'; }
function dateTime(ts) { return ts ? new Intl.DateTimeFormat('en-US', {timeZone: TZ, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}).format(new Date(ts * 1000)) : '—'; }
function fmtT(text) { return String(text).replace(/@@t=(\\d+)@@/g, function (m, s) { return dateTime(Number(s)); }); }
function tickPT() {
  const el = document.getElementById('pt-clock');
  if (el) el.textContent = 'Vancouver ' + new Intl.DateTimeFormat('en-US', {timeZone: TZ, weekday: 'short', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'}).format(new Date());
}
setInterval(tickPT, 15000);
document.addEventListener('DOMContentLoaded', function () {
  tickPT();
  document.querySelectorAll('.notice').forEach(function (el) { el.textContent = fmtT(el.textContent); });
});
const CAN_CONTROL = document.body.dataset.role !== 'viewer';
function queueTable(events, acks) {
  if (!events.length) return '<p class="empty">Nothing queued. Plan events on the Schedule page, or use Run now.</p>';
  return '<table><tr><th>Event ID</th><th>What</th><th>When</th><th>Status</th><th>Told</th>' + (CAN_CONTROL ? '<th></th>' : '') + '</tr>' + events.map(function (e) {
    const kind = e.power_kw >= 0 ? '<span class="tag charge">CHARGE</span>' : '<span class="tag discharge">DISCHARGE</span>';
    const ack = acks && acks[e.mrid] ? '<span class="ack-yes">Yes</span>' : '<span class="ack-no">Pending</span>';
    return '<tr><td class="id">' + esc(e.mrid) + '</td><td>' + kind + ' ' + Math.abs(e.power_kw).toFixed(0) + ' kW</td><td>' + esc(dateTime(e.start)) + '<br>to ' + esc(dateTime(e.end)) +
           '</td><td>' + (e.running ? '<b class="sev-ok">Running now</b>' : 'Waiting to start') + '</td><td title="Has the utility been told this event was received?">' + ack +
           '</td>' + (CAN_CONTROL ? '<td><button class="btn grey" type="button" data-cancel="' + esc(e.mrid) + '">Cancel</button></td>' : '') + '</tr>';
  }).join('') + '</table>';
}
document.addEventListener('click', async function (ev) {
  const b = ev.target.closest('button[data-cancel]'); if (!b) return;
  if (!confirm('Cancel event ' + b.dataset.cancel + '?')) return;
  await fetch('/api/schedule/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mrid: b.dataset.cancel})});
  if (window.afterChange) window.afterChange();
});
function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 90) return s + ' s ago'; if (s < 5400) return Math.round(s / 60) + ' min ago';
  if (s < 129600) return Math.round(s / 3600) + ' h ago'; return Math.round(s / 86400) + ' days ago';
}
function limitsHint(b) {
  const l = b && b.limits_kw, c = l && l.max_charge_kw, x = l && l.max_discharge_kw;
  let t = 'Type a whole number of kilowatts.';
  if (typeof c === 'number' && typeof x === 'number') {
    t = (c > 0 || x > 0)
      ? 'Right now the battery accepts up to ' + c + ' kW charging and ' + x + ' kW discharging.'
      : 'The battery is not accepting power right now (it may be off or in standby), so a command cannot run yet.';
  }
  document.querySelectorAll('.limits-hint').forEach(function (el) { el.textContent = t; });
}
async function pollBanner() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) return;
    const d = await r.json(), a = d.battery && d.battery.authority;
    const on = !!(a && a.authority === 'operator_force_idle');
    limitsHint(d.battery);
    document.getElementById('idle-banner').hidden = !on;
    if (on) {
      const m = /engaged by ([^:]+)/.exec(a.reason || '');
      document.getElementById('idle-who').textContent = m ? ' Set by ' + m[1] + '.' : '';
    }
  } catch (e) { /* next poll retries */ }
}
setInterval(pollBanner, 3000); document.addEventListener('DOMContentLoaded', pollBanner);
function labelTables() {
  document.querySelectorAll('table').forEach(function (t) {
    const heads = Array.prototype.map.call(t.querySelectorAll('tr:first-child th'), function (h) { return h.textContent.trim(); });
    if (!heads.length) return;
    t.querySelectorAll('tr').forEach(function (r) {
      Array.prototype.forEach.call(r.children, function (c, i) { if (c.tagName === 'TD' && !c.hasAttribute('data-label') && heads[i]) c.setAttribute('data-label', heads[i]); });
    });
  });
}
new MutationObserver(labelTables).observe(document.body, {childList: true, subtree: true}); labelTables();
function setBadge(n) {
  const b = document.getElementById('nav-badge'); if (!b) return;
  b.textContent = n; b.hidden = !(n > 0);
}
function activityRow(a) {
  return '<div class="act ' + esc(a.sev) + '"><span class="when" title="' + esc(dateTime(a.ts)) + '">' + esc(clock(a.ts)) + '</span><span class="pip"></span><div>' +
    '<div class="ttl">' + fmtT(esc(a.title)) + '</div>' + (a.action ? '<div class="sub">What the gateway did: ' + esc(a.action) + '</div>' : '') +
    '<details><summary>Technical details</summary><code>' + esc(a.kind + ' / ' + a.source + ' / ' + a.outcome + '\\n' + a.detail) + '</code></details></div></div>';
}
function lineChart(el, series, opt) {
  const W = Math.max(280, el.clientWidth), H = 180, L = 46, R = 10, T = 10, B = 22;
  const pts = series.flatMap(function (s) { return s.pts; }).filter(function (p) { return p[1] !== null && p[1] !== undefined; });
  if (pts.length < 2) { el.innerHTML = '<p class="empty">Collecting data. The chart fills in while the dashboard is running.</p>'; return; }
  const t0 = Math.min.apply(null, pts.map(function (p) { return p[0]; })), t1 = Math.max.apply(null, pts.map(function (p) { return p[0]; }));
  let lo = opt.min !== undefined ? opt.min : Math.min.apply(null, pts.map(function (p) { return p[1]; }));
  let hi = opt.max !== undefined ? opt.max : Math.max.apply(null, pts.map(function (p) { return p[1]; }));
  if (opt.symmetric) { const m = Math.max(Math.abs(lo), Math.abs(hi), 10); lo = -m; hi = m; }
  if (hi - lo < 1) { hi += 1; lo -= 1; }
  const x = function (t) { return L + (t - t0) / Math.max(1, t1 - t0) * (W - L - R); };
  const y = function (v) { return T + (hi - v) / (hi - lo) * (H - T - B); };
  const line = cssv('--line'), muted = cssv('--muted');
  let g = '';
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4;
    g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="' + line + '"/>' +
         '<text x="' + (L - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end" font-size="11" fill="' + muted + '">' + Math.round(v) + (opt.unit || '') + '</text>';
  }
  g += '<text x="' + L + '" y="' + (H - 4) + '" font-size="11" fill="' + muted + '">' + clock(t0) + '</text>' +
       '<text x="' + (W - R) + '" y="' + (H - 4) + '" text-anchor="end" font-size="11" fill="' + muted + '">' + clock(t1) + '</text>';
  series.forEach(function (s) {
    let d = ''; s.pts.forEach(function (p) { if (p[1] === null || p[1] === undefined) return; d += (d ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1); });
    g += '<path d="' + d + '" fill="none" stroke="' + cssv(s.color) + '" stroke-width="2"' + (s.dash ? ' stroke-dasharray="5 4"' : '') + '/>';
  });
  el.innerHTML = '<svg width="' + W + '" height="' + H + '" role="img" aria-label="' + esc(opt.label) + '">' + g + '</svg>' +
    '<div class="legend">' + series.map(function (s) { return '<span><i style="background:' + cssv(s.color) + '"></i>' + esc(s.name) + '</span>'; }).join('') + '</div>';
}
</script>
"""

KIND_NAMES = {
    "gateway": "Gateway start and stop", "bms_condition": "Battery condition", "bms_command": "Battery set-up",
    "authority": "Control decisions", "dispatch": "Power settings sent", "modbus_write": "Battery confirmations",
    "ieee_time": "Clock checks", "ieee_discovery": "Finding the utility", "ieee_reporting": "Reports to the utility",
    "ieee_response": "Replies to the utility", "schedule_received": "Events received", "schedule_status": "Event status changes",
    "operator_command": "Operator actions",
}
MRID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _fmt_utc(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _csv_cell(v):
    """Numbers stay numbers; text that a spreadsheet could run as a formula is defused."""
    if isinstance(v, (int, float)):
        return v
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv_response(name: str, header: list, rows: list) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in rows:
        writer.writerow([_csv_cell(c) for c in r])
    resp = Response("﻿" + buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


def _audit_rows(limit: int, since: float = 0.0, outcomes: set | None = None, kind: str | None = None,
                before: int | None = None, exclude: set | None = None) -> list:
    sql = "SELECT id, ts_utc, event_type, source, detail, outcome FROM audit_log"
    where, args = [], []
    if since:
        where.append("ts_utc >= ?")
        args.append(datetime.fromtimestamp(since, timezone.utc).isoformat())
    if outcomes:
        where.append("outcome IN (%s)" % ",".join("?" * len(outcomes)))
        args += sorted(outcomes)
    if exclude:
        where.append("outcome NOT IN (%s)" % ",".join("?" * len(exclude)))
        args += sorted(exclude)
    if kind:
        where.append("event_type = ?")
        args.append(kind)
    if before:
        where.append("id < ?")
        args.append(before)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in _query(AUDIT_DB, sql, tuple(args)):
        d = explain(*r[1:])
        d["id"] = r[0]
        out.append(d)
    return out


def _search_audit(q: str, kind: str | None, level: str, since: float, before: int | None, limit: int) -> tuple[list, bool]:
    outcomes = BAD_OUTCOMES if level == "bad" else WARN_OUTCOMES if level == "warn" else None
    exclude = BAD_OUTCOMES | WARN_OUTCOMES if level == "ok" else None
    q = (q or "").strip().lower()
    out: list = []
    cursor = before
    for _ in range(40):
        chunk = _audit_rows(500, since, outcomes, kind, cursor, exclude)
        if not chunk:
            break
        for r in chunk:
            if not q or q in " ".join((r["title"], r["detail"], r["source"], r["outcome"], r["kind"])).lower():
                if len(out) == limit:
                    return out, True
                out.append(r)
        cursor = chunk[-1]["id"]
        if len(chunk) < 500:
            break
    return out, False


def _group(rows: list) -> list:
    groups: dict = {}
    for r in rows:
        title = r["title"].split(" (error")[0]
        g = groups.setdefault((r["kind"], r["outcome"], title),
                              {"title": title, "sev": r["sev"], "action": r["action"], "count": 0, "last": 0})
        g["count"] += 1
        g["last"] = max(g["last"], r["ts"])
    return sorted(groups.values(), key=lambda g: -g["last"])


def _events_list(since: float = 0.0, status: str | None = None, limit: int = 200) -> list:
    sql = ("SELECT mrid, start_ts, end_ts, power_kw, status, revision, received_at, superseded_by, status_reason, "
           "program, primacy FROM events")
    where, args = [], []
    if since:
        where.append("received_at >= ?")
        args.append(since)
    if status:
        where.append("status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY received_at DESC LIMIT ?"
    args.append(limit)
    told = {r[0]: (r[1] or 0, r[2], r[3] or 0) for r in _query(
        SCHEDULE_DB, "SELECT mrid, SUM(sent_at IS NOT NULL), COUNT(*), SUM(gave_up_at IS NOT NULL) FROM responses GROUP BY mrid")}
    out = []
    for mrid, start, end, kw, st, rev, recv, sup, reason, program, primacy in _query(SCHEDULE_DB, sql, tuple(args)):
        sent, total, gave_up = told.get(mrid, (0, 0, 0))
        out.append({
            "mrid": mrid, "start": start, "end": end, "duration": max(0, (end or 0) - (start or 0)), "kw": kw,
            "what": _kw(kw), "status": EVENT_STATUS_WORDS.get(st, st), "raw_status": st, "revision": rev, "received": recv,
            "replaced_by": sup or "", "reason": reason or "", "program": program or "", "priority": primacy,
            "sent": sent, "total": total,
            "told": f"{sent} of {total} replies sent" if total else "No replies needed",
            "told_tone": "bad" if gave_up else ("warn" if total and sent < total else "ok"),
        })
    return out


def _event_trail(mrid: str) -> list:
    rows = _query(AUDIT_DB, "SELECT id, ts_utc, event_type, source, detail, outcome FROM audit_log "
                            "WHERE source = ? OR detail LIKE ? ORDER BY id ASC LIMIT 200", (f"schedule:{mrid}", f"%{mrid}%"))
    out = []
    for r in rows:
        d = explain(*r[1:])
        d["id"] = r[0]
        out.append(d)
    return out


def _battery_checks(state: dict) -> list:
    b = state.get("battery") or {}
    if not b.get("available"):
        return [{"tone": "bad", "label": "Live battery data", "detail": b.get("reason", "Not available.")}]
    a = b.get("authority") or {}
    age = b.get("device_age_sec")
    offset = b.get("clock_offset_sec")
    rel = (b.get("reliability_state") or "").upper()
    return [
        {"tone": "bad" if b.get("device_stale") else "ok", "label": "Battery readings are fresh",
         "detail": "Never read yet." if age is None else f"Last read {age} seconds ago."},
        {"tone": "bad" if b.get("bms_heartbeat_stale") else "ok", "label": "Battery controller heartbeat",
         "detail": "Stopped changing, so the controller may be frozen." if b.get("bms_heartbeat_stale") else "Running normally."},
        {"tone": "bad" if b.get("operating_status") in ("Fault", "Critical Fault") else "ok", "label": "No battery fault",
         "detail": f"The battery reports: {b.get('operating_status') or 'unknown'}."},
        {"tone": "ok" if a.get("confirmed") else "warn", "label": "Battery confirmed its setting",
         "detail": "The last power setting was read back and matches." if a.get("confirmed") else "The last setting has not been confirmed yet."},
        {"tone": "ok" if state.get("gateway_connected") else "bad", "label": "Link to the utility",
         "detail": "Checking in normally." if state.get("gateway_connected") else "The gateway has stopped checking in."},
        {"tone": "ok" if rel == "CONNECTED" else "warn" if rel in ("DERATED", "AUTONOMOUS") else "bad", "label": "Gateway reliability state",
         "detail": (rel.replace("_", " ").title() or "Unknown") + "."},
        {"tone": "ok" if offset is None or abs(offset) < 2 else "warn", "label": "Clocks agree with the utility",
         "detail": "No clock check yet." if offset is None else f"Difference {offset:+.1f} seconds."},
    ]


def _report(now: float, hours: int) -> dict:
    since = now - hours * 3600 if hours else 0.0
    rows = _audit_rows(50000, since)
    events = _events_list(since)
    statuses: dict = {}
    for e in events:
        statuses[e["status"]] = statuses.get(e["status"], 0) + 1
    bad = [r for r in rows if r["sev"] == "bad"]
    warn = [r for r in rows if r["sev"] == "warn"]
    return {
        "from": since or (min((r["ts"] for r in rows), default=now)), "to": now, "hours": hours,
        "counts": {
            "events": len(events), "event_statuses": statuses,
            "reports_sent": sum(1 for r in rows if r["kind"] == "ieee_reporting" and r["outcome"] == "sent"),
            "power_settings": sum(1 for r in rows if r["kind"] == "dispatch" and r["outcome"] == "success"),
            "operator_actions": sum(1 for r in rows if r["kind"] == "operator_command"),
            "failures": len(bad), "warnings": len(warn), "audit_entries": len(rows),
            "gateway_starts": sum(1 for r in rows if r["kind"] == "gateway" and r["outcome"] == "started"),
            "undelivered": len(_undelivered()),
        },
        "events": events[:100], "groups": _group(bad + warn)[:50],
        "operator": [r for r in rows if r["kind"] == "operator_command"][:50],
    }


EXTRA_CSS = """
<style>
  .ico-battery-full { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square'%3E%3Crect x='2' y='7' width='16' height='10'/%3E%3Cpath d='M22 11v2M6 10v4M10 10v4M14 10v4'/%3E%3C/svg%3E"); }
  .ico-calendar { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square'%3E%3Crect x='3' y='5' width='18' height='16'/%3E%3Cpath d='M3 10h18M8 3v4M16 3v4'/%3E%3C/svg%3E"); }
  .ico-search { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='M20 20l-4-4'/%3E%3C/svg%3E"); }
  .ico-file { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square'%3E%3Cpath d='M6 2h9l5 5v15H6z'/%3E%3Cpath d='M14 2v6h6M9 13h8M9 17h8'/%3E%3C/svg%3E"); }
  .ico-download { --i: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='square'%3E%3Cpath d='M12 3v12M7 10l5 5 5-5M4 20h16'/%3E%3C/svg%3E"); }
  .check { display: grid; grid-template-columns: 10px 1fr; column-gap: 12px; row-gap: 2px; align-items: baseline; padding: 10px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
  .check:last-child { border-bottom: 0; }
  .check .pip { flex: none; width: 10px; height: 10px; background: var(--muted); }
  .check.ok .pip { background: var(--green); } .check.warn .pip { background: var(--amber); } .check.bad .pip { background: var(--red); }
  .check span.s { grid-column: 2; color: var(--muted); font-size: 12px; }
  .banner { padding: 14px 18px; font-weight: 700; border: 1px solid; margin-bottom: 20px; }
  .banner.ok { color: var(--green); background: var(--ok-bg); } .banner.warn { color: var(--amber); background: var(--warn-bg); }
  .banner.bad { color: var(--red-text); background: var(--bad-bg); }
  .bignum { font-size: 34px; font-weight: 800; letter-spacing: -0.03em; }
  .factrow td:first-child { width: 34%; font-weight: 700; } .factrow td.h { color: var(--faint); font-size: 12px; }
  input.search { width: 260px; max-width: 100%; } .linkbtn { text-decoration: none; display: inline-block; }
  .trail { background: var(--zebra); padding: 12px 16px; } .more { text-align: center; margin-top: 14px; }
  .dl { display: flex; gap: 14px; align-items: center; padding: 14px 0; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .dl:last-child { border-bottom: 0; } .dl div { flex: 1; min-width: 220px; } .dl .t { font-weight: 700; }
  .dl .d { color: var(--muted); font-size: 12px; margin-top: 3px; }
  .rep-h { display: none; }
  @media (max-width: 640px) {
    table, tbody, tr, td { display: block; width: 100%; }
    th { display: none; }
    tr { border: 1px solid var(--line); background: var(--panel); margin-bottom: 10px; }
    tr[hidden] { display: none; }
    td { display: flex; gap: 14px; justify-content: space-between; align-items: baseline; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; text-align: right; }
    td:last-child { border-bottom: 0; }
    td::before { content: attr(data-label); flex: none; max-width: 42%; text-align: left; color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; }
    td[colspan]::before, td:not([data-label])::before { content: none; }
    td[colspan], td:not([data-label]) { display: block; text-align: left; }
    tr:nth-child(even) td { background: transparent; }
    .factrow td { display: block; text-align: left; width: 100%; } .factrow td:first-child { width: 100%; }
  }
  @media print {
    html[data-theme="dark"], html[data-theme="light"], :root {
      --ink: #0B0B0B; --paper: #FFFFFF; --red-text: #B81C1C; --line: #BDBDB6; --muted: #4A4A46; --faint: #6B6B66; --panel: #FFFFFF;
      --green: #1F6B3F; --amber: #8A5F00; --dis: #A04400; --field: #FFFFFF; --ok-bg: #FFFFFF; --bad-bg: #FFFFFF; --warn-bg: #FFFFFF;
      --grid-line: transparent; --zebra: transparent; --btn-dark: #0B0B0B; --on-dark: #FFFFFF;
    }
    .topbar, .side, .noprint, .toolbar, #nav-badge { display: none !important; }
    .content { max-width: none; padding: 0; } .card, .tile { break-inside: avoid; } body { background: #fff; }
    .rep-h { display: block; border-bottom: 2px solid #0B0B0B; margin-bottom: 14px; padding-bottom: 8px; }
    .rep-h img { height: 28px; }
  }
</style>
"""

BATTERY_BODY = """
<h1 class="page-h">Live battery</h1>
<p class="page-sub">A health check of the battery and the gateway, with the numbers behind it. Refreshes every few seconds.</p>
<div id="banner"></div>
<div class="two">
  <div class="card"><h2><i class="ico ico-battery"></i>Health check</h2><div id="checks"><p class="empty">Loading…</p></div></div>
  <div class="card"><h2><i class="ico ico-zap"></i>Right now</h2><div id="now"><p class="empty">Loading…</p></div></div>
</div>
<div class="two">
  <div class="card"><h2><i class="ico ico-battery"></i>Charge level over time</h2><div id="chart-soc" class="chart"></div></div>
  <div class="card"><h2><i class="ico ico-activity"></i>Power over time</h2><div id="chart-power" class="chart"></div>
    <p class="stat-hint">Above zero the battery is charging; below zero it is discharging.</p></div>
</div>
<div class="card"><h2><i class="ico ico-list"></i>All the details</h2><div id="facts" class="tablewrap"></div></div>
"""

BATTERY_JS = """
<script>
function kwWords(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return n === 0 ? 'Idle (0 kW)' : (n > 0 ? 'Charging ' : 'Discharging ') + Math.abs(n).toFixed(Number.isInteger(n) ? 0 : 1) + ' kW';
}
async function loadBattery() {
  try {
    const r = await fetch('/api/battery');
    if (r.status === 401) { window.location.href = '/login'; return; }
    const d = await r.json();
    setBadge(d.attention);
    const bad = d.checks.filter(function (c) { return c.tone === 'bad'; }).length, warn = d.checks.filter(function (c) { return c.tone === 'warn'; }).length;
    document.getElementById('banner').innerHTML = bad ? '<div class="banner bad">' + bad + (bad === 1 ? ' check has' : ' checks have') + ' failed. See the list below.</div>' :
      warn ? '<div class="banner warn">' + warn + (warn === 1 ? ' check needs' : ' checks need') + ' a look.</div>' : '<div class="banner ok">Everything is normal.</div>';
    document.getElementById('checks').innerHTML = d.checks.map(function (c) {
      return '<div class="check ' + esc(c.tone) + '"><span class="pip"></span><b>' + esc(c.label) + '</b><span class="s">' + esc(c.detail) + '</span></div>';
    }).join('');
    const b = d.battery;
    if (b && b.available) {
      const a = b.authority || {}, soc = b.soc_pct;
      document.getElementById('now').innerHTML =
        '<div class="stat-label">Charge level</div><div class="bignum">' + (soc === null || soc === undefined ? '—' : soc.toFixed(1) + '%') + '</div>' +
        '<div class="soc-bar-outer" style="margin-bottom:18px"><div class="soc-bar-inner" style="width:' + Math.max(0, Math.min(100, soc || 0)) + '%"></div></div>' +
        '<div class="stat-label">What the battery is doing</div><div class="stat-value">' + esc(kwWords(b.verified_kw)) + '</div>' +
        '<p class="meta">Who is in control: ' + esc(d.control) + '</p>';
      const lim = b.limits_kw || {};
      const rows = [
        ['Power the gateway asked for', kwWords(a.desired_kw), 'The gateway\\'s decision, from the utility schedule or a safety rule.'],
        ['Power the battery confirmed', kwWords(b.verified_kw), 'Read back from the battery controller after the setting was sent.'],
        ['Power actually flowing', kwWords(b.measured_kw), 'Measured by the battery.'],
        ['Most it can charge / discharge', (lim.max_charge_kw !== undefined ? lim.max_charge_kw : '—') + ' kW / ' + (lim.max_discharge_kw !== undefined ? lim.max_discharge_kw : '—') + ' kW', 'Set limits the gateway never goes beyond.'],
        ['Reason for the current decision', a.reason || '—', ''],
        ['Decision lasts until', a.until ? dateTime(a.until) : 'No end time (until something changes)', ''],
        ['Battery state', (b.operating_state || '—') + ' / status ' + (b.operating_status || '—') + ' / control mode ' + (b.control_mode || '—'), 'As reported by the battery controller.'],
        ['Reliability state', b.reliability_state || '—', 'How far the gateway trusts its link to the utility.'],
        ['Last command came from', b.last_command_source || '—', ''],
        ['Control method', b.dispatch_kind || '—', ''],
        ['Last reading from the battery', b.device_age_sec === null ? 'Never' : b.device_age_sec + ' seconds ago', ''],
        ['Gateway last published', b.age_sec + ' seconds ago', ''],
        ['Clock difference to utility', b.clock_offset_sec === null || b.clock_offset_sec === undefined ? '—' : b.clock_offset_sec.toFixed(1) + ' s', '']
      ];
      document.getElementById('facts').innerHTML = '<table>' + rows.map(function (r) {
        return '<tr class="factrow"><td>' + esc(r[0]) + '</td><td>' + esc(r[1]) + '</td><td class="h">' + esc(r[2]) + '</td></tr>';
      }).join('') + '</table>';
    } else {
      document.getElementById('now').innerHTML = '<p class="empty">' + esc(b && b.reason ? b.reason : 'No data yet.') + '</p>';
      document.getElementById('facts').innerHTML = '';
    }
    lineChart(document.getElementById('chart-soc'), [{name: 'Charge level (%)', color: '--red', pts: d.history.map(function (h) { return [h[0], h[1]]; })}],
              {min: 0, max: 100, unit: '%', label: 'Charge level over time'});
    lineChart(document.getElementById('chart-power'), [
      {name: 'Asked for (kW)', color: '--ink', dash: true, pts: d.history.map(function (h) { return [h[0], h[2]]; })},
      {name: 'Actually flowing (kW)', color: '--red', pts: d.history.map(function (h) { return [h[0], h[3]]; })}],
      {symmetric: true, label: 'Power requested versus measured'});
  } catch (e) { /* next poll retries */ }
}
loadBattery(); setInterval(loadBattery, 3000);
</script>
"""

EVENTS_BODY = """
<h1 class="page-h">Utility events</h1>
<p class="page-sub">Every instruction the utility has sent. The Event ID (mRID) is the utility's own reference number for it. Use "Show history" to see everything that happened to one event.</p>
<div class="toolbar">
  <label for="range" style="margin:0">Received in</label>
  <select id="range" onchange="loadEvents()"><option value="24">the last 24 hours</option><option value="168">the last 7 days</option><option value="0" selected>all time</option></select>
  <label for="status" style="margin:0 0 0 12px">Status</label>
  <select id="status" onchange="loadEvents()">
    <option value="">any</option><option value="active">Running now</option><option value="scheduled">Waiting to start</option>
    <option value="completed">Finished</option><option value="superseded">Replaced by a newer event</option><option value="cancelled">Cancelled</option>
  </select>
  <span id="count"></span>
</div>
<div class="card"><div id="events" class="tablewrap"><p class="empty">Loading…</p></div></div>
"""

EVENTS_JS = """
<script>
let events = [];
async function loadEvents() {
  try {
    const r = await fetch('/api/events?hours=' + encodeURIComponent(document.getElementById('range').value) + '&status=' + encodeURIComponent(document.getElementById('status').value));
    if (r.status === 401) { window.location.href = '/login'; return; }
    const d = await r.json();
    setBadge(d.attention); events = d.events;
    document.getElementById('count').textContent = d.events.length + (d.events.length === 1 ? ' event' : ' events');
    document.getElementById('events').innerHTML = d.events.length === 0 ? '<p class="empty">No events match.</p>' :
      '<table><tr><th>Event ID</th><th>What it asks for</th><th>Starts</th><th>Length</th><th>Status</th><th>Utility told</th><th></th></tr>' +
      d.events.map(function (e, i) {
        const note = e.replaced_by ? 'Newer event: ' + e.replaced_by : e.reason;
        return '<tr><td class="id" title="mRID">' + esc(e.mrid) + '</td><td>' + esc(e.what) + '</td><td>' + esc(dateTime(e.start)) + '</td><td>' + esc(Math.round(e.duration / 60 * 10) / 10) + ' min</td>' +
          '<td>' + esc(e.status) + (note ? '<div class="stat-hint">' + esc(note) + '</div>' : '') + '</td><td class="sev-' + esc(e.told_tone) + '">' + esc(e.told) + '</td>' +
          '<td><button class="btn grey" type="button" data-i="' + i + '">Show history</button></td></tr><tr id="trail-' + i + '" hidden><td colspan="7" class="trail"></td></tr>';
      }).join('') + '</table>';
  } catch (e) { /* retry on next change */ }
}
document.addEventListener('click', async function (ev) {
  const btn = ev.target.closest('button[data-i]'); if (!btn) return;
  const row = document.getElementById('trail-' + btn.dataset.i), cell = row.firstElementChild;
  if (!row.hidden) { row.hidden = true; btn.textContent = 'Show history'; return; }
  btn.textContent = 'Hide history'; row.hidden = false; cell.innerHTML = '<p class="empty">Loading…</p>';
  const r = await fetch('/api/event/' + encodeURIComponent(events[btn.dataset.i].mrid));
  const d = await r.json();
  cell.innerHTML = d.trail.length === 0 ? '<p class="empty">Nothing recorded for this event.</p>' : d.trail.map(activityRow).join('');
});
loadEvents(); setInterval(function () { if (!document.querySelector('tr[id^="trail-"]:not([hidden])')) loadEvents(); }, 8000);
</script>
"""

ACTIVITY_BODY = """
<h1 class="page-h">Activity log</h1>
<p class="page-sub">Everything the gateway has done and decided, newest first, in plain words. Every row can be expanded to see the exact technical record.</p>
<div class="toolbar">
  <input id="q" class="search" type="search" placeholder="Search, for example an Event ID" aria-label="Search the activity log" oninput="queue()">
  <select id="kind" onchange="reload()" aria-label="Type"><option value="">All types</option></select>
  <select id="level" onchange="reload()" aria-label="Level"><option value="all">Everything</option><option value="bad">Failures only</option><option value="warn">Warnings only</option><option value="ok">Normal only</option></select>
  <select id="range" onchange="reload()" aria-label="Period"><option value="1">last hour</option><option value="24" selected>last 24 hours</option><option value="168">last 7 days</option><option value="0">all time</option></select>
  <span id="count"></span>
</div>
<div class="card"><div id="log"><p class="empty">Loading…</p></div><div class="more"><button id="more" class="btn grey" type="button" hidden onclick="loadMore()">Load older entries</button></div></div>
"""

ACTIVITY_JS = """
<script>
let rows = [], timer = null, kindsFilled = false;
function queue() { clearTimeout(timer); timer = setTimeout(reload, 300); }
function url(before) {
  const p = new URLSearchParams({q: document.getElementById('q').value, kind: document.getElementById('kind').value, level: document.getElementById('level').value, hours: document.getElementById('range').value, limit: 50});
  if (before) p.set('before', before);
  return '/api/activity?' + p.toString();
}
function show(more) {
  document.getElementById('log').innerHTML = rows.length === 0 ? '<p class="empty">Nothing matches.</p>' : rows.map(activityRow).join('');
  document.getElementById('count').textContent = rows.length + ' shown';
  document.getElementById('more').hidden = !more;
}
async function reload() {
  try {
    const r = await fetch(url());
    if (r.status === 401) { window.location.href = '/login'; return; }
    const d = await r.json(); rows = d.rows; setBadge(d.attention);
    if (!kindsFilled) { kindsFilled = true; const s = document.getElementById('kind'); d.kinds.forEach(function (k) { const o = document.createElement('option'); o.value = k.value; o.textContent = k.label; s.appendChild(o); }); }
    show(d.more);
  } catch (e) { /* retry on next change */ }
}
async function loadMore() {
  const r = await fetch(url(rows[rows.length - 1].id)); const d = await r.json();
  rows = rows.concat(d.rows); show(d.more);
}
reload();
</script>
"""

REPORTS_BODY = """
<h1 class="page-h">Reports</h1>
<p class="page-sub">Take the records out of the dashboard: spreadsheets for analysis, or a printable summary you can save as a PDF. The summary uses Pacific Time (Vancouver); the CSV files use UTC, the time the gateway records.</p>
<div class="toolbar">
  <label for="range" style="margin:0">Period</label>
  <select id="range"><option value="24">last 24 hours</option><option value="168" selected>last 7 days</option><option value="720">last 30 days</option><option value="0">all time</option></select>
</div>
<div class="card">
  <div class="dl"><div><div class="t"><i class="ico ico-file"></i>Printable summary</div><div class="d">Key numbers, utility events, problems and operator actions on one page. Use Print, then choose Save as PDF.</div></div>
    <a id="l-summary" class="btn linkbtn" href="#"><i class="ico ico-file"></i>Open summary</a></div>
  <div class="dl"><div><div class="t"><i class="ico ico-list"></i>Activity log (CSV)</div><div class="d">Every recorded action and decision, in plain words and with the technical record, for Excel or Google Sheets.</div></div>
    <a id="l-activity" class="btn grey linkbtn" href="#"><i class="ico ico-download"></i>Download</a></div>
  <div class="dl"><div><div class="t"><i class="ico ico-zap"></i>Utility events (CSV)</div><div class="d">Each event with its Event ID, power, times, status and whether the utility was told.</div></div>
    <a id="l-events" class="btn grey linkbtn" href="#"><i class="ico ico-download"></i>Download</a></div>
  <div class="dl"><div><div class="t"><i class="ico ico-alert"></i>Failures and warnings (CSV)</div><div class="d">Only the things that went wrong, with what the gateway did about each.</div></div>
    <a id="l-failures" class="btn grey linkbtn" href="#"><i class="ico ico-download"></i>Download</a></div>
</div>
"""

REPORTS_JS = """
<script>
function updateLinks() {
  const h = document.getElementById('range').value;
  document.getElementById('l-summary').href = '/dashboard/reports/summary?hours=' + h;
  ['activity', 'events', 'failures'].forEach(function (k) { document.getElementById('l-' + k).href = '/reports/' + k + '.csv?hours=' + h; });
}
document.getElementById('range').addEventListener('change', updateLinks);
updateLinks();
fetch('/api/overview').then(function (r) { return r.json(); }).then(function (d) { setBadge(d.attention); }).catch(function () {});
</script>
"""

SUMMARY_BODY = """
<div class="rep-h"><img src="/static/invinity-logo.svg" alt="Invinity Energy Systems"></div>
<h1 class="page-h">Operations summary</h1>
<p class="page-sub" id="meta">Loading…</p>
<div class="noprint" style="margin-bottom:18px"><button class="btn" type="button" onclick="window.print()"><i class="ico ico-file"></i>Print or save as PDF</button></div>
<div id="tiles" class="tiles"></div>
<div class="card"><h2><i class="ico ico-zap"></i>Utility events</h2><div id="events" class="tablewrap"></div></div>
<div class="card"><h2><i class="ico ico-alert"></i>Problems</h2><div id="groups" class="tablewrap"></div></div>
<div class="card"><h2><i class="ico ico-list"></i>Operator actions</h2><div id="operator"></div></div>
"""

SUMMARY_JS = """
<script>
(async function () {
  const h = new URLSearchParams(location.search).get('hours') || '168';
  const r = await fetch('/api/report?hours=' + encodeURIComponent(h));
  if (r.status === 401) { window.location.href = '/login'; return; }
  const d = await r.json(), c = d.counts, who = document.querySelector('.who b');
  document.getElementById('meta').textContent = 'Period: ' + dateTime(d.from) + ' to ' + dateTime(d.to) + '. Prepared ' + dateTime(d.to) + (who ? ' by ' + who.textContent : '') + '.';
  const t = function (label, value, sub, tone) { return '<div class="tile ' + tone + '"><div class="l">' + esc(label) + '</div><div class="v">' + esc(value) + '</div><div class="s">' + esc(sub) + '</div></div>'; };
  document.getElementById('tiles').innerHTML =
    t('Utility events', c.events, Object.keys(c.event_statuses).map(function (k) { return c.event_statuses[k] + ' ' + k.toLowerCase(); }).join(', ') || 'none in this period', 'ok') +
    t('Power settings sent', c.power_settings, 'to the battery', 'ok') + t('Reports to utility', c.reports_sent, 'status and meter reports', 'ok') +
    t('Failures', c.failures, 'things that went wrong', c.failures ? 'bad' : 'ok') + t('Warnings', c.warnings, 'things that were degraded', c.warnings ? 'warn' : 'ok') +
    t('Operator actions', c.operator_actions, 'Force Idle on or off', 'ok') + t('Gateway starts', c.gateway_starts, 'times it was started', 'ok') +
    t('Undelivered replies', c.undelivered, 'still waiting for the utility', c.undelivered ? 'warn' : 'ok');
  document.getElementById('events').innerHTML = d.events.length === 0 ? '<p class="empty">No events in this period.</p>' :
    '<table><tr><th>Event ID</th><th>What it asked for</th><th>Starts</th><th>Length</th><th>Status</th><th>Utility told</th></tr>' + d.events.map(function (e) {
      return '<tr><td class="id">' + esc(e.mrid) + '</td><td>' + esc(e.what) + '</td><td>' + esc(dateTime(e.start)) + '</td><td>' + esc(Math.round(e.duration / 60 * 10) / 10) + ' min</td><td>' + esc(e.status) + '</td><td>' + esc(e.told) + '</td></tr>';
    }).join('') + '</table>';
  document.getElementById('groups').innerHTML = d.groups.length === 0 ? '<p class="empty">No problems recorded in this period.</p>' :
    '<table><tr><th>What happened</th><th>Times</th><th>Last seen</th><th>What the gateway did</th></tr>' + d.groups.map(function (g) {
      return '<tr><td class="sev-' + esc(g.sev) + '">' + esc(g.title) + '</td><td>' + g.count + '</td><td>' + esc(dateTime(g.last)) + '</td><td>' + esc(g.action || '—') + '</td></tr>';
    }).join('') + '</table>';
  document.getElementById('operator').innerHTML = d.operator.length === 0 ? '<p class="empty">No operator actions in this period.</p>' : d.operator.map(activityRow).join('');
  fetch('/api/overview').then(function (x) { return x.json(); }).then(function (o) { setBadge(o.attention); }).catch(function () {});
})();
</script>
"""

SCHEDULE_CSS = """
<style>
  .pt-clock { font-size: 11px; color: var(--muted); border: 1px solid var(--line); padding: 5px 10px; white-space: nowrap; }
  .chk { display: flex; gap: 8px; align-items: center; text-transform: none; letter-spacing: 0; font-weight: 400; font-size: 13px; color: var(--ink); margin: 10px 0 0; }
  .chk input { width: auto; }
  .cal-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .cal-head .title { flex: 1; text-align: center; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; font-size: 12px; }
  .cal-head .btn { padding: 6px 12px; }
  .cal { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 1px; background: var(--line); border: 1px solid var(--line); }
  .cal .dow { background: var(--btn-dark); color: var(--on-dark); font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; padding: 6px 4px; text-align: center; }
  .cal .day { background: var(--panel); min-height: 84px; padding: 4px 5px; cursor: pointer; font-size: 11px; overflow: hidden; }
  .cal .day.blank { background: var(--zebra); cursor: default; }
  .cal .day:hover:not(.blank) { background: var(--zebra); }
  .cal .day .n { font-weight: 700; color: var(--muted); }
  .cal .day.today .n { color: var(--red-text); }
  .cal .day.sel { outline: 2px solid var(--red); outline-offset: -2px; }
  .chip { font-size: 10px; padding: 1px 4px; margin-top: 3px; border-left: 3px solid var(--muted); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .chip.charge { border-color: var(--green); background: var(--ok-bg); }
  .chip.discharge { border-color: var(--dis); background: var(--dis-bg); }
  .chip.past { opacity: 0.6; } .chip.more { border-color: transparent; color: var(--faint); }
  .formgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
  .formgrid label { grid-column: auto; }
  .formgrid .wide { grid-column: 1 / -1; }
  input[type=date], input[type=time] { width: 100%; padding: 10px 12px; border: 1px solid var(--line); background: var(--field); font-family: var(--mono); font-size: 14px; color: var(--ink); }
  select.full { width: 100%; padding: 10px 12px; font-size: 14px; }
  .splitrow { display: flex; gap: 8px; } .splitrow > * { flex: 1; min-width: 0; }
  .dropzone { border: 1px dashed var(--muted); padding: 16px; margin: 10px 0; font-size: 13px; }
  code.inline { background: var(--zebra); padding: 1px 5px; font-family: var(--mono); font-size: 12px; }
  .schedgrid { grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); }
  .schedgrid .card { display: flex; flex-direction: column; } .schedgrid .card .grow { flex: 1; }
  .schedgrid .card > button.btn { align-self: flex-start; margin-top: auto; }
  @media (max-width: 1100px) { .schedgrid { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 900px) {
    .chip { font-size: 0; height: 6px; padding: 0; margin-top: 2px; } .chip.more { display: none; }
    .cal .day { min-height: 54px; padding: 3px; }
  }
</style>
"""

SCHEDULE_BODY = """
<h1 class="page-h">Schedule</h1>
<p class="page-sub">Plan battery events ahead of time and see them on the calendar. Every time on this page is Pacific Time (Vancouver) and follows daylight saving automatically.</p>
<div id="notice"></div>
<div class="two schedgrid">
  <div class="card">
    <h2><i class="ico ico-calendar"></i>Calendar</h2>
    <div class="cal-head">
      <button class="btn grey" type="button" id="prev" aria-label="Previous month">&lt;</button>
      <div class="title" id="month-title"></div>
      <button class="btn grey" type="button" id="next" aria-label="Next month">&gt;</button>
      <button class="btn grey" type="button" id="today">Today</button>
    </div>
    <div id="cal" class="cal"></div>
    <div class="legend"><span><i style="background:var(--green)"></i>Charge</span><span><i style="background:var(--dis)"></i>Discharge</span><span>Click a day to plan on it</span></div>
  </div>
  <div class="card">
    <h2><i class="ico ico-zap"></i>Add an event</h2>
    <div class="viewer-note">You are signed in with a view-only account. You can browse the calendar and the queue; only the operator can add or cancel events.</div>
    <div class="formgrid ops-only">
      <div><label for="f-dir">Direction</label>
        <select id="f-dir" class="full"><option value="1">Charge (power in)</option><option value="-1">Discharge (power out)</option></select></div>
      <div><label for="f-kw">Power (kW)</label><input id="f-kw" type="number" min="1" max="100000" step="1" placeholder="e.g. 25" inputmode="numeric" aria-describedby="f-kw-hint"><p id="f-kw-hint" class="stat-hint limits-hint" style="margin:2px 0 0">Type a whole number of kilowatts.</p></div>
      <div class="wide"><label class="chk"><input type="radio" name="when" value="at" checked> At a set time</label>
        <label class="chk"><input type="radio" name="when" value="now"> Right now</label></div>
      <div><label for="f-date">Date</label><input id="f-date" type="date"></div>
      <div><label for="f-time">Start time</label><input id="f-time" type="time" value="09:00"></div>
      <div class="wide"><label for="f-len">Length</label>
        <div class="splitrow"><input id="f-len" type="number" min="1" step="1" placeholder="e.g. 60">
          <select id="f-unit" class="full"><option value="60">minutes</option><option value="3600">hours</option></select></div></div>
      <div class="wide"><label class="chk"><input id="f-replace" type="checkbox"> Replace any event this one overlaps</label></div>
    </div>
    <p id="f-preview" class="stat-hint ops-only" style="min-height:2.6em"></p>
    <button class="btn ops-only" id="f-add" type="button"><i class="ico ico-calendar"></i>Add to schedule</button>
  </div>
</div>

<div class="card"><h2><i class="ico ico-list"></i><span id="day-title">Events on the selected day</span></h2><div id="day-list" class="tablewrap"></div></div>

<div class="card">
  <h2><i class="ico ico-zap"></i>Queue: running and upcoming</h2>
  <div id="queue" class="tablewrap"><p class="empty">Loading…</p></div>
  <div class="ops-only" style="margin-top:12px"><button class="btn grey" type="button" id="cancel-all">Cancel everything in the queue</button></div>
</div>

<div class="card ops-only">
  <h2><i class="ico ico-file"></i>Add many events from a file</h2>
  <p class="stat-hint" style="font-size:13px;color:var(--muted)">Prepare a spreadsheet saved as CSV with one row per event, then choose it here. Each row is checked before anything is added, and you see the result first.</p>
  <p class="stat-hint" style="font-size:12px">Columns: <code class="inline">date</code> (YYYY-MM-DD), <code class="inline">start_time</code> (24-hour, HH:MM, Pacific), <code class="inline">duration_minutes</code>, <code class="inline">direction</code> (charge or discharge), <code class="inline">power_kw</code>.
    <a href="#" id="tpl">Download a template</a></p>
  <div class="dropzone"><input id="imp-file" type="file" accept=".csv,text/csv" aria-label="Choose a schedule file"></div>
  <div id="imp-preview" class="tablewrap"></div>
  <div style="margin-top:12px"><button class="btn" id="imp-go" type="button" hidden>Add these events</button></div>
</div>
"""

SCHEDULE_JS = """
<script>
const $ = function (id) { return document.getElementById(id); };
const parts = function (ms, opts) { const o = {}; new Intl.DateTimeFormat('en-US', Object.assign({timeZone: TZ, hourCycle: 'h23'}, opts)).formatToParts(new Date(ms)).forEach(function (p) { o[p.type] = p.value; }); return o; };
function dayKey(ts) { const p = parts(ts * 1000, {year: 'numeric', month: '2-digit', day: '2-digit'}); return p.year + '-' + p.month + '-' + p.day; }
function offsetMs(ms) { const p = parts(ms, {year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric'}); return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(ms / 1000) * 1000; }
function pacificEpoch(dateStr, timeStr) {
  const d = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(dateStr), t = /^(\\d{1,2}):(\\d{2})$/.exec(timeStr);
  if (!d || !t || +t[1] > 23 || +t[2] > 59) return null;
  const guess = Date.UTC(+d[1], +d[2] - 1, +d[3], +t[1], +t[2]);
  let at = guess - offsetMs(guess); at = guess - offsetMs(at);
  const back = parts(at, {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
  const same = back.year + '-' + back.month + '-' + back.day === dateStr && +back.hour === +t[1] && +back.minute === +t[2];
  return same ? Math.round(at / 1000) : null;
}
function fullTime(ts) { return new Intl.DateTimeFormat('en-US', {timeZone: TZ, weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'}).format(new Date(ts * 1000)); }
function dayName(key) { const a = key.split('-'); return new Intl.DateTimeFormat('en-US', {timeZone: 'UTC', weekday: 'long', month: 'long', day: 'numeric', year: 'numeric'}).format(new Date(Date.UTC(+a[0], +a[1] - 1, +a[2]))); }
function shortTime(ts) { return new Intl.DateTimeFormat('en-US', {timeZone: TZ, hour: 'numeric', minute: '2-digit'}).format(new Date(ts * 1000)); }

let events = [], queue = [], acks = {}, today = dayKey(Date.now() / 1000), selected = today;
let view = {y: +today.slice(0, 4), m: +today.slice(5, 7) - 1};

function say(kind, html) { $('notice').innerHTML = '<div class="notice ' + kind + '" role="status">' + html + '</div>'; window.scrollTo(0, 0); }
function post(url, body) { return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(function (r) { return r.json().then(function (j) { return {status: r.status, body: j}; }); }); }

async function loadAll() {
  try {
    const rs = await Promise.all([fetch('/api/status'), fetch('/api/events?hours=0')]);
    if (rs[0].status === 401) { window.location.href = '/login'; return; }
    const st = await rs[0].json(), ev = await rs[1].json();
    queue = st.active_events; acks = st.acknowledgements;
    const map = {};
    ev.events.forEach(function (e) { map[e.mrid] = {mrid: e.mrid, start: e.start, end: e.end, kw: e.kw, status: e.status}; });
    queue.forEach(function (e) { if (!map[e.mrid]) map[e.mrid] = {mrid: e.mrid, start: e.start, end: e.end, kw: e.power_kw, status: e.running ? 'Running now' : 'Waiting to start'}; });
    events = Object.keys(map).map(function (k) { return map[k]; }).sort(function (a, b) { return a.start - b.start; });
    setBadge(ev.attention); draw();
  } catch (e) { /* next poll retries */ }
}
function draw() {
  $('queue').innerHTML = queueTable(queue, acks);
  drawCalendar(); drawDay();
}
function chipFor(e) {
  const kind = e.kw >= 0 ? 'charge' : 'discharge', past = e.end * 1000 < Date.now();
  return '<div class="chip ' + kind + (past ? ' past' : '') + '" title="' + esc((e.kw >= 0 ? 'Charge ' : 'Discharge ') + Math.abs(Math.round(e.kw)) + ' kW, ' + e.mrid + ': ' + e.status) + '">' + esc(shortTime(e.start).replace(' AM', 'a').replace(' PM', 'p')) + '</div>';
}
function drawCalendar() {
  const first = new Date(Date.UTC(view.y, view.m, 1)), lead = first.getUTCDay(), days = new Date(Date.UTC(view.y, view.m + 1, 0)).getUTCDate();
  $('month-title').textContent = new Intl.DateTimeFormat('en-US', {timeZone: 'UTC', month: 'long', year: 'numeric'}).format(first);
  const byDay = {}; events.forEach(function (e) { (byDay[dayKey(e.start)] = byDay[dayKey(e.start)] || []).push(e); });
  let h = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map(function (d) { return '<div class="dow">' + d + '</div>'; }).join('');
  for (let i = 0; i < lead; i++) h += '<div class="day blank"></div>';
  for (let d = 1; d <= days; d++) {
    const key = view.y + '-' + String(view.m + 1).padStart(2, '0') + '-' + String(d).padStart(2, '0'), list = byDay[key] || [];
    h += '<div class="day' + (key === today ? ' today' : '') + (key === selected ? ' sel' : '') + '" data-day="' + key + '"><div class="n">' + d + '</div>' +
      list.slice(0, 3).map(chipFor).join('') + (list.length > 3 ? '<div class="chip more" title="' + (list.length - 3) + ' more events on this day. Click the day to see them.">+' + (list.length - 3) + '</div>' : '') + '</div>';
  }
  $('cal').innerHTML = h;
}
function drawDay() {
  $('day-title').textContent = 'Events on ' + dayName(selected);
  const list = events.filter(function (e) { return dayKey(e.start) === selected; });
  $('day-list').innerHTML = list.length === 0 ? '<p class="empty">Nothing planned for this day.</p>' :
    '<table><tr><th>Time (Pacific)</th><th>What</th><th>Status</th><th>Event ID</th></tr>' + list.map(function (e) {
      return '<tr><td>' + esc(shortTime(e.start)) + ' to ' + esc(shortTime(e.end)) + '</td><td>' + (e.kw >= 0 ? 'Charge ' : 'Discharge ') + esc(Math.abs(Math.round(e.kw))) + ' kW</td><td>' + esc(e.status) + '</td><td class="id">' + esc(e.mrid) + '</td></tr>';
    }).join('') + '</table>';
}
function pick(key) { selected = key; $('f-date').value = key; drawCalendar(); drawDay(); preview(); }
$('cal').addEventListener('click', function (ev) { const d = ev.target.closest('.day[data-day]'); if (d) pick(d.dataset.day); });
$('prev').onclick = function () { view.m--; if (view.m < 0) { view.m = 11; view.y--; } drawCalendar(); };
$('next').onclick = function () { view.m++; if (view.m > 11) { view.m = 0; view.y++; } drawCalendar(); };
$('today').onclick = function () { today = dayKey(Date.now() / 1000); view = {y: +today.slice(0, 4), m: +today.slice(5, 7) - 1}; pick(today); };

function formRow() {
  const kw = Math.abs(Number($('f-kw').value)) * Number($('f-dir').value), len = Number($('f-len').value) * Number($('f-unit').value);
  const now = document.querySelector('input[name=when]:checked').value === 'now';
  if (!Number.isFinite(kw) || kw === 0) return {error: 'Enter a power above 0 kW.'};
  if (!Number.isFinite(len) || len < 60) return {error: 'Enter a length of at least 1 minute.'};
  if (now) return {row: {power_kw: kw, start: null, duration_sec: Math.round(len)}, start: Math.floor(Date.now() / 1000), len: Math.round(len), kw: kw};
  const start = pacificEpoch($('f-date').value, $('f-time').value);
  if (start === null) return {error: 'Choose a valid date and time. (Times that do not exist when the clocks change are not allowed.)'};
  return {row: {power_kw: kw, start: start, duration_sec: Math.round(len)}, start: start, len: Math.round(len), kw: kw};
}
function preview() {
  const f = formRow();
  if (f.error && !$('f-kw').value && !$('f-len').value) { $('f-preview').textContent = 'Fill in the power and the length, and this line will say exactly what will be added.'; return; }
  $('f-preview').textContent = f.error ? f.error : (f.kw > 0 ? 'Charge ' : 'Discharge ') + Math.abs(f.kw) + ' kW from ' + fullTime(f.start) + ', for ' + (f.len >= 3600 && f.len % 3600 === 0 ? f.len / 3600 + ' h' : Math.round(f.len / 60) + ' min') + ', ending at ' + shortTime(f.start + f.len) + '.';
}
['f-dir', 'f-kw', 'f-date', 'f-time', 'f-len', 'f-unit'].forEach(function (id) { $(id).addEventListener('input', preview); });
document.querySelectorAll('input[name=when]').forEach(function (r) { r.addEventListener('change', function () { const now = r.value === 'now' && r.checked; $('f-date').disabled = now; $('f-time').disabled = now; preview(); }); });
$('f-add').onclick = async function () {
  const f = formRow(); if (f.error) { say('warn', esc(f.error)); return; }
  const r = await post('/api/schedule', {events: [f.row], replace: $('f-replace').checked});
  if (!r.body.ok) { say('warn', 'Not added. ' + r.body.errors.map(function (e) { return fmtT(esc(e.message)); }).join(' ')); return; }
  const c = r.body.created[0];
  say('ok', 'Added event ' + esc(c.mrid) + ': ' + (c.power_kw > 0 ? 'charge ' : 'discharge ') + esc(Math.abs(c.power_kw)) + ' kW, ' + esc(fullTime(c.start)) + '.' + (r.body.removed.length ? ' It replaced ' + r.body.removed.length + ' overlapping event(s).' : '') + ' The gateway picks it up within about 5 seconds.');
  selected = dayKey(c.start); view = {y: +selected.slice(0, 4), m: +selected.slice(5, 7) - 1}; loadAll();
};
$('cancel-all').onclick = async function () {
  if (!queue.length) { say('warn', 'The queue is already empty.'); return; }
  if (!confirm('Cancel all ' + queue.length + ' events in the queue?')) return;
  await post('/api/schedule/cancel', {all: true}); say('ok', 'The queue was cleared.'); loadAll();
};
window.afterChange = loadAll;

function parseCsv(text) {
  const rows = []; let row = [], cell = '', q = false;
  text = text.replace(/^\\uFEFF/, '');
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += c; }
    else if (c === '"') q = true;
    else if (c === ',') { row.push(cell); cell = ''; }
    else if (c === '\\n' || c === '\\r') { if (c === '\\r' && text[i + 1] === '\\n') i++; row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += c;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows.filter(function (r) { return r.some(function (x) { return x.trim() !== ''; }); });
}
let importRows = [];
async function readFile(file) {
  $('imp-go').hidden = true; importRows = [];
  if (!file) { $('imp-preview').innerHTML = ''; return; }
  if (file.size > 200000) { $('imp-preview').innerHTML = '<div class="notice warn">That file is too large. Keep it under 200 KB.</div>'; return; }
  const table = parseCsv(await file.text());
  if (table.length < 2) { $('imp-preview').innerHTML = '<div class="notice warn">The file needs a header row and at least one event.</div>'; return; }
  const want = ['date', 'start_time', 'duration_minutes', 'direction', 'power_kw'];
  const head = table[0].map(function (h) { return h.trim().toLowerCase().replace(/[ -]+/g, '_'); });
  const missing = want.filter(function (w) { return head.indexOf(w) < 0; });
  if (missing.length) { $('imp-preview').innerHTML = '<div class="notice warn">The header row is missing: ' + esc(missing.join(', ')) + '.</div>'; return; }
  const col = function (r, n) { return (r[head.indexOf(n)] || '').trim(); };
  const body = table.slice(1, 201);
  importRows = body.map(function (r, i) {
    const o = {n: i + 1, date: col(r, 'date'), time: col(r, 'start_time'), mins: Number(col(r, 'duration_minutes')), dir: col(r, 'direction').toLowerCase(), kw: Number(col(r, 'power_kw')), error: ''};
    if (!/^\\d{4}-\\d{2}-\\d{2}$/.test(o.date)) o.error = 'Date must look like 2026-10-09.';
    else if (!/^\\d{1,2}:\\d{2}$/.test(o.time)) o.error = 'Start time must look like 14:00.';
    else if (!Number.isFinite(o.mins) || o.mins < 1) o.error = 'Duration must be a number of minutes, 1 or more.';
    else if (o.dir !== 'charge' && o.dir !== 'discharge') o.error = 'Direction must be charge or discharge.';
    else if (!Number.isFinite(o.kw) || o.kw <= 0) o.error = 'Power must be a number above 0.';
    else { o.start = pacificEpoch(o.date, o.time); if (o.start === null) o.error = 'That date and time is not valid in Pacific Time.'; }
    return o;
  });
  const good = importRows.filter(function (o) { return !o.error; });
  if (good.length) {
    const r = await post('/api/schedule', {dry_run: true, replace: false, events: good.map(function (o) { return {power_kw: (o.dir === 'charge' ? 1 : -1) * o.kw, start: o.start, duration_sec: Math.round(o.mins * 60)}; })});
    if (!r.body.ok) r.body.errors.forEach(function (e) { const o = good[e.row - 1]; if (o && !o.error) o.error = e.message; });
  }
  const bad = importRows.filter(function (o) { return o.error; }).length;
  $('imp-preview').innerHTML = '<table><tr><th>#</th><th>Date</th><th>Start (Pacific)</th><th>Minutes</th><th>What</th><th>Check</th></tr>' + importRows.map(function (o) {
    return '<tr><td>' + o.n + '</td><td>' + esc(o.date) + '</td><td>' + esc(o.time) + '</td><td>' + esc(o.mins) + '</td><td>' + esc(o.dir) + ' ' + esc(o.kw) + ' kW</td><td class="' + (o.error ? 'sev-bad' : 'sev-ok') + '">' + (o.error ? fmtT(esc(o.error)) : 'OK') + '</td></tr>';
  }).join('') + '</table>' + (bad ? '<div class="notice warn" style="margin-top:12px">' + bad + ' row(s) need fixing. Correct the file and choose it again. Nothing has been added.</div>' : '');
  $('imp-go').hidden = bad > 0; $('imp-go').textContent = 'Add ' + importRows.length + ' events';
}
$('imp-file').addEventListener('change', function () { readFile($('imp-file').files[0]); });
$('imp-go').onclick = async function () {
  const rows = importRows.map(function (o) { return {power_kw: (o.dir === 'charge' ? 1 : -1) * o.kw, start: o.start, duration_sec: Math.round(o.mins * 60)}; });
  const r = await post('/api/schedule', {events: rows, replace: false});
  if (!r.body.ok) { say('warn', 'Not added. ' + r.body.errors.map(function (e) { return fmtT(esc(e.message)); }).join(' ')); return; }
  say('ok', 'Added ' + r.body.created.length + ' events from the file.'); $('imp-file').value = ''; $('imp-preview').innerHTML = ''; $('imp-go').hidden = true; loadAll();
};
$('tpl').addEventListener('click', function (ev) {
  ev.preventDefault();
  const t = new Date(Date.now() + 86400000), d = dayKey(t.getTime() / 1000);
  const blob = new Blob(['date,start_time,duration_minutes,direction,power_kw\\r\\n' + d + ',14:00,60,charge,25\\r\\n' + d + ',16:30,45,discharge,20\\r\\n'], {type: 'text/csv'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'schedule-template.csv'; document.body.appendChild(a); a.click(); a.remove();
});

$('f-date').value = today; $('f-date').min = today; preview(); loadAll(); setInterval(loadAll, 5000);
</script>
"""

SHELL = """
<!doctype html><html><head><title>{{ title }} — Grid Operator Portal</title><meta name="viewport" content="width=device-width, initial-scale=1">{{ style|safe }}""" + COMMON_CSS + EXTRA_CSS + SCHEDULE_CSS + """</head>
<body data-role="{{ role }}">
  <div class="topbar">
    <div class="brand">
      <img class="logo-light swap" src="{{ url_for('static', filename='invinity-logo.svg') }}" alt="Invinity Energy Systems">
      <img class="logo-dark" src="{{ url_for('static', filename='invinity-logo-dark.svg') }}" alt="" aria-hidden="true">
      <span class="title">// Grid Operator Portal</span>
    </div>
    <div class="who">
      <span class="pt-clock" id="pt-clock" title="Every time in this portal is Pacific Time (Vancouver)"></span>
      {% if secure %}<span class="tls-pill secure topbar-pill"><i class="ico ico-lock"></i>TLS</span>{% else %}<span class="tls-pill insecure topbar-pill"><i class="ico ico-unlock"></i>Unencrypted</span>{% endif %}
      <button type="button" class="btn grey theme-toggle" onclick="toggleTheme()" aria-label="Switch between light and dark mode" title="Switch light / dark"><i class="ico ico-moon"></i><i class="ico ico-sun"></i></button>
      Signed in as <b>{{ username }}</b>{% if role == 'viewer' %}<span class="role-tag" title="You can look at everything; only the operator can send commands.">View only</span>{% endif %}
      <form method="post" action="/logout"><button class="btn grey" type="submit"><i class="ico ico-logout"></i>Log Out</button></form>
    </div>
  </div>
  <div id="idle-banner" class="idle-banner" role="status" hidden>
    <span><i class="ico ico-alert"></i><b>Force Idle is on.</b> The battery is held at 0 kW and utility events are being ignored.<span id="idle-who"></span></span>
    <form class="ops-only" method="post" action="/dashboard/send" onsubmit="this.next.value = location.pathname">
      <input type="hidden" name="action" value="release"><input type="hidden" name="next" value="">
      <button class="btn secondary" type="submit">Release Force Idle</button>
    </form>
  </div>
  <div class="shell">
    <nav class="side" aria-label="Sections">
      <div class="side-h">// Sections</div>
      <a class="nav {{ 'on' if active == 'overview' }}" href="{{ url_for('dashboard') }}"><i class="ico ico-grid"></i>Overview</a>
      <a class="nav {{ 'on' if active == 'schedule' }}" href="{{ url_for('schedule_page') }}"><i class="ico ico-calendar"></i>Schedule</a>
      <a class="nav {{ 'on' if active == 'battery' }}" href="{{ url_for('battery_page') }}"><i class="ico ico-battery-full"></i>Live battery</a>
      <a class="nav {{ 'on' if active == 'events' }}" href="{{ url_for('events_page') }}"><i class="ico ico-list"></i>Utility events</a>
      <a class="nav {{ 'on' if active == 'activity' }}" href="{{ url_for('activity_page') }}"><i class="ico ico-search"></i>Activity log</a>
      <a class="nav {{ 'on' if active == 'alerts' }}" href="{{ url_for('alerts_page') }}"><i class="ico ico-alert"></i>Alerts &amp; failures<span id="nav-badge" class="badge" hidden></span></a>
      <a class="nav {{ 'on' if active == 'reports' }}" href="{{ url_for('reports_page') }}"><i class="ico ico-file"></i>Reports</a>
    </nav>
    <main class="content">{% if flash %}<div class="notice {{ flash[0] }}" role="status">{{ flash[1] }}</div>{% endif %}{{ body|safe }}</main>
  </div>
""" + COMMON_JS + """{{ script|safe }}
</body></html>
"""

OVERVIEW_BODY = """
<h1 class="page-h">Overview</h1>
<p class="page-sub">What the battery is doing right now, in plain words. This page refreshes by itself.</p>
<div id="tiles" class="tiles"></div>

<div class="two">
  <div class="card"><h2><i class="ico ico-battery"></i>Charge level over time</h2><div id="chart-soc" class="chart"></div></div>
  <div class="card"><h2><i class="ico ico-activity"></i>Power: asked for vs actually flowing</h2><div id="chart-power" class="chart"></div>
    <p class="stat-hint">Above zero the battery is charging; below zero it is discharging.</p></div>
</div>

<div class="card">
  <h2><i class="ico ico-battery"></i>Battery status</h2>
  <div id="battery-status"><p class="empty">Loading…</p></div>
</div>

<div class="grid">
  <div class="card">
    <h2><i class="ico ico-zap"></i>Run now</h2>
    <div class="viewer-note">You are signed in with a view-only account. You can look at everything on this dashboard; only the operator can send commands.</div>
    <div class="ops-only">
    <form method="post" action="/dashboard/send" onsubmit="this.next.value = location.pathname">
      <input type="hidden" name="next" value="">
      <p class="stat-hint" style="margin:0 0 4px">Starts straight away. To start at a set time, or to line up several events, use <a href="/dashboard/schedule">Schedule</a>.</p>
      <label for="rn-power">Power (kW)</label>
      <input id="rn-power" type="number" name="power_kw" min="1" max="100000" step="1" placeholder="e.g. 25" inputmode="numeric" aria-describedby="rn-power-hint" required>
      <p id="rn-power-hint" class="stat-hint limits-hint" style="margin:2px 0 0">Type a whole number of kilowatts.</p>
      <label for="rn-duration">How long (seconds)</label>
      <input id="rn-duration" type="number" name="duration_sec" min="60" max="604800" step="1" placeholder="e.g. 120" inputmode="numeric" aria-describedby="rn-duration-hint" required>
      <p id="rn-duration-hint" class="stat-hint" style="margin:2px 0 0">At least 60. For example 120 is 2 minutes and 3600 is 1 hour.</p>
      <label class="chk"><input type="checkbox" name="replace" value="1"> Replace anything it overlaps</label>
      <div class="action-row">
        <button class="btn" type="submit" name="action" value="charge"><i class="ico ico-zap"></i>Charge</button>
        <button class="btn secondary" type="submit" name="action" value="discharge">Discharge</button>
      </div>
    </form>
    <form method="post" action="/dashboard/send" style="margin-top:10px">
      <button class="btn grey" type="submit" name="action" value="idle" style="width:100%">Clear queue and Force Idle</button>
    </form>
    <form method="post" action="/dashboard/send" style="margin-top:10px">
      <button class="btn secondary" type="submit" name="action" value="release" style="width:100%">Release Force Idle</button>
    </form>
    </div>
  </div>
  <div class="card">
    <h2><i class="ico ico-list"></i>Queue: running and upcoming</h2>
    <div id="events-table" class="tablewrap"><p class="empty">Loading…</p></div>
  </div>
</div>

<div class="card">
  <h2><i class="ico ico-list"></i>Recent utility events</h2>
  <div id="recent-events" class="tablewrap"><p class="empty">Loading…</p></div>
</div>

<div class="card">
  <h2><i class="ico ico-activity"></i>Latest activity</h2>
  <div id="activity"><p class="empty">Loading…</p></div>
</div>
"""

OVERVIEW_JS = """
<script>
async function loadOverview() {
  try {
    const r = await fetch('/api/overview');
    if (r.status === 401) { window.location.href = '/login'; return; }
    const d = await r.json();
    document.getElementById('tiles').innerHTML = d.tiles.map(function (t) {
      return '<div class="tile ' + esc(t.tone) + '"><div class="l">' + esc(t.label) + '</div><div class="v">' + esc(t.value) + '</div><div class="s">' + esc(t.sub) + '</div></div>';
    }).join('');
    setBadge(d.attention);
    lineChart(document.getElementById('chart-soc'), [{name: 'Charge level (%)', color: '--red', pts: d.history.map(function (h) { return [h[0], h[1]]; })}],
              {min: 0, max: 100, unit: '%', label: 'Charge level over time'});
    lineChart(document.getElementById('chart-power'), [
      {name: 'Asked for (kW)', color: '--ink', dash: true, pts: d.history.map(function (h) { return [h[0], h[2]]; })},
      {name: 'Actually flowing (kW)', color: '--red', pts: d.history.map(function (h) { return [h[0], h[3]]; })}],
      {symmetric: true, label: 'Power requested versus measured'});
    const ev = document.getElementById('recent-events');
    ev.innerHTML = d.recent_events.length === 0 ? '<p class="empty">No events received from the utility yet.</p>' :
      '<table><tr><th>Event ID</th><th>What it asks for</th><th>Window</th><th>Status</th><th>Utility told</th></tr>' +
      d.recent_events.map(function (e) {
        return '<tr><td class="id" title="mRID, the utility\\'s reference for this event">' + esc(e.mrid) + '</td><td>' + esc(e.what) + '</td><td>' + esc(dateTime(e.start)) + ' to ' + esc(clock(e.end)) +
               '</td><td>' + esc(e.status) + '</td><td class="sev-' + esc(e.told_tone) + '">' + esc(e.told) + '</td></tr>';
      }).join('') + '</table>';
    document.getElementById('activity').innerHTML = d.activity.length === 0 ? '<p class="empty">Nothing recorded yet.</p>' : d.activity.map(activityRow).join('');
  } catch (e) { /* next poll retries */ }
}
function kwText(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  const size = Number.isInteger(n) ? Math.abs(n).toFixed(0) : String(Math.round(Math.abs(n) * 1000) / 1000);
  return size + ' kW ' + (n > 0 ? 'charge' : (n < 0 ? 'discharge' : 'idle'));
}
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    if (r.status === 401) { window.location.href = '/login'; return; }
    const data = await r.json();
    const batteryDiv = document.getElementById('battery-status');
    const b = data.battery;
    if (!b || !b.available) {
      batteryDiv.innerHTML = '<p class="empty">' + esc(b && b.reason ? b.reason : 'No data yet.') + '</p>';
    } else {
      const a = b.authority;
      const verified = (b.verified_kw === null || b.verified_kw === undefined) ? null : b.verified_kw;
      const modeClass = verified === null ? 'unconfirmed' : (verified > 0 ? 'charge' : (verified < 0 ? 'discharge' : 'idle'));
      const modeText = verified === null ? 'UNCONFIRMED' : (verified > 0 ? 'CHARGE' : (verified < 0 ? 'DISCHARGE' : 'IDLE'));
      const relClass = (b.reliability_state || '').toLowerCase();
      const socKnown = b.soc_pct !== null && b.soc_pct !== undefined;
      const socPct = socKnown ? Math.max(0, Math.min(100, b.soc_pct)) : 0;
      const statusBad = b.operating_status === 'Fault' || b.operating_status === 'Critical Fault';
      const staleTag = b.device_stale ? ' <span class="stale-tag">stale</span>' : '';
      let notes = '';
      if (a && !a.confirmed) {
        notes += '<p class="warn">The BMS has not confirmed the requested setpoint.</p>';
      } else if (a && verified !== null && verified !== a.desired_kw) {
        notes += '<p class="note">The BMS holds ' + kwText(verified) + ' of the requested ' + kwText(a.desired_kw) + ' (power limits / whole-kW register).</p>';
      }
      if (b.device_stale) {
        notes += '<p class="warn">BMS data is stale: ' + (b.device_age_sec === null ? 'the BMS has not been read yet' : 'last read ' + b.device_age_sec + 's ago') + '.</p>';
      }
      if (b.bms_heartbeat_stale) {
        notes += '<p class="warn">The BMS heartbeat (31003) is not changing.</p>';
      }
      batteryDiv.innerHTML =
        '<div class="battery-grid">' +
          '<div title="What the gateway decided the battery should do"><div class="stat-label">Asked for</div><div class="stat-value">' + (a ? kwText(a.desired_kw) : '—') + '</div></div>' +
          '<div title="What the battery controller (BMS) confirmed it is holding"><div class="stat-label">Battery confirms</div><div class="stat-value">' + kwText(verified) + '</div>' +
            '<span class="mode-badge ' + modeClass + '">' + modeText + '</span></div>' +
          '<div title="Power actually measured flowing through the battery"><div class="stat-label">Actually flowing' + staleTag + '</div><div class="stat-value">' + kwText(b.measured_kw) + '</div></div>' +
          '<div><div class="stat-label">Charge level' + staleTag + '</div><div class="stat-value">' + (socKnown ? b.soc_pct.toFixed(1) + '%' : '—') + '</div>' +
            '<div class="soc-bar-outer"><div class="soc-bar-inner" style="width:' + socPct + '%"></div></div></div>' +
          '<div><div class="stat-label">Reliability</div><span class="rel-badge ' + esc(relClass) + '">' + esc(b.reliability_state || 'UNKNOWN') + '</span></div>' +
        '</div>' + notes +
        '<p class="meta">Who is in control: ' + esc(a ? a.authority + ' (' + a.reason + ')' : 'unknown') + '</p>' +
        '<p class="meta">BMS ' + esc(b.operating_state || 'state unknown') + ' · status <span class="' + (statusBad ? 'bad' : '') + '">' +
          esc(b.operating_status || 'unknown') + '</span> · control ' + esc(b.control_mode || 'unknown') +
          ' · BMS read ' + (b.device_age_sec === null ? 'never' : b.device_age_sec + 's ago') + ' · published ' + b.age_sec + 's ago</p>';
    }
    document.getElementById('events-table').innerHTML = queueTable(data.active_events, data.acknowledgements);
  } catch (e) { /* next poll retries */ }
}
window.afterChange = loadStatus;
loadOverview(); loadStatus();
setInterval(loadOverview, 4000); setInterval(loadStatus, 3000);
</script>
"""

ALERTS_BODY = """
<h1 class="page-h">Alerts &amp; failures</h1>
<p class="page-sub">Anything that has gone wrong or needs a person to look at it, with what it means and what the gateway did about it.</p>
<div id="tiles" class="tiles"></div>

<div class="card">
  <h2><i class="ico ico-alert"></i>Happening now</h2>
  <div id="live"><p class="empty">Loading…</p></div>
</div>

<div class="toolbar">
  <label for="range" style="margin:0">Show problems from</label>
  <select id="range" onchange="loadAlerts()">
    <option value="24">the last 24 hours</option><option value="168">the last 7 days</option><option value="0">all time</option>
  </select>
  <label for="level" style="margin:0 0 0 12px">Type</label>
  <select id="level" onchange="loadAlerts()">
    <option value="all">failures and warnings</option><option value="bad">failures only</option><option value="warn">warnings only</option>
  </select>
</div>

<div class="card">
  <h2><i class="ico ico-list"></i>What went wrong, grouped</h2>
  <div id="groups" class="tablewrap"><p class="empty">Loading…</p></div>
</div>

<div class="card">
  <h2><i class="ico ico-activity"></i>Latest problems, in order</h2>
  <div id="log"><p class="empty">Loading…</p></div>
</div>

<div class="card">
  <h2><i class="ico ico-list"></i>Replies to the utility that have not gone through</h2>
  <div id="undelivered" class="tablewrap"><p class="empty">Loading…</p></div>
</div>
"""

ALERTS_JS = """
<script>
async function loadAlerts() {
  try {
    const hours = document.getElementById('range').value, level = document.getElementById('level').value;
    const r = await fetch('/api/alerts?hours=' + encodeURIComponent(hours) + '&level=' + encodeURIComponent(level));
    if (r.status === 401) { window.location.href = '/login'; return; }
    const d = await r.json();
    setBadge(d.attention);
    document.getElementById('tiles').innerHTML = d.tiles.map(function (t) {
      return '<div class="tile ' + esc(t.tone) + '"><div class="l">' + esc(t.label) + '</div><div class="v">' + esc(t.value) + '</div><div class="s">' + esc(t.sub) + '</div></div>';
    }).join('');
    document.getElementById('live').innerHTML = d.live.length === 0 ? '<div class="allclear">All clear. Nothing needs attention right now.</div>' :
      d.live.map(function (a) {
        return '<div class="alert ' + esc(a.sev) + '"><div class="t">' + esc(a.title) + '</div><div class="d">' + esc(a.detail) + '</div><div class="todo"><b>What to do</b> ' + esc(a.todo) + '</div></div>';
      }).join('');
    document.getElementById('groups').innerHTML = d.groups.length === 0 ? '<p class="empty">No problems recorded for this period.</p>' :
      '<table><tr><th>What happened</th><th>Times</th><th>Last seen</th><th>What the gateway did</th></tr>' + d.groups.map(function (g) {
        return '<tr><td class="sev-' + esc(g.sev) + '">' + esc(g.title) + '</td><td>' + g.count + '</td><td>' + esc(ago(g.last)) + '</td><td>' + esc(g.action || '—') + '</td></tr>';
      }).join('') + '</table>';
    document.getElementById('log').innerHTML = d.log.length === 0 ? '<p class="empty">No problems recorded for this period.</p>' : d.log.map(activityRow).join('');
    document.getElementById('undelivered').innerHTML = d.undelivered.length === 0 ? '<p class="empty">Every reply has reached the utility.</p>' :
      '<table><tr><th>Event ID</th><th>Reply</th><th>Tries</th><th>State</th><th>Last error</th></tr>' + d.undelivered.map(function (u) {
        return '<tr><td class="id">' + esc(u.mrid) + '</td><td>' + esc(u.reply) + '</td><td>' + u.attempts + '</td><td class="' + (u.gave_up ? 'sev-bad' : 'sev-warn') + '">' +
               (u.gave_up ? 'Gave up' : 'Still trying') + '</td><td>' + esc(u.error || '—') + '</td></tr>';
      }).join('') + '</table>';
  } catch (e) { /* next poll retries */ }
}
loadAlerts(); setInterval(loadAlerts, 5000);
</script>
"""


def register(app, *, page_style: str, login_required, state_provider) -> None:
    """Adds /dashboard (Overview), /dashboard/alerts, /api/overview and /api/alerts to the app."""
    def render(title, active, body, script):
        return render_template_string(SHELL, title=title, active=active, body=body, script=script, style=page_style,
                                      username=session.get("username"), role=session.get("role", "operator"), secure=request.is_secure, flash=session.pop("flash", None))

    @login_required
    def dashboard():
        return render("Overview", "overview", OVERVIEW_BODY, OVERVIEW_JS)

    @login_required
    def alerts_page():
        return render("Alerts & failures", "alerts", ALERTS_BODY, ALERTS_JS)

    @login_required
    def schedule_page():
        return render("Schedule", "schedule", SCHEDULE_BODY, SCHEDULE_JS)

    @login_required
    def battery_page():
        return render("Live battery", "battery", BATTERY_BODY, BATTERY_JS)

    @login_required
    def events_page():
        return render("Utility events", "events", EVENTS_BODY, EVENTS_JS)

    @login_required
    def activity_page():
        return render("Activity log", "activity", ACTIVITY_BODY, ACTIVITY_JS)

    @login_required
    def reports_page():
        return render("Reports", "reports", REPORTS_BODY, REPORTS_JS)

    @login_required
    def summary_page():
        return render("Operations summary", "reports", SUMMARY_BODY, SUMMARY_JS)

    def _hours(default: int) -> int:
        try:
            return max(0, min(8760, int(request.args.get("hours", default))))
        except ValueError:
            return default

    def _unauth():
        return jsonify({"error": "not logged in"}), 401

    def api_battery():
        if not session.get("logged_in"):
            return _unauth()
        state = state_provider()
        b = state.get("battery") or {}
        return jsonify({"battery": b, "checks": _battery_checks(state), "history": [list(h) for h in HISTORY],
                        "control": _control_text(b.get("authority"))[0], "attention": _tiles(state, time.time())[1]})

    def api_events():
        if not session.get("logged_in"):
            return _unauth()
        now = time.time()
        hours = _hours(0)
        status = request.args.get("status") or None
        if status is not None and status not in EVENT_STATUS_WORDS:
            status = None
        return jsonify({"events": _events_list(now - hours * 3600 if hours else 0.0, status),
                        "attention": _tiles(state_provider(), now)[1]})

    def api_event(mrid):
        if not session.get("logged_in"):
            return _unauth()
        if not MRID_RE.match(mrid):
            return jsonify({"error": "bad event id"}), 400
        return jsonify({"trail": _event_trail(mrid)})

    def api_activity():
        if not session.get("logged_in"):
            return _unauth()
        now = time.time()
        hours = _hours(24)
        kind = request.args.get("kind") or None
        if kind is not None and kind not in KIND_NAMES:
            kind = None
        level = request.args.get("level", "all")
        try:
            limit = max(1, min(200, int(request.args.get("limit", 50))))
            before = int(request.args["before"]) if request.args.get("before") else None
        except ValueError:
            return jsonify({"error": "bad number"}), 400
        rows, more = _search_audit(request.args.get("q", "")[:100], kind, level, now - hours * 3600 if hours else 0.0, before, limit)
        kinds = [{"value": k, "label": v} for k, v in KIND_NAMES.items()]
        return jsonify({"rows": rows, "more": more, "kinds": kinds, "attention": _tiles(state_provider(), now)[1]})

    def api_report():
        if not session.get("logged_in"):
            return _unauth()
        return jsonify(_report(time.time(), _hours(168)))

    def csv_activity():
        if not session.get("logged_in"):
            return _unauth()
        now = time.time()
        hours = _hours(168)
        rows = _audit_rows(100000, now - hours * 3600 if hours else 0.0)
        return _csv_response("activity-log.csv",
                             ["time_utc", "what_happened", "type", "source", "outcome", "level", "technical_detail"],
                             [[_fmt_utc(r["ts"]), r["title"], KIND_NAMES.get(r["kind"], r["kind"]), r["source"], r["outcome"],
                               {"ok": "normal", "warn": "warning", "bad": "failure"}[r["sev"]], r["detail"]] for r in rows])

    def csv_events():
        if not session.get("logged_in"):
            return _unauth()
        now = time.time()
        hours = _hours(168)
        ev = _events_list(now - hours * 3600 if hours else 0.0, None, 100000)
        return _csv_response("utility-events.csv",
                             ["event_id", "what_it_asks_for", "power_kw", "starts_utc", "ends_utc", "length_seconds", "status", "status_reason",
                              "replaced_by", "priority", "received_utc", "replies_sent", "replies_expected"],
                             [[e["mrid"], e["what"], e["kw"], _fmt_utc(e["start"]), _fmt_utc(e["end"]), e["duration"], e["status"], e["reason"],
                               e["replaced_by"], e["priority"], _fmt_utc(e["received"]), e["sent"], e["total"]] for e in ev])

    def csv_failures():
        if not session.get("logged_in"):
            return _unauth()
        now = time.time()
        hours = _hours(168)
        rows = _audit_rows(100000, now - hours * 3600 if hours else 0.0, BAD_OUTCOMES | WARN_OUTCOMES)
        return _csv_response("failures-and-warnings.csv",
                             ["time_utc", "what_happened", "level", "what_the_gateway_did", "type", "source", "technical_detail"],
                             [[_fmt_utc(r["ts"]), r["title"], "failure" if r["sev"] == "bad" else "warning", r["action"],
                               KIND_NAMES.get(r["kind"], r["kind"]), r["source"], r["detail"]] for r in rows])

    def api_overview():
        if not session.get("logged_in"):
            return jsonify({"error": "not logged in"}), 401
        now = time.time()
        state = state_provider()
        tiles, attention = _tiles(state, now)
        return jsonify({
            "tiles": tiles, "attention": attention,
            "history": [list(h) for h in list(HISTORY)[-720:]],
            "recent_events": _recent_events(8),
            "activity": _recent_audit(10),
            "control": _control_text((state.get("battery") or {}).get("authority"))[0],
        })

    def api_alerts():
        if not session.get("logged_in"):
            return jsonify({"error": "not logged in"}), 401
        now = time.time()
        try:
            hours = max(0, int(request.args.get("hours", 24)))
        except ValueError:
            hours = 24
        level = request.args.get("level", "all")
        wanted = BAD_OUTCOMES | WARN_OUTCOMES if level == "all" else BAD_OUTCOMES if level == "bad" else WARN_OUTCOMES
        since = now - hours * 3600 if hours else 0.0
        rows = _recent_audit(2000, since, wanted)
        state = state_provider()
        tiles, attention = _tiles(state, now)
        day_rows = _recent_audit(2000, now - 86400, BAD_OUTCOMES | WARN_OUTCOMES)
        undelivered = _undelivered()
        tiles = [
            next(t for t in tiles if t["label"] == "Needs attention"),
            {"label": "Failures (24 h)", "value": str(sum(1 for r in day_rows if r["sev"] == "bad")), "sub": "things that went wrong",
             "tone": "ok" if not any(r["sev"] == "bad" for r in day_rows) else "bad"},
            {"label": "Warnings (24 h)", "value": str(sum(1 for r in day_rows if r["sev"] == "warn")), "sub": "things that were degraded",
             "tone": "ok" if not any(r["sev"] == "warn" for r in day_rows) else "warn"},
            {"label": "Undelivered replies", "value": str(len(undelivered)), "sub": "waiting to reach the utility",
             "tone": "ok" if not undelivered else "warn"},
        ]
        return jsonify({
            "attention": attention, "tiles": tiles,
            "live": [{"sev": s, "title": t, "detail": d, "todo": w} for s, t, d, w in live_alerts(state)],
            "groups": _group(rows),
            "log": rows[:50], "undelivered": undelivered,
        })

    app.add_url_rule("/dashboard", "dashboard", dashboard)
    app.add_url_rule("/dashboard/alerts", "alerts_page", alerts_page)
    app.add_url_rule("/dashboard/schedule", "schedule_page", schedule_page)
    app.add_url_rule("/dashboard/battery", "battery_page", battery_page)
    app.add_url_rule("/dashboard/events", "events_page", events_page)
    app.add_url_rule("/dashboard/activity", "activity_page", activity_page)
    app.add_url_rule("/dashboard/reports", "reports_page", reports_page)
    app.add_url_rule("/dashboard/reports/summary", "summary_page", summary_page)
    app.add_url_rule("/api/battery", "api_battery", api_battery)
    app.add_url_rule("/api/events", "api_events", api_events)
    app.add_url_rule("/api/event/<mrid>", "api_event", api_event)
    app.add_url_rule("/api/activity", "api_activity", api_activity)
    app.add_url_rule("/api/report", "api_report", api_report)
    app.add_url_rule("/reports/activity.csv", "csv_activity", csv_activity)
    app.add_url_rule("/reports/events.csv", "csv_events", csv_events)
    app.add_url_rule("/reports/failures.csv", "csv_failures", csv_failures)
    app.add_url_rule("/api/overview", "api_overview", api_overview)
    app.add_url_rule("/api/alerts", "api_alerts", api_alerts)
