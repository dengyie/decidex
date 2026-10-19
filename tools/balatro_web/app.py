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
SameSite=Strict cookie. Login credentials must be supplied with BALATRO_WEB_USER
and BALATRO_WEB_PASSWORD before startup. A raw X-Input-Token header (the
BALATRO_VNC_PASSWORD secret) is still accepted so mesh scripts can actuate
without a browser session.

Run:  python tools/balatro_web/app.py --host 127.0.0.1 --port 8091
Bridge snapshots dir: $BALATRO_BRIDGE_DIR or <repo>/data/balatro_bridge
"""

from __future__ import annotations

import argparse
import base64
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sys
import subprocess
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from decidex.balatro import (  # noqa: E402
    BalatroAdvisor,
    BalatroState,
    load_state,
)
from decidex.balatro.state import SUIT_SYMBOL  # noqa: E402
from decidex.providers.base import BaseInferenceProvider  # noqa: E402
from decidex.providers.mock import MockReplayProvider  # noqa: E402
from decidex.viz import build_report  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)

BRIDGE_DIR = Path(os.environ.get("BALATRO_BRIDGE_DIR", ROOT / "data" / "balatro_bridge"))
WASM_WEB_DIR = ROOT / "tools" / "balatro_wasm" / "web"
# Shared, framework-free visualization assets used by every DecideX web viewer
# (see tools/web_shared/viz.js). Served from this app too so a viewer needs one
# origin, and so the two viewers cannot drift.
SHARED_DIR = ROOT / "tools" / "web_shared"
VNC_TARGET = os.environ.get("BALATRO_VNC_TARGET", "10.144.144.8::5900")
VNC_PASSWORD = os.environ.get("BALATRO_VNC_PASSWORD", "")
# Bound every VNC round-trip (connect/capture/disconnect). vncdotool's proxy
# waits on a result queue with this timeout, and the default is a full hour: if
# x11vnc restarts, captureScreen blocks on the dead socket for up to 3600 s and
# the worker never reaches its reconnect branch. A few seconds turns that hang
# into a TimeoutError so the loop reconnects promptly.
VNC_TIMEOUT = float(os.environ.get("BALATRO_VNC_TIMEOUT", "8"))
FRAME_PATH = Path(os.environ.get("BALATRO_FRAME_PATH", "/tmp/balatro_live_frame.jpg"))
FRAME_RAW_PATH = Path(os.environ.get("BALATRO_FRAME_RAW_PATH", "/tmp/balatro_live_frame.raw.jpg"))
# The public path is bounded by this host's EGRESS uplink, not by CPU or the
# tunnel: measured ~8.7 KB/s while the game runs (the 2-vCPU box is CPU-starved,
# which collapses the TCP congestion window and drops ~1% of segments). A raw
# 1280x800 capture is ~98 KB, so it needs 11+ s to leave the box — longer than
# the browser waits, so the live feed never paints. Serving a downscaled frame
# is the only lever inside this host. The public box now runs the game at
# 960x600 and the served frame at 560x350, and every egress KB counts against a
# 300 GB/month traffic allowance, so size is deliberately conservative.
FRAME_WIDTH = int(os.environ.get("BALATRO_FRAME_WIDTH", "560"))
FRAME_QUALITY = int(os.environ.get("BALATRO_FRAME_QUALITY", "40"))
# The worker mints a fresh JPEG every FRAME_INTERVAL; the browser polls no
# faster than this, so a fast client cannot multiply egress by re-fetching the
# same frame. Cloudflare edge caching (below) then collapses many viewers into
# ~one origin fetch per interval.
FRAME_INTERVAL = float(os.environ.get("BALATRO_FRAME_INTERVAL", "0.5"))
# Serve the frame with a tiny s-maxage so Cloudflare collapses concurrent
# viewers into one origin request per interval. The per-request cache-buster is
# gone (dropped from index.html), so the URL is stable and edge-cacheable; the
# content still changes every FRAME_INTERVAL, so the feed stays live.
FRAME_MAXAGE = int(os.environ.get("BALATRO_FRAME_MAXAGE", "1"))
GAME_DISPLAY = os.environ.get("BALATRO_GAME_DISPLAY", ":66")
# Xvfb :66 screen size; the game window tracks it (no window manager), so this
# is also the click-coordinate space the page maps onto.
GAME_W = int(os.environ.get("BALATRO_GAME_W", "960"))
GAME_H = int(os.environ.get("BALATRO_GAME_H", "600"))
# Operator credentials are injected by the deployment environment.
WEB_USER = os.environ.get("BALATRO_WEB_USER", "")
WEB_PASSWORD = os.environ.get("BALATRO_WEB_PASSWORD", "")
_ADVISORS: Dict[str, BalatroAdvisor] = {}
_CACHE: Dict[str, Any] = {}
_META_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_MAX_ENTRIES = 256
_CACHE_LOCK = threading.Lock()
_DISPATCH_LOCK = threading.Lock()

VALID_GAME_ACTIONS = {
    "play", "discard", "toggle_card", "select_cards", "unhighlight_all",
    "sort_rank", "sort_suit", "reroll_shop", "buy_card", "buy_from_shop",
    "use_card", "sell_card", "select_blind", "cash_out", "toggle_shop", "start_run"
}

VALID_AREAS = {
    "shop_jokers", "shop_booster", "shop_boosters", "shop_vouchers", "shop_voucher",
    "consumables", "consumeables", "jokers", "joker", "hand"
}

# Login sessions: random bearer held only in server memory, keyed to a
# cookie value. Restarting the service invalidates every session (fine — the
# operator just logs in again), and nothing sensitive is ever written to disk.
_SESSION_COOKIE = "balatro_session"
_SESSION_TTL = 12 * 3600
_SSO_COOKIE = "mango_game_session"
_SSO_SECRET = os.environ.get("MANGO_AUTH_SECRET", "")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _verify_sso_token(token: Optional[str], secret: Optional[str] = None) -> Optional[dict]:
    s = secret if secret is not None else _SSO_SECRET
    if not token or "." not in token or not s:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected = hmac.new(s.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        actual = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, actual):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if time.time() > payload.get("exp", 0):
            return None
        return payload
    except Exception:
        return None


def _create_sso_token(user: str, secret: Optional[str] = None, ttl: int = _SESSION_TTL) -> str:
    s = secret if secret is not None else _SSO_SECRET
    now = int(time.time())
    payload = {
        "u": user,
        "role": "operator",
        "iat": now,
        "exp": now + ttl
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(s.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"
_SESSIONS: Dict[str, float] = {}          # cookie value -> expiry epoch
_SESSIONS_LOCK = threading.Lock()

# A post-login `next` target must be a single-slash same-origin path made only
# of RFC-3986 path/query characters. This is the one gate that keeps the login
# page from becoming an open redirect (//host, https://host) and from letting a
# crafted value break out of the JS literal it is injected into.
_SAFE_NEXT_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$")

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


def _format_card_label(c: Any) -> str:
    """Turn a raw card dict into a concise label like 'A♠'."""
    if isinstance(c, dict):
        rank = str(c.get("rank") or "?")
        suit = str(c.get("suit") or "?")
        return f"{rank}{SUIT_SYMBOL.get(suit, suit)}"
    return str(c)


def _get_snapshot_meta(p: Path) -> Dict[str, Any]:
    """Extract and cache structured metadata from a bridge state snapshot."""
    try:
        mtime = p.stat().st_mtime
        sz = round(p.stat().st_size / 1024, 1)
    except OSError:
        return {
            "name": p.name, "size_kb": 0.0, "source": "bridge",
            "ante": 1, "round": 1, "blind_name": "Small Blind", "blind_type": "small",
            "target_chips": 300, "scored_chips": 0, "hands_left": 4, "discards_left": 3,
            "money": 4, "state_name": "SELECTING_HAND", "actual_type": None, "actual_desc": None,
            "mtime": 0.0,
        }

    cache_key = str(p.resolve())
    with _CACHE_LOCK:
        cached = _META_CACHE.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]

    meta: Dict[str, Any] = {
        "name": p.name,
        "size_kb": sz,
        "source": "bridge",
        "ante": 1,
        "round": 1,
        "blind_name": "Small Blind",
        "blind_type": "small",
        "target_chips": 300,
        "scored_chips": 0,
        "hands_left": 4,
        "discards_left": 3,
        "money": 4,
        "state_name": "SELECTING_HAND",
        "actual_type": None,
        "actual_cards": [],
        "actual_desc": None,
        "mtime": mtime,
    }

    try:
        raw_text = p.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        if isinstance(data, dict):
            meta["ante"] = int(data.get("ante") or 1)
            meta["round"] = int(data.get("round") or 1)
            blind = data.get("blind") or {}
            meta["blind_name"] = str(blind.get("name") or "Small Blind")
            meta["blind_type"] = str(blind.get("type") or "small")
            meta["target_chips"] = int(blind.get("chips") or 300)
            meta["scored_chips"] = int(blind.get("scored") or 0)
            meta["hands_left"] = int(data.get("hands_left") if data.get("hands_left") is not None else 4)
            meta["discards_left"] = int(data.get("discards_left") if data.get("discards_left") is not None else 3)
            meta["money"] = int(data.get("money") or 0)
            meta["state_name"] = str(data.get("state_name") or "SELECTING_HAND")

            actual = data.get("actual")
            if isinstance(actual, dict):
                act_type = str(actual.get("type") or "")
                meta["actual_type"] = act_type
                cards_idx = actual.get("cards") or []
                hand = data.get("hand") or []
                cards_str = []
                for idx in cards_idx:
                    if isinstance(idx, int) and 0 <= idx < len(hand):
                        cards_str.append(_format_card_label(hand[idx]))
                    else:
                        cards_str.append(f"#{idx}")
                meta["actual_cards"] = cards_str
                cards_repr = " ".join(cards_str)
                if act_type == "play":
                    meta["actual_desc"] = f"打出 {len(cards_idx)} 张: {cards_repr}" if cards_repr else f"打出 {len(cards_idx)} 张"
                elif act_type == "discard":
                    meta["actual_desc"] = f"弃牌 {len(cards_idx)} 张: {cards_repr}" if cards_repr else f"弃牌 {len(cards_idx)} 张"
                elif act_type:
                    meta["actual_desc"] = act_type
    except Exception:
        pass

    with _CACHE_LOCK:
        if len(_META_CACHE) >= _CACHE_MAX_ENTRIES:
            oldest = next(iter(_META_CACHE), None)
            if oldest:
                _META_CACHE.pop(oldest, None)
        _META_CACHE[cache_key] = (mtime, meta)
    return meta


def list_games(limit: Optional[int] = 60) -> List[Dict[str, Any]]:
    """Real bridge snapshots only — sorted latest first with rich metadata."""
    games: List[Dict[str, Any]] = []
    if BRIDGE_DIR.is_dir():
        files = [p for p in BRIDGE_DIR.glob("*.json") if p.name != "live_state.json"]
        def _sort_key(p: Path):
            m = re.search(r"-(\d{4})\.json$", p.name)
            seq = int(m.group(1)) if m else 0
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            return (mtime, seq, p.name)

        sorted_files = sorted(files, key=_sort_key, reverse=True)
        if limit is not None and limit > 0:
            sorted_files = sorted_files[:limit]
        for p in sorted_files:
            games.append(_get_snapshot_meta(p))
    return games


def _snapshot_path(name: str) -> Optional[Path]:
    if not BRIDGE_DIR.is_dir():
        return None
    safe = Path(name).name if name else ""
    if not safe or safe in ("live", "latest"):
        live_p = BRIDGE_DIR / "live_state.json"
        if live_p.is_file():
            return live_p
        files = [p for p in BRIDGE_DIR.glob("*.json") if p.name != "live_state.json"]
        if files:
            def _sort_key(p: Path):
                m = re.search(r"-(\d{4})\.json$", p.name)
                seq = int(m.group(1)) if m else 0
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0.0
                return (mtime, seq, p.name)
            return max(files, key=_sort_key)
        return None
    p = BRIDGE_DIR / safe
    if p.is_file():
        return p
    if not safe.endswith(".json"):
        p_ext = BRIDGE_DIR / f"{safe}.json"
        if p_ext.is_file():
            return p_ext
    return None


def get_state(name: str) -> BalatroState:
    p = _snapshot_path(name)
    if p is None:
        raise FileNotFoundError(name or "live")
    mtime = p.stat().st_mtime
    cache_key = str(p.resolve())
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
    state = load_state(p)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            oldest = next(iter(_CACHE), None)
            if oldest:
                _CACHE.pop(oldest, None)
        _CACHE[cache_key] = (mtime, state)
    return state


def state_payload(state: BalatroState) -> Dict[str, Any]:
    return state.to_dict()


async def _recommend(name: str, kind: str) -> Dict[str, Any]:
    state = get_state(name)
    advisor = get_advisor(kind)
    rec = await advisor.recommend(state)
    chosen = next((a for a in rec.legal_actions if a["id"] == rec.candidate_id), None)
    # The presentation model is built server-side so viz.py stays the single
    # source of truth for bars / confidence banding / token budget / lint and
    # this viewer cannot drift from the Hearthstone one. It is a pure transform
    # of data the advisor already produced — it never feeds back into a verdict.
    report = build_report(
        rec.evidence,
        {rec.question.get("id", "balatro_action"): rec.question.get("criteria", {})},
        primitive=rec.primitive,
        distribution=rec.distribution,
        selected=rec.candidate_id,
        confidence=rec.confidence,
        options=rec.question.get("options"),
        labels={a["id"]: a.get("description", a["id"]) for a in rec.legal_actions},
        is_fallback=rec.is_fallback,
    )
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
        "viz": report,
    }


async def _replay(kind: str) -> Dict[str, Any]:
    """Advisor over every pre-decision snapshot; pair with the following
    action snapshot to compare against the human's actual move."""
    files = [p for p in BRIDGE_DIR.glob("*.json") if p.name != "live_state.json"] if BRIDGE_DIR.is_dir() else []
    def _seq_key(p: Path):
        m = re.search(r"-(\d{4})\.json$", p.name)
        seq = int(m.group(1)) if m else 0
        return (seq, p.name)
    chronological = [p.name for p in sorted(files, key=_seq_key)]
    loaded = [(name, get_state(name)) for name in chronological]
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
            "ante": state.ante,
            "round": state.round,
            "blind": state.blind.name,
            "advisor": rec["candidate_id"],
            "advisor_desc": rec["description"],
            "confidence": rec["confidence"],
            "human": human,
        })
    return {"provider": kind, "reports": reports}


