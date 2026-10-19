import asyncio
from playwright.async_api import async_playwright

async def run():
    print("[Test] Launching headless Chromium for full round test...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        page.on('pageerror', lambda err: print(f"[PageError] {err}", flush=True))
        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if any(x in msg.text for x in ["[AutoPilot]", "[DecideX]", "dispatched", "Error", "error"]) else None)

        await page.goto("http://127.0.0.1:8092", wait_until="domcontentloaded")
        btn = await page.wait_for_selector("#btn-launch", timeout=15000)
        await btn.click()
        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=30000)

        for _ in range(15):
            await asyncio.sleep(1)
            st_count = await page.evaluate("() => window._decidexStateCount || 0")
            if st_count > 0:
                break

        print("[Test] Enabling AutoPilot...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        reached_shop = False
        reached_round_2 = False
        for step in range(75):
            await asyncio.sleep(1)
            st = await page.evaluate("() => window._lastDecidexState")
            status = await page.evaluate("() => { const b = document.getElementById('autopilot-status'); return b ? b.textContent : ''; }")
            if st:
                sname = st.get("state_name")
                cards = len(st.get("hand", []))
                scored = st.get("blind", {}).get("scored", 0)
                target = st.get("blind", {}).get("chips", 0)
                hands = st.get("hands_left", 0)
                round_num = st.get("round", 0)
                ante = st.get("ante", 1)
                can_cash = st.get("can_cash_out")
                can_shop = st.get("can_toggle_shop")
                print(f"[{step+1:02d}s] State: {sname:<15} | Cards: {cards} | Scored: {scored}/{target} | Hands: {hands} | Round: {round_num} | Cash: {can_cash} | Shop: {can_shop} | Status: {status}", flush=True)

                if sname == "SHOP":
                    reached_shop = True
                if round_num >= 2 or (sname == "BLIND_SELECT" and step > 40):
                    reached_round_2 = True
                    print(f"\n[Test Success] Seamlessly transitioned to Round 2 / Next Blind! Ante: {ante}, Round: {round_num}", flush=True)
                    break

        print(f"\n[Final] Reached Shop: {reached_shop}, Reached Next Blind/Round: {reached_round_2}", flush=True)
        if not reached_shop or not reached_round_2:
            raise AssertionError("full round smoke test did not reach SHOP and the next blind/round")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
