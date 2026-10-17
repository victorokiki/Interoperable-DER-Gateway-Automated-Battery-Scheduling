"""
tests/test_dashboard_forms.py
The Run now and Schedule forms: nothing is pre-filled, the words say what to type, and a command that is blank or
above what the battery accepts right now is refused with a clear message instead of being queued.

Found on the real battery: the forms were pre-filled with 500 kW (and 3600 s), so a click without reading the fields
sent a 500 kW, one-hour command. The gateway's own ceiling would have held it down, but the form should not offer it.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(monkeypatch, tmp_path, limits=None):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "operator-test-password")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("PUBLIC_MODE", "0")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("grid_forms_under_test", ROOT / "simulators" / "fake_grid_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    status = tmp_path / "live_status.json"
    if limits is not None:
        from capstone import __main__ as main_mod
        from capstone.reliability import ReliabilityManager
        from capstone.state import GatewayState
        state = GatewayState()
        state.limits_kw = {"max_charge_kw": limits[0], "max_discharge_kw": limits[1]}
        state.soc_pct = 50.0
        state.last_status_poll_at = __import__("time").time()
        status.write_text(json.dumps(main_mod.status_snapshot(state, ReliabilityManager({"offline_timeout_sec": 30}, state))))
    monkeypatch.setattr(module, "BATTERY_STATUS_FILE", status)
    return module


def operator(grid):
    client = grid.app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["username"] = "operator"
    return client


def flash_text(client):
    """The message the next page shows (flashes are shown once)."""
    page = client.get("/dashboard").get_data(as_text=True)
    return page


def test_neither_form_is_pre_filled_with_numbers(monkeypatch, tmp_path):
    client = operator(load(monkeypatch, tmp_path))
    run_now = client.get("/dashboard").get_data(as_text=True)
    schedule = client.get("/dashboard/schedule").get_data(as_text=True)
    import re
    for page, ids in ((run_now, ("rn-power", "rn-duration")), (schedule, ("f-kw", "f-len"))):
        for field in ids:
            tag = re.search(r'<input id="%s"[^>]*>' % field, page).group(0)
            assert " value=" not in tag, tag                                  # nothing pre-filled
    assert 'id="rn-power"' in run_now and 'placeholder="e.g. 25"' in run_now and 'placeholder="e.g. 120"' in run_now
    assert 'id="f-kw"' in schedule and 'placeholder="e.g. 25"' in schedule and 'id="f-len"' in schedule
    assert "limits-hint" in run_now and "limitsHint" in run_now           # the live "what the battery accepts" hint


def test_a_blank_power_is_refused_with_words_not_queued(monkeypatch, tmp_path):
    grid = load(monkeypatch, tmp_path)
    client = operator(grid)
    r = client.post("/dashboard/send", data={"action": "charge", "power_kw": "", "duration_sec": "120"})
    assert r.status_code == 302 and grid.active_events == []
    assert "Type how much power first" in flash_text(client)


def test_a_blank_duration_is_refused_with_words_not_queued(monkeypatch, tmp_path):
    grid = load(monkeypatch, tmp_path)
    client = operator(grid)
    r = client.post("/dashboard/send", data={"action": "discharge", "power_kw": "25", "duration_sec": ""})
    assert r.status_code == 302 and grid.active_events == []
    assert "Type how long it should run" in flash_text(client)


def test_a_missing_field_is_not_assumed_to_be_a_default(monkeypatch, tmp_path):
    """Before: no power meant 0, no duration meant one hour."""
    grid = load(monkeypatch, tmp_path)
    client = operator(grid)
    assert client.post("/dashboard/send", data={"action": "charge"}).status_code == 302
    assert client.post("/dashboard/send", data={"action": "charge", "power_kw": "25"}).status_code == 302
    assert grid.active_events == []


def test_more_than_the_battery_accepts_right_now_is_refused_and_says_the_limit(monkeypatch, tmp_path):
    grid = load(monkeypatch, tmp_path, limits=(50, 40))
    client = operator(grid)
    client.post("/dashboard/send", data={"action": "charge", "power_kw": "500", "duration_sec": "120"})
    assert grid.active_events == []
    assert "accepts up to 50 kW charging" in flash_text(client)
    client.post("/dashboard/send", data={"action": "discharge", "power_kw": "45", "duration_sec": "120"})
    assert grid.active_events == []
    assert "accepts up to 40 kW discharging" in flash_text(client)


def test_a_request_within_the_limit_is_sent(monkeypatch, tmp_path):
    grid = load(monkeypatch, tmp_path, limits=(50, 50))
    client = operator(grid)
    client.post("/dashboard/send", data={"action": "charge", "power_kw": "30", "duration_sec": "120"})
    assert len(grid.active_events) == 1


def test_when_the_battery_accepts_nothing_the_limit_check_does_not_block_a_request(monkeypatch, tmp_path):
    """Off or Standby publishes limits of 0: events can still be planned (the gateway holds them until it is On)."""
    grid = load(monkeypatch, tmp_path, limits=(0, 0))
    client = operator(grid)
    client.post("/dashboard/send", data={"action": "charge", "power_kw": "30", "duration_sec": "120"})
    assert len(grid.active_events) == 1
