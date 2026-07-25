"""
capstone/audit_log.py
Structured Audit Log (Requirement NF-03)

"Every command, state transition, and ML recommendation SHALL be
logged with UTC timestamp, source, and outcome." — Sprint 1 Report,
Table 2.

This is separate from Python's `logging` module output: that's for a
human watching a terminal. This is a queryable, structured record —
the kind that can actually be reviewed after a test scenario to prove
every event was captured, which is what NF-03's validation method
("review log after full integration test scenario") requires.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("audit_log")

AUDIT_DB_PATH = Path("schedule_store/audit_log.db")


class AuditLog:
    def __init__(self, db_path: Path = AUDIT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _db(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self):
        self.db_path.parent.mkdir(exist_ok=True, parents=True)
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc      TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    source      TEXT NOT NULL,
                    detail      TEXT,
                    outcome     TEXT NOT NULL
                )
            """)

    def record(self, event_type: str, source: str, detail: str = "", outcome: str = "ok"):
        """Record one auditable event. Called for: commands issued,
        state transitions, schedule decisions, and (later) ML
        recommendations.

        CRITICAL: this must never fail silently. If the write itself
        fails (e.g. a locked database under heavy retry load), that
        failure is logged loudly via the standard logger — an audit
        trail that can silently drop entries defeats its own purpose
        (NF-03)."""
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with self._db() as conn:
                conn.execute("""
                    INSERT INTO audit_log (ts_utc, event_type, source, detail, outcome)
                    VALUES (?, ?, ?, ?, ?)
                """, (ts, event_type, source, detail, outcome))
        except Exception as e:
            log.error(
                f"AUDIT LOG WRITE FAILED — entry lost: "
                f"event_type={event_type} source={source} detail={detail} "
                f"outcome={outcome} error={e}"
            )

    def recent(self, limit: int = 50) -> list:
        """Return the most recent audit entries — useful for a
        post-test review or a future dashboard."""
        with self._db() as conn:
            rows = conn.execute("""
                SELECT ts_utc, event_type, source, detail, outcome
                FROM audit_log ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        return [
            {"ts_utc": r[0], "event_type": r[1], "source": r[2], "detail": r[3], "outcome": r[4]}
            for r in rows
        ]