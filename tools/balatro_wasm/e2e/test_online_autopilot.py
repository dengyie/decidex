import asyncio
from playwright.async_api import async_playwright

async def run():
    print("[Online Test] Launching Chromium to test https://balatro.mangoqwq.com ...", flush=True)
    import os
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu-watchdog',
                '--enable-webgl'
            ]
        )
        context = await browser.new_context()
        page = await context.new_page()

        page.on('pageerror', lambda err: print(f"[PageError] {err}", flush=True))
        page.on('dialog', lambda dialog: (print(f"[Dialog] {dialog.message}", flush=True), asyncio.create_task(dialog.dismiss())))
        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if (
            (msg.type in ['error', 'warning'] or
             any(x in msg.text for x in ["[AutoPilot]", "[DecideX]", "arbitration", "Error"])) and
            "LONG DT" not in msg.text
        ) else None)

        # Route large 53.7MB balatro.love and 4.6MB love.wasm and app.js from local disk
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

        print("[Online Test] Navigating to https://balatro.mangoqwq.com ...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=45000)

        # Clear any old CacheStorage except v24
        await page.evaluate("""async () => {
            if (window.caches) {
                const keys = await caches.keys();
                for (const k of keys) {
                    if (k !== 'balatro-asset-cache-v24') {
                        await caches.delete(k);
                    }
                }
            }
        }""")

        print("[Online Test] Waiting for download / asset cache readiness...", flush=True)
        for i in range(300):
            st_text = await page.evaluate("() => { const el = document.getElementById('status-text'); return el ? el.textContent : ''; }")
            bar_width = await page.evaluate("() => { const el = document.getElementById('progress-bar'); return el ? el.style.width : '0%'; }")
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if i % 5 == 0 or btn_visible:
                print(f"[Online Test] Status ({i}s): {st_text} ({bar_width}) | Ready: {btn_visible}", flush=True)
            if btn_visible:
                break
            await asyncio.sleep(1)

        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=20000)
        await btn.click()
        print("[Online Test] Clicked launch button, waiting for Love WASM initialization...", flush=True)

        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=60000)
        print("[Online Test] Splash overlay hidden! Waiting for DecideX state stream...", flush=True)

        for _ in range(20):
            await asyncio.sleep(1)
            st_count = await page.evaluate("() => window._lastDecidexStateCount || 0")
            if st_count > 0:
                break

        print("[Online Test] Enabling AutoPilot on production...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        action_count = 0
        saw_cash_out = False
        saw_shop = False
        saw_big_blind = False

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

                    if "Cash out" in status or sname == "ROUND_EVAL":
                        saw_cash_out = True
                    if "离开商店" in status or sname == "SHOP":
                        saw_shop = True
                    if bname == "Big Blind" or (sname == "BLIND_SELECT" and saw_cash_out and saw_shop):
                        saw_big_blind = True

                    if saw_cash_out and saw_shop and saw_big_blind:
                        print(f"\n[Online Test Multi-Round Success] Verified complete loop: Small Blind -> Cash Out -> Shop -> Big Blind seamlessly without stall!", flush=True)
                        break
                else:
                    print(f"[{step+1:02d}s] Waiting for first DecideX state... | Status: {status}", flush=True)
            except Exception as e:
                print(f"[{step+1:02d}s] Loop error: {repr(e)}", flush=True)

        await context.close()
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
