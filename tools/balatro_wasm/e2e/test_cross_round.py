import asyncio
from playwright.async_api import async_playwright

async def run():
    print("[Test] Starting Cross-Round Autonomous Game Test...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Filter logs for AutoPilot and DecideX actions
        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if any(x in msg.text for x in ["[AutoPilot]", "[DecideX] Dispatched"]) else None)

        await page.goto("http://127.0.0.1:8092", wait_until="domcontentloaded")
        btn = await page.wait_for_selector("#btn-launch", timeout=15000)
        await btn.click()
        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=30000)

        # Wait for game to initialize
        for _ in range(15):
            await asyncio.sleep(1)
            st_count = await page.evaluate("() => window._decidexStateCount || 0")
            if st_count > 0:
                break

        print("[Test] Enabling AutoPilot...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        reached_eval = False
        reached_shop = False
        reached_next_blind = False

        for step in range(60):
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
                can_cash = st.get("can_cash_out", False)
                can_shop = st.get("can_toggle_shop", False)
                can_blind = st.get("can_select_blind", False)
                bname = st.get("blind", {}).get("name", "Unknown")

                print(f"[{step+1:02d}s] {sname:<15} | R{round_num} A{ante} | Blind: {bname:<11} | Scored: {scored:>3}/{target:<3} | Cash:{str(can_cash)[0]} Shop:{str(can_shop)[0]} Blind:{str(can_blind)[0]} | Status: {status}", flush=True)

                if sname == "ROUND_EVAL":
                    reached_eval = True
                elif sname == "SHOP":
                    reached_shop = True
                elif reached_shop and sname in ("BLIND_SELECT", "SELECTING_HAND"):
                    reached_next_blind = True
                    print(f"\n[Test Success] Seamlessly transitioned from Shop into Next Stage ({sname}, Blind: {bname})!", flush=True)
                    # Let it run 5 more seconds to verify next blind action
                    await asyncio.sleep(5)
                    break

        print(f"\n[Final Results] Reached Eval: {reached_eval}, Reached Shop: {reached_shop}, Reached Next Blind: {reached_next_blind}", flush=True)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
