"""
DecideX Balatro web viewer: browse real bridge snapshots, run the advisor, and
compare recommendations against what the human actually played.

Only REAL snapshots from the bridge mod are ever shown — no demo data.

Routes:
  GET  /                      UI
  GET  /login                 operator login page
  GET  /api/health            service + snapshot inventory
  GET  /api/game?name=        one snapshot: board + legal envelope
  GET  /api/session           whether the caller already holds a login session
  POST /api/recommend         advisor recommendation for one snapshot
  POST /api/replay            advisor over every snapshot (adviser vs human)
  POST /api/login             exchange the operator password for a session cookie
  POST /api/logout            drop the session cookie
  GET  /api/frame.jpg         latest live game frame (captured from VNC)
  POST /api/input/click       inject a mouse click into the game (input auth)
  POST /api/input/key         inject a keystroke into the game (input auth)

Auth model (mirrors the user's other sites, e.g. 2fa-hub): interactive input
requires a login session minted by POST /api/login and carried in an HttpOnly,
SameSite=Strict cookie. Login is a username/password pair (BALATRO_WEB_USER /
BALATRO_WEB_PASSWORD, default mango/12345678) — override both via env in any
real deployment. A raw X-Input-Token header (the BALATRO_VNC_PASSWORD secret)
is still accepted so mesh scripts can actuate without a browser session.

Run:  python tools/balatro_web/app.py --host 0.0.0.0 --port 8091
Bridge snapshots dir: $BALATRO_BRIDGE_DIR or <repo>/data/balatro_bridge
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import sys
import subprocess
import threading
import time
import traceback
from http.cookies import SimpleCookie
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
VNC_TARGET = os.environ.get("BALATRO_VNC_TARGET", "10.144.144.8::5900")
VNC_PASSWORD = os.environ.get("BALATRO_VNC_PASSWORD", "")
FRAME_PATH = Path(os.environ.get("BALATRO_FRAME_PATH", "/tmp/balatro_live_frame.jpg"))
GAME_DISPLAY = os.environ.get("BALATRO_GAME_DISPLAY", ":66")
# Login credentials for the operator page. Defaults are the well-known dev pair;
# override via env in any real deployment (BALATRO_WEB_USER / BALATRO_WEB_PASSWORD).
WEB_USER = os.environ.get("BALATRO_WEB_USER", "mango")
WEB_PASSWORD = os.environ.get("BALATRO_WEB_PASSWORD", "12345678")
_ADVISORS: Dict[str, BalatroAdvisor] = {}
_CACHE: Dict[str, Any] = {}

# Login sessions: random bearer held only in server memory, keyed to a
# cookie value. Restarting the service invalidates every session (fine — the
# operator just logs in again), and nothing sensitive is ever written to disk.
_SESSION_COOKIE = "balatro_session"
_SESSION_TTL = 12 * 3600
_SESSIONS: Dict[str, float] = {}          # cookie value -> expiry epoch
_SESSIONS_LOCK = threading.Lock()

# xdotool keysyms differ from DOM KeyboardEvent.key names; map the ones the page
# can emit so arrow/enter/backspace actually reach the game instead of silently
# being ignored ("No such key name").
_KEY_ALIASES = {
    "enter": "Return", "return": "Return",
    "escape": "Escape", "esc": "Escape",
    "backspace": "BackSpace", "delete": "Delete",
    "tab": "Tab", "space": "space",
    "arrowup": "Up", "arrowdown": "Down",
    "arrowleft": "Left", "arrowright": "Right",
}


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
    """One advisor per provider kind; the first request must not pin every
    later request to whatever kind happened to arrive first."""
    advisor = _ADVISORS.get(kind)
    if advisor is None:
        advisor = BalatroAdvisor(provider=_build_provider(kind),
                                 top_k=10, min_confidence=0.30, min_margin=0.01)
        _ADVISORS[kind] = advisor
    return advisor


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
    # HTTP/1.1 keeps the connection alive so Cloudflare's connector multiplexes
    # its streams over fewer sockets instead of reconnecting per request. Safe
    # here because every response sets Content-Length.
    protocol_version = "HTTP/1.1"

    # Keep-alive without an idle timeout parks one ThreadingHTTPServer thread per
    # abandoned socket forever — the live feed polls /api/frame.jpg every 600ms
    # from every open tab, and the connector also opens speculative streams, so
    # threads piled up into the hundreds on this 2-vCPU box and starved the
    # origin. StreamRequestHandler applies this to the socket, and
    # handle_one_request() turns the resulting socket.timeout into
    # close_connection, so idle keep-alive sockets are reaped.
    timeout = 15

    def log_message(self, *a):  # silence request spam
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              nocache: bool = False, extra: Optional[Dict[str, str]] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if nocache:
            # The live frame is a parameterless .jpg; Cloudflare caches those
            # unless told otherwise, which freezes the public feed.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200, extra: Optional[Dict[str, str]] = None):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   extra=extra)

    def _auth_via(self) -> Optional[str]:
        """How the caller proved input rights: 'session' (ambient cookie) or
        'token' (explicit X-Input-Token header for mesh scripts), else None."""
        if _valid_session(_cookie_token(self.headers.get("Cookie"))):
            return "session"
        if _check_input_token(self.headers.get("X-Input-Token", "")):
            return "token"
        return None

    def _has_csrf_header(self) -> bool:
        """Mutations that ride the ambient session cookie must carry
        X-Requested-With, matching the other sites' CSRF guard so a cross-site
        form post cannot act on the operator's behalf. An explicit
        X-Input-Token is not ambient, so it needs no such guard."""
        return self.headers.get("X-Requested-With", "") == "decidex-web"

    def _deny_input(self) -> Optional[int]:
        """Authorize an input mutation; returns the HTTP status to send, or
        None when the request may proceed."""
        via = self._auth_via()
        if via is None:
            return 401
        if via == "session" and not self._has_csrf_header():
            return 403
        return None

    def _run_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout=120)

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                page = (Path(__file__).parent / "index.html").read_bytes()
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path in ("/login", "/login.html"):
                page = (Path(__file__).parent / "login.html").read_bytes()
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/session":
                return self._json({"authenticated":
                                   _valid_session(_cookie_token(self.headers.get("Cookie")))})
            if self.path.startswith("/api/frame.jpg"):
                if not FRAME_PATH.exists():
                    return self._json({"error": "no frame yet"}, 503)
                data = FRAME_PATH.read_bytes()
                self._send(200, data, "image/jpeg", nocache=True)
                return
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
            if self.path == "/api/login":
                if not self._has_csrf_header():
                    return self._json({"error": "csrf"}, 403)
                if not _check_credentials(str(req.get("user", "")),
                                          str(req.get("password", ""))):
                    return self._json({"error": "invalid credentials"}, 401)
                token = _new_session()
                cookie = (f"{_SESSION_COOKIE}={token}; Path=/; HttpOnly; "
                          f"SameSite=Strict; Max-Age={_SESSION_TTL}")
                return self._json({"ok": True}, extra={"Set-Cookie": cookie})
            if self.path == "/api/logout":
                _drop_session(_cookie_token(self.headers.get("Cookie")))
                cookie = f"{_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                return self._json({"ok": True}, extra={"Set-Cookie": cookie})
            if self.path == "/api/recommend":
                return self._json(self._run_async(_recommend(str(req.get("name", "")), kind)))
            if self.path == "/api/replay":
                return self._json(self._run_async(_replay(kind)))
            if self.path == "/api/input/click":
                deny = self._deny_input()
                if deny:
                    return self._json({"error": "unauthorized" if deny == 401 else "csrf"}, deny)
                try:
                    x = int(req.get("x", 0)); y = int(req.get("y", 0))
                except (TypeError, ValueError):
                    return self._json({"error": "bad coords"}, 400)
                out = _xdotool(_click_args(x, y, bool(req.get("double"))))
                return self._json({"ok": not _xdotool_failed(out), "out": out[:120]})
            if self.path == "/api/input/key":
                deny = self._deny_input()
                if deny:
                    return self._json({"error": "unauthorized" if deny == 401 else "csrf"}, deny)
                key = str(req.get("key", ""))[:24]
                if not re.fullmatch(r"[a-zA-Z0-9_]+", key):
                    return self._json({"error": "bad key"}, 400)
                out = _xdotool(["key", _key_name(key)])
                return self._json({"ok": not _xdotool_failed(out), "out": out[:120]})
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": f"snapshot not found: {e}"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


_LOOP = None


def _frame_worker() -> None:
    """Connect to the live game's VNC server and keep the latest frame JPEG
    on disk so /api/frame.jpg can serve the real game feed. One persistent
    client, ~2 fps, auto-reconnect on failure."""
    global _FRAME_MTIME
    while True:
        client = None
        try:
            from vncdotool import api
            client = api.connect(VNC_TARGET, password=VNC_PASSWORD or None)
            logging.getLogger().info("live feed: connected to %s", VNC_TARGET)
            while True:
                client.captureScreen(str(FRAME_PATH))
                if FRAME_PATH.exists():
                    mtime = FRAME_PATH.stat().st_mtime
                    if mtime != _FRAME_MTIME:
                        _FRAME_MTIME = mtime
                time.sleep(0.45)
        except Exception as e:  # noqa: BLE001 - keep the feed alive forever
            logging.getLogger().warning("live feed: %s; reconnecting", e)
            try:
                if client is not None:
                    client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2.0)


def _check_input_token(req_token: str) -> bool:
    """Raw-header escape hatch for mesh scripts: the shared secret doubles as
    the VNC password. Interactive browser use goes through a login session."""
    if not VNC_PASSWORD or not req_token:
        return False
    return hmac.compare_digest(req_token, VNC_PASSWORD)


def _check_credentials(user: str, pw: str) -> bool:
    """Constant-time compare against the configured login pair. Both fields are
    compared unconditionally so a wrong username cannot be timed apart from a
    wrong password."""
    ok_user = hmac.compare_digest((user or "").encode(), WEB_USER.encode())
    ok_pw = hmac.compare_digest((pw or "").encode(), WEB_PASSWORD.encode())
    return ok_user and ok_pw


def _new_session() -> str:
    """Mint a session cookie value with a fresh TTL."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _SESSIONS_LOCK:
        for k, exp in list(_SESSIONS.items()):
            if exp < now:
                _SESSIONS.pop(k, None)
        _SESSIONS[token] = now + _SESSION_TTL
    return token