def _dispatch_game_action(action: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """Send an in-game action to the running Balatro instance via bridge eval."""
    if action not in VALID_GAME_ACTIONS:
        logging.warning("Refused invalid game action: %s", action)
        return False

    args = args or {}
    indices = args.get("indices")
    if indices is not None:
        try:
            lua_indices = "{" + ",".join(str(int(i) + 1) for i in indices if int(i) >= 0) + "}"
        except (TypeError, ValueError):
            logging.warning("Invalid indices format: %s", indices)
            return False
    else:
        lua_indices = "nil"

    idx = args.get("index")
    if idx is not None:
        try:
            i_val = int(idx)
            if i_val < 0:
                return False
            lua_idx = str(i_val + 1)
        except (TypeError, ValueError):
            logging.warning("Invalid index format: %s", idx)
            return False
    else:
        lua_idx = "nil"

    area = args.get("area")
    if area is not None:
        if area not in VALID_AREAS:
            logging.warning("Refused invalid game area: %s", area)
            return False
        lua_area = f"'{area}'"
    else:
        lua_area = "nil"

    lua_code = f"""
if G and G.DECIDEX_EXECUTE then
  G.DECIDEX_EXECUTE('{action}', {{
    indices = {lua_indices},
    index = {lua_idx},
    area = {lua_area}
  }})
end
"""
    eval_p = Path("/tmp/balatro_eval.lua")
    tmp_p = eval_p.parent / f"balatro_eval_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
    with _DISPATCH_LOCK:
        try:
            tmp_p.write_text(lua_code, encoding="utf-8")
            os.replace(tmp_p, eval_p)
            return True
        except OSError as e:
            logging.error("Failed to write balatro_eval: %s", e)
            try:
                if tmp_p.is_file():
                    tmp_p.unlink()
            except OSError:
                pass
            return False


async def _execute_step(action_type: Optional[str] = None,
                        indices: Optional[List[int]] = None,
                        index: Optional[int] = None,
                        area: Optional[str] = None,
                        kind: str = "heuristic") -> Dict[str, Any]:
    """Execute a single gameplay step or auto-step based on current live state."""
    if action_type not in (None, "", "auto") and action_type not in VALID_GAME_ACTIONS:
        return {"ok": False, "error": f"unrecognized action_type: {action_type}"}

    if action_type in ("play", "discard"):
        args: Dict[str, Any] = {}
        if indices is not None:
            try:
                args["indices"] = [int(i) for i in indices if int(i) >= 0]
            except (TypeError, ValueError):
                return {"ok": False, "error": "indices must be integers"}
            if not args["indices"]:
                return {"ok": False, "error": f"{action_type} requires at least one card index"}
        ok = _dispatch_game_action(action_type, args)
        act_cn = "出牌" if action_type == "play" else "弃牌"
        desc = f"已执行 {act_cn}" + (f" ({len(indices)} 张)" if indices is not None else " (当前选中牌)")
        return {
            "ok": ok,
            "executed": ok,
            "action_type": action_type,
            "indices": indices,
            "description": desc,
        }

    if action_type == "toggle_card":
        if index is None:
            return {"ok": False, "error": "toggle_card requires index"}
        try:
            idx_val = int(index)
            if idx_val < 0:
                return {"ok": False, "error": "index must be non-negative"}
        except (TypeError, ValueError):
            return {"ok": False, "error": "index must be an integer"}
        ok = _dispatch_game_action("toggle_card", {"index": idx_val})
        return {
            "ok": ok,
            "executed": ok,
            "action_type": action_type,
            "index": idx_val,
            "description": f"已切换第 {idx_val + 1} 张手牌选中状态",
        }

    if action_type == "select_cards":
        if indices is None:
            idxs = []
        else:
            try:
                idxs = [int(i) for i in indices if int(i) >= 0]
            except (TypeError, ValueError):
                return {"ok": False, "error": "indices must be integers"}
        ok = _dispatch_game_action("select_cards", {"indices": idxs})
        return {
            "ok": ok,
            "executed": ok,
            "action_type": action_type,
            "indices": idxs,
            "description": f"已选中指定手牌 ({len(idxs)} 张)",
        }

    if action_type == "unhighlight_all":
        ok = _dispatch_game_action("unhighlight_all")
        return {
            "ok": ok,
            "executed": ok,
            "action_type": action_type,
            "description": "已取消所有手牌选中",
        }

    if action_type in ("buy_card", "buy_from_shop"):
        if index is None:
            return {"ok": False, "error": f"{action_type} requires index"}
        try:
            idx_val = int(index)
            if idx_val < 0:
                return {"ok": False, "error": "index must be non-negative"}
        except (TypeError, ValueError):
            return {"ok": False, "error": "index must be an integer"}
        card_area = area or "shop_jokers"
        if card_area not in VALID_AREAS:
            return {"ok": False, "error": f"invalid area: {card_area}"}
        args = {"index": idx_val, "area": card_area}
        ok = _dispatch_game_action("buy_card", args)
        return {
            "ok": ok,
            "executed": ok,
            "action_type": action_type,
            "index": idx_val,
            "area": card_area,
            "description": f"已购买商店第 {idx_val + 1} 张卡牌",
        }

    if action_type in ("sort_rank", "sort_suit"):
        ok = _dispatch_game_action(action_type)
        desc = "按点数排序手牌" if action_type == "sort_rank" else "按花色排序手牌"
        return {"ok": ok, "executed": ok, "action_type": action_type, "description": f"已{desc}"}

    if action_type == "reroll_shop":
        ok = _dispatch_game_action("reroll_shop")
        return {"ok": ok, "executed": ok, "action_type": action_type, "description": "已重掷商店"}

    if action_type in ("use_card", "sell_card"):
        if index is None:
            return {"ok": False, "error": f"{action_type} requires index"}
        try:
            idx_val = int(index)
            if idx_val < 0:
                return {"ok": False, "error": "index must be non-negative"}
        except (TypeError, ValueError):
            return {"ok": False, "error": "index must be an integer"}
        card_area = area or ("consumables" if action_type == "use_card" else "jokers")
        if card_area not in VALID_AREAS:
            return {"ok": False, "error": f"invalid area: {card_area}"}
        args = {"index": idx_val, "area": card_area}
        ok = _dispatch_game_action(action_type, args)
        desc = f"已{'使用' if action_type == 'use_card' else '出售'} {card_area} 第 {idx_val + 1} 张"
        return {"ok": ok, "executed": ok, "action_type": action_type, "index": idx_val, "area": card_area, "description": desc}

    if action_type in ("select_blind", "cash_out", "toggle_shop", "start_run"):
        ok = _dispatch_game_action(action_type)
        action_names = {
            "select_blind": "选择下一盲注",
            "cash_out": "结算本轮奖励进入商店",
            "toggle_shop": "离开商店进入下一盲注",
            "start_run": "开始新对局",
        }
        return {
            "ok": ok,
            "executed": ok,
            "action_type": action_type,
            "description": f"已执行 {action_names.get(action_type, action_type)}",
        }

    try:
        live_state = get_state("live")
    except FileNotFoundError:
        return {"ok": False, "error": "live_state.json not found"}
    except Exception as e:
        return {"ok": False, "error": f"read live_state error: {e}"}

    st_name = live_state.state_name or "unknown"

    if st_name == "SELECTING_HAND":
        blind = live_state.blind
        if blind.chips > 0 and blind.scored >= blind.chips:
            return {"ok": True, "executed": False, "state_name": st_name, "action_type": "wait", "description": "目标分数已达成，等待结算进入商店"}
        if live_state.hands_left <= 0:
            return {"ok": False, "error": "no hands left"}
        if not live_state.hand:
            return {"ok": False, "error": "hand is empty"}

        rec = await _recommend("live", kind)
        act = rec.get("action_type")
        idxs = rec.get("indices", [])
        if act in ("play", "discard"):
            if not idxs:
                return {"ok": False, "error": f"no card indices recommended for {act}"}
            ok = _dispatch_game_action(act, {"indices": idxs})
            return {
                "ok": ok,
                "executed": ok,
                "state_name": st_name,
                "action_type": act,
                "indices": idxs,
                "description": rec.get("description", ""),
                "recommendation": rec,
            }
        return {"ok": False, "error": f"unsupported recommended action {act}"}

    elif st_name == "BLIND_SELECT":
        ok = _dispatch_game_action("select_blind")
        return {
            "ok": ok,
            "executed": ok,
            "state_name": st_name,
            "action_type": "select_blind",
            "description": "选择下一盲注",
        }

    elif st_name == "ROUND_EVAL":
        ok = _dispatch_game_action("cash_out")
        return {
            "ok": ok,
            "executed": ok,
            "state_name": st_name,
            "action_type": "cash_out",
            "description": "结算本轮奖励进入商店",
        }

    elif st_name == "SHOP":
        ok = _dispatch_game_action("toggle_shop")
        return {
            "ok": ok,
            "executed": ok,
            "state_name": st_name,
            "action_type": "toggle_shop",
            "description": "离开商店进入下一盲注",
        }

    elif st_name == "MENU":
        ok = _dispatch_game_action("start_run")
        return {
            "ok": ok,
            "executed": ok,
            "state_name": st_name,
            "action_type": "start_run",
            "description": "开始新对局",
        }

    else:
        return {
            "ok": True,
            "executed": False,
            "waiting": True,
            "state_name": st_name,
            "description": f"游戏状态过渡中 ({st_name})，等待完成",
        }


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
        if extra:
            for k, v in extra.items():
                if isinstance(v, list):
                    for sub_v in v:
                        self.send_header(k, sub_v)
                else:
                    self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200, extra: Optional[Dict[str, str]] = None):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   extra=extra)

    def _auth_via(self) -> Optional[str]:
        """How the caller proved input rights: 'session' (ambient cookie) or
        'token' (explicit X-Input-Token header for mesh scripts), else None."""
        balatro_tok, sso_tok = _cookie_tokens(self.headers.get("Cookie"))
        if _valid_session(balatro_tok, sso_tok):
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
        if _LOOP is None or not _LOOP.is_running():
            return asyncio.run(coro)
        return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout=120)

    @staticmethod
    def _guess_wasm_ctype(fname: str) -> str:
        if fname.endswith((".wasm", ".wasm.bin")):
            return "application/wasm"
        if fname.endswith((".love", ".love.zip", ".bin", ".zip")):
            return "application/octet-stream"
        if fname.endswith(".js"):
            return "application/javascript; charset=utf-8"
        if fname.endswith(".css"):
            return "text/css; charset=utf-8"
        if fname.endswith(".html"):
            return "text/html; charset=utf-8"
        if fname.endswith(".json"):
            return "application/json; charset=utf-8"
        return "application/octet-stream"

    @classmethod
    def _resolve_wasm_file(cls, fname: str) -> tuple[Path | None, str]:
        real_name = fname
        if fname in ("balatro.love.zip", "balatro.bin"):
            real_name = "balatro.love"
        elif fname == "love.wasm.bin":
            real_name = "love.wasm"

        if not real_name or real_name.startswith("."):
            return None, ""

        try:
            target = (WASM_WEB_DIR / real_name).resolve()
            root = WASM_WEB_DIR.resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return None, ""
            return target, cls._guess_wasm_ctype(fname)
        except (ValueError, OSError):
            return None, ""

    def _send_file(self, target: Path, ctype: str, cache_seconds: int = 0, is_head: bool = False):
        if not target.is_file():
            return self._json({"error": f"file not found: {target.name}"}, 404)
        stat = target.stat()
        file_size = stat.st_size
        etag = f'"{file_size}-{int(stat.st_mtime)}"'

        # Check for conditional GET (If-None-Match / ETag)
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match and (if_none_match == etag or if_none_match == f'W/{etag}' or if_none_match == "*"):
            self.send_response(304)
            self.send_header("ETag", etag)
            if cache_seconds > 0:
                is_immutable = target.suffix in (".wasm", ".love", ".zip", ".bin") or ".wasm." in target.name or ".love." in target.name
                cache_val = f"public, max-age={cache_seconds}, immutable" if is_immutable else f"public, max-age={cache_seconds}"
                self.send_header("Cache-Control", cache_val)
            self.end_headers()
            return

        # Check for a single HTTP byte range. Multi-range responses are not
        # needed by the asset loader, so serve the complete file for those.
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        is_range = False

        if range_header and range_header.startswith("bytes="):
            range_spec = range_header[6:].strip()
            if "," not in range_spec:
                parts = range_spec.split("-", 1)
                try:
                    if len(parts) != 2 or (not parts[0] and not parts[1]):
                        raise ValueError
                    if parts[0] and parts[1]:
                        start = int(parts[0])
                        end = min(int(parts[1]), file_size - 1)
                    elif parts[0]:
                        start = int(parts[0])
                    else:
                        suffix_length = int(parts[1])
                        if suffix_length <= 0:
                            raise ValueError
                        start = max(file_size - suffix_length, 0)

                    if start >= file_size or start > end:
                        raise ValueError
                    is_range = 0 <= start <= end < file_size
                except ValueError:
                    # RFC 7233 Section 4.4: unsatisfiable ranges must carry the
                    # complete size so resumable clients can recover correctly.
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return

        if is_range:
            content_length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        else:
            content_length = file_size
            self.send_response(200)

        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(content_length))
        self.send_header("ETag", etag)
        self.send_header("Accept-Ranges", "bytes")
        if cache_seconds > 0:
            is_immutable = target.suffix in (".wasm", ".love", ".zip", ".bin") or ".wasm." in target.name or ".love." in target.name
            cache_val = f"public, max-age={cache_seconds}, immutable" if is_immutable else f"public, max-age={cache_seconds}"
            self.send_header("Cache-Control", cache_val)
        else:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.end_headers()

        if not is_head:
            try:
                with open(target, "rb") as f:
                    if start > 0:
                        f.seek(start)
                    remaining = content_length
                    bufsize = 512 * 1024
                    while remaining > 0:
                        chunk = f.read(min(remaining, bufsize))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_HEAD(self):
        try:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                wasm_index = WASM_WEB_DIR / "index.html"
                if wasm_index.is_file():
                    return self._send_file(wasm_index, "text/html; charset=utf-8", 0, is_head=True)
                page = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path in ("/viz.js", "/viz.css"):
                name = Path(path).name
                target = SHARED_DIR / name
                if not target.is_file():
                    return self._json({"error": "shared asset missing: " + name}, 404)
                ctype = ("application/javascript; charset=utf-8" if name.endswith(".js")
                         else "text/css; charset=utf-8")
                return self._send(200, b"", ctype)

            fname = path.lstrip("/")
            wasm_target, ctype = self._resolve_wasm_file(fname)
            if wasm_target:
                if fname.endswith((".wasm", ".wasm.bin", ".love", ".love.zip", ".bin", ".zip")):
                    cache_sec = 604800
                elif fname.endswith((".js", ".lua", ".json", ".html")):
                    cache_sec = 0
                else:
                    cache_sec = 86400
                return self._send_file(wasm_target, ctype, cache_seconds=cache_sec, is_head=True)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def do_GET(self):
        try:
            # self.path still carries the query string; route on the bare path
            # so /login?next=%2F (the 401 redirect target) is served, not 404'd.
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                wasm_index = WASM_WEB_DIR / "index.html"
                if wasm_index.is_file():
                    return self._send_file(wasm_index, "text/html; charset=utf-8", cache_seconds=0)
                page = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
                # Inject the real game screen size so the page's click-coordinate
                # mapping can never drift from the server's xdotool clamp.
                page = page.replace("__GAME_DIMS__", f"{GAME_W}x{GAME_H}")
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path in ("/stream", "/stream/", "/stream/index.html"):
                page = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
                page = page.replace("__GAME_DIMS__", f"{GAME_W}x{GAME_H}")
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

            if path in ("/viz.js", "/viz.css"):
                name = Path(path).name
                target = SHARED_DIR / name
                if not target.is_file():
                    return self._json({"error": "shared asset missing: " + name}, 404)
                ctype = ("application/javascript; charset=utf-8" if name.endswith(".js")
                         else "text/css; charset=utf-8")
                return self._send(200, target.read_bytes(), ctype)

            # Serve WASM static bundle assets if requested
            fname = path.lstrip("/")
            wasm_target, ctype = self._resolve_wasm_file(fname)
            if wasm_target:
                if fname.endswith((".wasm", ".wasm.bin", ".love", ".love.zip", ".bin", ".zip")):
                    cache_sec = 604800
                elif fname.endswith((".js", ".lua", ".json", ".html")):
                    cache_sec = 0
                else:
                    cache_sec = 86400
                return self._send_file(wasm_target, ctype, cache_seconds=cache_sec)
            if path in ("/login", "/login.html"):
                balatro_tok, sso_tok = _cookie_tokens(self.headers.get("Cookie"))
                if _valid_session(balatro_tok, sso_tok):
                    qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                    nxt = _safe_next_path(parse_qs(qs).get("next", [None])[0])
                    return self._send(302, b"", "text/plain", extra={"Location": nxt})
                page = (Path(__file__).parent / "login.html").read_text(encoding="utf-8")
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                nxt = _safe_next_path(parse_qs(qs).get("next", [None])[0])
                page = page.replace("__NEXT_PATH__", json.dumps(nxt))
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/session":
                balatro_tok, sso_tok = _cookie_tokens(self.headers.get("Cookie"))
                sso_payload = _verify_sso_token(sso_tok) if sso_tok else None
                authed = _valid_session(balatro_tok, sso_tok)
                user = (sso_payload.get("u") if sso_payload else None) or ("operator" if authed else None)
                return self._json({"authenticated": authed, "user": user, "sso": bool(sso_payload)})
            if path.startswith("/api/frame.jpg"):
                with _FRAME_LOCK:
                    if not FRAME_PATH.exists():
                        return self._json({"error": "no frame yet"}, 503)
                    data = FRAME_PATH.read_bytes()
                    frame_etag = _FRAME_ETAG or hashlib.sha1(data).hexdigest()[:16]
                    etag = '"%s"' % frame_etag
                # If requested with explicit fresh query parameter (e.g. ?_t=... after interactive click):
                # bypass edge caching completely so clicks immediately reflect on the viewer.
                is_fresh_req = "?" in self.path and ("_t=" in self.path or "t=" in self.path)
                if not is_fresh_req and self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control",
                                     f"public, max-age={FRAME_MAXAGE}, s-maxage={FRAME_MAXAGE}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                cache_ctrl = ("no-store, no-cache, must-revalidate, max-age=0"
                              if is_fresh_req
                              else f"public, max-age={FRAME_MAXAGE}, s-maxage={FRAME_MAXAGE}")
                self._send(200, data, "image/jpeg", extra={
                    "ETag": etag,
                    "Cache-Control": cache_ctrl,
                })
                return
            if path == "/api/health":
                has_live = (BRIDGE_DIR / "live_state.json").is_file() if BRIDGE_DIR.is_dir() else False
                qs = dict(pair.split("=", 1) for pair in self.path.split("?", 1)[1].split("&")) \
                    if "?" in self.path else {}
                try:
                    limit_val = int(qs.get("limit", "60"))
                except ValueError:
                    limit_val = 60
                return self._json({
                    "ok": True, "engine": "balatro-advisor",
                    "wasm_available": (WASM_WEB_DIR / "balatro.love").is_file(),
                    "bridge_dir": str(BRIDGE_DIR),
                    "has_live": has_live,
                    "games": list_games(limit=limit_val),
                })
            if path.startswith("/api/game"):
                qs = dict(pair.split("=", 1) for pair in self.path.split("?", 1)[1].split("&")) \
                    if "?" in self.path else {}
                name = qs.get("name", "live")
                state = get_state(name)
                payload = state_payload(state)
                p = _snapshot_path(name)
                payload["_snapshot"] = p.name if p else name
                payload["_is_live"] = (name in ("live", "latest", "") or (p and p.name == "live_state.json"))
                return self._json(payload)
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": f"snapshot not found: {e}"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            # Strip the query string for routing, same as do_GET.
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            kind = str(req.get("kind", "heuristic"))
            if path == "/api/login":
                if not self._has_csrf_header():
                    return self._json({"error": "csrf"}, 403)
                if not _check_credentials(str(req.get("user", "")),
                                          str(req.get("password", ""))):
                    return self._json({"error": "invalid credentials"}, 401)
                token = _new_session()
                cookies = [
                    f"{_SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={_SESSION_TTL}"
                ]
                if _SSO_SECRET:
                    sso_token = _create_sso_token(str(req.get("user", "")))
                    cookies.append(
                        f"{_SSO_COOKIE}={sso_token}; Path=/; Domain=.mangoqwq.com; HttpOnly; SameSite=Lax; Secure; Max-Age={_SESSION_TTL}"
                    )
                return self._json({"ok": True}, extra={"Set-Cookie": cookies})
            if path == "/api/logout":
                _drop_session(_cookie_token(self.headers.get("Cookie")))
                cookies_to_drop = [
                    f"{_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
                    f"{_SSO_COOKIE}=; Path=/; Domain=.mangoqwq.com; HttpOnly; SameSite=Lax; Secure; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
                ]
                return self._json({"ok": True}, extra={"Set-Cookie": cookies_to_drop})
            if path == "/api/recommend":
                return self._json(self._run_async(_recommend(str(req.get("name", "")), kind)))
            if path == "/api/replay":
                return self._json(self._run_async(_replay(kind)))
            if path == "/api/execute":
                deny = self._deny_input()
                if deny:
                    return self._json({"error": "unauthorized" if deny == 401 else "csrf"}, deny)
                action_type = req.get("action_type") or req.get("action")
                indices = req.get("indices")
                index = req.get("index")
                area = req.get("area")
                res = self._run_async(_execute_step(action_type, indices=indices, index=index, area=area, kind=kind))
                return self._json(res)
            if path == "/api/input/click":
                deny = self._deny_input()
                if deny:
                    return self._json({"error": "unauthorized" if deny == 401 else "csrf"}, deny)
                try:
                    x = int(req.get("x", 0)); y = int(req.get("y", 0))
                except (TypeError, ValueError):
                    return self._json({"error": "bad coords"}, 400)
                out = _xdotool(_click_args(x, y, bool(req.get("double"))))
                return self._json({"ok": not _xdotool_failed(out), "out": out[:120]})
            if path == "/api/input/key":
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
# Fingerprint of the current served frame (sha1 of its bytes). Cloudflare's
# edge revalidates with If-None-Match; when the frame is unchanged we answer
# 304 with no body, so an idle client costs ~0 egress instead of re-downloading
# an identical 18 KB JPEG every poll.
_FRAME_ETAG: str = ""
_FRAME_LOCK = threading.Lock()


