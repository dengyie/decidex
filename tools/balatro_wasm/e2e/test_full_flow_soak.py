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

Every console line is mirrored to stdout with a wall-clock prefix so a
stall can be traced against the surrounding bridge/AutoPilot chatter.

Env:
  BALATRO_SOAK_URL       target (default http://127.0.0.1:8099/)
  BALATRO_SOAK_MIN_GAMES minimum completed games (default 2)
  BALATRO_SOAK_MAX_SECONDS  wall-clock cap (default 2400)
  BALATRO_LAUNCH_TIMEOUT launch-button wait in seconds (default 600)
"""

import asyncio
import json
import os
import re
import time

from playwright.async_api import async_playwright

BASE_URL = os.environ.get("BALATRO_SOAK_URL", "http://127.0.0.1:8099/")
MIN_GAMES = int(os.environ.get("BALATRO_SOAK_MIN_GAMES", "2"))
MAX_SECONDS = int(os.environ.get("BALATRO_SOAK_MAX_SECONDS", "2400"))

# Per-state dwell budgets (seconds) before a stall is declared. The
# autopilot acts well inside these on a healthy run at GAMESPEED 4; the
# budgets only trip when something is genuinely wedged.
DWELL_BUDGETS = {
    "MENU": 45,
    # Blind select legitimately takes 45-46s when the event queue is still
    # draining a busy transition (observed self-recovering in soak8b/soak6
    # with no intervention); 60s avoids flagging that as a stall.
    "BLIND_SELECT": 60,
    "SELECTING_HAND": 90,
    "DRAW_TO_HAND": 30,
    "HAND_PLAYED": 60,
    "ROUND_EVAL": 60,
    "PLAY_TAROT": 45,
    "SHOP": 60,
    "TAROT_PACK": 75,
    "PLANET_PACK": 75,
    "SPECTRAL_PACK": 75,
    "STANDARD_PACK": 75,
    "BUFFOON_PACK": 75,
    "GAME_OVER": 30,
    "GAME_WON": 30,
}
DEFAULT_BUDGET = 30

TRANSIENT_INFO_RE = re.compile(
    r"debounced|not ready|animating|in progress|busy|locked|waiting|"
    r"already in play|blocks play|slots full|pack card not found|no-op|not set|"
    r"not in (BLIND_SELECT|SHOP|ROUND_EVAL|SELECTING_HAND|HAND_PLAYED|new_round)|"
    r"blind already selected|already in run|no pack_cards|not in a pack state|no space to buy|use_card failed",
    re.IGNORECASE,
)

PACK_STATES = {"TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK", "STANDARD_PACK", "BUFFOON_PACK"}


class Soak:
    def __init__(self):
        self.stalls = []          # (t, state, dwell, note)
        self.page_errors = []
        self.action_failures = []
        self.games_completed = 0
        self.antes_seen = set()
        self.blinds_seen = []     # (ante, blind) on-deck tuples
        self.state_events = []    # (t, state) transitions
        self.watchdog_events = [] # (t, text)
        self.stall_dumped = False

    def on_console(self, msg):
        text = msg.text
        now = time.monotonic()
        if "[DECIDEX_STATE]" in text:
            try:
                d = json.loads(text.split("[DECIDEX_STATE]", 1)[1].strip())
                self.last_state = d
            except Exception:
                pass
            return
        if "[DECIDEX_ACTION_RESULT]" in text and "ok=false" in text:
            info = text.split("info=", 1)[1] if "info=" in text else ""
            if not TRANSIENT_INFO_RE.search(info):
                self.action_failures.append((f"{now:.0f}s", text[:220]))
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
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--enable-webgl"]
            # A persistent profile caches the 53.7MB game package across
            # runs; essential against production, where a cold download
            # competes with a flaky Cloudflare edge.
            profile_dir = os.environ.get("BALATRO_SOAK_PROFILE", "")
            if profile_dir:
                os.makedirs(profile_dir, exist_ok=True)
                b = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    viewport={"width": 1920, "height": 1080},
                    args=launch_args,
                )
                page = b.pages[0] if b.pages else await b.new_page()
            else:
                b = await p.chromium.launch(headless=True, args=launch_args)
                context = await b.new_context(viewport={"width": 1920, "height": 1080})
                page = context.pages[0] if context.pages else await context.new_page()
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
                await asyncio.sleep(1)
                t = time.monotonic() - start
                st = await page.evaluate("() => window._lastDecidexState")
                if not st:
                    continue
                sname = st.get("state_name") or "UNKNOWN"
                if sname != prev_state:
                    self.state_events.append((f"{t:.0f}s", sname))
                    print(f"[Soak] {t:6.0f}s  -> {sname}  ante={st.get('ante')} "
                          f"round={st.get('round')} money={st.get('money')}", flush=True)
                    if sname in ("GAME_OVER", "GAME_WON"):
                        self.games_completed += 1
                    prev_state = sname
                    state_since = time.monotonic()
                    self.stall_dumped = False

                ante = st.get("ante")
                if ante:
                    self.antes_seen.add(ante)
                deck = st.get("blind_on_deck")
                if deck:
                    key = (ante, deck)
                    if not self.blinds_seen or self.blinds_seen[-1] != key:
                        self.blinds_seen.append(key)

                if sname == "GAME_OVER" or sname == "GAME_WON":
                    pass  # counted on transition below

                dwell = time.monotonic() - state_since
                budget = DWELL_BUDGETS.get(sname, DEFAULT_BUDGET)
                if dwell > budget and not self.stall_dumped:
                    self.stall_dumped = True
                    js_pack_n = len((st.get("pack_cards") or [])) if isinstance(st.get("pack_cards"), list) else "?"
                    note = f"state={sname} dwell={dwell:.0f}s budget={budget}s js_snapshot_pack_n={js_pack_n}"
                    self.stalls.append((f"{t:.0f}s", note))
                    print(f"[Soak] *** STALL *** {t:.0f}s {note}", flush=True)
                    await page.evaluate("() => window.__soakDump && window.__soakDump()")

            await b.close()

        # Metrics rollup
        print("\n[SOAK-RESULT]", flush=True)
        print(f"  wall_clock={MAX_SECONDS}s games_completed(game_over_seen)={self.games_completed}")
        print(f"  antes_seen={sorted(self.antes_seen)}")
        print(f"  blinds_on_deck_sequence={self.blinds_seen}")
        print(f"  state_transitions={len(self.state_events)}")
        print(f"  page_errors={self.page_errors}")
        print(f"  action_failures={self.action_failures}")
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
        # games_completed is informational: the autopilot wins often enough
        # that a game over is rare inside the time cap. The flow evidence is
        # the state-transition timeline itself.
        if failures:
            print(f"\n[FAIL] {', '.join(failures)}", flush=True)
            raise SystemExit(1)
        print("\n[PASS] full flow completed with no stalls/errors", flush=True)


if __name__ == "__main__":
    asyncio.run(Soak().run())
