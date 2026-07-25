"""
core/scheduler.py
Calendar-based Schedule Manager

Stores DERControl events in a local SQLite database (WAL mode — see
Requirement F-03: "store events ... in SQLite (WAL mode)").
Every second, checks if any event should start or end right now
and sends the command to the Modbus driver.

Why SQLite?
  - Survives a crash or reboot (schedule is not lost)
  - Simple, no server needed
  - Built into Python standard library

Why WAL mode specifically?
  - Allows concurrent reads while a write is in progress, which matters
    here since the scheduler polls every second while add_event() can
    be called at any time from the IEEE client's poll loop.
"""

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("scheduler")

DB_PATH = Path("schedule_store/schedule.db")


class Scheduler:
    def __init__(self, config: dict, state, modbus_driver, audit_log=None):
        self.state  = state
        self.modbus = modbus_driver
        self.audit_log = audit_log
        self._init_db()

    # ── Database setup ─────────────────────────────────────────

    def _init_db(self):
        DB_PATH.parent.mkdir(exist_ok=True)
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    mrid        TEXT PRIMARY KEY,
                    start_ts    INTEGER NOT NULL,
                    end_ts      INTEGER NOT NULL,
                    power_kw    REAL NOT NULL,
                    source      TEXT,
                    executed    INTEGER DEFAULT 0   -- 0=pending, 1=started, 2=done, -1/-2=in-progress
                )
            """)
        log.info(f"Schedule database ready at {DB_PATH} (WAL mode)")

    def _db(self):
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── Public API ─────────────────────────────────────────────

    def add_event(self, event: dict):
        """Insert a new event, or update an existing one — but ONLY
        reset its execution state if something actually changed. The
        IEEE server re-serves the same active DERControl on every poll
        (this is normal per spec), so without this check every poll
        would re-trigger the event and re-send a redundant Modbus
        write."""
        with self._db() as conn:
            existing = conn.execute("""
                SELECT start_ts, end_ts, power_kw, source, executed
                FROM events WHERE mrid=?
            """, (event["mrid"],)).fetchone()

            if existing is not None:
                unchanged = (
                    existing[0] == event["start"] and
                    existing[1] == event["end"] and
                    existing[2] == event["power_kw"] and
                    existing[3] == event["source"]
                )
                if unchanged:
                    log.debug(f"Event {event['mrid']} re-seen on poll, no change "
                              f"(executed={existing[4]}) — not re-triggering")
                    return

            conn.execute("""
                INSERT OR REPLACE INTO events
                    (mrid, start_ts, end_ts, power_kw, source, executed)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (event["mrid"], event["start"], event["end"],
                  event["power_kw"], event["source"]))

        log.info(f"Scheduled event {event['mrid']}: "
                 f"{event['power_kw']:+.1f} kW "
                 f"from {_ts(event['start'])} to {_ts(event['end'])}")

        if self.audit_log:
            self.audit_log.record(
                event_type="schedule_received",
                source=event.get("source", "unknown"),
                detail=f"mrid={event['mrid']} power_kw={event['power_kw']:+.1f} "
                       f"start={_ts(event['start'])} end={_ts(event['end'])}",
                outcome="scheduled",
            )

    def get_upcoming(self, limit=5) -> list[dict]:
        """Return the next N pending events (for display/debugging)."""
        now = int(time.time())
        with self._db() as conn:
            rows = conn.execute("""
                SELECT mrid, start_ts, end_ts, power_kw, source
                FROM events
                WHERE end_ts > ? AND executed < 2
                ORDER BY start_ts ASC
                LIMIT ?
            """, (now, limit)).fetchall()
        return [{"mrid": r[0], "start": r[1], "end": r[2],
                 "power_kw": r[3], "source": r[4]} for r in rows]

    def has_valid_schedule(self, now: int = None) -> bool:
        """Does a currently-active (started, not yet ended) event
        exist? Used by ReliabilityManager to decide between AUTONOMOUS
        (keep running an approved schedule) and SAFE_IDLE (nothing
        valid to run)."""
        now = now if now is not None else int(time.time())
        with self._db() as conn:
            row = conn.execute("""
                SELECT 1 FROM events
                WHERE start_ts <= ? AND end_ts > ? AND executed IN (1, -1)
                LIMIT 1
            """, (now, now)).fetchone()
        return row is not None

    # ── Main loop ──────────────────────────────────────────────

    async def run(self):
        """Runs every second, checks for events to start or stop."""
        log.info("Scheduler running.")
        while True:
            now = int(time.time())
            self._start_due_events(now)
            self._stop_expired_events(now)
            await asyncio.sleep(1)

    def _start_due_events(self, now: int):
        """Find events that should start now and haven't yet."""
        with self._db() as conn:
            rows = conn.execute("""
                SELECT mrid, power_kw FROM events
                WHERE start_ts <= ? AND end_ts > ? AND executed = 0
            """, (now, now)).fetchall()

        for mrid, power_kw in rows:
            log.info(f"STARTING event {mrid}: {power_kw:+.1f} kW")
            with self._db() as conn:
                conn.execute("UPDATE events SET executed=-1 WHERE mrid=?", (mrid,))
            asyncio.create_task(self._execute_start(mrid, power_kw))

    async def _execute_start(self, mrid: str, power_kw: float):
        """Attempt the write; only mark done on success, else retry next tick."""
        success = await self.modbus.set_power(power_kw, source=f"schedule:{mrid}")
        new_status = 1 if success else 0
        with self._db() as conn:
            conn.execute("UPDATE events SET executed=? WHERE mrid=?", (new_status, mrid))
        if not success:
            log.warning(f"Event {mrid} failed to start — will retry on next tick")
        if self.audit_log:
            self.audit_log.record(
                event_type="schedule_execution",
                source=f"schedule:{mrid}",
                detail=f"start power_kw={power_kw:+.1f}",
                outcome="success" if success else "failed_will_retry",
            )

    def _stop_expired_events(self, now: int):
        """Find events that have ended and are still marked as running."""
        with self._db() as conn:
            rows = conn.execute("""
                SELECT mrid FROM events
                WHERE end_ts <= ? AND executed = 1
            """, (now,)).fetchall()

        for (mrid,) in rows:
            log.info(f"Event {mrid} ended — returning to IDLE")
            with self._db() as conn:
                conn.execute("UPDATE events SET executed=-2 WHERE mrid=?", (mrid,))
            asyncio.create_task(self._execute_stop(mrid))

    async def _execute_stop(self, mrid: str):
        """Attempt safe_idle; only mark done on success, else retry next tick."""
        success = await self.modbus.safe_idle()
        new_status = 2 if success else 1
        with self._db() as conn:
            conn.execute("UPDATE events SET executed=? WHERE mrid=?", (new_status, mrid))
        if not success:
            log.warning(f"Event {mrid} failed to stop — will retry on next tick")
        if self.audit_log:
            self.audit_log.record(
                event_type="schedule_execution",
                source=f"schedule:{mrid}",
                detail="stop (event ended)",
                outcome="success" if success else "failed_will_retry",
            )


# ── Helpers ────────────────────────────────────────────────────

def _ts(unix_ts: int) -> str:
    """Format a Unix timestamp as a human-readable string."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")