import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright

SCREENSHOT_DIR = Path("tools/balatro_wasm/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

async def run():
    print("[Live Prod Test] Testing 100% DIRECT https://balatro.mangoqwq.com (Multi-Round with Screenshots)...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu-watchdog',
                '--enable-webgl',
            ]
        )
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()

        page.on('pageerror', lambda err: print(f"[PageError] {err}", flush=True))
        page.on('dialog', lambda dialog: (print(f"[Dialog] {dialog.message}", flush=True), asyncio.create_task(dialog.dismiss())))
        def on_console(msg):
            t = msg.text
            if any(skip in t for skip in ['LONG DT', 'AudioContext', 'GL_INVALID', 'favicon']):
                return
            print(f'[{msg.type.upper()}] {t}', flush=True)
        page.on('console', on_console)

        print("[Live Prod Test] Navigating to https://balatro.mangoqwq.com ...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=45000)

        # Wait for launch button (assets are already cached in IndexedDB/CacheStorage)
        for i in range(120):
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if btn_visible:
                break
            if i % 5 == 0:
                st_text = await page.evaluate("() => { const el = document.getElementById('status-text'); return el ? el.textContent : ''; }")
                print(f"[Live Prod Test] Waiting readiness ({i}s): {st_text}", flush=True)
            await asyncio.sleep(1)

        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=30000)
        await btn.click()
        print("[Live Prod Test] Clicked launch button, waiting for Love WASM...", flush=True)

        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=60000)
        print("[Live Prod Test] Splash overlay hidden! Waiting for DecideX state stream...", flush=True)

        for _ in range(20):
            await asyncio.sleep(1)
            st_count = await page.evaluate("() => window._lastDecidexStateCount || 0")
            if st_count > 0:
                break

        print("[Live Prod Test] Enabling AutoPilot...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        screenshot_taken = {}
        cleared_rounds = 0

        for step in range(120):
            try:
                await asyncio.sleep(1)
                st = await asyncio.wait_for(page.evaluate("() => window._lastDecidexState"), timeout=5.0)
                status = await asyncio.wait_for(page.evaluate("() => { const b = document.getElementById('autopilot-status'); return b ? b.textContent : ''; }"), timeout=5.0)
                if st:
                    sname = st.get("state_name")
                    cards = len(st.get("hand", []))
                    scored = st.get("blind", {}).get("scored", 0)
                    target = st.get("blind", {}).get("chips", 0)
                    hands = st.get("hands_left", 0)
                    bname = st.get("blind", {}).get("name", "Unknown")
                    ante = st.get("ante", 1)
                    round_num = st.get("round", 1)
                    money = st.get("money", 0)
                    jokers = len(st.get("jokers", []))

                    cash_ok = st.get("can_cash_out", False)
                    shop_ok = st.get("can_toggle_shop", False)
                    blind_ok = st.get("can_select_blind", False)

                    print(f"[{step+1:03d}s] {sname:<15} | Blind: {bname:<11} | Scored: {scored:>4}/{target:<4} | H:{hands} ${money} J:{jokers} | Status: {status}", flush=True)

                    # Capture screenshots at each milestone
                    if sname == "SELECTING_HAND" and "hand1" not in screenshot_taken:
                        shot_path = str(SCREENSHOT_DIR / "01_selecting_hand.png")
                        await page.screenshot(path=shot_path)
                        screenshot_taken["hand1"] = shot_path
                        print(f"[*] Saved screenshot: {shot_path}", flush=True)

                    if sname == "ROUND_EVAL" and "round_eval1" not in screenshot_taken:
                        shot_path = str(SCREENSHOT_DIR / "02_round_eval.png")
                        await page.screenshot(path=shot_path)
                        screenshot_taken["round_eval1"] = shot_path
                        print(f"[*] Saved screenshot: {shot_path}", flush=True)

                    if sname == "SHOP" and "shop1" not in screenshot_taken:
                        shot_path = str(SCREENSHOT_DIR / "03_shop.png")
                        await page.screenshot(path=shot_path)
                        screenshot_taken["shop1"] = shot_path
                        print(f"[*] Saved screenshot: {shot_path}", flush=True)

                    if (bname == "Big Blind" or round_num >= 2) and sname == "SELECTING_HAND" and "round2_hand" not in screenshot_taken:
                        shot_path = str(SCREENSHOT_DIR / "04_round2_big_blind.png")
                        await page.screenshot(path=shot_path)
                        screenshot_taken["round2_hand"] = shot_path
                        print(f"[*] Saved screenshot: {shot_path}", flush=True)

                    if (bname == "Big Blind" or round_num >= 2) and sname == "ROUND_EVAL" and "round2_eval" not in screenshot_taken:
                        shot_path = str(SCREENSHOT_DIR / "05_round2_eval.png")
                        await page.screenshot(path=shot_path)
                        screenshot_taken["round2_eval"] = shot_path
                        print(f"[*] Saved screenshot: {shot_path}", flush=True)

                    if sname == "ROUND_EVAL":
                        cleared_rounds = max(cleared_rounds, round_num)

                    # If we played through Big Blind (Round 2) and got to Shop or Blind Select 3
                    if (round_num >= 2 and sname in ("SHOP", "BLIND_SELECT") and "round2_hand" in screenshot_taken) or step >= 90:
                        if round_num >= 2:
                            print(f"\n[LIVE PROD TEST SUCCESS] Successfully completed multi-round live gameplay on production (Round {round_num}, Ante {ante}, Money ${money})!", flush=True)
                            final_shot = str(SCREENSHOT_DIR / "06_multi_round_success.png")
                            await page.screenshot(path=final_shot)
                            screenshot_taken["final"] = final_shot
                            print(f"[*] Saved final screenshot: {final_shot}", flush=True)
                            break
            except Exception as e:
                print(f"[{step+1:03d}s] Error evaluating state: {repr(e)}", flush=True)

        await context.close()
        await browser.close()
        print("[Live Prod Test] Done. Screenshots saved in:", SCREENSHOT_DIR, flush=True)

if __name__ == "__main__":
    asyncio.run(run())
