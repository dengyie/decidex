import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

SCREENSHOT_DIR = Path("tools/balatro_wasm/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

async def run():
    print("[Live Prod Boss Stress Test] Running extended test on https://balatro.mangoqwq.com ...", flush=True)
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
            if 'CRITICAL' in t or 'ERROR' in t or 'AutoPilot' in t or 'DECIDEX_ACTION_RESULT' in t or 'DECIDEX_STATE' in t:
                print(f'[{msg.type.upper()}] {t[:120]}', flush=True)
        page.on('console', on_console)

        print("[Live Prod] Navigating to https://balatro.mangoqwq.com ...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=45000)

        for i in range(360):
            st_text = await page.evaluate("() => { const el = document.getElementById('status-text'); return el ? el.textContent : ''; }")
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if i % 5 == 0 or btn_visible:
                print(f"[Live Prod] Waiting readiness ({i}s): {st_text} (btn_visible={btn_visible})", flush=True)
            if btn_visible:
                break
            await asyncio.sleep(1)

        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=60000)
        await btn.click()
        print("[Live Prod] Clicked launch button, waiting for Love WASM...", flush=True)

        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=60000)
        print("[Live Prod] Splash overlay hidden! Waiting for DecideX state stream...", flush=True)

        for _ in range(20):
            await asyncio.sleep(1)
            st_count = await page.evaluate("() => window._lastDecidexStateCount || 0")
            if st_count > 0:
                break

        print("[Live Prod] Enabling AutoPilot...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        max_round = 0
        max_ante = 0

        for step in range(150):
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
                    max_round = max(max_round, round_num)
                    max_ante = max(max_ante, ante)

                    if step % 2 == 0 or sname in ("ROUND_EVAL", "SHOP", "BLIND_SELECT"):
                        print(f"[{step+1:03d}s] {sname:<15} | Ante {ante} Round {round_num} | Blind: {bname:<11} | Scored: {scored:>4}/{target:<4} | H:{hands} ${money} J:{jokers} | {status}", flush=True)

                    if round_num >= 3 and sname == "SELECTING_HAND":
                        print(f"\n[STRESS TEST PROGRESS] Reached Boss Blind (Round {round_num}, Ante {ante})! Blind: {bname}", flush=True)
                        shot_path = str(SCREENSHOT_DIR / "boss_blind_reached.png")
                        await page.screenshot(path=shot_path)
                        print(f"[*] Saved boss screenshot: {shot_path}", flush=True)

                    if round_num >= 4 or ante >= 2:
                        print(f"\n[STRESS TEST SUCCESS] Successfully defeated Boss Blind and advanced to Ante {ante} Round {round_num}! Total Money: ${money}, Jokers: {jokers}", flush=True)
                        final_shot = str(SCREENSHOT_DIR / "boss_blind_cleared.png")
                        await page.screenshot(path=final_shot)
                        print(f"[*] Saved cleared screenshot: {final_shot}", flush=True)
                        break
            except Exception as e:
                print(f"[{step+1:03d}s] Error evaluating state: {repr(e)}", flush=True)

        await context.close()
        await browser.close()
        print(f"[Live Prod Boss Stress Test] Finished. Max Ante: {max_ante}, Max Round: {max_round}", flush=True)

if __name__ == "__main__":
    asyncio.run(run())
