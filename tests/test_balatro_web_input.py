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
            return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def _login(base, password=PASSWORD, user=USER):
    """Log in and return the session cookie header value."""
    code, _, headers = _post(base, "/api/login", {}, password=password, user=user)
    assert code == 200, f"login failed: {code}"
    set_cookie = headers.get("Set-Cookie", "")
    return set_cookie.split(";", 1)[0]


# ---- pure helpers -----------------------------------------------------------

def test_click_args_single():
    assert app._click_args(100, 200) == [
        "mousemove", "100", "200", "sleep", "0.05", "mousedown", "1", "sleep", "0.05", "mouseup", "1"
    ]


def test_click_args_double():
    assert app._click_args(100, 200, double=True) == [
        "mousemove", "100", "200", "sleep", "0.05",
        "mousedown", "1", "sleep", "0.05", "mouseup", "1",
        "sleep", "0.05",
        "mousedown", "1", "sleep", "0.05", "mouseup", "1",
    ]


def test_click_args_clamps_out_of_range():
    # bounds track the game screen (BALATRO_GAME_W/H default 960x600)
    assert app._click_args(-5, 99999) == [
        "mousemove", "0", "599", "sleep", "0.05", "mousedown", "1", "sleep", "0.05", "mouseup", "1"
    ]
    assert app._click_args(99999, -5) == [
        "mousemove", "959", "0", "sleep", "0.05", "mousedown", "1", "sleep", "0.05", "mouseup", "1"
    ]


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
    assert captured == [[
        "mousemove", "640", "400", "sleep", "0.05", "mousedown", "1", "sleep", "0.05", "mouseup", "1"
    ]]


def test_double_click_sends_repeat(server):
    base, captured = server
    _post(base, "/api/input/click", {"x": 5, "y": 5, "double": True}, token=TOKEN)
    assert captured[0] == [
        "mousemove", "5", "5", "sleep", "0.05",
        "mousedown", "1", "sleep", "0.05", "mouseup", "1",
        "sleep", "0.05",
        "mousedown", "1", "sleep", "0.05", "mouseup", "1",
    ]


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


# ---- routing (query string strip) -------------------------------------------

def _get(base, path, cookie=None):
    headers = {}
    if cookie is not None:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_login_page_served_with_query_string(server):
    """The 401 flow redirects to /login?next=%2F; a query string must not 404."""
    base, _ = server
    code, body, headers = _get(base, "/login?next=%2F")
    assert code == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"loginForm" in body


def test_index_served_with_query_string(server):
    base, _ = server
    code, body, _ = _get(base, "/?x=1")
    assert code == 200 and b"__GAME_DIMS__" not in body


# ---- open-redirect guard (_safe_next_path) ----------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("/", "/"),
    ("/play", "/play"),
    ("/a/b?c=d", "/a/b?c=d"),
    (None, "/"),
    ("", "/"),
    ("https://evil.example", "/"),
    ("//evil.example", "/"),
    ("/\\evil.example", "/"),
    ("\\\\evil.example", "/"),
    ("javascript:alert(1)", "/"),
    ("/<script>", "/"),             # decoded form (parse_qs decodes %3C etc.)
    ("</script>", "/"),
    ("/a\tb", "/"),
    ("/a\nb", "/"),
    ("/ok<script>", "/"),
])
def test_safe_next_path(raw, expected):
    assert app._safe_next_path(raw) == expected


def test_login_page_injects_sanitized_next(server):
    """A hostile next must be neutralized in the served page, not echoed."""
    base, _ = server
    code, body, _ = _get(base, "/login?next=https://evil.example")
    assert code == 200
    text = body.decode()
    assert "evil.example" not in text
    assert "const next = \"/\";" in text


def test_login_page_keeps_safe_next(server):
    base, _ = server
    code, body, _ = _get(base, "/login?next=%2Fplay")
    assert code == 200 and 'const next = "/play";' in body.decode()


def test_login_page_never_leaks_placeholder(server):
    base, _ = server
    _, body, _ = _get(base, "/login")
    assert "__NEXT_PATH__" not in body.decode()


