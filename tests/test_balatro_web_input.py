"""Tests for the Balatro web viewer's interactive-input layer.

These drive the real HTTP handler with a stubbed `_xdotool` so the argv that
would be sent to the game is asserted directly, plus the auth gate (login
session cookie and the mesh X-Input-Token escape hatch), the CSRF guard, and
the bad-input paths.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parents[1] / "tools" / "balatro_web" / "app.py"

spec = importlib.util.spec_from_file_location("balatro_web_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

TOKEN = "s3cret-token"
USER = "operator"
PASSWORD = "operator-pw"


@pytest.fixture
def server(monkeypatch):
    """Live handler on a random port with xdotool stubbed and a known secret."""
    captured = []
    monkeypatch.setattr(app, "VNC_PASSWORD", TOKEN)
    monkeypatch.setattr(app, "WEB_USER", USER)
    monkeypatch.setattr(app, "WEB_PASSWORD", PASSWORD)
    monkeypatch.setattr(app, "_xdotool",
                        lambda args: captured.append(list(args)) or "")
    app._SESSIONS.clear()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base, captured
    finally:
        srv.shutdown()
        srv.server_close()
        app._SESSIONS.clear()


@pytest.fixture
def server_pw(monkeypatch):
    """Same as `server`, but login creds differ from the raw token so the
    login vs. header paths are distinguishable."""
    captured = []
    monkeypatch.setattr(app, "VNC_PASSWORD", TOKEN)
    monkeypatch.setattr(app, "WEB_USER", USER)
    monkeypatch.setattr(app, "WEB_PASSWORD", PASSWORD)
    monkeypatch.setattr(app, "_xdotool",
                        lambda args: captured.append(list(args)) or "")
    app._SESSIONS.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base, captured
    finally:
        srv.shutdown()
        srv.server_close()
        app._SESSIONS.clear()


def _post(base, path, body, token=None, cookie=None, csrf=True, password=None, user=None):
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-Requested-With"] = "decidex-web"
    if token is not None:
        headers["X-Input-Token"] = token
    if cookie is not None:
        headers["Cookie"] = cookie
    if password is not None:
        body = dict(body, password=password)
    if user is not None:
        body = dict(body, user=user)
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def _login(base, password=PASSWORD, user=USER):
    """Log in and return the session cookie header value."""
    code, _, headers = _post(base, "/api/login", {}, password=password, user=user)
    assert code == 200, f"login failed: {code}"
    set_cookie = headers.get("Set-Cookie", "")
    return set_cookie.split(";", 1)[0]


# ---- pure helpers -----------------------------------------------------------

def test_click_args_single():
    assert app._click_args(100, 200) == ["mousemove", "100", "200", "click", "1"]


def test_click_args_double():
    assert app._click_args(100, 200, double=True) == [
        "mousemove", "100", "200", "click", "--repeat", "2", "1"]


def test_click_args_clamps_out_of_range():
    assert app._click_args(-5, 99999) == ["mousemove", "0", "799", "click", "1"]


def test_key_name_translates_page_keys():
    assert app._key_name("arrowup") == "Up"
    assert app._key_name("enter") == "Return"
    assert app._key_name("space") == "space"
    assert app._key_name("a") == "a"


def test_xdotool_failed_detects_errors():
    assert app._xdotool_failed("error: xdotool not installed")
    assert app._xdotool_failed("(symbol) No such key name 'ArrowUp'. Ignoring it.")
    assert not app._xdotool_failed("")


# ---- HTTP behaviour ---------------------------------------------------------

def test_click_requires_auth(server):
    base, captured = server
    code, _, _ = _post(base, "/api/input/click", {"x": 10, "y": 20})
    assert code == 401
    assert captured == []


def test_click_rejects_wrong_token(server):
    base, captured = server
    code, _, _ = _post(base, "/api/input/click", {"x": 10, "y": 20}, token="nope")
    assert code == 401
    assert captured == []


def test_click_with_token_moves_and_clicks(server):
    base, captured = server
    code, body, _ = _post(base, "/api/input/click", {"x": 640, "y": 400}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert captured == [["mousemove", "640", "400", "click", "1"]]


def test_double_click_sends_repeat(server):
    base, captured = server
    _post(base, "/api/input/click", {"x": 5, "y": 5, "double": True}, token=TOKEN)
    assert captured[0][-3:] == ["--repeat", "2", "1"]


def test_click_bad_coords_is_400_not_500(server):
    base, captured = server
    code, _, _ = _post(base, "/api/input/click", {"x": "abc", "y": None}, token=TOKEN)
    assert code == 400
    assert captured == []


def test_key_arrow_is_translated(server):
    base, captured = server
    code, body, _ = _post(base, "/api/input/key", {"key": "arrowleft"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert captured == [["key", "Left"]]


def test_key_rejects_injection(server):
    base, captured = server
    code, _, _ = _post(base, "/api/input/key", {"key": "a; rm -rf /"}, token=TOKEN)
    assert code == 400
    assert captured == []


def test_key_reports_xdotool_failure(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(app, "_xdotool",
                        lambda args: "(symbol) No such key name 'Bogus'. Ignoring it.")
    code, body, _ = _post(base, "/api/input/key", {"key": "bogus"}, token=TOKEN)
    assert code == 200 and body["ok"] is False


# ---- login session ----------------------------------------------------------

def test_login_sets_session_cookie_and_authorizes(server_pw):
    base, captured = server_pw
    cookie = _login(base, PASSWORD)
    assert app._SESSION_COOKIE in cookie
    # No X-Input-Token at all — the session cookie alone must authorize.
    code, body, _ = _post(base, "/api/input/click", {"x": 8, "y": 9}, cookie=cookie)
    assert code == 200 and body["ok"] is True
    assert captured == [["mousemove", "8", "9", "click", "1"]]


def test_login_wrong_password_is_401(server_pw):
    base, captured = server_pw
    code, _, headers = _post(base, "/api/login", {}, password="wrong")
    assert code == 401
    assert "Set-Cookie" not in headers
    assert captured == []


def test_login_wrong_user_is_401(server_pw):
    base, captured = server_pw
    code, _, headers = _post(base, "/api/login", {}, password=PASSWORD, user="someone-else")
    assert code == 401
    assert "Set-Cookie" not in headers
    assert captured == []


def test_login_missing_user_is_401(server_pw):
    base, _ = server_pw
    code, _, _ = _post(base, "/api/login", {}, password=PASSWORD)
    assert code == 401


def test_default_credentials_are_mango_12345678(monkeypatch):
    """The shipped defaults must be the documented pair (no env override)."""
    monkeypatch.delenv("BALATRO_WEB_USER", raising=False)
    monkeypatch.delenv("BALATRO_WEB_PASSWORD", raising=False)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.WEB_USER == "mango"
    assert mod.WEB_PASSWORD == "12345678"
    assert mod._check_credentials("mango", "12345678")
    assert not mod._check_credentials("mango", "nope")
    assert not mod._check_credentials("root", "12345678")


def test_login_requires_csrf_header(server_pw):
    base, _ = server_pw
    code, _, headers = _post(base, "/api/login", {}, password=PASSWORD, csrf=False)
    assert code == 403
    assert "Set-Cookie" not in headers


def test_input_requires_csrf_header(server_pw):
    """A logged-in session alone is not enough: cross-site form posts carry the
    cookie but cannot set X-Requested-With, so mutations must be refused."""
    base, captured = server_pw
    cookie = _login(base, PASSWORD)
    code, _, _ = _post(base, "/api/input/click", {"x": 1, "y": 2}, cookie=cookie, csrf=False)
    assert code == 403
    assert captured == []


def test_token_works_without_csrf_header(server):
    """The mesh escape hatch is an explicit header, not ambient browser
    credentials, so it must not require X-Requested-With."""
    base, captured = server
    code, body, _ = _post(base, "/api/input/click", {"x": 7, "y": 7}, token=TOKEN, csrf=False)
    assert code == 200 and body["ok"] is True
    assert captured == [["mousemove", "7", "7", "click", "1"]]


def test_logout_invalidates_session(server_pw):
    base, captured = server_pw
    cookie = _login(base, PASSWORD)
    code, _, headers = _post(base, "/api/logout", {}, cookie=cookie)
    assert code == 200
    assert "Max-Age=0" in headers.get("Set-Cookie", "")
    code, _, _ = _post(base, "/api/input/click", {"x": 3, "y": 4}, cookie=cookie)
    assert code == 401
    assert captured == []


def test_session_endpoint_reports_state(server_pw):
    base, _ = server_pw
    req = urllib.request.Request(base + "/api/session")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read())["authenticated"] is False
    cookie = _login(base, PASSWORD)
    req = urllib.request.Request(base + "/api/session", headers={"Cookie": cookie})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read())["authenticated"] is True


def test_session_cookie_is_httponly_samesite_strict(server_pw):
    base, _ = server_pw
    code, _, headers = _post(base, "/api/login", {}, password=PASSWORD, user=USER)
    set_cookie = headers.get("Set-Cookie", "")
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie


def test_expired_session_is_rejected(server_pw, monkeypatch):
    base, captured = server_pw
    cookie = _login(base, PASSWORD)
    token = cookie.split("=", 1)[1]
    with app._SESSIONS_LOCK:
        app._SESSIONS[token] = 0.0          # force-expire
    code, _, _ = _post(base, "/api/input/click", {"x": 1, "y": 1}, cookie=cookie)
    assert code == 401
    assert captured == []


# ---- provider selection -----------------------------------------------------

def test_advisor_cached_per_kind():
    """Choosing the real model must not be pinned to a prior heuristic advisor."""
    app._ADVISORS.clear()
    seen = []

    def fake_build(kind):
        seen.append(kind)
        return object()

    orig = app._build_provider
    app._build_provider = fake_build
    try:
        app.get_advisor("heuristic")
        app.get_advisor("model")
        app.get_advisor("heuristic")   # cached, not rebuilt
    finally:
        app._build_provider = orig
        app._ADVISORS.clear()
    assert seen == ["heuristic", "model"]