def _valid_session(token: Optional[str]) -> bool:
    if not token:
        return False
    now = time.time()
    with _SESSIONS_LOCK:
        exp = _SESSIONS.get(token)
        if exp is None:
            return False
        if exp < now:
            _SESSIONS.pop(token, None)
            return False
    return True


def _drop_session(token: Optional[str]) -> None:
    if token:
        with _SESSIONS_LOCK:
            _SESSIONS.pop(token, None)


def _cookie_token(cookie_header: Optional[str]) -> Optional[str]:
    if not cookie_header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:  # noqa: BLE001 - malformed cookie jar, treat as anonymous
        return None
    morsel = jar.get(_SESSION_COOKIE)
    return morsel.value if morsel else None


def _click_args(x: int, y: int, double: bool = False) -> list:
    """Clamp to the game window and build the xdotool argv for one click."""
    args = ["mousemove", str(max(0, min(1279, x))), str(max(0, min(799, y)))]
    if double:
        return args + ["click", "--repeat", "2", "1"]
    return args + ["click", "1"]


def _key_name(key: str) -> str:
    """Translate page-side key names to xdotool keysyms."""
    return _KEY_ALIASES.get(key.lower(), key)


def _xdotool(args: list) -> str:
    """Run xdotool against the game display. Failures are returned as text so
    the caller can report them instead of swallowing them."""
    env = dict(os.environ, DISPLAY=GAME_DISPLAY)
    try:
        out = subprocess.run(["xdotool"] + args, env=env,
                             capture_output=True, text=True, timeout=8)
    except FileNotFoundError:
        return "error: xdotool not installed"
    except subprocess.TimeoutExpired:
        return "error: xdotool timed out"
    return (out.stdout + out.stderr).strip()


def _xdotool_failed(out: str) -> bool:
    return out.lower().startswith("error") or "no such key name" in out.lower()


class _Server(ThreadingHTTPServer):
    # request_queue_size is consumed by server_activate() during __init__, so it
    # must be a class attribute — setting it after construction is too late.
    # The default 5 overflows under the connector's parallel streams and the
    # kernel RSTs them, which cloudflared reports as "http2: stream closed".
    allow_reuse_address = True
    request_queue_size = 128


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
    threading.Thread(target=_frame_worker, daemon=True).start()

    srv = _Server((args.host, args.port), Handler)
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