def _shrink_frame() -> None:
    """Re-encode the raw capture into the small frame the uplink can afford.
    draft() decodes the source JPEG at a reduced DCT scale (cheap on this
    CPU-starved box) before the LANCZOS resize, and we always start from the
    full-size raw capture, so quality never accumulates loss. If Pillow is
    unavailable we fall back to the raw frame rather than serving nothing."""
    global _FRAME_ETAG
    temp_path = FRAME_PATH.with_name(
        FRAME_PATH.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow is optional; raw frame still works
        temp_path.write_bytes(FRAME_RAW_PATH.read_bytes())
    else:
        with Image.open(FRAME_RAW_PATH) as im:
            im.draft("RGB", (FRAME_WIDTH, 1))   # 1 keeps draft from demanding full height
            if im.width <= FRAME_WIDTH:
                im.convert("RGB").save(temp_path, "JPEG",
                                        quality=FRAME_QUALITY, optimize=True)
            else:
                height = max(1, round(im.height * FRAME_WIDTH / im.width))
                im.convert("RGB").resize((FRAME_WIDTH, height), Image.LANCZOS).save(
                    temp_path, "JPEG", quality=FRAME_QUALITY, optimize=True)
    try:
        with _FRAME_LOCK:
            data = temp_path.read_bytes()
            etag = hashlib.sha1(data).hexdigest()[:16]
            os.replace(temp_path, FRAME_PATH)
            _FRAME_ETAG = etag
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _frame_worker() -> None:
    """Connect to the live game's VNC server and keep the latest frame JPEG
    on disk so /api/frame.jpg can serve the real game feed. One persistent
    client, auto-reconnect on failure. Frames are captured full-size to
    FRAME_RAW_PATH and then shrunk into FRAME_PATH before serving."""
    if not VNC_TARGET or VNC_TARGET.lower() in ("none", "disabled", "off", "0"):
        logging.getLogger().info("live feed: VNC capturing disabled by configuration")
        return

    last_log_time = 0.0
    backoff = 3.0
    while True:
        client = None
        try:
            from vncdotool import api
            client = api.connect(VNC_TARGET, password=VNC_PASSWORD or None,
                                 timeout=VNC_TIMEOUT)
            logging.getLogger().info("live feed: connected to %s", VNC_TARGET)
            backoff = 3.0
            while True:
                client.captureScreen(str(FRAME_RAW_PATH))
                if FRAME_RAW_PATH.exists():
                    _shrink_frame()
                time.sleep(FRAME_INTERVAL)
        except Exception as e:  # noqa: BLE001 - keep the feed alive forever
            now = time.time()
            if now - last_log_time > 60.0:
                logging.getLogger().warning("live feed: %s; reconnecting in %.0fs (muted for 60s)", e, backoff)
                last_log_time = now
            try:
                if client is not None:
                    client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)


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
    return bool(WEB_USER and WEB_PASSWORD) and ok_user and ok_pw


