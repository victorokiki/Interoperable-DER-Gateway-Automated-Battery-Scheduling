"""
tests/test_public_mode.py
PUBLIC_MODE: the dashboard copy that is reachable from the internet.

Only the dashboard (and its APIs, downloads and static files) may answer to the world. The
machine endpoints (the IEEE 2030.5 server, /simulate/*, /status) answer only to the gateway
on the same server, and the server refuses to start with the published demo password.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INTERNET = {"REMOTE_ADDR": "203.0.113.9"}
INTERNAL = {"SERVER_PORT": "5001"}                     # the localhost-only port the gateway uses
GATEWAY = INTERNAL
STRONG = "a-long-new-password-1"


def load(monkeypatch, public: bool, password: str = STRONG, secret: bool = True):
    monkeypatch.setenv("PUBLIC_MODE", "1" if public else "0")
    monkeypatch.setenv("DASHBOARD_PORT", "10000")
    monkeypatch.setenv("INTERNAL_PORT", "5001")
    monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    if secret:
        monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret-key")
    else:
        monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("grid_public_mode_under_test", ROOT / "simulators" / "fake_grid_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module            # as in a normal start, so Flask finds simulators/static
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", ["/simulate/idle", "/simulate/charge", "/simulate/batch", "/simulate/raw", "/simulate/default"])
def test_simulate_endpoints_are_hidden_from_the_internet(monkeypatch, path):
    client = load(monkeypatch, True).app.test_client()
    assert client.post(path, json={"power_kw": 1, "duration_sec": 60}, environ_overrides=INTERNET).status_code == 404


@pytest.mark.parametrize("path", ["/status", "/dcap", "/tm", "/edev", "/derp", "/derp/1/derc", "/mup"])
def test_ieee_endpoints_are_hidden_from_the_internet(monkeypatch, path):
    client = load(monkeypatch, True).app.test_client()
    assert client.get(path, environ_overrides=INTERNET).status_code == 404


def test_the_gateway_on_the_same_server_still_reaches_them(monkeypatch):
    client = load(monkeypatch, True).app.test_client()
    assert client.get("/dcap", environ_overrides=GATEWAY).status_code == 200
    assert client.get("/derp/1/derc", environ_overrides=GATEWAY).status_code == 200
    assert client.get("/status", environ_overrides=GATEWAY).status_code == 200


def test_a_caller_that_looks_local_still_cannot_reach_them_on_the_public_port(monkeypatch):
    """Behind the host's proxy every request can appear to come from 127.0.0.1; only the port counts."""
    client = load(monkeypatch, True).app.test_client()
    looks_local = {"REMOTE_ADDR": "127.0.0.1", "SERVER_PORT": "10000"}
    assert client.get("/status", environ_overrides=looks_local).status_code == 404
    assert client.get("/status", environ_overrides=looks_local, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 404
    assert client.post("/simulate/idle", environ_overrides=looks_local).status_code == 404


def test_the_dashboard_still_works_for_the_world(monkeypatch):
    client = load(monkeypatch, True).app.test_client()
    assert client.get("/", environ_overrides=INTERNET).status_code == 302
    assert client.get("/login", environ_overrides=INTERNET).status_code == 200
    assert client.get("/dashboard", environ_overrides=INTERNET).status_code == 302          # to the login page
    assert client.get("/api/status", environ_overrides=INTERNET).status_code == 401         # its own login check
    assert client.get("/static/invinity-logo.svg", environ_overrides=INTERNET).status_code == 200


def test_login_page_does_not_publish_the_demo_account(monkeypatch):
    html = load(monkeypatch, True).app.test_client().get("/login", environ_overrides=INTERNET).get_data(as_text=True)
    assert "invinity2026" not in html and "Demo account" not in html
    local = load(monkeypatch, False, password="invinity2026").app.test_client().get("/login").get_data(as_text=True)
    assert "Demo account" in local                                                           # unchanged on a developer's PC


def test_login_works_and_failures_are_throttled_per_address(monkeypatch):
    client = load(monkeypatch, True).app.test_client()
    for _ in range(5):
        r = client.post("/login", data={"username": "operator", "password": "wrong"}, environ_overrides=INTERNET)
        assert r.status_code == 401
    blocked = client.post("/login", data={"username": "operator", "password": STRONG}, environ_overrides=INTERNET)
    assert blocked.status_code == 429 and "Too many failed attempts" in blocked.get_data(as_text=True)
    other = client.post("/login", data={"username": "operator", "password": STRONG}, environ_overrides={"REMOTE_ADDR": "198.51.100.4"})
    assert other.status_code == 302                                                          # someone else is not locked out


def test_security_headers_are_sent_in_public_mode(monkeypatch):
    r = load(monkeypatch, True).app.test_client().get("/login", environ_overrides=INTERNET)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_a_public_copy_will_not_start_with_the_published_password(monkeypatch):
    with pytest.raises(SystemExit, match="DASHBOARD_PASSWORD"):
        load(monkeypatch, True, password="invinity2026").check_public_config()
    with pytest.raises(SystemExit, match="DASHBOARD_PASSWORD"):
        load(monkeypatch, True, password="short").check_public_config()
    with pytest.raises(SystemExit, match="DASHBOARD_SECRET_KEY"):
        load(monkeypatch, True, secret=False).check_public_config()
    same_ports = load(monkeypatch, True)
    same_ports.DASHBOARD_PORT = same_ports.INTERNAL_PORT          # the machine endpoints must not share the public port
    with pytest.raises(SystemExit, match="INTERNAL_PORT"):
        same_ports.check_public_config()
    load(monkeypatch, True).check_public_config()                                            # a strong password is fine


def test_a_developers_copy_is_unchanged(monkeypatch):
    grid = load(monkeypatch, False, password="invinity2026", secret=False)
    grid.check_public_config()                                                               # no complaint
    client = grid.app.test_client()
    assert client.get("/dcap").status_code == 200
    assert client.post("/simulate/idle", environ_overrides=INTERNET).status_code == 200      # open, as before


def test_pages_say_secure_when_the_hosts_proxy_ended_https(monkeypatch):
    client = load(monkeypatch, True).app.test_client()
    behind_proxy = client.get("/login", environ_overrides=INTERNET, headers={"X-Forwarded-Proto": "https"}).get_data(as_text=True)
    assert "Secure connection (TLS)" in behind_proxy and "Unencrypted" not in behind_proxy
    plain = client.get("/login", environ_overrides=INTERNET).get_data(as_text=True)
    assert "Unencrypted" in plain                                                            # no header, no claim of security