def test_login_page_rejects_encoded_injection(server):
    """%3C%2Fscript%3E decodes to </script>; it must not survive into the page."""
    base, _ = server
    code, body, _ = _get(base, "/login?next=%2F%3C%2Fscript%3E")
    assert code == 200
    assert b"</script>" not in body.split(b"const next =", 1)[1].split(b";", 1)[0]
    assert 'const next = "/";' in body.decode()


# ---- login session ----------------------------------------------------------

def test_login_sets_session_cookie_and_authorizes(server_pw):
    base, captured = server_pw
    cookie = _login(base, PASSWORD)
    assert app._SESSION_COOKIE in cookie
    # No X-Input-Token at all — the session cookie alone must authorize.
    code, body, _ = _post(base, "/api/input/click", {"x": 8, "y": 9}, cookie=cookie)
    assert code == 200 and body["ok"] is True
    assert captured == [[
        "mousemove", "8", "9", "sleep", "0.05", "mousedown", "1", "sleep", "0.05", "mouseup", "1"
    ]]


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
    assert captured == [[
        "mousemove", "7", "7", "sleep", "0.05", "mousedown", "1", "sleep", "0.05", "mouseup", "1"
    ]]


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

# ---- SSO integration tests --------------------------------------------------

def test_sso_cookie_authenticates_session_and_input(server_pw, monkeypatch):
    base, captured = server_pw
    secret = "test-sso-secret-xyz"
    monkeypatch.setattr(app, "_SSO_SECRET", secret)
    sso_tok = app._create_sso_token("mango_operator", secret=secret)
    sso_cookie = f"{app._SSO_COOKIE}={sso_tok}"

    # Test /api/session
    code, raw_body, _ = _get(base, "/api/session", cookie=sso_cookie)
    body = json.loads(raw_body.decode())
    assert code == 200
    assert body.get("authenticated") is True
    assert body.get("user") == "mango_operator"
    assert body.get("sso") is True

    # Test /api/input/click
    code, _, _ = _post(base, "/api/input/click", {"x": 10, "y": 20}, cookie=sso_cookie)
    assert code == 200
    assert len(captured) == 1


def test_sso_tampered_or_expired_cookie_rejected(server_pw, monkeypatch):
    base, captured = server_pw
    secret = "test-sso-secret-xyz"
    monkeypatch.setattr(app, "_SSO_SECRET", secret)
    
    # Tampered token
    tampered_cookie = f"{app._SSO_COOKIE}=invalid.signature"
    code, raw_body, _ = _get(base, "/api/session", cookie=tampered_cookie)
    body = json.loads(raw_body.decode())
    assert code == 200
    assert body.get("authenticated") is False

    code, _, _ = _post(base, "/api/input/click", {"x": 10, "y": 20}, cookie=tampered_cookie)
    assert code == 401
    assert captured == []

    # Expired token
    expired_tok = app._create_sso_token("mango", secret=secret, ttl=-10)
    expired_cookie = f"{app._SSO_COOKIE}={expired_tok}"
    code, raw_body, _ = _get(base, "/api/session", cookie=expired_cookie)
    body = json.loads(raw_body.decode())
    assert code == 200
    assert body.get("authenticated") is False


def test_sso_logout_drops_both_cookies(server_pw, monkeypatch):
    base, _ = server_pw
    secret = "test-sso-secret-xyz"
    monkeypatch.setattr(app, "_SSO_SECRET", secret)
    sso_tok = app._create_sso_token("mango", secret=secret)
    sso_cookie = f"{app._SSO_COOKIE}={sso_tok}"

    code, body, headers = _post(base, "/api/logout", {}, cookie=sso_cookie)
    assert code == 200
    assert body.get("ok") is True
    # Verify multiple Set-Cookie headers in response
    cookies_header = headers.get_all("Set-Cookie") if hasattr(headers, "get_all") else [headers.get("Set-Cookie")]
    cookies_str = " ".join(str(c) for c in cookies_header)
    assert "balatro_session=" in cookies_str
    assert "mango_game_session=" in cookies_str


