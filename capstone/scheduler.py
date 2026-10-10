"""
core/scheduler.py
Calendar-based Schedule Manager and dispatch arbiter

Stores DERControl events in a local SQLite database (WAL mode — see
Requirement F-03: "store events ... in SQLite (WAL mode)") and, once a
second, decides which single source has authority over the battery
setpoint and makes the battery match that decision.

Why SQLite?
  - Survives a crash or reboot (schedule is not lost)
  - Simple, no server needed
  - Built into Python standard library

Why WAL mode specifically?
  - Allows concurrent reads while a write is in progress, which matters
    here since the scheduler polls every second while add_event() can
    be called at any time from the IEEE client's poll loop.

Dispatch is level-triggered: every tick recomputes the desired setpoint
from the stored schedule and writes it whenever the driver's last
confirmed command differs from it or is unknown (after a failed write, a
reconnect or a restart). A retry therefore always carries the CURRENT
decision — never an older event's value — and an update to a running
event is applied on the next tick.

Dispatch authority, highest precedence first:
  1. operator_force_idle  the operator's Force Idle override is engaged
                          (set_force_idle(); persisted, survives restarts)
  2. approved_schedule    the authoritative stored event (below)
  3. safe_idle            no valid approved schedule: 0 kW

Loss of the utility link does not change the decision (Sprint 2 Table 1):
the last approved valid schedule keeps running until it completes or
expires, then Safe Idle applies; when the link returns, new utility
events take effect through the same precedence rules. If the scheduler
itself fails (e.g. the schedule database is unavailable), step() keeps
the last approved event only until its known end and otherwise holds
the battery at 0 kW (failsafe_scheduler_error).

Authoritative event: among live events whose window contains `now`, the
one of the program with the lowest primacy, then the newest creationTime
(IEEE 2030.5-2018 §10.2.3.3), then the latest start, then the most
recently received revision. When it takes authority, every other live
event whose window contains `now` is marked SUPERSEDED and never executes
again, even after the superseding event ends or is cancelled (Sprint 2
F-03: "superseded events do not fire").

Between events, the DefaultDERControl of the program with the lowest
primacy that asks for a power the gateway can apply takes over from Safe
Idle (default_control). Events the server marks Cancelled or Superseded through
EventStatus never execute. Re-serving an event unchanged never revives it;
a changed instruction under the same mRID is a new revision and is
evaluated afresh.

Every status change the utility asked to hear about (the event's replyTo
and responseRequired) queues a DERControlResponse in the same transaction:
received, started (its setpoint first confirmed by the BMS), completed,
cancelled, superseded, or rejected. See capstone/responses.py.
"""

import asyncio
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from capstone import responses

log = logging.getLogger("scheduler")

DB_PATH = Path("schedule_store/schedule.db")

# ── Event lifecycle ─────────────────────────────────────────────
SCHEDULED  = "scheduled"    # approved; waiting for its window or for authority
ACTIVE     = "active"       # holds (or held) dispatch authority in its window
COMPLETED  = "completed"    # window ended after its setpoint was applied
EXPIRED    = "expired"      # window ended without its setpoint ever being applied
SUPERSEDED = "superseded"   # a later-starting overlapping event took authority
CANCELLED  = "cancelled"    # withdrawn by the server; never executes
REJECTED   = "rejected"     # refused (BMS exception, unrecognised status); never executes
LIVE = (SCHEDULED, ACTIVE)

# Dispatch failures the BMS will never get past for this setpoint: the BMS
# answered with a Modbus exception, or the gateway cannot dispatch this BMS
# at all (DC-configured, working answer E8). The event is rejected (252).
REFUSALS = ("exception", "not_dispatchable")

# IEEE 2030.5 EventStatus.currentStatus: 0 Scheduled and 1 Active (or no
# EventStatus element) leave an event executable; 2 Cancelled, 3 Cancelled
# with Randomization and 4 Superseded are the server saying it must not run.
_SERVER_TERMINAL = {2: CANCELLED, 3: CANCELLED, 4: SUPERSEDED}

# ── Dispatch authority, highest precedence first ────────────────
AUTH_FORCE_IDLE = "operator_force_idle"
AUTH_SCHEDULE   = "approved_schedule"
AUTH_DEFAULT    = "default_control"       # the utility's DefaultDERControl, between events
AUTH_SAFE_IDLE  = "safe_idle"
# Used only when the scheduler cannot evaluate the schedule (see step()).
AUTH_FAILSAFE   = "failsafe_scheduler_error"


