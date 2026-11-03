"""Deep-diagnosis full-flow probe: drive AutoPilot through whole games while
mirroring operational console lines and sampling the JS autopilot's internal
state, so a wedge carries its own explanation in the log.

Unlike test_full_flow_soak.py (which only prints watchdog/state lines), this
probe prints every [DECIDEX_ACTION_RESULT] (including transient rejections),
periodically emits a JS internal snapshot (in-flight lock, busy, skip latches,
pack signature), and on a stall dumps BOTH sides: the Lua SOAK-DUMP (bridge
eval) and the JS snapshot plus a screenshot. High-volume [DECIDEX_STATE]
console lines are suppressed; the probe reads state directly from the page.

Env:
  PROBE_URL           target (default https://balatro.mangoqwq.com/)
  PROBE_MIN_GAMES     completed games before success (default 2)
  PROBE_MAX_SECONDS   wall-clock cap (default 2400)
  PROBE_HEADLESS      "0" to run headed
  PROBE_PROFILE       persistent-context user data dir; caches the 53.7MB
                      game package per-origin across runs (essential against
                      production, where every cold download competes with a
                      flaky Cloudflare edge)
"""

import asyncio
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

from harness_support import (
    TERMINAL_LATCH_READ,
    TERMINAL_LATCH_SCRIPT,
    positive_finite_float,
    positive_viewport,
)

BASE_URL = os.environ.get("PROBE_URL", "https://balatro.mangoqwq.com/")
MIN_GAMES = int(os.environ.get("PROBE_MIN_GAMES", "2"))
MAX_SECONDS = int(os.environ.get("PROBE_MAX_SECONDS", "2400"))
HEADLESS = os.environ.get("PROBE_HEADLESS", "1") != "0"
# Low-power defaults: 1024x576 requests about 72% fewer canvas pixels than
# 1920x1080. This is expected to reduce rasterization work; actual CPU savings
# depend on the browser renderer and host. The probe is state-driven and only
# needs screenshots legible enough for a human.
VIEWPORT = positive_viewport("PROBE_VIEWPORT", "1024x576")
POLL_SECONDS = positive_finite_float("PROBE_POLL", 2)
DIAG_EVERY = positive_finite_float("PROBE_DIAG_EVERY", 30)

