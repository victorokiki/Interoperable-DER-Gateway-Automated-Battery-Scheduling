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

Rows are written by one background thread, in the order they were
recorded: record() never makes its caller wait for SQLite, so the event
loop that runs the 1 s BMS heartbeat is never stalled by an audit write
(stalls of up to 0.41 s were measured when they ran on it). Reading
(recent()) and stopping the gateway (flush()) wait until every row
recorded so far is in the database; rows recorded in the last moments
before a process is killed outright can be lost.
"""

import atexit
import logging
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("audit_log")

AUDIT_DB_PATH = Path("schedule_store/audit_log.db")

_INSERT = "INSERT INTO audit_log (ts_utc, event_type, source, detail, outcome) VALUES (?, ?, ?, ?, ?)"

# One writer thread for the process: (db_path, row) in the order recorded.
_rows: "queue.Queue[tuple]" = queue.Queue()
_pending = 0                          # rows recorded and not yet written
_written = threading.Condition()
_writer: threading.Thread | None = None


@contextmanager
def _connect(db_path: Path):
    """Commit on success, roll back on error, and always close — see
    Scheduler._db(): sqlite3's `with conn:` alone never closes."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        with conn:
            yield conn
    finally:
        conn.close()


def _write_forever():
    global _pending
    while True:
        batch = [_rows.get()]
        while True:
            try:
                batch.append(_rows.get_nowait())
            except queue.Empty:
                break
        try:
            _write_batch(batch)
        finally:
            with _written:
                _pending -= len(batch)
                _written.notify_all()


def _write_batch(batch: list):
    start = 0
    while start < len(batch):               # consecutive rows for the same database: one transaction
        end = start
        while end < len(batch) and batch[end][0] == batch[start][0]:
            end += 1
        rows = [row for _, row in batch[start:end]]
        try:
            with _connect(batch[start][0]) as conn:
                conn.executemany(_INSERT, rows)
        except Exception as e:
            # CRITICAL: never fail silently — an audit trail that can
            # silently drop entries defeats its own purpose (NF-03).
            for ts, event_type, source, detail, outcome in rows:
                log.error(f"AUDIT LOG WRITE FAILED — entry lost: event_type={event_type} "
                          f"source={source} detail={detail} outcome={outcome} error={e}")
        start = end


def flush(timeout: float = 10.0) -> bool:
    """Wait until every row recorded so far is in its database. Returns
    False if that took longer than `timeout` seconds."""
    with _written:
        return _written.wait_for(lambda: _pending == 0, timeout)


atexit.register(flush)


class AuditLog:
    def __init__(self, db_path: Path = AUDIT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _db(self):
        return _connect(self.db_path)

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
        recommendations. Timestamped now; written by the audit writer
        thread (see the module docstring), where a failed write is
        logged loudly rather than dropped silently (NF-03)."""
        global _writer, _pending
        ts = datetime.now(timezone.utc).isoformat()
        # The database this row belongs in is fixed now: a relative path
        # resolved later, by the writer, could land in another directory.
        db_path = os.path.abspath(self.db_path)
        with _written:
            if _writer is None or not _writer.is_alive():
                _writer = threading.Thread(target=_write_forever, name="audit-writer", daemon=True)
                _writer.start()
            _pending += 1
            _rows.put((db_path, (ts, event_type, source, detail, outcome)))

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every row recorded so far is written (see flush())."""
        return flush(timeout)

    def recent(self, limit: int = 50) -> list:
        """Return the most recent audit entries — useful for a
        post-test review or a future dashboard. Waits for rows still being
        written first."""
        flush()
        with self._db() as conn:
            rows = conn.execute("""
                SELECT ts_utc, event_type, source, detail, outcome
                FROM audit_log ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        return [
            {"ts_utc": r[0], "event_type": r[1], "source": r[2], "detail": r[3], "outcome": r[4]}
            for r in rows
        ]