@dataclass(frozen=True)
class Decision:
    """Who has authority over the battery setpoint right now, and what it wants."""
    authority: str
    power_kw: float
    mrid: str | None = None
    reason: str = ""
    until: int | None = None      # end of the authoritative event's window

    @property
    def source(self) -> str:
        return f"schedule:{self.mrid}" if self.authority == AUTH_SCHEDULE else self.authority


_EVENTS_DDL = """
    CREATE TABLE IF NOT EXISTS events (
        mrid          TEXT PRIMARY KEY,
        start_ts      INTEGER NOT NULL,
        end_ts        INTEGER NOT NULL,
        power_kw      REAL NOT NULL,
        source        TEXT,
        status        TEXT NOT NULL DEFAULT 'scheduled',
        revision      INTEGER NOT NULL DEFAULT 0,
        received_at   REAL,
        updated_at    REAL,
        applied_at    REAL,
        superseded_by TEXT,
        status_reason TEXT,
        reply_to      TEXT,
        response_required INTEGER NOT NULL DEFAULT 0,
        program       TEXT,
        primacy       INTEGER,
        creation_time INTEGER
    )
"""
# Columns added to databases created before the dispatch arbiter (and,
# for the later ones, DERControlResponses and DER programs) existed.
_ADDED_COLUMNS = {
    "status":        "TEXT NOT NULL DEFAULT 'scheduled'",
    "revision":      "INTEGER NOT NULL DEFAULT 0",
    "received_at":   "REAL",
    "updated_at":    "REAL",
    "applied_at":    "REAL",
    "superseded_by": "TEXT",
    "status_reason": "TEXT",
    "reply_to":      "TEXT",
    "response_required": "INTEGER NOT NULL DEFAULT 0",
    "program":       "TEXT",
    "primacy":       "INTEGER",
    "creation_time": "INTEGER",
}
# The DefaultDERControl of each DER program, as last read.
_DEFAULTS_DDL = """
    CREATE TABLE IF NOT EXISTS default_controls (
        program    TEXT PRIMARY KEY,
        primacy    INTEGER,
        power_kw   REAL,
        control    TEXT,
        problem    TEXT,
        updated_at REAL
    )
"""
# Event metadata stored alongside the instruction, updated in place.
_METADATA = ("reply_to", "response_required", "program", "primacy", "creation_time")
# The response each lifecycle status is reported with, when asked for.
_STATUS_RESPONSE = {
    "completed":  responses.COMPLETED,
    "cancelled":  responses.CANCELLED,
    "superseded": responses.SUPERSEDED,
    "rejected":   responses.REJECTED_INVALID,
}
_OVERRIDE_DDL = """
    CREATE TABLE IF NOT EXISTS operator_override (
        id         INTEGER PRIMARY KEY CHECK (id = 1),
        force_idle INTEGER NOT NULL DEFAULT 0,
        set_by     TEXT,
        reason     TEXT,
        set_at     REAL
    )
"""


@contextmanager
def _connect(db_path: Path):
    """One connection per unit of work: commit on success, roll back
    on error, and ALWAYS close. sqlite3's own `with conn:` only
    commits/rolls back — it never closes — and a Connection is kept
    alive by a reference cycle with its statement cache, so an
    unclosed one keeps the db/-wal/-shm file handles open until the
    cyclic garbage collector happens to run."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            yield conn
    finally:
        conn.close()


# ── Operator Force Idle override ────────────────────────────────

def set_force_idle(active: bool, by: str, reason: str = "", db_path: Path | None = None) -> None:
    """Engage (active=True) or release the operator's Force Idle override.
    It is stored in the schedule database, so it survives gateway restarts
    and a local operator tool can set it while the gateway runs; the
    gateway applies it on its next tick, ahead of every schedule."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.execute(_OVERRIDE_DDL)
        conn.execute(
            "INSERT OR REPLACE INTO operator_override (id, force_idle, set_by, reason, set_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (1 if active else 0, by, reason, time.time()),
        )


