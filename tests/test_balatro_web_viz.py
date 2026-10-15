"""Contract test: /api/recommend must serve the visualization report.

The viewers draw whatever `viz` says, so the shape of that payload is the
interface between `decidex.viz` and the two HTML pages. This drives the real
handler end-to-end (real advisor, mock provider) rather than asserting on the
model in isolation.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "tools" / "balatro_web" / "app.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "balatro"

spec = importlib.util.spec_from_file_location("balatro_web_app_viz", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

USER = "operator"
PASSWORD = "operator-pw"


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Live handler on a random port, bridge dir pointed at the fixtures.

    The app serves async work onto a persistent loop it starts in main(); the
    tests drive the handler directly, so they start it themselves.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(app, "_LOOP", loop)
    threading.Thread(target=loop.run_forever, daemon=True).start()

    bridge = tmp_path / "bridge"
    bridge.mkdir()
    for f in sorted(FIXTURES.glob("state-*.json"))[:3]:
        (bridge / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(app, "BRIDGE_DIR", bridge)
    monkeypatch.setattr(app, "WEB_USER", USER)
    monkeypatch.setattr(app, "WEB_PASSWORD", PASSWORD)
    monkeypatch.setattr(app, "VNC_PASSWORD", "tok")
    monkeypatch.setattr(app, "_xdotool", lambda args: "")
    app._SESSIONS.clear()
    app._CACHE.clear()
    app._ADVISORS.clear()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base, sorted(f.name for f in bridge.glob("*.json"))[0]
    finally:
        srv.shutdown()
        srv.server_close()
        loop.call_soon_threadsafe(loop.stop)
        app._SESSIONS.clear()


def _login(base):
    """Log in and return the session cookie; login itself is a mutation, so it
    needs the same X-Requested-With header the page sends."""
    import urllib.request
    req = urllib.request.Request(
        base + "/api/login",
        data=json.dumps({"user": USER, "password": PASSWORD}).encode(),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "decidex-web"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return r.headers.get("Set-Cookie", "").split(";")[0]


def _post(base, path, body, cookie=""):
    import urllib.request
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "decidex-web",
                 **({"Cookie": cookie} if cookie else {})},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def test_recommend_returns_viz_report(server):
    base, name = server
    cookie = _login(base)
    out = _post(base, "/api/recommend", {"name": name, "kind": "heuristic"}, cookie)

    assert "viz" in out and out["viz"] is not None
    report = out["viz"]
    assert set(report) >= {"visual", "tokens", "lint", "thresholds"}

    # visual: the recommendation is a CHOICE question, so we get option bars
    visual = report["visual"]
    assert visual["kind"] == "choice"
    assert visual["bars"], "choice visual must carry option bars"
    assert visual["bars"][0]["winner"] is True
    # the advised candidate must be marked on the bar list
    assert any(b["selected"] for b in visual["bars"])
    assert visual["confidence"]["tier"] in ("high", "medium", "low")
    assert visual["confidence"]["thresholds"]["high"] == report["thresholds"]["confidence_high"]

    # bars must be ordered descending
    ratios = [b["ratio"] for b in visual["bars"]]
    assert ratios == sorted(ratios, reverse=True)

    # tokens: a real budget report for the evidence we just sent
    tokens = report["tokens"]
    assert tokens["total"] > 0
    assert tokens["binding"]["kind"] in ("total", "state_plus_longest")
    assert "level" in tokens["binding"]

    # lint: list + counts
    assert isinstance(report["lint"]["issues"], list)
    assert report["lint"]["summary"]["total"] == len(report["lint"]["issues"])


def test_recommend_viz_survives_provider_failure(server, monkeypatch):
    """A provider blowing up yields a fallback verdict; the viewer must still
    get a report (bars may be empty) rather than a 500 or a missing key."""
    base, name = server
    cookie = _login(base)

    def _boom(*a, **kw):
        raise RuntimeError("provider down")

    advisor = app.get_advisor("heuristic")
    monkeypatch.setattr(advisor.provider, "infer", _boom, raising=False)
    out = _post(base, "/api/recommend", {"name": name, "kind": "heuristic"}, cookie)

    assert out["is_fallback"] is True
    assert out["viz"] is not None
    codes = {i["code"] for i in out["viz"]["lint"]["issues"]}
    assert "verdict_is_fallback" in codes


def test_viz_assets_are_served_and_not_placeholder(server):
    import urllib.request
    base, _ = server
    for path, needle in (("/viz.js", "DecidexViz"), ("/viz.css", ".vz-badge")):
        with urllib.request.urlopen(base + path) as r:
            body = r.read().decode("utf-8")
        assert r.status == 200
        assert needle in body


def test_missing_shared_asset_is_404_not_500(server, monkeypatch):
    base, _ = server
    import urllib.request
    import urllib.error
    monkeypatch.setattr(app, "SHARED_DIR", Path("/nonexistent/shared"))
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/viz.js")
    assert exc.value.code == 404


def test_api_game_live_and_highlighted(server, tmp_path):
    import urllib.request
    base, name = server
    with urllib.request.urlopen(base + "/api/game") as r:
        payload = json.loads(r.read().decode("utf-8"))
    assert payload["_is_live"] is True
    assert "hand" in payload
    assert isinstance(payload["hand"], list)

    # Now write a distinct live_state.json with highlighted card, state_name and shop
    live_file = app.BRIDGE_DIR / "live_state.json"
    dummy_state = {
        "hand": [{"rank": "A", "suit": "S", "enhancement": "bonus", "highlighted": True}],
        "blind": {"name": "Small Blind", "type": "small", "chips": 300, "scored": 50},
        "jokers": [],
        "consumables": [],
        "hands_left": 3,
        "discards_left": 2,
        "money": 10,
        "ante": 2,
        "round": 3,
        "state_name": "SHOP",
        "shop": {"cards": [{"name": "Joker", "cost": 4}], "reroll_cost": 5},
    }
    live_file.write_text(json.dumps(dummy_state), encoding="utf-8")

    with urllib.request.urlopen(base + "/api/game?name=live") as r:
        live_payload = json.loads(r.read().decode("utf-8"))
    assert live_payload["_is_live"] is True
    assert live_payload["money"] == 10
    assert live_payload["state_name"] == "SHOP"
    assert live_payload["shop"]["reroll_cost"] == 5
    assert live_payload["hand"][0]["rank"] == "A"
    assert live_payload["hand"][0]["highlighted"] is True


def test_execute_endpoint(server, monkeypatch, tmp_path):
    base, _ = server
    cookie = _login(base)

    # Test executing a specific action (play / discard)
    dispatched = []
    monkeypatch.setattr(app, "_dispatch_game_action", lambda act, args=None: (dispatched.append((act, args)) or True))

    out = _post(base, "/api/execute", {"action_type": "play", "indices": [0, 1, 2]}, cookie)
    assert out["ok"] is True
    assert out["executed"] is True
    assert len(dispatched) == 1
    assert dispatched[0][0] == "play"
    assert dispatched[0][1] == {"indices": [0, 1, 2]}

    # Test auto execute on live state
    live_file = app.BRIDGE_DIR / "live_state.json"
    dummy_state = {
        "state_name": "SELECTING_HAND",
        "hand": [{"rank": "A", "suit": "S"}, {"rank": "K", "suit": "H"}, {"rank": "Q", "suit": "D"}],
        "blind": {"name": "Small Blind", "type": "small", "chips": 300, "scored": 0},
        "jokers": [],
        "consumables": [],
        "hands_left": 4,
        "discards_left": 3,
        "money": 4,
        "ante": 1,
        "round": 1,
    }
    live_file.write_text(json.dumps(dummy_state), encoding="utf-8")

    out_auto = _post(base, "/api/execute", {"action_type": "auto"}, cookie)
    assert out_auto["ok"] is True
    assert out_auto["executed"] is True
    assert out_auto["action_type"] in ("play", "discard")
    assert len(dispatched) == 2