# ---- /api/execute direct game control tests ---------------------------------

def test_execute_unauthorized_requires_auth(server):
    base, _ = server
    code, body, _ = _post(base, "/api/execute", {"action": "play"})
    assert code == 401
    assert body.get("error") == "unauthorized"


def test_execute_requires_csrf_with_session_cookie(server_pw):
    base, _ = server_pw
    cookie = _login(base, PASSWORD)
    code, body, _ = _post(base, "/api/execute", {"action": "play"}, cookie=cookie, csrf=False)
    assert code == 403
    assert body.get("error") == "csrf"


def test_execute_card_and_shop_actions(server, monkeypatch):
    base, _ = server
    dispatched = []
    monkeypatch.setattr(app, "_dispatch_game_action",
                        lambda act, args=None: dispatched.append((act, args)) or True)

    # 1. toggle_card
    code, body, _ = _post(base, "/api/execute", {"action": "toggle_card", "index": 2}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("toggle_card", {"index": 2})

    # toggle_card missing index
    code, body, _ = _post(base, "/api/execute", {"action": "toggle_card"}, token=TOKEN)
    assert code == 200 and body["ok"] is False and "requires index" in body["error"]

    # 2. select_cards
    code, body, _ = _post(base, "/api/execute", {"action": "select_cards", "indices": [0, 2, 4]}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("select_cards", {"indices": [0, 2, 4]})

    # 3. unhighlight_all
    code, body, _ = _post(base, "/api/execute", {"action": "unhighlight_all"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("unhighlight_all", None)

    # 4. play with and without indices
    code, body, _ = _post(base, "/api/execute", {"action": "play", "indices": [1, 3]}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("play", {"indices": [1, 3]})

    code, body, _ = _post(base, "/api/execute", {"action": "play"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("play", {})

    # 5. discard with and without indices
    code, body, _ = _post(base, "/api/execute", {"action": "discard", "indices": [0]}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("discard", {"indices": [0]})

    code, body, _ = _post(base, "/api/execute", {"action": "discard"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("discard", {})

    # 6. sort_rank and sort_suit
    code, body, _ = _post(base, "/api/execute", {"action": "sort_rank"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("sort_rank", None)

    code, body, _ = _post(base, "/api/execute", {"action": "sort_suit"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("sort_suit", None)

    # 7. reroll_shop
    code, body, _ = _post(base, "/api/execute", {"action": "reroll_shop"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("reroll_shop", None)

    # 8. buy_card
    code, body, _ = _post(base, "/api/execute", {"action": "buy_card", "index": 1, "area": "shop_jokers"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("buy_card", {"index": 1, "area": "shop_jokers"})

    # 9. use_card and sell_card
    code, body, _ = _post(base, "/api/execute", {"action": "use_card", "index": 0, "area": "consumables"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("use_card", {"index": 0, "area": "consumables"})

    code, body, _ = _post(base, "/api/execute", {"action": "sell_card", "index": 0, "area": "jokers"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert dispatched[-1] == ("sell_card", {"index": 0, "area": "jokers"})

    # 10. stage transitions
    for stage_act in ("select_blind", "cash_out", "toggle_shop", "start_run"):
        code, body, _ = _post(base, "/api/execute", {"action": stage_act}, token=TOKEN)
        assert code == 200 and body["ok"] is True
        assert dispatched[-1] == (stage_act, None)


def test_execute_rejects_unknown_action_without_auto_play(server, monkeypatch):
    base, _ = server
    dispatched = []
    monkeypatch.setattr(app, "_dispatch_game_action",
                        lambda act, args=None: dispatched.append((act, args)) or True)

    code, body, _ = _post(base, "/api/execute", {"action": "invalid_action_foo"}, token=TOKEN)
    assert code == 200
    assert body.get("ok") is False
    assert "unrecognized action_type" in body.get("error", "")
    assert len(dispatched) == 0


def test_execute_rejects_invalid_area_or_index(server, monkeypatch):
    base, _ = server
    dispatched = []
    monkeypatch.setattr(app, "_dispatch_game_action",
                        lambda act, args=None: dispatched.append((act, args)) or True)

    # Invalid area (injection attempt)
    code, body, _ = _post(base, "/api/execute", {
        "action": "buy_card",
        "index": 0,
        "area": "'); os.execute('whoami'); --"
    }, token=TOKEN)
    assert code == 200
    assert body.get("ok") is False
    assert "invalid area" in body.get("error", "")
    assert len(dispatched) == 0

    # Negative index
    code, body, _ = _post(base, "/api/execute", {
        "action": "toggle_card",
        "index": -5
    }, token=TOKEN)
    assert code == 200
    assert body.get("ok") is False
    assert "non-negative" in body.get("error", "")
    assert len(dispatched) == 0

    # String index
    code, body, _ = _post(base, "/api/execute", {
        "action": "toggle_card",
        "index": "not_an_int"
    }, token=TOKEN)
    assert code == 200
    assert body.get("ok") is False
    assert "integer" in body.get("error", "")
    assert len(dispatched) == 0


def test_dispatch_game_action_direct_sanitization_and_offset(tmp_path, monkeypatch):
    test_eval_file = tmp_path / "balatro_eval.lua"
    monkeypatch.setattr(app, "Path", lambda p: test_eval_file if "balatro_eval" in str(p) else Path(p))

    # 1. Valid action and index conversion from 0-based to 1-based
    ok = app._dispatch_game_action("toggle_card", {"index": 0})
    assert ok is True
    assert test_eval_file.is_file()
    content = test_eval_file.read_text(encoding="utf-8")
    assert "G.DECIDEX_EXECUTE('toggle_card'" in content
    assert "index = 1" in content
    assert "indices = nil" in content

    # 2. Valid indices conversion
    ok = app._dispatch_game_action("select_cards", {"indices": [0, 3, 4]})
    assert ok is True
    content = test_eval_file.read_text(encoding="utf-8")
    assert "indices = {1,4,5}" in content

    # 3. Valid area
    ok = app._dispatch_game_action("buy_card", {"index": 1, "area": "shop_jokers"})
    assert ok is True
    content = test_eval_file.read_text(encoding="utf-8")
    assert "area = 'shop_jokers'" in content

    # 4. Reject invalid action
    ok = app._dispatch_game_action("malicious_action", {"index": 0})
    assert ok is False

    # 5. Reject invalid area (injection)
    ok = app._dispatch_game_action("buy_card", {"index": 0, "area": "evil_area"})
    assert ok is False


def test_balatro_state_preserves_runtime_fields():
    from decidex.balatro.state import BalatroState, Card

    raw = {
        "hand": [{"rank": "A", "suit": "S", "highlighted": True}],
        "state_name": "SHOP",
        "shop": {"cards": [{"name": "Joker", "cost": 4}], "reroll_cost": 5},
        "hover": "reroll_btn",
        "money": 15,
    }
    state = BalatroState.from_dict(raw)
    assert state.state_name == "SHOP"
    assert state.shop == {"cards": [{"name": "Joker", "cost": 4}], "reroll_cost": 5}
    assert state.hover == "reroll_btn"
    assert len(state.hand) == 1
    assert state.hand[0].highlighted is True

    out = state.to_dict()
    assert out["state_name"] == "SHOP"
    assert out["shop"] == {"cards": [{"name": "Joker", "cost": 4}], "reroll_cost": 5}
    assert out["hover"] == "reroll_btn"
    assert out["hand"][0]["highlighted"] is True


def test_load_state_retries_on_json_decode_error(tmp_path, monkeypatch):
    import json
    from decidex.balatro.state import load_state

    p = tmp_path / "test_state.json"
    p.write_text('{"hand": []}', encoding="utf-8")

    attempts = 0
    orig_open = open

    def mock_open(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # First attempt fails with truncated JSON
            import io
            return io.StringIO('{"hand": [')
        return orig_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", mock_open)
    st = load_state(p)
    assert len(st.hand) == 0
    assert attempts == 2


