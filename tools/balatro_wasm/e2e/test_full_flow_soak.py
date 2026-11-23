"""Full-flow soak test: drive N complete Balatro games end to end.

Runs the DecideX AutoPilot through whole game lifecycles -- menu, run start,
blinds, shop, packs, round eval, game over, auto-restart -- and measures
where the flow still stalls. This is the "full flow" companion to
test_multi_round_prod.py (which only requires 4 blinds) and
test_probe_tarot_pack.py (which is pack-focused).

Pass criteria:
  * at least MIN_GAMES completed game-over transitions (win or loss),
  * no pageerror,
  * no hard action failures (transient rejections excluded, mirroring the
    AutoPilot's own classification),
  * no stall: a single state must not outlive its dwell budget without
    any acknowledged action.

Operational console lines are mirrored to stdout with a wall-clock prefix so
stalls can be traced against the surrounding bridge/AutoPilot chatter;
high-volume [DECIDEX_STATE] lines are suppressed at the page boundary.

Env:
  BALATRO_SOAK_URL       target (default http://127.0.0.1:8099/)
  BALATRO_SOAK_MIN_GAMES minimum completed games (default 2)
  BALATRO_SOAK_MAX_SECONDS  wall-clock cap (default 2400)
  BALATRO_LAUNCH_TIMEOUT launch-button wait in seconds (default 600)
"""

import asyncio
import os
import re
import time

try:
    from playwright.async_api import async_playwright
except ImportError:  # regex-parity tests import this module without playwright
    async_playwright = None

from harness_support import (
    TERMINAL_LATCH_READ,
    TERMINAL_LATCH_SCRIPT,
    positive_finite_float,
    positive_viewport,
    effective_stall_budget,
    scale_dwell_budget,
    classify_action_rejection,
)

BASE_URL = os.environ.get("BALATRO_SOAK_URL", "http://127.0.0.1:8099/")
MIN_GAMES = int(os.environ.get("BALATRO_SOAK_MIN_GAMES", "2"))
MAX_SECONDS = int(os.environ.get("BALATRO_SOAK_MAX_SECONDS", "2400"))

# 1024x576 requests about 72% fewer canvas pixels than 1920x1080. This is
# expected to reduce rasterization work; actual CPU savings depend on the
# browser renderer and host.
VIEWPORT = positive_viewport("BALATRO_SOAK_VIEWPORT", "1024x576")
POLL_SECONDS = positive_finite_float("BALATRO_SOAK_POLL", 2)

# The bridge emits a multi-KB [DECIDEX_STATE] JSON several times a second.
# Mirroring it through CDP console events is pure overhead for the soak
# (the state is read via page.evaluate), so drop it at the source and keep
# only a tiny ring for post-mortem inspection.
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

# Per-state dwell budgets (seconds) before a stall is declared. The
# autopilot acts well inside these on a healthy run at GAMESPEED 4; the
# budgets only trip when something is genuinely wedged.
DWELL_BUDGETS = {
    "MENU": 45,
    # Blind select legitimately takes 45-46s when the event queue is still
    # draining a busy transition (observed self-recovering in soak8b/soak6
    # with no intervention); 60s avoids flagging that as a stall.
    # v165b: deep-ante queue drains (40+ card hands at ante 9-11) measured
    # 66-68s in soak3 — every one self-recovered, so 90s covers it.
    "BLIND_SELECT": 90,
    "SELECTING_HAND": 90,
    "DRAW_TO_HAND": 80,
    "HAND_PLAYED": 80,
    "ROUND_EVAL": 60,
    # Same settlement-animation family as HAND_PLAYED/ROUND_EVAL; deep-ante
    # joker/tarot chains legitimately take 46s+ (soak20 801s self-recovered
    # at exactly 46s), so 45s flagged a healthy recovery as a stall.
    "PLAY_TAROT": 60,
    # Deep-ante shops with pack purchases + voucher browsing legitimately
    # exceed 60s (v163+ economy makes AutoPilot buy packs every shop);
    # 75s matches the pack-family budgets.
    "SHOP": 75,
    "TAROT_PACK": 75,
    "PLANET_PACK": 75,
    "SPECTRAL_PACK": 75,
    "STANDARD_PACK": 75,
    "BUFFOON_PACK": 75,
    "GAME_OVER": 30,
    "GAME_WON": 30,
    # v165b: deep-ante NEW_ROUND card-draw chains measured 34-67s in soak3 —
    # every one self-recovered and advanced. Wedged states outlive these.
    "NEW_ROUND": 80,
}
DEFAULT_BUDGET = 30

# Single source of truth for "transient rejection" classification is the
# JS isTransient / isStateMismatch pair in tools/balatro_wasm/web/app.js
# (~line 4558). This regex must mirror it word for word:
#   - "use_card failed" is deliberately NOT transient (the vanilla crash
#     behind it, "interval is empty" on an empty hand, is a broken decision
#     the caller must see);
#   - the state-mismatch alternation is folded in here because Python has
#     no separate latch for it (JS keeps isStateMismatch apart to avoid
#     consuming the pack-select fail-streak budget).
TRANSIENT_INFO_RE = re.compile(
    r"not ready|animating|debounced|in progress|busy|locked|waiting|"
    r"already in play|blocks play|slots full|pack card not found|no pack_cards|"
    r"not in a pack state|no space to buy|no-op|not set|acknowledgement timed out|"
    r"not in (BLIND_SELECT|SHOP|ROUND_EVAL|SELECTING_HAND|HAND_PLAYED|new_round)",
    re.IGNORECASE,
)

