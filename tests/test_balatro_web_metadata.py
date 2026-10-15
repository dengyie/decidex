"""Unit and contract tests for Balatro Web snapshot metadata extraction and caching.

Tests that _get_snapshot_meta extracts semantic game fields (Ante, Round, Blind,
Chips, Hands, Discards, Actual Action), formats poker cards with visual suit glyphs,
maintains an in-memory mtime cache, gracefully degrades on invalid/missing files,
and supplies enriched metadata in the /api/health games list.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading
import urllib.request
import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "tools" / "balatro_web" / "app.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "balatro"

spec = importlib.util.spec_from_file_location("balatro_web_app_meta", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def test_format_card_label():
    assert app._format_card_label({"rank": "A", "suit": "S"}) == "A♠"
    assert app._format_card_label({"rank": "K", "suit": "H"}) == "K♥"
    assert app._format_card_label({"rank": "10", "suit": "D"}) == "10♦"
    assert app._format_card_label({"rank": "2", "suit": "C"}) == "2♣"
    # fallback on missing fields or non-dict
    assert app._format_card_label({}) == "??"
    assert app._format_card_label({"rank": "7"}) == "7?"
    assert app._format_card_label("raw-str") == "raw-str"


def test_get_snapshot_meta_pre_decision(tmp_path):
    app._META_CACHE.clear()
    src = FIXTURES / "state-0001.json"
    dest = tmp_path / "state-0001.json"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    meta = app._get_snapshot_meta(dest)
    assert meta["name"] == "state-0001.json"
    assert meta["ante"] == 2
    assert meta["round"] == 5
    assert meta["blind_name"] == "The Pillar"
    assert meta["blind_type"] == "big"
    assert meta["target_chips"] == 600
    assert meta["scored_chips"] == 0
    assert meta["hands_left"] == 4
    assert meta["discards_left"] == 3
    assert meta["money"] == 8
    assert meta["actual_type"] is None
    assert meta["actual_desc"] is None


def test_get_snapshot_meta_action_play(tmp_path):
    app._META_CACHE.clear()
    src = FIXTURES / "state-0002.json"
    dest = tmp_path / "state-0002.json"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    meta = app._get_snapshot_meta(dest)
    assert meta["name"] == "state-0002.json"
    assert meta["actual_type"] == "play"
    assert meta["actual_cards"] == ["A♠", "K♠", "Q♠", "J♠", "10♠"]
    assert "打出 5 张" in meta["actual_desc"]
    assert "A♠" in meta["actual_desc"]


def test_get_snapshot_meta_action_discard(tmp_path):
    app._META_CACHE.clear()
    state_discard = {
        "ante": 1,
        "round": 2,
        "blind": {"name": "Small Blind", "type": "small", "chips": 300, "scored": 50},
        "hands_left": 3,
        "discards_left": 2,
        "money": 5,
        "hand": [
            {"rank": "7", "suit": "H"},
            {"rank": "8", "suit": "D"},
            {"rank": "2", "suit": "C"},
        ],
        "actual": {"type": "discard", "cards": [0, 2]},
    }
    f = tmp_path / "state-0005.json"
    f.write_text(json.dumps(state_discard), encoding="utf-8")

    meta = app._get_snapshot_meta(f)
    assert meta["actual_type"] == "discard"
    assert meta["actual_cards"] == ["7♥", "2♣"]
    assert "弃牌 2 张: 7♥ 2♣" in meta["actual_desc"]


def test_snapshot_meta_caching_and_invalidation(tmp_path):
    app._META_CACHE.clear()
    f = tmp_path / "cached_state.json"
    f.write_text(json.dumps({"ante": 1, "round": 1, "blind": {"name": "Small Blind", "chips": 300}}), encoding="utf-8")

    meta1 = app._get_snapshot_meta(f)
    assert meta1["ante"] == 1
    cache_key = str(f.resolve())
    assert cache_key in app._META_CACHE

    # Direct retrieval hits cache
    meta2 = app._get_snapshot_meta(f)
    assert meta2 is meta1

    # Invalidate by updating mtime & content
    time.sleep(0.05)
    f.write_text(json.dumps({"ante": 3, "round": 7, "blind": {"name": "The Wall", "chips": 8000}}), encoding="utf-8")
    meta3 = app._get_snapshot_meta(f)
    assert meta3["ante"] == 3
    assert meta3["blind_name"] == "The Wall"
    assert meta3["target_chips"] == 8000


def test_get_snapshot_meta_error_resilience(tmp_path):
    app._META_CACHE.clear()
    # Missing file
    non_existent = tmp_path / "does_not_exist.json"
    m_missing = app._get_snapshot_meta(non_existent)
    assert m_missing["name"] == "does_not_exist.json"
    assert m_missing["size_kb"] == 0.0

    # Corrupt JSON
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("NOT_VALID_JSON{{{", encoding="utf-8")
    m_corrupt = app._get_snapshot_meta(corrupt)
    assert m_corrupt["name"] == "corrupt.json"
    assert m_corrupt["ante"] == 1
    assert m_corrupt["target_chips"] == 300


@pytest.fixture
def health_server(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    for f in sorted(FIXTURES.glob("state-*.json"))[:2]:
        (bridge / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(app, "BRIDGE_DIR", bridge)
    monkeypatch.setattr(app, "WEB_USER", "operator")
    monkeypatch.setattr(app, "WEB_PASSWORD", "operator-pw")
    monkeypatch.setattr(app, "VNC_PASSWORD", "tok")
    app._SESSIONS.clear()
    app._META_CACHE.clear()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()
        app._SESSIONS.clear()


def test_health_api_serves_rich_game_metadata(health_server):
    req = urllib.request.Request(f"{health_server}/api/health")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())

    assert "games" in data
    games = data["games"]
    assert len(games) >= 2

    # Verify state-0001
    g1 = next((g for g in games if g["name"] == "state-0001.json"), None)
    assert g1 is not None
    assert g1["ante"] == 2
    assert g1["round"] == 5
    assert g1["blind_name"] == "The Pillar"
    assert g1["target_chips"] == 600
    assert g1["hands_left"] == 4
    assert g1["discards_left"] == 3

    # Verify state-0002 has actual play action description
    g2 = next((g for g in games if g["name"] == "state-0002.json"), None)
    assert g2 is not None
    assert g2["actual_type"] == "play"
    assert "打出 5 张" in g2["actual_desc"]


def test_list_games_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "BRIDGE_DIR", tmp_path)
    for i in range(10):
        p = tmp_path / f"state-{i:04d}.json"
        p.write_text(json.dumps({"ante": 1, "round": 1}), encoding="utf-8")

    res_all = app.list_games(limit=None)
    assert len(res_all) == 10

    res_3 = app.list_games(limit=3)
    assert len(res_3) == 3


def test_health_api_limit_param(health_server):
    req = urllib.request.Request(f"{health_server}/api/health?limit=1")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
    assert len(data["games"]) == 1


def test_concurrent_cache_access_under_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(app, "_CACHE_MAX_ENTRIES", 4)
    app._META_CACHE.clear()
    app._CACHE.clear()

    # Create 8 files to force eviction when capacity is 4
    file_paths = []
    for i in range(8):
        f = tmp_path / f"state-{i:04d}.json"
        f.write_text(json.dumps({
            "hand": [{"rank": "A", "suit": "S"}],
            "blind": {"name": "Small Blind", "type": "small", "chips": 300},
            "money": i,
        }), encoding="utf-8")
        file_paths.append(f)

    errors = []

    def worker(worker_id):
        try:
            for _ in range(50):
                target = file_paths[(_ + worker_id) % len(file_paths)]
                meta = app._get_snapshot_meta(target)
                assert meta["name"] == target.name
                st = app.get_state(target.name)
                assert st.money == (_ + worker_id) % len(file_paths)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent cache access raised: {errors}"

