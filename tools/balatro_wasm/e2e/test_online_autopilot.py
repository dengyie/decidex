import asyncio
from playwright.async_api import async_playwright

async def run():
    print("[Online Test] Launching Chromium to test https://balatro.mangoqwq.com ...", flush=True)
    import os
    user_data_dir = os.path.abspath("E:/code/decidex/.browser_cache")
    os.makedirs(user_data_dir, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(user_data_dir, headless=True)
        page = context.pages[0] if context.pages else await context.new_page()

        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if any(x in msg.text for x in ["[AutoPilot]", "[DecideX] Dispatched", "arbitration"]) else None)

        print("[Online Test] Navigating to https://balatro.mangoqwq.com ...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=45000)

        # Clear any old CacheStorage except v20
        await page.evaluate("""async () => {
            if (window.caches) {
                const keys = await caches.keys();
                for (const k of keys) {
                    if (k !== 'balatro-asset-cache-v20') {
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
            st_count = await page.evaluate("() => window._decidexStateCount || 0")
            if st_count > 0:
                break

        print("[Online Test] Enabling AutoPilot on production...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        action_count = 0
        saw_cash_out = False
        saw_shop = False
        saw_big_blind = False

        for step in range(100):
            await asyncio.sleep(1)
            st = await page.evaluate("() => window._lastDecidexState")
            status = await page.evaluate("() => { const b = document.getElementById('autopilot-status'); return b ? b.textContent : ''; }")
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
                if bname == "Big Blind" or (sname == "BLIND_SELECT" and saw_cash_out):
                    saw_big_blind = True

                if saw_cash_out and saw_shop and saw_big_blind and sname == "GAME_OVER":
                    print(f"\n[Online Test Multi-Round Success] Completed!", flush=True)
                    break

        await context.close()

if __name__ == "__main__":
    asyncio.run(run())
