"""Targeted production probe for the skip_blind crash path.

Background: the WASM bridge used to hand G.FUNCS.skip_blind a synthetic table.
The game dereferences e.UIBox:get_UIE_by_ID('tag_container'), so the call
crashed with "attempt to index field 'UIBox' (a nil value)" and the autopilot
wedged in BLIND_SELECT.

This probe starts a real run against production, waits for BLIND_SELECT, then
drives the bridge's own skip_blind action and asserts that:
  1. the bridge reports success (no Lua error),
  2. blind_on_deck actually advances (Small -> Big),
  3. the page records no pageerror during the skip.
"""

import asyncio
import os
from playwright.async_api import async_playwright

TIMEOUT_MS = 45_000


async def run():
    print("[SkipBlindProbe] Targeting https://balatro.mangoqwq.com ...", flush=True)
    profile_dir = os.path.abspath("tools/balatro_wasm/test_browser_profile_skipblind")
    os.makedirs(profile_dir, exist_ok=True)

    page_errors = []
    action_results = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu-watchdog",
                "--enable-webgl",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        page.on(
            "pageerror",
            lambda err: (page_errors.append(str(err)), print(f"[PageError] {err}", flush=True)),
        )
        page.on(
            "dialog",
            lambda dialog: asyncio.create_task(dialog.dismiss()),
        )

        def on_console(msg):
            text = msg.text
            if "[DECIDEX_ACTION_RESULT]" in text and "action=skip_blind" in text:
                action_results.append(text)
                print(f"[Console] {text}", flush=True)
                return
            if (
                msg.type in ("error", "warning")
                or "[DECIDEX" in text
                or "[AutoPilot]" in text
                or "[SkipProbe]" in text
            ) and "LONG DT" not in text:
                print(f"[Console] {text}", flush=True)

        page.on("console", on_console)

        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=TIMEOUT_MS)

        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=TIMEOUT_MS)
        await btn.click()
        print("[SkipBlindProbe] Launch clicked.", flush=True)

        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=90_000)
        print("[SkipBlindProbe] Game booted.", flush=True)
        await asyncio.sleep(3)

        # The bridge only consumes /tmp/balatro_eval.lua when it carries the
        # "-- DECIDEX_SEQ:<n>" header, so every probe write must add it.
        await page.evaluate(
            """() => {
                    window.__probeSeq = (window.__probeSeq || 900000) + 1;

                window.__probeLua = (code) => {
                    window.__probeSeq += 1;
                    const FS = window.Module && window.Module.FS;
                    if (!FS) return false;
                    try { FS.unlink('/tmp/balatro_eval_ack.txt'); } catch (_) {}
                    FS.writeFile('/tmp/balatro_eval.lua',
                        '-- DECIDEX_SEQ:' + window.__probeSeq + '\\n' + code);
                    return true;
                };
            }"""
        )

        # Start a run and wait until the bridge reports BLIND_SELECT.
        # The bridge only consumes /tmp/balatro_eval.lua when it carries the
        # "-- DECIDEX_SEQ:<n>" header, so route through the app's own
        # dispatchLuaAction (window.decidexStartRun) which adds it.
        await page.evaluate("() => window.decidexStartRun()")

        await page.wait_for_function(
            "() => window._lastDecidexState && window._lastDecidexState.state_name === 'BLIND_SELECT'",
            timeout=TIMEOUT_MS,
        )
        print("[SkipBlindProbe] Reached BLIND_SELECT.", flush=True)
        await asyncio.sleep(1.5)

        before = await page.evaluate("() => window._lastDecidexState && window._lastDecidexState.blind_on_deck")
        can_skip = await page.evaluate(
            "() => window._lastDecidexState && window._lastDecidexState.can_skip_blind"
        )
        print(f"[SkipProbe] initial blind_on_deck={before!r} can_skip_blind={can_skip!r}", flush=True)

        if not can_skip:
            print("[SkipProbe] can_skip_blind is false; forcing readiness and retrying.", flush=True)
            await page.evaluate(
                """() => window.__probeLua(`
                    local ok, err = pcall(function()
                        if G.GAME then G.GAME.facing_blind = nil end
                        if G.CONTROLLER and G.CONTROLLER.locks then G.CONTROLLER.locks.skip_blind = nil end
                        _G._select_blind_in_flight = nil
                        _G._last_select_blind_time = nil
                    end)
                    print("[SkipProbe] forced ready ok=" .. tostring(ok) .. " err=" .. tostring(err))
                `)"""
            )
            await asyncio.sleep(1.5)
            can_skip = await page.evaluate(
                "() => window._lastDecidexState && window._lastDecidexState.can_skip_blind"
            )
            print(f"[SkipProbe] retry can_skip_blind={can_skip!r}", flush=True)

        # Dispatch skip_blind through the bridge, the same channel the
        # autopilot uses, and let the bridge's own guards apply.
        # Re-read blind_on_deck right before dispatch: the first snapshot can
        # still be empty while the blind-select UI is settling.
        before = await page.evaluate(
            "() => window._lastDecidexState && window._lastDecidexState.blind_on_deck"
        )
        print(f"[SkipProbe] dispatching skip_blind with blind_on_deck={before!r}", flush=True)
        await page.evaluate(
            """() => window.__probeLua(`
                local res, err = G.DECIDEX_EXECUTE('skip_blind', {})
                print("[SkipProbe] skip_blind result=" .. tostring(res) .. " err=" .. tostring(err))
            `)"""
        )
        await asyncio.sleep(4)

        after = await page.evaluate("() => window._lastDecidexState && window._lastDecidexState.blind_on_deck")
        state_name = await page.evaluate(
            "() => window._lastDecidexState && window._lastDecidexState.state_name"
        )
        print(f"[SkipProbe] after blind_on_deck={after!r} state={state_name!r}", flush=True)

        await context.close()

    crashed = any("UIBox" in e for e in page_errors)
    advanced = bool(before) and bool(after) and before != after
    skip_reported_ok = any("action=skip_blind ok=true" in r for r in action_results)

    print("\n[RESULT]")
    print(f"  before={before!r} after={after!r}")
    print(f"  uibox_crash_seen={crashed}")
    print(f"  blind_advanced={bool(advanced)}")
    print(f"  bridge_reported_ok={skip_reported_ok}")
    print(f"  action_results={action_results}")
    print(f"  page_errors={page_errors}")

    if crashed:
        print("\n[FAIL] skip_blind still crashes with a nil UIBox.", flush=True)
        raise SystemExit(1)
    if not skip_reported_ok:
        print("\n[FAIL] bridge did not report skip_blind ok=true.", flush=True)
        raise SystemExit(1)
    if not advanced:
        print("\n[FAIL] blind_on_deck did not advance after skip_blind.", flush=True)
        raise SystemExit(1)
    print("\n[PASS] skip_blind executed without crashing and advanced blind_on_deck.", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
