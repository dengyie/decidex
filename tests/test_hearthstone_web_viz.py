"""Contract test: the Hearthstone viewer serves the same visualization report.

Both viewers render `viz.py`'s output through tools/web_shared/viz.js, so this
asserts the second producer honours the same shape as the Balatro one — that is
what makes the shared component real rather than aspirational.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "tools" / "hearthstone_web" / "app.py"

spec = importlib.util.spec_from_file_location("hearthstone_web_app_viz", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

pytestmark = pytest.mark.skipif(not app.HSLOG, reason="hslog unavailable: " + app.HSLOG_ERR)


def _recommend_for_fixture():
    """First decision point of the first replay log that has one."""
    from decidex.hearthstone.perception import load_game_snapshots
    for log in sorted(Path(app.REPLAY_DIR).glob("*.log")):
        try:
            snaps = app.get_snapshots(log.name)
        except Exception:
            continue
        if snaps:
            return log.name, 0, snaps[0]
    return None


def test_hearthstone_recommend_carries_viz_report():
    got = _recommend_for_fixture()
    if got is None:
        pytest.skip("no replay logs with decision points available")
    log, idx, snap = got

    out = app.run_async(app._recommend_one(snap, "heuristic"), timeout=60.0)

    assert out.get("error") is None, out.get("error")
    assert out["viz"] is not None
    report = out["viz"]
    assert set(report) >= {"visual", "tokens", "lint", "thresholds"}
    assert report["visual"]["kind"] == "choice"
    assert report["visual"]["bars"], "choice visual must carry option bars"
    assert report["visual"]["confidence"]["tier"] in ("high", "medium", "low")
    assert report["tokens"]["total"] > 0
    assert isinstance(report["lint"]["issues"], list)
    assert report["lint"]["summary"]["total"] == len(report["lint"]["issues"])


def test_hearthstone_report_matches_balatro_contract_keys():
    """Both producers must emit the identical top-level contract, or the shared
    renderer breaks on one page and not the other."""
    from decidex.viz import build_report

    reference = build_report({"a": 1}, {"q": "y"}, primitive="choice",
                             distribution={"a": 1.0}, selected="a")
    got = _recommend_for_fixture()
    if got is None:
        pytest.skip("no replay logs with decision points available")
    _, _, snap = got
    out = app.run_async(app._recommend_one(snap, "heuristic"), timeout=60.0)
    assert set(out["viz"]) == set(reference)
    assert set(out["viz"]["visual"]) >= {"kind", "bars", "confidence", "stats"}


def test_hearthstone_shared_assets_are_served():
    import urllib.request
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for path, needle in (("/viz.js", "DecidexViz"), ("/viz.css", ".vz-badge")):
            with urllib.request.urlopen(base + path) as r:
                assert r.status == 200
                assert needle in r.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()