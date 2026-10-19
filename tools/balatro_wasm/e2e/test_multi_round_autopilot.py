"""
Multi-round and cross-blind stress test for Balatro DecideX AutoPilot on production.
Observes continuous automated play across Small Blind -> Big Blind -> Boss Blind -> Ante 2+.
"""

import asyncio
import os
from playwright.async_api import async_playwright


async def run():
    print("[Multi-Round Test] Launching Chromium to test live autopilot over extended rounds...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu-watchdog',
                '--use-gl=angle',
                '--use-angle=d3d11',
                '--enable-webgl',
            ]
        )
        context = await browser.new_context()
        page = await context.new_page()

        page.on('pageerror', lambda err: print(f"[PageError] {err}", flush=True))
        page.on('dialog', lambda dialog: (print(f"[Dialog] {dialog.message}", flush=True), asyncio.create_task(dialog.dismiss())))
        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if (
            (msg.type in ['error', 'warning'] or
             any(x in msg.text for x in ["[AutoPilot]", "[DecideX]", "arbitration", "Error", "[LÖVE stdout]"])) and
            "LONG DT" not in msg.text
        ) else None)

        local_love_path = os.path.abspath("tools/balatro_wasm/web/balatro.love")
        local_wasm_path = os.path.abspath("tools/balatro_wasm/web/love.wasm")
        local_app_path = os.path.abspath("tools/balatro_wasm/web/app.js")

        async def handle_love(route):
            await route.fulfill(path=local_love_path)

        async def handle_wasm(route):
            await route.fulfill(path=local_wasm_path)

        async def handle_app(route):
            await route.fulfill(path=local_app_path)

        await page.route("**/balatro.love*", handle_love)
        await page.route("**/love.wasm*", handle_wasm)
        await page.route("**/app.js*", handle_app)

        print("[Multi-Round Test] Navigating to https://balatro.mangoqwq.com ...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=45000)

        await page.evaluate("""async () => {
            if (window.caches) {
                const keys = await caches.keys();
                for (const k of keys) {
                    if (k !== 'balatro-asset-cache-v28') {
                        await caches.delete(k);
                    }
                }
            }
        }""")

        for i in range(120):
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if btn_visible:
                break
            await asyncio.sleep(1)

        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=20000)
        await btn.click()
        print("[Multi-Round Test] Launching game...", flush=True)

        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=60000)
        print("[Multi-Round Test] Splash hidden! Waiting for game state...", flush=True)

        for _ in range(20):
            await asyncio.sleep(1)
            st_count = await page.evaluate("() => window._lastDecidexStateCount || 0")
            if st_count > 0:
                break

        print("[Multi-Round Test] Enabling AutoPilot...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        rounds_completed = 0
        cleared_blinds = set()
        max_ante_seen = 1

        page.set_default_timeout(10000)

        # Run up to 240 seconds to test multi-blind gameplay (Small -> Big -> Boss)
        for step in range(240):
            try:
                await asyncio.sleep(1)
                res = await page.evaluate("""() => {
                    return {
                        state: window._lastDecidexState,
                        status: (document.getElementById('autopilot-status') || {}).textContent || ''
                    };
                }""")
                st = res.get("state")
                status = res.get("status")

                if st:
                    sname = st.get("state_name")
                    cards = len(st.get("hand", []))
                    b_obj = st.get("blind", {}) or {}
                    scored = b_obj.get("scored", 0)
                    target = b_obj.get("chips", 0)
                    hands = st.get("hands_left", 0)
                    discards = st.get("discards_left", 0)
                    money = st.get("money", 0)
                    ante = st.get("ante", 1)
                    round_num = st.get("round", 1)
                    bname = b_obj.get("name", "Unknown")
                    jokers = [j.get("name") for j in st.get("jokers", [])]
                    cons = [c.get("name") for c in st.get("consumables", [])]

                    max_ante_seen = max(max_ante_seen, ante)

                    if sname == "ROUND_EVAL" and bname and bname not in cleared_blinds:
                        cleared_blinds.add(bname)
                        rounds_completed += 1
                        print(f"\n[Round Cleared] Blind: {bname} (Ante {ante}, Round {round_num})! Total Clears: {rounds_completed}\n", flush=True)

                    if step % 3 == 0 or sname in ("BLIND_SELECT", "ROUND_EVAL", "SHOP", "TAROT_PACK", "PLANET_PACK", "BUFFOON_PACK", "GAME_OVER"):
                        flags = f"Ante:{ante} R:{round_num} ${money} Jokers:{len(jokers)} Cons:{len(cons)}"
                        print(f"[{step+1:03d}s] {sname:<15} | {bname:<12} | Scored: {scored:>4}/{target:<4} | H:{hands} D:{discards} | {flags} | Status: {status}", flush=True)

                    if sname == "GAME_OVER":
                        print(f"[Multi-Round Test] Game over reached at Ante {ante}. Cleared {rounds_completed} blinds: {cleared_blinds}", flush=True)
                        break

                    # If we cleared at least 3 blinds (Small + Big + Boss) or entered Ante 2
                    if (rounds_completed >= 3 or (rounds_completed >= 2 and ante >= 2)) and sname in ("BLIND_SELECT", "SELECTING_HAND", "SHOP"):
                        print(f"\n[Multi-Round Test Success] Successfully cleared multiple blinds in sequence: {cleared_blinds} (Ante {ante}, Money ${money})!", flush=True)
                        break
                else:
                    if step % 5 == 0:
                        print(f"[{step+1:03d}s] Waiting for state...", flush=True)
            except Exception as e:
                print(f"[{step+1:03d}s] Error: {repr(e)}", flush=True)

        await context.close()
        await browser.close()
        print("[Multi-Round Test] Finished successfully.", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