SCREENSHOT_DIR = Path("tools/balatro_wasm/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Kills the console flood AT SOURCE: the bridge emits a multi-KB
# [DECIDEX_STATE] JSON several times per second, and every emission cost a
# CDP serialization + a Python handler + a log-file write -- on a long soak
# that was hundreds of MB of pure I/O for data the probe never reads from
# console lines (it polls window._lastDecidexState directly). Dropped lines
# go into a small ring buffer so a stall dump can still show the last ones.
CONSOLE_SHIM = """
(() => {
  const orig = console.log.bind(console);
  const w = window.__dxShim = { dropped: 0, ring: [] };
  console.log = function (...args) {
    try {
      const line = args.length === 1 ? String(args[0]) : args.join(' ');
      if (line.startsWith('[DECIDEX_STATE]')) {
        w.dropped++;
        w.ring.push(line.slice(0, 400));
        if (w.ring.length > 2) w.ring.shift();
        return;
      }
    } catch (_) {}
    orig(...args);
  };
})();
"""

DWELL_BUDGETS = {
    "MENU": 45,
    "BLIND_SELECT": 60,
    "SELECTING_HAND": 90,
    "DRAW_TO_HAND": 30,
    "HAND_PLAYED": 60,
    "ROUND_EVAL": 60,
    "PLAY_TAROT": 60,
    "SHOP": 60,
    "TAROT_PACK": 75,
    "PLANET_PACK": 75,
    "SPECTRAL_PACK": 75,
    "STANDARD_PACK": 75,
    "BUFFOON_PACK": 75,
}
DEFAULT_BUDGET = 75
STALL_GRACE = 1.1

JS_DIAG = """() => {
  const ap = window.AutoPilot || {};
  const st = window._lastDecidexState || {};
  const packs = (st.pack_cards || []).map(c => (c.name || '?') + (c.highlighted ? '*' : ''));
  return JSON.stringify({
    t: Math.round(performance.now() / 1000),
    state: st.state_name,
    ante: st.ante, round: st.round, money: st.money,
    hand: (st.hand || []).length,
    consumables: (st.consumables || []).length + '/' + (st.consumable_slots || '?'),
    jokers: (st.jokers || []).length + '/' + (st.joker_slots || '?'),
    can_skip: st.can_skip_booster,
    pack: packs.join('|'),
    ap_enabled: !!ap.enabled, ap_busy: !!ap.busy,
    inflight: ap._actionInFlight || null,
    busy_ms: ap._busyTimestamp ? Math.round(Date.now() - ap._busyTimestamp) : null,
    since_progress_ms: ap._lastProgressTimestamp ? Math.round(Date.now() - ap._lastProgressTimestamp) : null,
    skip_latch: !!ap._skipCurrentPack,
    pack_epoch: st.pack_epoch,
    states_dropped: (window.__dxShim || {}).dropped || 0,
    last_states: ((window.__dxShim || {}).ring || []).map(s => s.slice(0, 90)),
    status: (document.getElementById('autopilot-status') || {}).textContent || ''
  });
}"""

LUA_DUMP = """() => {
  window.__probeSeq = (window.__probeSeq || 900000) + 1;
  const FS = window.Module && window.Module.FS;
  if (!FS) return;
  try { FS.unlink('/tmp/balatro_eval_ack.txt'); } catch (_) {}
  FS.writeFile('/tmp/balatro_eval.lua', '-- DECIDEX_SEQ:' + window.__probeSeq +
    '\\nprint("[LUA-DUMP] STATE=" .. tostring(G.STATE) .. " complete=" .. tostring(G.STATE_COMPLETE)' +
    ' .. " paused=" .. tostring(G.SETTINGS and G.SETTINGS.paused)' +
    ' .. " overlay=" .. tostring(not not G.OVERLAY_MENU)' +
    ' .. " booster=" .. tostring(G.booster_pack ~= nil)' +
    ' .. " pack_cards=" .. tostring(G.pack_cards and G.pack_cards.cards and #G.pack_cards.cards)' +
    ' .. " pack_hl=" .. tostring(G.pack_cards and G.pack_cards.highlighted and #G.pack_cards.highlighted)' +
    ' .. " pack_rem=" .. tostring(G.pack_cards and G.pack_cards.REMOVED)' +
    ' .. " pack_intent=" .. tostring(G.pack_cards and G.pack_cards.config and G.pack_cards.config.card_limit)' +
    ' .. " hand=" .. tostring(G.hand and G.hand.cards and #G.hand.cards)' +
    ' .. " pkint=" .. tostring(G.GAME and G.GAME.PACK_INTERRUPT)' +
    ' .. " tarint=" .. tostring(G.TAROT_INTERRUPT)' +
    ' .. " exitlatch=" .. tostring(_G._decidex_pack_exit_until and (_G._decidex_pack_exit_until - (love.timer and love.timer.getTime() or 0)))' +
    ' .. " stallt=" .. tostring(_G._pack_stall_time)' +
    ' .. " queue=" .. (function() local p={} if G.E_MANAGER and G.E_MANAGER.queues then for k,v in pairs(G.E_MANAGER.queues) do if type(v)=="table" then p[#p+1]=k.."="..#v end end end return table.concat(p,",") end)()' +
    ');');
}"""


class Probe:
    def __init__(self):
        self.games = 0
        self.stalls = []
        self.stall_dumped = False
        self.prev_state = None
        self.state_since = time.monotonic()
        self.terminal_total = 0
        self.terminal_seq = 0
        self.terminal_events = []

    async def on_stall(self, page, sname, dwell, budget, t):
        note = f"{t:.0f}s state={sname} dwell={dwell:.0f}s budget={budget}s"
        self.stalls.append(note)
        print(f"[Probe] *** STALL *** {note}", flush=True)
        try:
            print(f"[Probe] JS {await page.evaluate(JS_DIAG)}", flush=True)
        except Exception as e:
            print(f"[Probe] JS diag failed: {e}", flush=True)
        await page.evaluate(LUA_DUMP)
        try:
            await page.screenshot(path=str(SCREENSHOT_DIR / f"probe_stall_{int(t)}.jpg"), type="jpeg", quality=55)
        except Exception:
            pass


async def run():
    probe = Probe()
    async with async_playwright() as p:
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--enable-webgl", "--mute-audio"]
        profile_dir = os.environ.get("PROBE_PROFILE", "")
        if profile_dir:
            os.makedirs(profile_dir, exist_ok=True)
            browser = await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir, headless=HEADLESS,
                viewport=VIEWPORT, args=launch_args,
                # The production PWA service worker (and the 4h HTTP cache on
                # app.js?v=N) can serve install-day shell bytes to a persistent
                # profile -- a probe must exercise the RELEASED bytes, so block
                # SWs here and disable the HTTP cache via CDP below.
                service_workers="block")
            page = browser.pages[0] if browser.pages else await browser.new_page()
        else:
            browser = await p.chromium.launch(headless=HEADLESS, args=launch_args)
            context = await browser.new_context(viewport=VIEWPORT, service_workers="block")
            page = context.pages[0] if context.pages else await context.new_page()

        def on_console(msg):
            t = msg.text
            if any(k in t for k in ("LONG DT", "AudioContext", "GL_INVALID", "favicon",
                                    "Download the React DevTools")):
                return
            print(f"[C] {t[:2000]}", flush=True)

        await page.add_init_script(CONSOLE_SHIM)
        page.on("console", on_console)
        page.on("pageerror", lambda e: print(f"[PageError] {e}", flush=True))

        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)
        launch_timeout = int(os.environ.get("PROBE_LAUNCH_TIMEOUT", "900")) * 1000
        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=launch_timeout)
        # The service worker serves the CACHED core (e.g. core-v110) on the
        # first load after a deploy while installing the new one in the
        # background — a probe that launches immediately plays the OLD
        # assets and tests nothing. Reload once so the updated core takes
        # control, then re-query the button (the reload detaches it).
        # Deterministic bytes: disable the HTTP cache via CDP so the reload
        # re-fetches every shell asset from the edge, and confirm WHICH
        # script the page executed (performance entries list the URL, the
        # served bytes may still differ -- see the sw-v2 incident).
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Network.enable", {})
        await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        await page.reload(wait_until="domcontentloaded")
        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=launch_timeout)
        core = await page.evaluate(
            "() => { const r = performance.getEntriesByType('resource').map(e => e.name).filter(n => n.includes('app.js')).pop(); return r ? r.slice(r.length - 12) : 'app.js?not-found'; }"
        )
        print(f"[Probe] loaded core: {core}", flush=True)
        await btn.click()
        print("[Probe] launch clicked", flush=True)
        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=120_000)
        for _ in range(30):
            await asyncio.sleep(1)
            if await page.evaluate("() => window._lastDecidexStateCount || 0"):
                break
        await page.evaluate(TERMINAL_LATCH_SCRIPT)
        await page.evaluate("() => window.AutoPilot.toggle(true)")
        print("[Probe] AutoPilot enabled with terminal-event latch", flush=True)

        start = time.monotonic()
        last_diag = 0
        while time.monotonic() - start < MAX_SECONDS:
            await asyncio.sleep(POLL_SECONDS)
            t = time.monotonic() - start
            terminal = await page.evaluate(TERMINAL_LATCH_READ)
            if terminal["total"] > probe.terminal_total:
                probe.terminal_total = terminal["total"]
                fresh_events = [e for e in terminal["events"] if e.get("seq", 0) > probe.terminal_seq]
                if fresh_events:
                    probe.terminal_seq = max(e["seq"] for e in fresh_events)
                    probe.terminal_events.extend(fresh_events)
                probe.games = probe.terminal_total
                print(f"[Probe] === terminal event(s): {probe.games} total ===", flush=True)
            st = await page.evaluate("() => window._lastDecidexState")
            if not st:
                continue
            sname = st.get("state_name") or "UNKNOWN"
            if sname != probe.prev_state:
                print(f"[Probe] {t:6.0f}s -> {sname} ante={st.get('ante')} "
                      f"round={st.get('round')} money={st.get('money')}", flush=True)
                probe.prev_state = sname
                probe.state_since = time.monotonic()
                probe.stall_dumped = False
            if t - last_diag >= DIAG_EVERY:
                last_diag = t
                try:
                    print(f"[Probe:JS] {await page.evaluate(JS_DIAG)}", flush=True)
                except Exception as e:
                    print(f"[Probe:JS] diag failed: {e}", flush=True)
            dwell = time.monotonic() - probe.state_since
            budget = DWELL_BUDGETS.get(sname, DEFAULT_BUDGET)
            if dwell > budget * STALL_GRACE and not probe.stall_dumped:
                probe.stall_dumped = True
                await probe.on_stall(page, sname, dwell, budget, t)
            if probe.games >= MIN_GAMES:
                break

        terminal = await page.evaluate(TERMINAL_LATCH_READ)
        probe.games = terminal["total"]
        print(f"\n[Probe-RESULT] games={probe.games} stalls={len(probe.stalls)}", flush=True)
        for s in probe.stalls:
            print(f"  stall: {s}", flush=True)
        ok = probe.games >= MIN_GAMES and not probe.stalls
        print(f"[Probe-RESULT] {'PASS' if ok else 'FAIL'}", flush=True)
        await browser.close()
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run())
