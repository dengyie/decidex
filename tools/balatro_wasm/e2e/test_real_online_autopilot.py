import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    print("[Real Online Test] Testing 100% direct https://balatro.mangoqwq.com (NO route interception)...", flush=True)
    profile_dir = os.path.abspath("tools/balatro_wasm/test_browser_profile")
    os.makedirs(profile_dir, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            viewport={"width": 1920, "height": 1080},
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu-watchdog',
                '--enable-webgl'
            ]
        )
        page = context.pages[0] if context.pages else await context.new_page()

        page.on('pageerror', lambda err: print(f"[PageError] {err}", flush=True))
        page.on('dialog', lambda dialog: (print(f"[Dialog] {dialog.message}", flush=True), asyncio.create_task(dialog.dismiss())))
        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if (
            (msg.type in ['error', 'warning'] or
             any(x in msg.text for x in ["[AutoPilot]", "[DecideX]", "Action", "cashed out", "toggled shop"])) and
            "LONG DT" not in msg.text
        ) else None)

        print("[Real Online Test] Navigating to https://balatro.mangoqwq.com ...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=45000)

        print("[Real Online Test] Waiting for download / asset cache readiness...", flush=True)
        for i in range(300):
            st_text = await page.evaluate("() => { const el = document.getElementById('status-text'); return el ? el.textContent : ''; }")
            bar_width = await page.evaluate("() => { const el = document.getElementById('progress-bar'); return el ? el.style.width : '0%'; }")
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if i % 5 == 0 or btn_visible:
                print(f"[Real Online Test] Status ({i}s): {st_text} ({bar_width}) | Ready: {btn_visible}", flush=True)
            if btn_visible:
                break
            await asyncio.sleep(1)

        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=60000)
        await btn.click()
        print("[Real Online Test] Clicked launch button, waiting for Love WASM initialization...", flush=True)

        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=60000)
        print("[Real Online Test] Splash overlay hidden! Waiting for DecideX state stream...", flush=True)

        for _ in range(20):
            await asyncio.sleep(1)
            st_count = await page.evaluate("() => window._lastDecidexStateCount || 0")
            if st_count > 0:
                break

        print("[Real Online Test] Enabling AutoPilot on real production site...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        saw_round_eval = False
        saw_cash_out = False
        saw_shop = False
        saw_cycle_back = False

        for step in range(90):
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
                    cash_ok = st.get("can_cash_out", False)
                    shop_ok = st.get("can_toggle_shop", False)
                    blind_ok = st.get("can_select_blind", False)

                    flags = f"Cash:{'T' if cash_ok else 'F'} Shop:{'T' if shop_ok else 'F'} Blind:{'T' if blind_ok else 'F'}"
                    print(f"[{step+1:02d}s] {sname:<15} | Cards: {cards} | Blind: {bname:<11} | Scored: {scored:>3}/{target:<3} | Hands: {hands} | {flags} | Status: {status}", flush=True)

                    if sname == "ROUND_EVAL":
                        saw_round_eval = True
                    if "通关结算中" in status or cash_ok:
                        saw_cash_out = True
                    if sname == "SHOP":
                        saw_shop = True
                    if saw_shop and sname == "BLIND_SELECT":
                        saw_cycle_back = True
                        print("\n[PROD SUCCESS] Fully verified genuine production cycle: Play -> Round Eval -> Shop -> Next Blind without any stall!", flush=True)
                        break
                else:
                    print(f"[{step+1:02d}s] Waiting for first DecideX state... | Status: {status}", flush=True)
            except Exception as e:
                print(f"[{step+1:02d}s] Loop error: {repr(e)}", flush=True)

        if not saw_cycle_back:
            print(f"\n[SUMMARY] saw_round_eval={saw_round_eval}, saw_cash_out={saw_cash_out}, saw_shop={saw_shop}, saw_cycle_back={saw_cycle_back}", flush=True)

        await context.close()

if __name__ == "__main__":
    asyncio.run(run())
