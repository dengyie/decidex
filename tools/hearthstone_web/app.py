"""
DecideX Hearthstone advisor — read-only web viewer.

Replays real Power.log games (data/replays/*.log) through the DecideX
``HearthstoneAdvisor`` and serves an interactive page showing, for every decision
point: the reconstructed board, the game engine's authoritative legal-action
envelope, the advisor's recommended "best play" (and its confidence / fallback
provenance), and how that compares to what the human actually played.

Read-only by construction: it enumerates legal actions and prints a
recommendation. It never drives a game client.

Run:
    python tools/hearthstone_web/app.py --host 0.0.0.0 --port 8090

Then open it from any device on the EasyTier mesh, e.g.
    http://<this-box-mesh-ip>:8090
No SSH involved.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

HERE = Path(__file__).resolve().parent
REPLAY_DIR = ROOT / "data" / "replays"
# Shared visualization assets (same files the Balatro viewer serves) — one
# implementation of the bars / confidence badge / token gauge / lint panel.
SHARED_DIR = ROOT / "tools" / "web_shared"

# hslog logs a wall of parse warnings on the root logger; keep the console readable.
logging.getLogger().setLevel(logging.ERROR)

try:
    from decidex.hearthstone.advisor import HearthstoneAdvisor
    from decidex.hearthstone.perception import load_game_snapshots
    from decidex.hearthstone.replay import game_options_to_candidates, _map_chosen, _same_action
    from decidex.providers.mock import MockReplayProvider
    from decidex.providers.base import BaseInferenceProvider
    from decidex.viz import build_report
    HSLOG = True
    HSLOG_ERR = ""
except Exception as e:  # hslog / hearthstone libs missing
    HSLOG = False
    HSLOG_ERR = f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Persistent asyncio loop (the advisor is async; http.server is threaded)
# ---------------------------------------------------------------------------
_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True).start()


def run_async(coro, timeout: float = 120.0):
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout=timeout)


# ---------------------------------------------------------------------------
# Advisor / provider management
# ---------------------------------------------------------------------------
_ADVISORS: Dict[str, HearthstoneAdvisor] = {}


def _heuristic_solver(payload, question):
    """Deterministic offline policy: develop threats, trade favourably, then pass.

    Prefers a real attack > a card play > hero power > end turn; picks the
    first candidate of the preferred class (ids are emitted in a stable order).
    """
    opts = list(getattr(question, "options", None) or [])
    if not opts:
        return "end_turn", 0.5, {}
    for prefix in ("attack:", "play:", "hero_attack:", "hero_power", "end_turn"):
        matches = [o for o in opts if o.startswith(prefix)]
        if matches:
            sel = matches[0]
            conf = 0.75
            dist = {o: (conf if o == sel else (1 - conf) / max(1, len(opts) - 1)) for o in opts}
            return sel, conf, dist
    sel = opts[0]
    return sel, 0.6, {o: 1.0 / len(opts) for o in opts}


def _build_provider(kind: str) -> BaseInferenceProvider:
    if kind == "model":
        from decidex.providers.failover import PriorityFailoverProvider
        # Four-tier chain: futureppo -> nace -> mindshub -> official pool.
        return PriorityFailoverProvider.from_default_routes()
    return MockReplayProvider(solver_fn=_heuristic_solver, simulated_latency_ms=1)


def get_advisor(kind: str) -> HearthstoneAdvisor:
    if kind not in _ADVISORS:
        provider = _build_provider(kind)
        _ADVISORS[kind] = HearthstoneAdvisor(
            provider=provider,
            top_k=12,
            min_confidence=0.30,
            min_margin=0.01,
        )
    return _ADVISORS[kind]


# ---------------------------------------------------------------------------
# Replay parsing + cache
# ---------------------------------------------------------------------------
_GAMES: Dict[str, Tuple[float, List[Any]]] = {}


def list_games() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not REPLAY_DIR.is_dir():
        return out
    for p in sorted(REPLAY_DIR.glob("*.log")):
        out.append({"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)})
    return out


def get_snapshots(log_name: str) -> List[Any]:
    path = REPLAY_DIR / Path(log_name).name  # basename only: no path traversal
    if not path.is_file():
        raise FileNotFoundError(f"replay not found: {path.name}")
    mtime = path.stat().st_mtime
    cached = _GAMES.get(path.name)
    if cached and cached[0] == mtime:
        return cached[1]
    snaps = load_game_snapshots(str(path))
    _GAMES[path.name] = (mtime, snaps)
    return snaps


def _board_json(board) -> Dict[str, Any]:
    return {
        "turn": board.turn,
        "mana": board.mana,
        "max_mana": board.max_mana,
        "deck_count": board.deck_count,
        "fatigue": board.fatigue,
        "my_hero": dataclasses.asdict(board.my_hero),
        "opp_hero": dataclasses.asdict(board.opp_hero),
        "my_minions": [dataclasses.asdict(m) for m in board.my_minions],
        "opp_minions": [dataclasses.asdict(m) for m in board.opp_minions],
        "hand": [dataclasses.asdict(c) for c in board.hand],
    }


def _candidates_json(snap) -> List[Dict[str, Any]]:
    return [
        {
            "id": c.candidate_id,
            "description": c.description,
            "action_type": c.metadata.get("action_type", "unknown"),
        }
        for c in game_options_to_candidates(snap)
    ]


def game_payload(log_name: str) -> Dict[str, Any]:
    snaps = get_snapshots(log_name)
    decisions: List[Dict[str, Any]] = []
    for i, snap in enumerate(snaps):
        human_id, human_type = _map_chosen(snap)
        decisions.append({
            "index": i,
            "turn": snap.board.turn,
            "mana": snap.board.mana,
            "max_mana": snap.board.max_mana,
            "option_count": len(snap.options),
            "board": _board_json(snap.board),
            "options": _candidates_json(snap),
            "human": {"candidate": human_id, "action_type": human_type},
        })
    return {"log": Path(log_name).name, "decisions": decisions}


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------
async def _recommend_one(snap, kind: str) -> Dict[str, Any]:
    advisor = get_advisor(kind)
    candidates = game_options_to_candidates(snap)
    t0 = time.perf_counter()
    try:
        rec = await advisor.recommend(snap.board, legal_actions=candidates)
        err = None
    except Exception as e:  # network / provider failure -> report, don't 500
        rec = None
        err = f"{type(e).__name__}: {e}"
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)

    human_id, human_type = _map_chosen(snap)

    if rec is None:
        return {
            "provider": kind,
            "error": err,
            "candidate_id": "-",
            "description": f"（{kind} 调用失败，已跳过）{err}",
            "action_type": "error",
            "confidence": 0.0,
            "is_fallback": True,
            "fallback_reason": "PROVIDER_ERROR",
            "distribution": {},
            "legal_actions": [
                {"id": c.candidate_id, "description": c.description,
                 "action_type": c.metadata.get("action_type", "unknown")}
                for c in candidates
            ],
            "human": {"candidate": human_id, "action_type": human_type},
            "agreed": False,
            "latency_ms": latency_ms,
            "viz": None,
        }

    # Presentation model built server-side; viz.py is the shared source of
    # truth for bars / confidence banding / token budget / lint (see viz.py).
    # Pure transform of what the advisor already produced — never feeds back.
    report = build_report(
        rec.evidence,
        {rec.question.get("id", "hearthstone_action"): rec.question.get("criteria", {})},
        primitive=rec.primitive,
        distribution=rec.distribution,
        selected=rec.candidate_id,
        confidence=rec.confidence,
        options=rec.question.get("options"),
        labels={a["id"]: a.get("description", a["id"]) for a in rec.legal_actions},
        is_fallback=rec.is_fallback,
    )

    return {
        "provider": kind,
        "error": None,
        "candidate_id": rec.candidate_id,
        "description": rec.description,
        "action_type": rec.action_type,
        "confidence": rec.confidence,
        "is_fallback": rec.is_fallback,
        "fallback_reason": rec.fallback_reason,
        "distribution": rec.distribution,
        "legal_actions": rec.legal_actions,
        "human": {"candidate": human_id, "action_type": human_type},
        "agreed": _same_action(rec.candidate_id, human_id),
        "latency_ms": latency_ms,
        "viz": report,
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "DecidexHSWeb/1.0"

    def log_message(self, fmt, *args):  # quieter
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _err(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _read_json(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                html = (HERE / "index.html").read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
                return
            if path in ("/viz.js", "/viz.css"):
                name = Path(path).name
                target = SHARED_DIR / name
                if not target.is_file():
                    return self._err("shared asset missing: " + name, 404)
                ctype = ("application/javascript; charset=utf-8" if name.endswith(".js")
                         else "text/css; charset=utf-8")
                self._send(200, target.read_bytes(), ctype)
                return
            if path == "/api/health":
                self._json({
                    "ok": True,
                    "hslog": HSLOG,
                    "hslog_error": HSLOG_ERR,
                    "games": list_games(),
                    "replay_dir": str(REPLAY_DIR),
                })
                return
            if path == "/api/game":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                name = (q.get("log") or [""])[0]
                if not name:
                    return self._err("missing ?log=")
                if not HSLOG:
                    return self._err("hslog unavailable: " + HSLOG_ERR, 503)
                self._json(game_payload(name))
                return
            self._err("not found", 404)
        except FileNotFoundError as e:
            self._err(str(e), 404)
        except Exception as e:
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_json()
            if not HSLOG:
                return self._err("hslog unavailable: " + HSLOG_ERR, 503)

            if path == "/api/recommend":
                log = body.get("log", "")
                idx = int(body.get("index", 0))
                kind = body.get("provider", "heuristic")
                snaps = get_snapshots(log)
                if not (0 <= idx < len(snaps)):
                    return self._err("index out of range", 400)
                rec = run_async(_recommend_one(snaps[idx], kind))
                self._json(rec)
                return

            if path == "/api/replay":
                log = body.get("log", "")
                kind = body.get("provider", "heuristic")
                snaps = get_snapshots(log)

                async def _all():
                    out = []
                    for s in snaps:
                        out.append(await _recommend_one(s, kind))
                    return out

                reports = run_async(_all(), timeout=600.0)
                self._json({"log": log, "provider": kind, "reports": reports})
                return

            self._err("not found", 404)
        except Exception as e:
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _local_ips() -> List[str]:
    ips = set()
    # mesh / primary interface via a UDP probe toward the Guangzhou peer
    for probe in ("10.144.144.8", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 22 if probe.startswith("10.") else 53))
            ips.add(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def main():
    ap = argparse.ArgumentParser(description="DecideX Hearthstone advisor web viewer")
    ap.add_argument("--host", default=os.environ.get("HS_WEB_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("HS_WEB_PORT", "8090")))
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    print("=" * 68)
    print(" DecideX · 炉石顾问 优秀操作回放")
    print("=" * 68)
    print(f"  hslog        : {'可用' if HSLOG else '缺失 -> ' + HSLOG_ERR}")
    print(f"  replays      : {REPLAY_DIR}  ({len(list_games())} 局)")
    print(f"  bind         : {args.host}:{args.port}")
    print("  可用地址（EasyTier mesh 内任一设备可直接打开）:")
    for ip in _local_ips():
        tag = "  <- EasyTier mesh" if ip.startswith("10.144.144.") else ""
        print(f"      http://{ip}:{args.port}/{tag}")
    print(f"      http://127.0.0.1:{args.port}/  (本机)")
    print("=" * 68)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
