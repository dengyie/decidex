"""Tests for the Balatro web viewer's interactive-input layer.

These drive the real HTTP handler with a stubbed `_xdotool` so the argv that
would be sent to the game is asserted directly, plus the token gate and the
bad-input paths.
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


@pytest.fixture
def server(monkeypatch):
    """Live handler on a random port with xdotool stubbed and a known token."""
    captured = []
    monkeypatch.setattr(app, "VNC_PASSWORD", TOKEN)
    monkeypatch.setattr(app, "_xdotool",
                        lambda args: captured.append(list(args)) or "")

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


def _post(base, path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Input-Token"] = token
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


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

def test_click_requires_token(server):
    base, captured = server
    code, body = _post(base, "/api/input/click", {"x": 10, "y": 20})
    assert code == 401
    assert captured == []


def test_click_rejects_wrong_token(server):
    base, captured = server
    code, _ = _post(base, "/api/input/click", {"x": 10, "y": 20}, token="nope")
    assert code == 401
    assert captured == []


def test_click_with_token_moves_and_clicks(server):
    base, captured = server
    code, body = _post(base, "/api/input/click", {"x": 640, "y": 400}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert captured == [["mousemove", "640", "400", "click", "1"]]


def test_double_click_sends_repeat(server):
    base, captured = server
    _post(base, "/api/input/click", {"x": 5, "y": 5, "double": True}, token=TOKEN)
    assert captured[0][-3:] == ["--repeat", "2", "1"]


def test_click_bad_coords_is_400_not_500(server):
    base, captured = server
    code, _ = _post(base, "/api/input/click", {"x": "abc", "y": None}, token=TOKEN)
    assert code == 400
    assert captured == []


def test_key_arrow_is_translated(server):
    base, captured = server
    code, body = _post(base, "/api/input/key", {"key": "arrowleft"}, token=TOKEN)
    assert code == 200 and body["ok"] is True
    assert captured == [["key", "Left"]]


def test_key_rejects_injection(server):
    base, captured = server
    code, _ = _post(base, "/api/input/key", {"key": "a; rm -rf /"}, token=TOKEN)
    assert code == 400
    assert captured == []


def test_key_reports_xdotool_failure(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(app, "_xdotool",
                        lambda args: "(symbol) No such key name 'Bogus'. Ignoring it.")
    code, body = _post(base, "/api/input/key", {"key": "bogus"}, token=TOKEN)
    assert code == 200 and body["ok"] is False


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