def get_force_idle(db_path: Path | None = None) -> dict:
    """Current Force Idle override: {"active", "by", "reason", "set_at"}."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.execute(_OVERRIDE_DDL)
        row = conn.execute(
            "SELECT force_idle, set_by, reason, set_at FROM operator_override WHERE id = 1"
        ).fetchone()
    if row is None:
        return {"active": False, "by": None, "reason": None, "set_at": None}
    return {"active": bool(row[0]), "by": row[1], "reason": row[2], "set_at": row[3]}


def _status_from_server(value) -> tuple[str, str | None]:
    if value in (None, 0, 1):
        return SCHEDULED, None
    if value in _SERVER_TERMINAL:
        return _SERVER_TERMINAL[value], f"server EventStatus currentStatus={value}"
    return REJECTED, f"unrecognised server EventStatus currentStatus={value!r}"


class Scheduler:
    def __init__(self, config: dict, state, modbus_driver, audit_log=None, reliability_manager=None):
        self.state  = state
        self.modbus = modbus_driver
        self.audit_log = audit_log
        self.reliability_manager = reliability_manager  # optional — F-05 FSM, refreshed every tick
        self.current_decision: Decision | None = None
        self._applied_marked: set[tuple[str, int]] = set()
        self._last_failure = None
        self._failed_attempts = 0
        self._last_override = None
        self._failed_ticks = 0
        self._init_db()

    # ── Database setup ─────────────────────────────────────────

    def _init_db(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute(_EVENTS_DDL)
            conn.execute(_OVERRIDE_DDL)
            conn.execute(responses.DDL)
            conn.execute(_DEFAULTS_DDL)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            for name, decl in _ADDED_COLUMNS.items():
                if name not in cols:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
            if "executed" in cols and "status" not in cols:
                # Database written by the earlier edge-triggered scheduler:
                # executed 0 = pending, 1/-1 = started, 2/-2 = finished.
                conn.execute("""
                    UPDATE events SET
                        status = CASE WHEN executed IN (1, -1) THEN 'active'
                                      WHEN executed IN (2, -2) THEN 'completed'
                                      ELSE 'scheduled' END,
                        revision = rowid
                """)
        log.info(f"Schedule database ready at {DB_PATH} (WAL mode)")

    def _db(self):
        return _connect(DB_PATH)

    def _now(self) -> float:
        """The utility's time, which event windows are expressed in: this
        machine's clock plus its offset to the utility server's Time
        resource (IEEE 2030.5 Time function set; 0 until one is read).
        Bookkeeping stamps stay on this machine's clock."""
        return time.time() + getattr(self.state, "clock_offset_sec", 0.0)

    # ── Public API ─────────────────────────────────────────────

    def add_event(self, event: dict) -> str:
        """Store a utility event. Returns what happened: 'new', 'updated',
        'unchanged', or the terminal status it was given ('cancelled',
        'superseded', 'rejected').

        The IEEE server re-serves the same DERControl on every poll (this
        is normal per spec), so an unchanged event is left alone — which
        also means a re-serve never revives an event that was superseded,
        cancelled or has finished. A changed instruction under the same
        mRID becomes a new revision and is evaluated afresh; the running
        dispatch picks it up on the next tick."""
        mrid = event["mrid"]
        status, reason = _status_from_server(event.get("server_status"))
        instruction = (event["start"], event["end"], event["power_kw"], event.get("source"))
        reply = (event.get("reply_to"), int(event.get("response_required") or 0))
        metadata = (*reply, event.get("program"), event.get("primacy"), event.get("creation_time"))
        now = time.time()
        utility_now = self._now()
        with self._db() as conn:
            row = conn.execute(f"""
                SELECT start_ts, end_ts, power_kw, source, status, {', '.join(_METADATA)}
                FROM events WHERE mrid=?
            """, (mrid,)).fetchone()

            if row is not None and tuple(row[:4]) == instruction:
                if tuple(row[5:]) != metadata:
                    conn.execute(f"UPDATE events SET {', '.join(f'{c}=?' for c in _METADATA)} WHERE mrid=?",
                                 (*metadata, mrid))
                if status == SCHEDULED or row[4] == status or row[4] not in LIVE:
                    log.debug(f"Event {mrid} re-seen on poll, no change "
                              f"(status={row[4]}) — not re-triggering")
                    return "unchanged"
                # The server now cancels (or supersedes) an event it served before.
                conn.execute("""
                    UPDATE events SET status=?, status_reason=?, updated_at=? WHERE mrid=?
                """, (status, reason, now, mrid))
                responses.enqueue(conn, mrid, _STATUS_RESPONSE[status], *reply, now)
                outcome = status
            else:
                revision = conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM events").fetchone()[0]
                conn.execute(f"""
                    INSERT INTO events (mrid, start_ts, end_ts, power_kw, source, status,
                                        revision, received_at, updated_at, applied_at,
                                        superseded_by, status_reason, {', '.join(_METADATA)})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, {', '.join('?' for _ in _METADATA)})
                    ON CONFLICT(mrid) DO UPDATE SET
                        start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                        power_kw=excluded.power_kw, source=excluded.source,
                        status=excluded.status, revision=excluded.revision,
                        updated_at=excluded.updated_at, applied_at=NULL,
                        superseded_by=NULL, status_reason=excluded.status_reason,
                        {', '.join(f'{c}=excluded.{c}' for c in _METADATA)}
                """, (mrid, event["start"], event["end"], event["power_kw"],
                      event.get("source"), status, revision, now, now, reason, *metadata))
                outcome = ("new" if row is None else "updated") if status == SCHEDULED else status
                if row is None:
                    # First sight: received, unless the server's status is unusable or
                    # the event is already over; then how the server marks it.
                    if status == REJECTED:
                        responses.enqueue(conn, mrid, responses.REJECTED_INVALID, *reply, now)
                    elif event["end"] <= utility_now:
                        responses.enqueue(conn, mrid, responses.REJECTED_EXPIRED, *reply, now)
                    else:
                        responses.enqueue(conn, mrid, responses.RECEIVED, *reply, now)
                        if status != SCHEDULED:
                            responses.enqueue(conn, mrid, _STATUS_RESPONSE[status], *reply, now)
                elif status != SCHEDULED:
                    responses.enqueue(conn, mrid, _STATUS_RESPONSE[status], *reply, now)

        # The control as the utility sent it (e.g. a percentage and the
        # rating it refers to), when the event came with one.
        control = f" control={event['control']}" if event.get("control") else ""
        log.info(f"Event {mrid} {outcome}: {event['power_kw']:+.1f} kW "
                 f"from {_ts(event['start'])} to {_ts(event['end'])}"
                 + (f" ({reason})" if reason else "") + control)
        self._audit("schedule_received", event.get("source") or "unknown",
                    f"mrid={mrid} power_kw={_kw(event['power_kw'])} "
                    f"start={_ts(event['start'])} end={_ts(event['end'])}"
                    + (f" status={status} ({reason})" if reason else "") + control,
                    outcome)
        return outcome

    def get_upcoming(self, limit=5) -> list[dict]:
        """Return the next N live (scheduled or active) events."""
        now = int(self._now())
        with self._db() as conn:
            rows = conn.execute("""
                SELECT mrid, start_ts, end_ts, power_kw, source
                FROM events
                WHERE end_ts > ? AND status IN ('scheduled', 'active')
                ORDER BY start_ts ASC
                LIMIT ?
            """, (now, limit)).fetchall()
        return [{"mrid": r[0], "start": r[1], "end": r[2],
                 "power_kw": r[3], "source": r[4]} for r in rows]

    def has_valid_schedule(self, now: int = None) -> bool:
        """Does an approved event hold (or qualify for) dispatch authority
        right now? Used by ReliabilityManager to decide between AUTONOMOUS
        (keep running an approved schedule) and SAFE_IDLE (nothing valid
        to run)."""
        now = now if now is not None else int(self._now())
        with self._db() as conn:
            return _authoritative(conn, now) is not None

    # ── Main loop ──────────────────────────────────────────────

    async def run(self):
        """Runs every second: re-decides authority and reconciles the battery."""
        log.info("Scheduler running.")
        while True:
            await self.step()
            await asyncio.sleep(1)

    async def step(self, now: int | None = None) -> Decision | None:
        """One iteration of run(). A failing tick (e.g. a locked or broken
        schedule database) never ends dispatch and never leaves an
        unvouched setpoint in place: the last approved event is kept only
        until its known end, otherwise the battery is held at 0 kW."""
        now = int(self._now()) if now is None else now
        try:
            decision = await self.tick(now)
        except Exception as exc:
            self._failed_ticks += 1
            log.exception("Scheduler tick failed")
            await self._failsafe_after_error(now, exc)
            return None
        if self._failed_ticks:
            log.warning(f"Scheduler recovered after {self._failed_ticks} failed tick(s)")
            self._audit("scheduler", "scheduler",
                        f"recovered after {self._failed_ticks} failed tick(s)", "recovered")
            self._failed_ticks = 0
        return decision

    async def tick(self, now: int | None = None) -> Decision:
        """One reconciliation step: advance event statuses, decide who has
        authority, and write the resulting setpoint if the battery was not
        already confirmed at it."""
        now = int(self._now()) if now is None else now
        # SQLite work runs in a worker thread: a locked schedule database
        # (another process holding a write lock) delays this decision, never
        # the event loop that runs the BMS heartbeat and utility polling (NF-01).
        decision = await asyncio.to_thread(self._decide_and_record, now)
        rejected = await self._apply(decision)
        if rejected:
            # The BMS refused that setpoint: re-decide at once so an older
            # setpoint is never left in place for another second.
            decision = await asyncio.to_thread(self._decide_and_record, now)
            await self._apply(decision)
        self._publish(decision)
        return decision

    def decide(self, now: int) -> Decision:
        """Who has authority at `now` (read-only; see the module docstring)."""
        with self._db() as conn:
            return self._decide(conn, now, _read_override(conn))

    def cancel_event(self, mrid: str, reason: str, source: str = "operator") -> bool:
        """Withdraw a live event; it will never execute. Returns False if
        the event is unknown or already finished."""
        with self._db() as conn:
            changed = conn.execute("""
                UPDATE events SET status='cancelled', status_reason=?, updated_at=?
                WHERE mrid=? AND status IN ('scheduled', 'active')
            """, (reason, time.time(), mrid)).rowcount
        if changed:
            log.warning(f"Event {mrid} cancelled: {reason}")
            self._audit("schedule_status", source, f"mrid={mrid} -> cancelled ({reason})", "cancelled")
        return bool(changed)

    def set_default_controls(self, defaults: list[dict], keep=()) -> None:
        """Store the DefaultDERControl of every program just read: dicts of
        program, primacy, power_kw (None: it asks for no power), control
        (description) and problem (why it cannot be honoured, or None). A
        program not listed loses its stored default, unless it is in `keep`
        (its default could not be read this time). Audited when one changes."""
        now = time.time()
        with self._db() as conn:
            stored = {row[0]: tuple(row[1:]) for row in conn.execute(
                "SELECT program, primacy, power_kw, control, problem FROM default_controls")}
            read = {d["program"]: (d["primacy"], d["power_kw"], d["control"], d["problem"]) for d in defaults}
            read.update({program: stored[program] for program in keep if program in stored})
            if read == stored:
                return
            conn.execute("DELETE FROM default_controls")
            conn.executemany("INSERT INTO default_controls VALUES (?, ?, ?, ?, ?, ?)",
                             [(program, *values, now) for program, values in read.items()])
        for program in sorted(set(stored) | set(read)):
            if stored.get(program) == read.get(program):
                continue
            if program not in read:
                detail, outcome = f"program={program}: DefaultDERControl no longer listed", "removed"
            else:
                primacy, power_kw, control, problem = read[program]
                if problem:
                    detail, outcome = f"program={program} primacy={primacy}: not applied: {problem}", "not_applied"
                elif power_kw is None:
                    detail, outcome = f"program={program} primacy={primacy}: {control}", "no_setpoint"
                else:
                    detail, outcome = (f"program={program} primacy={primacy}: power_kw={_kw(power_kw)} "
                                       f"control={control}"), "updated"
            log.info(f"DefaultDERControl {detail}")
            self._audit("default_control", "ieee2030.5", detail, outcome)

    # ── DERControlResponse outbox (capstone/responses.py) ──────

    def record_response(self, mrid: str, status: int, reply_to: str | None, response_required: int) -> bool:
        """Queue a response for an event that never reached the schedule
        (the IEEE client refused it). Returns True if it was queued now."""
        with self._db() as conn:
            return responses.enqueue(conn, mrid, status, reply_to, response_required, time.time())

    def due_responses(self, now: float, limit: int) -> list[tuple]:
        with self._db() as conn:
            return responses.due(conn, now, limit)

    def response_delivered(self, mrid: str, status: int, now: float):
        with self._db() as conn:
            responses.mark_delivered(conn, mrid, status, now)

    def response_failed(self, mrid: str, status: int, error: str, now: float) -> int:
        with self._db() as conn:
            return responses.mark_failed(conn, mrid, status, error, now)

    def response_given_up(self, mrid: str, status: int, error: str, now: float):
        with self._db() as conn:
            responses.mark_given_up(conn, mrid, status, error, now)

    # ── Internal ───────────────────────────────────────────────

    def _decide_and_record(self, now: int) -> Decision:
        with self._db() as conn:
            changes = self._advance_lifecycle(conn, now)
            override = _read_override(conn)
            decision = self._decide(conn, now, override)
        self._note_override(override)
        for mrid, status, detail in changes:
            log.info(f"Event {mrid} -> {status} ({detail})")
            self._audit("schedule_status", f"schedule:{mrid}",
                        f"mrid={mrid} -> {status} ({detail})", status)
        if decision != self.current_decision:
            log.warning(f"Dispatch authority: {decision.authority} "
                        f"{decision.power_kw:+.1f} kW — {decision.reason}")
            self._audit("authority", decision.source,
                        f"{decision.authority} power_kw={_kw(decision.power_kw)} ({decision.reason})",
                        "decided")
            self.current_decision = decision
        return decision

    def _publish(self, decision: Decision, reason: str | None = None):
        """Share the decision and whether the BMS confirmed it (the driver's
        last confirmed setpoint equals the desired one) on GatewayState, for
        the reliability FSM and the dashboard."""
        self.state.authority = {
            "authority": decision.authority, "mrid": decision.mrid,
            "desired_kw": decision.power_kw, "reason": reason or decision.reason,
            "until": decision.until,
            "confirmed": self._is_applied(decision.power_kw),
        }
        if self.reliability_manager:
            self.reliability_manager.refresh()

    async def _failsafe_after_error(self, now: int, exc: Exception):
        """The schedule could not be evaluated. Keep executing the last
        approved event only while its window (known from the last good
        tick) is still open; in every other case hold the battery at 0 kW."""
        last = self.current_decision
        if last is not None and last.authority == AUTH_SCHEDULE and last.until is not None and now < last.until:
            target = last
            reason = (f"schedule store unavailable ({type(exc).__name__}); continuing approved "
                      f"event {last.mrid} until its scheduled end {_ts(last.until)}")
        else:
            target = Decision(AUTH_FAILSAFE, 0.0, None,
                              f"schedule store unavailable ({type(exc).__name__}); "
                              f"no approved event can be confirmed: holding 0 kW")
            reason = target.reason
        if self._failed_ticks == 1:
            self._audit("scheduler", target.source, f"tick failed: {type(exc).__name__}: {exc}; {reason}",
                        "failsafe")
        try:
            if not self._is_applied(target.power_kw):
                await self.modbus.set_power(target.power_kw, source=target.source)
        except Exception:
            log.exception("Fail-safe write failed")
        self._publish(target, reason)

    def _advance_lifecycle(self, conn, now: int) -> list[tuple[str, str, str]]:
        """Finish events whose window has closed, give authority to the
        top event in its window, and supersede every other live event in
        that window. Returns (mrid, new_status, detail) changes."""
        changes = []
        stamp = time.time()
        for mrid, status, applied_at, *reply in conn.execute("""
            SELECT mrid, status, applied_at, reply_to, response_required FROM events
            WHERE status IN ('scheduled', 'active') AND end_ts <= ?
        """, (now,)).fetchall():
            if applied_at is not None:
                new, detail = COMPLETED, "window ended"
                responses.enqueue(conn, mrid, responses.COMPLETED, *reply, stamp)
            else:
                # IEEE 2030.5 has no status for "could not execute": after
                # Received, no Started or Completed says it never ran.
                new, detail = EXPIRED, "window ended without its setpoint ever being applied"
            conn.execute("UPDATE events SET status=?, updated_at=? WHERE mrid=?", (new, stamp, mrid))
            changes.append((mrid, new, detail))

        top = _authoritative(conn, now)
        if top is not None:
            top_mrid, top_status, top_program = top[0], top[4], top[5]
            if top_status == SCHEDULED:
                conn.execute("UPDATE events SET status='active', updated_at=? WHERE mrid=?",
                             (stamp, top_mrid))
                changes.append((top_mrid, ACTIVE, "took dispatch authority"))
            for mrid, program, *reply in conn.execute("""
                SELECT mrid, program, reply_to, response_required FROM events
                WHERE status IN ('scheduled', 'active') AND mrid != ?
                  AND start_ts <= ? AND end_ts > ?
            """, (top_mrid, now, now)).fetchall():
                conn.execute("""
                    UPDATE events SET status='superseded', superseded_by=?, updated_at=?
                    WHERE mrid=?
                """, (top_mrid, stamp, mrid))
                # Superseded within its own program; aborted for another program's event.
                responses.enqueue(conn, mrid, responses.SUPERSEDED if program == top_program
                                  else responses.ABORTED_OTHER_PROGRAM, *reply, stamp)
                changes.append((mrid, SUPERSEDED, f"superseded by {top_mrid}"))
        return changes

    def _decide(self, conn, now: int, override) -> Decision:
        if override is not None and override[0]:
            return Decision(AUTH_FORCE_IDLE, 0.0, None,
                            f"Force Idle engaged by {override[1]}: {override[2] or 'no reason given'}")
        top = _authoritative(conn, now)
        if top is not None:
            mrid, start, end, power_kw = top[:4]
            return Decision(AUTH_SCHEDULE, power_kw, mrid,
                            f"event {mrid} {_ts(start)} to {_ts(end)}", until=end)
        default = conn.execute("""
            SELECT program, power_kw, control FROM default_controls
            WHERE power_kw IS NOT NULL AND problem IS NULL
            ORDER BY COALESCE(primacy, 256), program
            LIMIT 1
        """).fetchone()
        if default is not None:
            return Decision(AUTH_DEFAULT, default[1], None,
                            f"no event: DefaultDERControl of {default[0]} ({default[2]})")
        return Decision(AUTH_SAFE_IDLE, 0.0, None, "no valid approved schedule")

    def _note_override(self, override):
        current = None if override is None else (bool(override[0]), override[1], override[2])
        if current != self._last_override:
            if current is not None and (self._last_override is not None or current[0]):
                action = "engaged" if current[0] else "released"
                self._audit("operator_command", f"operator:{current[1]}",
                            f"Force Idle {action} ({current[2] or 'no reason given'})", action)
            self._last_override = current

    def _is_applied(self, power_kw: float) -> bool:
        """Has the BMS confirmed holding what the driver would write for
        power_kw (the driver may hold it inside the BMS power limits)?"""
        check = getattr(self.modbus, "is_applied", None)
        if callable(check):
            return bool(check(power_kw))
        commanded = getattr(self.modbus, "commanded_kw", None)
        return commanded is not None and commanded == power_kw

    async def _apply(self, decision: Decision) -> bool:
        """Make the battery match `decision`. Returns True if the BMS
        refused the setpoint of the authoritative event (it is then
        marked REJECTED and must not be retried)."""
        if self._is_applied(decision.power_kw):
            await asyncio.to_thread(self._mark_applied, decision)
            return False

        ok = await self.modbus.set_power(decision.power_kw, source=decision.source)
        if ok:
            if self._last_failure is not None:
                log.info(f"Dispatch recovered after {self._failed_attempts} failed attempt(s)")
            self._audit("dispatch", decision.source,
                        f"setpoint {_kw(decision.power_kw)} kW written ({decision.authority})"
                        + self._held_note(decision.power_kw)
                        + (f" after {self._failed_attempts} failed attempt(s)"
                           if self._last_failure is not None else ""),
                        "success")
            self._last_failure, self._failed_attempts = None, 0
            await asyncio.to_thread(self._mark_applied, decision)
            return False

        error = getattr(self.modbus, "last_dispatch_error", None) or ("unknown", None)
        self._failed_attempts += 1
        failure = (decision.source, decision.power_kw, error[0])
        if failure != self._last_failure:
            self._audit("dispatch", decision.source,
                        f"setpoint {_kw(decision.power_kw)} kW NOT written ({decision.authority}): "
                        f"{error[0]}" + (f" {error[1]}" if error[1] is not None else ""),
                        "failed_will_retry" if error[0] not in REFUSALS else "rejected")
            self._last_failure = failure

        if decision.authority == AUTH_SCHEDULE and error[0] in REFUSALS:
            reason = (f"BMS rejected setpoint {_kw(decision.power_kw)} kW (Modbus exception {error[1]})"
                      if error[0] == "exception" else
                      f"setpoint {_kw(decision.power_kw)} kW not applicable: {error[1]}")
            await asyncio.to_thread(self._mark_rejected, decision.mrid, reason)
            log.error(f"Event {decision.mrid} rejected: {reason}")
            self._audit("schedule_status", decision.source,
                        f"mrid={decision.mrid} -> rejected ({reason})", "rejected")
            return True
        return False

    def _held_note(self, power_kw: float) -> str:
        """For the dispatch audit row: what the BMS holds when the driver
        wrote something other than the request (power limits, whole-kW
        setpoint register)."""
        held = getattr(self.modbus, "commanded_kw", None)
        if isinstance(held, (int, float)) and not isinstance(held, bool) and held != power_kw:
            return f"; BMS holds {_kw(held)} kW (power limits / whole-kW register)"
        return ""

    def _mark_rejected(self, mrid: str, reason: str):
        """The BMS refused the event's setpoint: the DER cannot apply it."""
        now = time.time()
        with self._db() as conn:
            if conn.execute("""
                UPDATE events SET status='rejected', status_reason=?, updated_at=?
                WHERE mrid=? AND status IN ('scheduled', 'active')
            """, (reason, now, mrid)).rowcount:
                reply = conn.execute("SELECT reply_to, response_required FROM events WHERE mrid=?",
                                     (mrid,)).fetchone()
                responses.enqueue(conn, mrid, responses.REJECTED_NOT_APPLICABLE, *reply, now)

    def _mark_applied(self, decision: Decision):
        if decision.authority != AUTH_SCHEDULE:
            return
        with self._db() as conn:
            row = conn.execute("""
                SELECT revision, power_kw, applied_at, reply_to, response_required FROM events WHERE mrid=?
            """, (decision.mrid,)).fetchone()
            # Only the revision whose setpoint was actually written counts: an
            # update that raced with the write is applied on the next tick.
            if (row is None or row[1] != decision.power_kw
                    or (decision.mrid, row[0]) in self._applied_marked):
                return
            now = time.time()
            conn.execute("UPDATE events SET applied_at=COALESCE(applied_at, ?) WHERE mrid=?",
                         (now, decision.mrid))
            if row[2] is None:
                # Execution begins when the BMS first confirms the event's setpoint.
                responses.enqueue(conn, decision.mrid, responses.STARTED, *row[3:5], now)
            self._applied_marked.add((decision.mrid, row[0]))

    def _audit(self, event_type: str, source: str, detail: str, outcome: str):
        if self.audit_log:
            self.audit_log.record(event_type=event_type, source=source,
                                  detail=detail, outcome=outcome)


