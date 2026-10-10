"""
capstone/responses.py
IEEE 2030.5 DERControlResponses: which ones to send, and the outbox they wait in.

A DERControl asks for responses with two attributes: replyTo, where to POST
them, and responseRequired, a bitmap (IEEE 2030.5-2018 ResponseRequired):
    bit 0  the end device shall indicate that the message was received
    bit 1  the end device shall indicate specific responses
    bit 2  an end user / customer response is required
The gateway has no customer interface, so it never sends bit-2 responses
(opt-in / opt-out): it never opts out of an event.

Each status is sent once per event, when it happens, never again on later
polls. A response waits in the `responses` table of the schedule database,
written in the same transaction as the event change that caused it, so a
restart neither loses nor repeats one. The IEEE client's response loop
delivers it and retries a failed POST with backoff.
"""

import hashlib
import re
import ssl
from pathlib import Path

# Response.status values used here (IEEE 2030.5-2018 Table 27).
RECEIVED   = 1      # Event Received
STARTED    = 2      # Event Started
COMPLETED  = 3      # Event Completed
CANCELLED  = 6      # Event Cancelled
SUPERSEDED = 7      # Event Superseded
ABORTED_OTHER_PROGRAM = 14      # Event aborted due to an alternate program event
REJECTED_NOT_APPLICABLE = 252   # Rejected - control parameter not applicable
REJECTED_INVALID        = 253   # Rejected - invalid event
REJECTED_EXPIRED        = 254   # Rejected - event received after it had expired

STATUS_NAMES = {
    RECEIVED: "EventReceived", STARTED: "EventStarted", COMPLETED: "EventCompleted",
    CANCELLED: "EventCancelled", SUPERSEDED: "EventSuperseded",
    ABORTED_OTHER_PROGRAM: "EventAbortedByOtherProgram",
    REJECTED_NOT_APPLICABLE: "RejectedNotApplicable", REJECTED_INVALID: "RejectedInvalid",
    REJECTED_EXPIRED: "RejectedExpired",
}

# Delivery: retry a failed POST after 5 s, doubling up to 5 min, and give
# up on a response that has not been delivered within a day.
FIRST_RETRY_SEC = 5
MAX_RETRY_SEC = 300
GIVE_UP_AFTER_SEC = 24 * 3600

DDL = """
    CREATE TABLE IF NOT EXISTS responses (
        mrid            TEXT NOT NULL,
        status          INTEGER NOT NULL,
        reply_to        TEXT NOT NULL,
        created_at      REAL NOT NULL,
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL,
        sent_at         REAL,
        gave_up_at      REAL,
        last_error      TEXT,
        PRIMARY KEY (mrid, status)
    )
"""


def parse_response_required(text) -> int:
    """responseRequired (HexBinary8, default "00") as an int; a value that
    is not two hex digits asks for nothing."""
    try:
        return int((text or "00").strip(), 16) & 0xFF
    except ValueError:
        return 0


def wanted(response_required: int, status: int) -> bool:
    """Does responseRequired ask for this status? A refusal (252-254) also
    tells the server the event was received, so it is sent when either bit
    0 or bit 1 is set."""
    if status == RECEIVED:
        return bool(response_required & 0b01)
    if status >= REJECTED_NOT_APPLICABLE:
        return bool(response_required & 0b11)
    return bool(response_required & 0b10)


def enqueue(conn, mrid: str, status: int, reply_to: str | None, response_required: int | None,
            now: float) -> bool:
    """Queue one response, if the event asked for it and it was never queued
    before. Returns True if it was queued now."""
    if not reply_to or not wanted(response_required or 0, status):
        return False
    return conn.execute("""
        INSERT OR IGNORE INTO responses (mrid, status, reply_to, created_at, next_attempt_at)
        VALUES (?, ?, ?, ?, ?)
    """, (mrid, status, reply_to, now, now)).rowcount == 1


def due(conn, now: float, limit: int) -> list[tuple]:
    """Responses to send now, oldest first, as (mrid, status, reply_to,
    created_at, attempts). An event's responses go out in the order they
    happened: one waits while an earlier one for the same event is still
    undelivered."""
    return conn.execute("""
        SELECT mrid, status, reply_to, created_at, attempts FROM responses AS r
        WHERE sent_at IS NULL AND gave_up_at IS NULL AND next_attempt_at <= ?
          AND NOT EXISTS (
              SELECT 1 FROM responses AS e
              WHERE e.mrid = r.mrid AND e.sent_at IS NULL AND e.gave_up_at IS NULL
                AND (e.created_at < r.created_at OR (e.created_at = r.created_at AND e.status < r.status)))
        ORDER BY created_at, status
        LIMIT ?
    """, (now, limit)).fetchall()


def mark_delivered(conn, mrid: str, status: int, now: float):
    conn.execute("""
        UPDATE responses SET sent_at=?, attempts=attempts + 1, last_error=NULL
        WHERE mrid=? AND status=?
    """, (now, mrid, status))


def mark_failed(conn, mrid: str, status: int, error: str, now: float) -> int:
    """Record a failed attempt and schedule the next one. Returns the
    number of failed attempts so far."""
    attempts = conn.execute("SELECT attempts FROM responses WHERE mrid=? AND status=?",
                            (mrid, status)).fetchone()[0] + 1
    retry_in = min(FIRST_RETRY_SEC * 2 ** (attempts - 1), MAX_RETRY_SEC)
    conn.execute("""
        UPDATE responses SET attempts=?, next_attempt_at=?, last_error=? WHERE mrid=? AND status=?
    """, (attempts, now + retry_in, error, mrid, status))
    return attempts


def mark_given_up(conn, mrid: str, status: int, error: str, now: float):
    conn.execute("""
        UPDATE responses SET gave_up_at=?, last_error=? WHERE mrid=? AND status=?
    """, (now, error, mrid, status))


_PEM_CERTIFICATE = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)


def certificate_der(path) -> bytes:
    """The DER encoding of the first certificate in a PEM file (for a chain,
    the device's own)."""
    match = _PEM_CERTIFICATE.search(Path(path).read_text(encoding="ascii", errors="replace"))
    if match is None:
        raise ValueError(f"no PEM certificate in {path}")
    return ssl.PEM_cert_to_DER_cert(match.group(0))


def lfdi_of_certificate(path) -> str:
    """IEEE 2030.5 LFDI of a device certificate: the SHA-256 hash of its DER
    encoding, truncated to the first 160 bits, as 40 hex digits. For a PEM
    file holding a chain, the first certificate is the device's."""
    return hashlib.sha256(certificate_der(path)).hexdigest()[:40].upper()


def sfdi_of_lfdi(lfdi: str) -> int:
    """IEEE 2030.5 SFDI: the LFDI truncated to its first 36 bits, as a
    decimal number with a sum-of-digits check digit appended (the digits,
    check digit included, add up to a multiple of 10)."""
    digits = str(int(lfdi[:9], 16))
    check = (10 - sum(int(d) for d in digits) % 10) % 10
    return int(digits + str(check))
