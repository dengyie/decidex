"""
DecideX Balatro web viewer: browse real bridge snapshots, run the advisor, and
compare recommendations against what the human actually played.

Only REAL snapshots from the bridge mod are ever shown — no demo data.

Routes:
  GET  /                      UI
  GET  /api/health            service + snapshot inventory
  GET  /api/game?name=        one snapshot: board + legal envelope
  POST /api/recommend         advisor recommendation for one snapshot
  POST /api/replay            advisor over every snapshot (adviser vs human)

Run:  python tools/balatro_web/app.py --host 0.0.0.0 --port 8091
Bridge snapshots dir: $BALATRO_BRIDGE_DIR or <repo>/data/balatro_bridge
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from decidex.balatro import (  # noqa: E402
    BalatroAdvisor,
    BalatroState,
    load_state,
)
from decidex.providers.base import BaseInferenceProvider  # noqa: E402
from decidex.providers.mock import MockReplayProvider  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)

BRIDGE_DIR = Path(os.environ.get("BALATRO_BRIDGE_DIR", ROOT / "data" / "balatro_bridge"))
ADVISOR: Optional[BalatroAdvisor] = None
_CACHE: Dict[str, Any] = {}


def _heuristic_solver(payload, question):
    """Deterministic stand-in for the Jev model: trust the simulator's best."""
    opts = question.options or []
    if not opts:
        return "discard:0", 0.4, {}
    return opts[0], 0.82, {o: (0.82 if o == opts[0] else 0.18 / max(1, len(opts) - 1)) for o in opts}


def _build_provider(kind: str) -> BaseInferenceProvider:
    if kind == "model":
        from decidex.providers.failover import PriorityFailoverProvider
        # Four-tier chain: futureppo -> nace -> mindshub-free -> official pool.
        # Keys: DECIDEX_KEY_FILE env or keys.jsonl in the working directory.
        return PriorityFailoverProvider.from_default_routes()
    return MockReplayProvider(solver_fn=_heuristic_solver, simulated_latency_ms=1)


def get_advisor(kind: str) -> BalatroAdvisor:
    global ADVISOR
    if ADVISOR is None:
        ADVISOR = BalatroAdvisor(provider=_build_provider(kind),
                                 top_k=10, min_confidence=0.30, min_margin=0.01)
    return ADVISOR


def list_games() -> List[Dict[str, Any]]:
    """Real bridge snapshots only — never demo fixtures."""
    games: List[Dict[str, Any]] = []
    if BRIDGE_DIR.is_dir():
        for p in sorted(BRIDGE_DIR.glob("*.json")):
            games.append({"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1),
                          "source": "bridge"})
    return games


def _snapshot_path(name: str) -> Optional[Path]:
    safe = Path(name).name
    p = BRIDGE_DIR / safe
    return p if p.is_file() else None


def get_state(name: str) -> BalatroState:
    mtime = None
    p = _snapshot_path(name)
    if p is None:
        raise FileNotFoundError(name)
    mtime = p.stat().st_mtime
    cached = _CACHE.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    state = load_state(p)
    _CACHE[name] = (mtime, state)
    return state


def state_payload(state: BalatroState) -> Dict[str, Any]:
    return state.to_dict()


async def _recommend(name: str, kind: str) -> Dict[str, Any]:
    state = get_state(name)
    advisor = get_advisor(kind)
    rec = await advisor.recommend(state)
    chosen = next((a for a in rec.legal_actions if a["id"] == rec.candidate_id), None)
    return {
        "log": name,
        "candidate_id": rec.candidate_id,
        "action_type": rec.action_type,
        "description": rec.description,
        "confidence": rec.confidence,
        "expected_score": rec.expected_score,
        "clears_blind": rec.clears_blind,
        "is_fallback": rec.is_fallback,
        "fallback_reason": rec.fallback_reason,
        "distribution": rec.distribution,
        "indices": (chosen or {}).get("indices"),
        "chips": (chosen or {}).get("chips"),
        "mult": (chosen or {}).get("mult"),
        "legal_actions": rec.legal_actions,
    }


async def _replay(kind: str) -> Dict[str, Any]:
    """Advisor over every pre-decision snapshot; pair with the following
    action snapshot to compare against the human's actual move."""
    games = list_games()
    loaded = [(g["name"], get_state(g["name"])) for g in games]
    reports = []
    for i, (name, state) in enumerate(loaded):
        if state.actual is not None:
            continue  # action snapshots are context, not decision points
        human = None
        for j in range(i + 1, len(loaded)):
            nxt = loaded[j][1]
            if nxt.actual is not None:
                if [c.label for c in nxt.hand] == [c.label for c in state.hand]:
                    human = f"{nxt.actual['type']}:{'-'.join(str(k) for k in nxt.actual['cards'])}"
                break
        rec = await _recommend(name, kind)
        reports.append({
            "name": name,
            "advisor": rec["candidate_id"],
            "advisor_desc": rec["description"],
            "confidence": rec["confidence"],
            "human": human,
        })
    return {"provider": kind, "reports": reports}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence request spam
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _run_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout=120)

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                page = (Path(__file__).parent / "index.html").read_bytes()
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/health":
                return self._json({
                    "ok": True, "engine": "balatro-advisor",
                    "bridge_dir": str(BRIDGE_DIR),
                    "games": list_games(),
                })
            if self.path.startswith("/api/game"):
                qs = dict(pair.split("=", 1) for pair in self.path.split("?", 1)[1].split("&")) \
                    if "?" in self.path else {}
                name = qs.get("name", "")
                state = get_state(name)
                return self._json(state_payload(state))
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": f"snapshot not found: {e}"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            kind = str(req.get("kind", "heuristic"))
            if self.path == "/api/recommend":
                return self._json(self._run_async(_recommend(str(req.get("name", "")), kind)))
            if self.path == "/api/replay":
                return self._json(self._run_async(_replay(kind)))
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": f"snapshot not found: {e}"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


_LOOP = None


def main():
    global _LOOP
    ap = argparse.ArgumentParser(description="DecideX Balatro advisor web viewer")
    ap.add_argument("--host", default=os.environ.get("BALATRO_WEB_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BALATRO_WEB_PORT", "8091")))
    args = ap.parse_args()

    import asyncio
    _LOOP = asyncio.new_event_loop()
    threading.Thread(target=_LOOP.run_forever, daemon=True).start()
    time.sleep(0.1)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print("=" * 68)
    print(" DecideX · 小丑牌顾问 优秀操作回放")
    print("=" * 68)
    print(f"  bridge dir   : {BRIDGE_DIR}  ({len(list_games())} snapshots)")
    print(f"  bind         : {args.host}:{args.port}")
    print("  mesh 内任一设备可直接打开本机 EasyTier IP 的该端口")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