# ── Helpers ────────────────────────────────────────────────────

def _read_override(conn):
    """(force_idle, set_by, reason) of the operator override, or None."""
    return conn.execute(
        "SELECT force_idle, set_by, reason FROM operator_override WHERE id = 1").fetchone()


def _authoritative(conn, now: int):
    """The live event with dispatch authority at `now`: the lowest primacy
    (an event without a program ranks last), then the newest creationTime
    (IEEE 2030.5-2018 §10.2.3.3; none ranks oldest), then the latest start,
    then the most recently received revision.
    (mrid, start, end, power, status, program)."""
    return conn.execute("""
        SELECT mrid, start_ts, end_ts, power_kw, status, program FROM events
        WHERE status IN ('scheduled', 'active') AND start_ts <= ? AND end_ts > ?
        ORDER BY COALESCE(primacy, 256), COALESCE(creation_time, -1) DESC, start_ts DESC, revision DESC
        LIMIT 1
    """, (now, now)).fetchone()


def _kw(power_kw: float) -> str:
    """A power for the audit trail: signed and exact (shortest repr), so a
    request such as 0.45 kW is never recorded as a rounded neighbour."""
    return f"{float(power_kw):+}"


def _ts(unix_ts: int) -> str:
    """Format a Unix timestamp as a human-readable UTC date and time.
    Calendar arithmetic, not fromtimestamp(), which fails after the year
    3000 on Windows; and it never raises, because the dispatch decision
    uses it."""
    from datetime import datetime, timedelta, timezone
    try:
        when = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=unix_ts)
    except OverflowError:
        return f"Unix time {unix_ts} (outside years 1-9999)"
    return when.strftime("%Y-%m-%d %H:%M:%S UTC")
