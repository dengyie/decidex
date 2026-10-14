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