def _safe_next_path(raw: Optional[str]) -> str:
    """Sanitize a post-login redirect target down to a same-origin absolute
    path. The value arrives from the query string, so it is fully
    attacker-controlled: `?next=https://evil.example` (or `//evil.example`)
    would turn the login page into an open redirect.

    A strict character whitelist is the single gate: the result must start with
    a single `/` and contain only RFC-3986 path/query characters (no backslash,
    no control chars, no `<`/`>`/`"`). That rejects host-relative and
    scheme-relative URLs *and* any value that could break out of the JS string
    literal it is injected into. Anything outside the set falls back to "/".
    The value is URL-decoded first (parse_qs), so percent-escapes can't smuggle
    a rejected character past the check."""
    if not raw:
        return "/"
    if raw.startswith("//"):
        return "/"
    if not _SAFE_NEXT_RE.match(raw):
        return "/"
    return raw


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


def _valid_session(token: Optional[str], sso_token: Optional[str] = None) -> bool:
    if sso_token:
        sso_payload = _verify_sso_token(sso_token)
        if sso_payload:
            return True
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


def _cookie_tokens(cookie_header: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not cookie_header:
        return None, None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:  # noqa: BLE001 - malformed cookie jar, treat as anonymous
        return None, None
    balatro_morsel = jar.get(_SESSION_COOKIE)
    sso_morsel = jar.get(_SSO_COOKIE)
    return (balatro_morsel.value if balatro_morsel else None,
            sso_morsel.value if sso_morsel else None)


def _cookie_token(cookie_header: Optional[str]) -> Optional[str]:
    balatro, _ = _cookie_tokens(cookie_header)
    return balatro


def _click_args(x: int, y: int, double: bool = False) -> list:
    """Clamp to the game window and build the xdotool argv for one click.
    Bounds track the Xvfb :66 screen (BALATRO_GAME_W/H), which may be shrunk
    from the original 1280x800 to cut render cost and frame size."""
    args = ["mousemove", str(max(0, min(GAME_W - 1, x))), str(max(0, min(GAME_H - 1, y))), "sleep", "0.05"]
    if double:
        return args + ["mousedown", "1", "sleep", "0.05", "mouseup", "1", "sleep", "0.05", "mousedown", "1", "sleep", "0.05", "mouseup", "1"]
    return args + ["mousedown", "1", "sleep", "0.05", "mouseup", "1"]


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
    ap.add_argument("--host", default=os.environ.get("BALATRO_WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BALATRO_WEB_PORT", "8091")))
    args = ap.parse_args()
    if not WEB_USER or not WEB_PASSWORD:
        ap.error("BALATRO_WEB_USER and BALATRO_WEB_PASSWORD must both be set")

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
