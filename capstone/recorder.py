"""
capstone/recorder.py
Records what the BMS is actually doing to a CSV, every few seconds, as test evidence.

The audit log says what the gateway SENT and what the BMS CONFIRMED holding. It does not say what
the battery measured while an event ran. This does: one row per sample with the BMS's own state,
health, control mode, charge level and measured power next to what the gateway had commanded, so a
test result does not depend on someone reading a screen at the right moment.

Reads only (the same reads the status poll makes); it never writes to the BMS. Turn it on with
RECORD_BMS=1 (RECORD_INTERVAL_SEC, default 2, at least 1; RECORD_DIR, default recordings/).
"""

import asyncio
import csv
import datetime
import logging
import os
import time

log = logging.getLogger("recorder")

COLUMNS = [
    "utc", "epoch_s", "vancouver",
    "bms_state", "bms_health", "control_mode", "soc_pct",
    "measured_kw", "measured_kvar",                     # + = charging, - = discharging (the app's sign)
    "confirmed_setpoint_kw", "requested_kw", "setpoint_source",
    "limit_charge_kw", "limit_discharge_kw",
    "alerts_hex", "heartbeat_stale",
    "registers_total", "registers_peak_per_s",           # what this gateway asked the BMS for (limit: 50 a second)
    "note",
]


def _pacific_offset_hours(moment: datetime.datetime) -> int:
    """-7 in Pacific Daylight Time, else -8: DST runs from 02:00 on the second Sunday of March to 02:00 on the
    first Sunday of November (the rule in force for Canada and the US), worked out here because Windows ships
    no time-zone database for zoneinfo."""
    def nth_sunday(year, month, n):
        first = datetime.date(year, month, 1)
        return first + datetime.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    year = moment.year
    start = datetime.datetime.combine(nth_sunday(year, 3, 2), datetime.time(2), tzinfo=datetime.timezone.utc) \
        + datetime.timedelta(hours=8)          # 02:00 PST is 10:00 UTC
    end = datetime.datetime.combine(nth_sunday(year, 11, 1), datetime.time(2), tzinfo=datetime.timezone.utc) \
        + datetime.timedelta(hours=7)          # 02:00 PDT is 09:00 UTC
    return -7 if start <= moment < end else -8


def _vancouver(moment: datetime.datetime) -> str:
    """Pacific time as text: from the time-zone database where there is one, else from the DST rule above."""
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("America/Vancouver")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        local = moment.astimezone(datetime.timezone(datetime.timedelta(hours=_pacific_offset_hours(moment))))
        return local.strftime("%Y-%m-%d %H:%M:%S")


class Recorder:
    def __init__(self, driver, state, directory="recordings", interval_sec=2.0):
        self.driver, self.state, self.interval = driver, state, float(interval_sec)
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(directory, f"bms_{stamp}.csv")
        self.rows = 0

    async def sample(self) -> dict:
        """One row. A failed read gives a row with blanks and a note, never an exception."""
        now = datetime.datetime.now(datetime.timezone.utc)
        row = {c: "" for c in COLUMNS}
        row.update(utc=now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", epoch_s=f"{now.timestamp():.3f}",
                   vancouver=_vancouver(now))
        d = self.driver
        row["registers_total"] = d.register_meter.total
        row["registers_peak_per_s"] = d.register_meter.peak_rate
        if not d._client.connected:
            row["note"] = "not connected to the BMS"
            return row
        try:
            status = await d.read_status()
        except Exception as e:
            status = {}
            row["note"] = f"read failed: {type(e).__name__}"
        if not status:
            row["note"] = row["note"] or "the BMS did not answer"
            return row
        limits = d.limits_kw or (None, None)
        row.update(
            bms_state=status["operating_state"], bms_health=status["operating_status"],
            control_mode=status["control_mode"], soc_pct=status["soc_pct"],
            measured_kw="" if status["measured_kw"] is None else status["measured_kw"],
            measured_kvar="" if status["measured_kvar"] is None else status["measured_kvar"],
            confirmed_setpoint_kw="" if d.commanded_kw is None else d.commanded_kw,
            requested_kw="" if d.requested_kw is None else d.requested_kw,
            setpoint_source=d.commanded_source or "",
            limit_charge_kw="" if limits[0] is None else limits[0],
            limit_discharge_kw="" if limits[1] is None else limits[1],
            alerts_hex=f"0x{status['alerts_raw']:08X}",
            heartbeat_stale=int(bool(self.state.bms_heartbeat_stale)),
        )
        return row

    async def run(self):
        """Sample until cancelled. Appends if the file exists (the supervisor restarted this loop)."""
        new = not os.path.exists(self.path)
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                writer.writeheader()
                f.flush()
            log.info(f"Recording the BMS every {self.interval:g} s to {self.path}")
            while True:
                started = time.monotonic()
                writer.writerow(await self.sample())
                f.flush()
                os.fsync(f.fileno())        # a crash or a pulled plug still leaves every row so far
                self.rows += 1
                await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - started)))