PACK_STATES = {"TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK", "STANDARD_PACK", "BUFFOON_PACK"}


class Soak:
    def __init__(self):
        self.stalls = []          # (t, state, dwell, note)
        self.page_errors = []
        self.action_failures = []
        self.benign_rejections = []  # (t, info) — bridge-emitted, client self-recovers
        self.games_completed = 0
        self.antes_seen = set()
        self.blinds_seen = []     # (ante, blind) on-deck tuples
        self.state_events = []    # (t, state) transitions
        self.watchdog_events = [] # (t, text)
        self.stall_dumped = False
        self._stall_worst = 0.0   # best dwell seen for the current state visit

    def on_console(self, msg):
        text = msg.text
        now = time.monotonic()
        verdict = classify_action_rejection(text, TRANSIENT_INFO_RE)
        if verdict != "none":
            info = text.split("info=", 1)[1].strip() if "info=" in text else ""
            if verdict == "hard":
                self.action_failures.append((f"{now:.0f}s", text[:220]))
            else:
                # Transient (JS-mirrored) or benign (bridge-emitted,
                # client self-recovers): recorded for observability, never
                # a verdict failure.
                if verdict == "benign":
                    self.benign_rejections.append((f"{now:.0f}s", info[:160]))
            return
        if any(k in text for k in ("[DecideX Pack Watchdog]", "[DecideX Event Guard]",
                                   "[DecideX Frame Guard]", "[AutoPilot Watchdog]",
                                   "[DecideX Bridge]", "[PageError]")):
            self.watchdog_events.append((f"{now:.0f}s", text[:200]))
            print(f"[{now:.0f}s] {text[:220]}", flush=True)
            return
        if "[SOAK-DUMP]" in text:
            print(f"[{now:.0f}s] {text[:260]}", flush=True)

    async def run(self):
        async with async_playwright() as p:
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--enable-webgl",
                           "--mute-audio"]
            # A persistent profile caches the 53.7MB game package across
            # runs; essential against production, where a cold download
            # competes with a flaky Cloudflare edge.
            profile_dir = os.environ.get("BALATRO_SOAK_PROFILE", "")
            if profile_dir:
                os.makedirs(profile_dir, exist_ok=True)
                b = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    viewport=VIEWPORT,
                    args=launch_args,
                )
                page = b.pages[0] if b.pages else await b.new_page()
            else:
                b = await p.chromium.launch(headless=True, args=launch_args)
                context = await b.new_context(viewport=VIEWPORT)
                page = context.pages[0] if context.pages else await context.new_page()
            await page.add_init_script(CONSOLE_SHIM)
            page.on("console", self.on_console)
            page.on("pageerror", lambda e: (self.page_errors.append(str(e)[:200]),
                                            print(f"[PageError] {e}", flush=True)))

            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)
            launch_timeout = int(os.environ.get("BALATRO_LAUNCH_TIMEOUT", "600")) * 1000
            btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=launch_timeout)
            await btn.click()
            print("[Soak] launch clicked", flush=True)
            await page.wait_for_selector("#splash-overlay", state="hidden", timeout=120_000)

            # Diagnostic channel: ask the bridge for a state dump when a
            # stall is declared.
            await page.evaluate(TERMINAL_LATCH_SCRIPT)
            await page.evaluate(
                """() => {
                    window.__soakSeq = 900000;
                    window.__soakDump = () => {
                        window.__soakSeq += 1;
                        const FS = window.Module && window.Module.FS;
                        if (!FS) return;
                        try { FS.unlink('/tmp/balatro_eval_ack.txt'); } catch (_) {}
                        FS.writeFile('/tmp/balatro_eval.lua', '-- DECIDEX_SEQ:' + window.__soakSeq +
                            '\\nprint("[SOAK-DUMP] STATE=" .. tostring(G.STATE) .. " complete=" .. tostring(G.STATE_COMPLETE)' +
                            ' .. " paused=" .. tostring(G.SETTINGS and G.SETTINGS.paused)' +
                            ' .. " overlay=" .. tostring(not not G.OVERLAY_MENU)' +
                            ' .. " booster=" .. tostring(G.booster_pack ~= nil) .. " pack_cards=" .. tostring(G.pack_cards ~= nil)' +
                            ' .. " hand=" .. tostring(G.hand and G.hand.cards and #G.hand.cards)' +
                            ' .. " interrupts=" .. tostring(G.GAME and G.GAME.PACK_INTERRUPT) .. "," .. tostring(G.TAROT_INTERRUPT)' +
                            ' .. " pack_n=" .. tostring(G.pack_cards and G.pack_cards.cards and #G.pack_cards.cards)' +
                            ' .. " pack_removed=" .. tostring(G.pack_cards and G.pack_cards.REMOVED)' +
                            ');');
                    };
                }"""
            )
            await page.evaluate("() => window.AutoPilot.toggle(true)")
            print("[Soak] AutoPilot enabled", flush=True)

            start = time.monotonic()
            prev_state = None
            state_since = start
            while time.monotonic() - start < MAX_SECONDS:
                await asyncio.sleep(POLL_SECONDS)
                t = time.monotonic() - start
                terminal = await page.evaluate(TERMINAL_LATCH_READ)
                self.games_completed = terminal["total"]
                st = await page.evaluate("() => window._lastDecidexState")
                if not st:
                    continue
                sname = st.get("state_name") or "UNKNOWN"
                if sname != prev_state:
                    self.state_events.append((f"{t:.0f}s", sname))
                    print(f"[Soak] {t:6.0f}s  -> {sname}  ante={st.get('ante')} "
                          f"round={st.get('round')} money={st.get('money')}", flush=True)
                    prev_state = sname
                    state_since = time.monotonic()
                    self.stall_dumped = False
                    self._stall_worst = 0.0

                ante = st.get("ante")
                if ante:
                    self.antes_seen.add(ante)
                deck = st.get("blind_on_deck")
                if deck:
                    key = (ante, deck)
                    if not self.blinds_seen or self.blinds_seen[-1] != key:
                        self.blinds_seen.append(key)

                dwell = time.monotonic() - state_since
                # Stall decision uses the flat 1.3x grace only; the hand-scale
                # stretch (scale_dwell_budget) is observability-only now, or a
                # 49-card hand silently doubled every stall budget.
                budget = effective_stall_budget(DWELL_BUDGETS.get(sname, DEFAULT_BUDGET))
                hand_n = len(st.get("hand") or []) if isinstance(st.get("hand"), list) else 0
                slow_budget = scale_dwell_budget(DWELL_BUDGETS.get(sname, DEFAULT_BUDGET), hand_n)
                # Every worsening beyond the budget appends to the timeline
                # (diagnosability), but the heavy js_snapshot dump fires once
                # per state visit.
                if dwell > budget:
                    # Append a new timeline record only when the stall has
                    # worsened by a meaningful step (>=10s), not on every
                    # 2s poll of a steady wedge — otherwise one 20-min wedge
                    # alone adds ~600 near-identical records to the rollup.
                    # The snapshot dump still fires once per state visit.
                    if dwell - self._stall_worst >= 10:
                        self._stall_worst = dwell
                        note = f"state={sname} dwell={dwell:.0f}s budget={budget}s hand_n={hand_n}"
                        self.stalls.append((f"{t:.0f}s", note))
                if dwell > budget and not self.stall_dumped:
                    self.stall_dumped = True
                    js_pack_n = len((st.get("pack_cards") or [])) if isinstance(st.get("pack_cards"), list) else "?"
                    print(f"[Soak] *** STALL *** {t:.0f}s state={sname} dwell={dwell:.0f}s "
                          f"stall_budget={budget}s slow_healthy_budget={slow_budget:.0f}s "
                          f"js_snapshot_pack_n={js_pack_n}", flush=True)
                    await page.evaluate("() => window.__soakDump && window.__soakDump()")

            terminal = await page.evaluate(TERMINAL_LATCH_READ)
            self.games_completed = terminal["total"]
            await b.close()

        # Metrics rollup
        print("\n[SOAK-RESULT]", flush=True)
        print(f"  wall_clock={MAX_SECONDS}s games_completed(game_over_seen)={self.games_completed}")
        print(f"  antes_seen={sorted(self.antes_seen)}")
        print(f"  blinds_on_deck_sequence={self.blinds_seen}")
        print(f"  state_transitions={len(self.state_events)}")
        print(f"  page_errors={self.page_errors}")
        print(f"  action_failures={self.action_failures}")
        print(f"  benign_rejections={len(self.benign_rejections)} (client self-recovers)")
        print(f"  stalls={self.stalls}")
        wd = {}
        for _, text in self.watchdog_events:
            key = text.split("]")[1].strip()[:60] if "]" in text else text[:60]
            wd[key] = wd.get(key, 0) + 1
        print("  interventions:", flush=True)
        for k, v in sorted(wd.items(), key=lambda kv: -kv[1]):
            print(f"    {v:3d}x {k}", flush=True)

        failures = []
        if self.page_errors:
            failures.append("page errors")
        if self.action_failures:
            failures.append("hard action failures")
        if self.stalls:
            failures.append("state stalls")
        if self.games_completed < MIN_GAMES:
            failures.append(f"completed {self.games_completed}/{MIN_GAMES} games")
        if failures:
            print(f"\n[FAIL] {', '.join(failures)}", flush=True)
            raise SystemExit(1)
        print(f"\n[PASS] full flow completed: {self.games_completed} games, no stalls/errors", flush=True)


if __name__ == "__main__":
    if async_playwright is None:
        raise SystemExit("playwright is required to run the soak (pip install playwright)")
    asyncio.run(Soak().run())
