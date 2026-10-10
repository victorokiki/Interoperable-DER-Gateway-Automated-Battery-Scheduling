"""
tests/test_accounts.py
View-only accounts: sign-up, login, and what such an account may and may not do.

The account database is replaced by an in-memory fake; SupabaseStore itself is tested against a
patched urlopen, so no test needs a network or a Supabase project.
"""

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OPERATOR_PASSWORD = "operator-test-password"
GOOD = "a-good-password-1"
WORLD = {"REMOTE_ADDR": "203.0.113.9"}


def load(monkeypatch, with_store=True, public=False):
    monkeypatch.setenv("DASHBOARD_PASSWORD", OPERATOR_PASSWORD)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("PUBLIC_MODE", "1" if public else "0")
    monkeypatch.setenv("DASHBOARD_PORT", "10000")
    monkeypatch.setenv("INTERNAL_PORT", "5001")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("grid_accounts_under_test", ROOT / "simulators" / "fake_grid_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.viewer_store = FakeStore(module.accounts) if with_store else None
    return module


class FakeStore:
    def __init__(self, accounts):
        self.accounts, self.rows, self.down = accounts, {}, False

    def create(self, username, password_hash):
        if self.down:
            raise self.accounts.StoreError("down")
        if username in self.rows:
            return False
        self.rows[username] = password_hash
        return True

    def get_hash(self, username):
        if self.down:
            raise self.accounts.StoreError("down")
        return self.rows.get(username)


def signup(client, name="visitor1", password=GOOD, confirm=None, **extra):
    data = {"username": name, "password": password, "confirm": password if confirm is None else confirm, **extra}
    return client.post("/signup", data=data, environ_overrides=extra.pop("env", None) or {})


def role_of(client):
    with client.session_transaction() as s:
        return s.get("role"), s.get("username"), s.get("logged_in")


# ── sign-up ──────────────────────────────────────────────────────────────────

def test_signup_is_off_when_there_is_no_account_database(monkeypatch):
    client = load(monkeypatch, with_store=False).app.test_client()
    assert client.get("/signup").status_code == 404
    assert client.post("/signup", data={"username": "visitor1", "password": GOOD, "confirm": GOOD}).status_code == 404
    assert "Create a view-only account" not in client.get("/login").get_data(as_text=True)


def test_signup_page_and_the_link_to_it_exist_with_a_database(monkeypatch):
    client = load(monkeypatch).app.test_client()
    page = client.get("/signup").get_data(as_text=True)
    assert "Sign up" in page and "Confirm password" in page and 'name="hp_field"' in page
    assert "Create a view-only account" in client.get("/login").get_data(as_text=True)


def test_signup_creates_a_view_only_session_and_stores_only_a_hash(monkeypatch):
    grid = load(monkeypatch)
    client = grid.app.test_client()
    r = signup(client, "Visitor_One")
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    assert role_of(client) == ("viewer", "visitor_one", True)                 # the name is lower-cased
    stored = grid.viewer_store.rows["visitor_one"]
    assert GOOD not in stored and stored.startswith(("scrypt:", "pbkdf2:"))


@pytest.mark.parametrize("name", ["ab", "x" * 31, "has space", "bad!", "", "operator", "Admin", "root"])
def test_bad_or_reserved_usernames_are_refused(monkeypatch, name):
    grid = load(monkeypatch)
    r = signup(grid.app.test_client(), name)
    assert r.status_code == 400 and not grid.viewer_store.rows


@pytest.mark.parametrize("password", ["short", "aaaaaaaaaaaa", "password123", "invinity2026", "VISITOR1"])
def test_weak_passwords_are_refused(monkeypatch, password):
    grid = load(monkeypatch)
    r = signup(grid.app.test_client(), "visitor1", password)
    assert r.status_code == 400 and not grid.viewer_store.rows


def test_mismatched_confirmation_and_duplicates_and_honeypot(monkeypatch):
    grid = load(monkeypatch)
    client = grid.app.test_client()
    assert signup(client, "visitor1", GOOD, confirm="another-good-pw-1").status_code == 400
    assert signup(client, "visitor1", GOOD, hp_field="http://spam.example").status_code == 400
    assert not grid.viewer_store.rows
    assert signup(client, "visitor1").status_code == 302
    r = signup(grid.app.test_client(), "visitor1")
    assert r.status_code == 409 and "taken" in r.get_data(as_text=True)


def test_signup_is_rate_limited_per_address_and_per_day(monkeypatch):
    grid = load(monkeypatch)
    for i in range(3):
        assert signup(grid.app.test_client(), f"visitor{i}").status_code == 302
    r = signup(grid.app.test_client(), "visitor9")
    assert r.status_code == 429 and "visitor9" not in grid.viewer_store.rows
    grid2 = load(monkeypatch)
    grid2._signup_created_today.extend([__import__("time").time()] * grid2.SIGNUP_CREATED_PER_DAY)
    assert signup(grid2.app.test_client(), "visitor1").status_code == 429
    grid3 = load(monkeypatch)
    for i in range(grid3.SIGNUP_ATTEMPTS_PER_IP):
        signup(grid3.app.test_client(), "x")                                   # invalid names still count as attempts
    assert signup(grid3.app.test_client(), "visitor1").status_code == 429


def test_signup_works_in_public_mode_without_opening_the_machine_endpoints(monkeypatch):
    grid = load(monkeypatch, public=True)
    client = grid.app.test_client()
    assert client.get("/signup", environ_overrides=WORLD).status_code == 200
    assert client.post("/signup", data={"username": "visitor1", "password": GOOD, "confirm": GOOD}, environ_overrides=WORLD).status_code == 302
    assert client.get("/status", environ_overrides=WORLD).status_code == 404


# ── login ────────────────────────────────────────────────────────────────────

def test_a_viewer_can_log_in_and_wrong_credentials_are_refused(monkeypatch):
    grid = load(monkeypatch)
    signup(grid.app.test_client(), "visitor1")
    client = grid.app.test_client()
    assert client.post("/login", data={"username": "visitor1", "password": "wrong-password-1"}).status_code == 401
    assert client.post("/login", data={"username": "nobody", "password": GOOD}).status_code == 401
    assert client.post("/login", data={"username": "Visitor1", "password": GOOD}).status_code == 302
    assert role_of(client) == ("viewer", "visitor1", True)


def test_the_operator_still_logs_in_as_operator_and_cannot_be_impersonated(monkeypatch):
    grid = load(monkeypatch)
    client = grid.app.test_client()
    assert client.post("/login", data={"username": "operator", "password": OPERATOR_PASSWORD}).status_code == 302
    assert role_of(client)[0] == "operator"
    assert client.post("/login", data={"username": "operator", "password": GOOD}).status_code == 401


def test_unknown_usernames_still_cost_one_password_check(monkeypatch):
    grid = load(monkeypatch)
    calls = []
    real = grid.accounts.check_password_hash
    monkeypatch.setattr(grid.accounts, "check_password_hash", lambda h, p: calls.append(1) or real(h, p))
    grid.app.test_client().post("/login", data={"username": "nobody", "password": GOOD})
    assert len(calls) == 1                                                       # same work as for a real account


def test_the_account_database_being_down_gives_a_clear_message(monkeypatch):
    grid = load(monkeypatch)
    grid.viewer_store.down = True
    client = grid.app.test_client()
    assert client.post("/login", data={"username": "visitor1", "password": GOOD}).status_code == 503
    assert signup(client, "visitor1").status_code == 503
    assert client.post("/login", data={"username": "operator", "password": OPERATOR_PASSWORD}).status_code == 302   # the operator never depends on it


# ── what a view-only account may do ─────────────────────────────────────────

def viewer_client(grid):
    client = grid.app.test_client()
    signup(client, "visitor1")
    return client


def test_a_viewer_can_look_at_every_page_and_api(monkeypatch):
    client = viewer_client(load(monkeypatch))
    for path in ["/dashboard", "/dashboard/schedule", "/dashboard/battery", "/dashboard/events", "/dashboard/activity",
                 "/dashboard/alerts", "/dashboard/reports", "/api/overview", "/api/status", "/api/battery", "/api/events",
                 "/api/activity", "/api/alerts", "/api/report", "/reports/activity.csv"]:
        assert client.get(path).status_code == 200, path
    page = client.get("/dashboard").get_data(as_text=True)
    assert 'data-role="viewer"' in page and "View only" in page


def test_a_viewer_cannot_send_schedule_or_cancel_anything(monkeypatch):
    grid = load(monkeypatch)
    client = viewer_client(grid)
    r = client.post("/dashboard/send", data={"action": "charge", "power_kw": "100", "duration_sec": "600"})
    assert r.status_code == 302
    assert "view-only" in client.get("/dashboard").get_data(as_text=True)
    for action in ("idle", "release"):
        assert client.post("/dashboard/send", data={"action": action}).status_code == 302
    body = {"events": [{"power_kw": 100, "start": None, "duration_sec": 600}]}
    assert client.post("/api/schedule", json=body).status_code == 403
    assert client.post("/api/schedule", json={**body, "dry_run": True}).status_code == 403
    assert client.post("/api/schedule/cancel", json={"all": True}).status_code == 403
    assert grid.active_events == []                                             # nothing was queued


def test_the_operator_and_old_sessions_can_still_control(monkeypatch):
    grid = load(monkeypatch)
    operator = grid.app.test_client()
    operator.post("/login", data={"username": "operator", "password": OPERATOR_PASSWORD})
    body = {"events": [{"power_kw": 100, "start": None, "duration_sec": 600}]}
    assert operator.post("/api/schedule", json=body).status_code == 200
    assert operator.post("/api/schedule/cancel", json={"all": True}).status_code == 200
    old = grid.app.test_client()                                                 # a session made before roles existed has no role
    with old.session_transaction() as s:
        s["logged_in"], s["username"] = True, "operator"
    assert old.post("/api/schedule/cancel", json={"all": True}).status_code == 200


def test_the_operator_page_does_not_say_view_only(monkeypatch):
    grid = load(monkeypatch)
    operator = grid.app.test_client()
    operator.post("/login", data={"username": "operator", "password": OPERATOR_PASSWORD})
    page = operator.get("/dashboard").get_data(as_text=True)
    assert 'data-role="operator"' in page and 'class="role-tag"' not in page


# ── the database client ─────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, body=b""):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_supabase_store_talks_to_the_table_the_way_it_should(monkeypatch):
    accounts = load(monkeypatch).accounts
    seen = []

    def fake_urlopen(request, timeout):
        seen.append((request.get_method(), request.full_url, dict(request.header_items()), request.data, timeout))
        if request.get_method() == "GET":
            return FakeResponse(200, json.dumps([{"password_hash": "scrypt:abc"}]).encode())
        return FakeResponse(201)

    monkeypatch.setattr(accounts.urllib.request, "urlopen", fake_urlopen)
    store = accounts.SupabaseStore("https://abc.supabase.co/", "sb_secret_xyz")
    assert store.create("visitor1", "scrypt:abc") is True
    method, url, headers, data, timeout = seen[0]
    assert (method, url) == ("POST", "https://abc.supabase.co/rest/v1/viewer_accounts")
    assert json.loads(data) == {"username": "visitor1", "password_hash": "scrypt:abc"}
    assert headers["Apikey"] == "sb_secret_xyz" and "Authorization" not in headers and timeout == store.timeout
    assert store.get_hash("visitor1") == "scrypt:abc"
    assert seen[1][1].endswith("/viewer_accounts?username=eq.visitor1&select=password_hash&limit=1")
    legacy = accounts.SupabaseStore("https://abc.supabase.co", "aaa.bbb.ccc")
    assert legacy._headers()["Authorization"] == "Bearer aaa.bbb.ccc"


def test_supabase_store_reports_a_taken_name_and_failures(monkeypatch):
    accounts = load(monkeypatch).accounts
    store = accounts.SupabaseStore("https://abc.supabase.co", "sb_secret_xyz")

    def respond(error):
        def fake(request, timeout):
            raise error
        return fake

    monkeypatch.setattr(accounts.urllib.request, "urlopen", respond(urllib.error.HTTPError("u", 409, "conflict", {}, None)))
    assert store.create("visitor1", "h") is False
    monkeypatch.setattr(accounts.urllib.request, "urlopen", respond(urllib.error.HTTPError("u", 500, "boom", {}, None)))
    with pytest.raises(accounts.StoreError, match="HTTP 500"):
        store.get_hash("visitor1")
    monkeypatch.setattr(accounts.urllib.request, "urlopen", respond(urllib.error.URLError("no route")))
    with pytest.raises(accounts.StoreError, match="could not be reached"):
        store.get_hash("visitor1")


def test_the_store_is_only_built_from_a_complete_https_configuration(monkeypatch):
    accounts = load(monkeypatch).accounts
    assert accounts.store_from_environment({}) is None
    assert accounts.store_from_environment({"SUPABASE_URL": "https://a.supabase.co"}) is None
    assert accounts.store_from_environment({"SUPABASE_URL": "http://a.supabase.co", "SUPABASE_SECRET_KEY": "k"}) is None
    assert accounts.store_from_environment({"SUPABASE_URL": "https://a.supabase.co", "SUPABASE_SECRET_KEY": "k"}) is not None
