"""
tests/test_dashboard_js.py
The time and length helpers the dashboard pages run in the browser, run here in Node (skipped where Node is not installed).

Found on the real battery: an event planned for 60 hours from 22 October 09:00 was shown as "9:00 AM to 9:00 PM" with no
dates, and the calendar did not show it on the days after the first. The functions are taken straight out of the page
source (simulators/dashboard_pages.py), so what is tested is the code the page runs.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


def _pages():
    spec = importlib.util.spec_from_file_location("dashboard_pages_under_test", ROOT / "simulators" / "dashboard_pages.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _function(source: str, name: str) -> str:
    """The text of `function name(...) { ... }`, found by matching braces."""
    start = source.index(f"function {name}(")
    depth, i = 0, source.index("{", start)
    while True:
        depth += source[i] == "{"
        depth -= source[i] == "}"
        i += 1
        if depth == 0:
            return source[start:i]


def run_js(expression: str):
    pages = _pages()
    common, schedule = pages.COMMON_JS.replace("\\\\", "\\"), pages.SCHEDULE_JS.replace("\\\\", "\\")
    parts_line = next(line for line in schedule.splitlines() if line.startswith("const parts ="))
    script = "\n".join([
        "const TZ = 'America/Vancouver';",
        *(_function(common, n) for n in ("clock", "dateTime", "dayOf", "lengthText", "rangeParts", "rangeText")),
        parts_line,
        *(_function(schedule, n) for n in ("dayKey", "nextDay", "daysOf")),
        f"console.log(JSON.stringify({expression}));",
    ])
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)




def epoch(text):
    """Epoch seconds of an ISO UTC time such as 2026-10-22T16:00:00Z."""
    import datetime
    return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


S = epoch("2026-10-22T16:00:00Z")           # Thursday 22 October, 9:00 AM Pacific


def test_length_in_words():
    got = run_js("[45, 100, 300, 5400, 86400, 90060, 216000].map(lengthText)")
    assert got == ["45 s", "100 s", "5 min", "1 h 30 min", "1 day", "1 day 1 h 1 min", "2 days 12 h"]


def test_a_same_day_event_keeps_its_end_as_a_time_only():
    first, second = run_js(f"rangeParts({S}, {S + 3600})")
    assert first == "Oct 22, 9:00 AM" and second == "10:00:00 AM"


def test_a_sixty_hour_event_shows_the_date_it_ends_on():
    first, second = run_js(f"rangeParts({S}, {S + 60 * 3600})")
    assert first == "Oct 22, 9:00 AM" and second == "Oct 24, 9:00 PM"
    assert run_js(f"rangeText({S}, {S + 60 * 3600})") == "Oct 22, 9:00 AM to Oct 24, 9:00 PM"


def test_the_calendar_puts_a_sixty_hour_event_on_each_day_it_covers():
    days = run_js(f"daysOf({{start: {S}, end: {S + 60 * 3600}}})")
    assert days == ["2026-10-22", "2026-10-23", "2026-10-24"]


def test_an_event_that_ends_exactly_at_midnight_does_not_spill_onto_the_next_day():
    midnight = epoch("2026-10-23T07:00:00Z")            # 00:00 Pacific on the 23rd
    assert run_js(f"daysOf({{start: {midnight - 3600}, end: {midnight}}})") == ["2026-10-22"]
    assert run_js(f"daysOf({{start: {midnight - 3600}, end: {midnight + 3600}}})") == ["2026-10-22", "2026-10-23"]


def test_a_single_day_event_is_on_one_day():
    assert run_js(f"daysOf({{start: {S}, end: {S + 600}}})") == ["2026-10-22"]


def test_days_follow_the_pacific_clock_across_the_end_of_daylight_saving():
    """Clocks go back at 02:00 on 1 November 2026, so that Sunday has 25 hours."""
    start = epoch("2026-11-01T06:00:00Z")               # 23:00 PDT on 31 October
    days = run_js(f"daysOf({{start: {start}, end: {start + 27 * 3600}}})")
    assert days == ["2026-10-31", "2026-11-01", "2026-11-02"]
