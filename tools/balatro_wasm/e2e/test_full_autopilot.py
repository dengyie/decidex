import asyncio
import sys
from playwright.async_api import async_playwright

async def run():
    print("[Test] Launching headless Chromium...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if any(x in msg.text for x in ["[AutoPilot]", "[DecideX]", "DECIDEX_STATE", "dispatched", "error", "Error"]) else None)

        print("[Test] Opening http://127.0.0.1:8092...", flush=True)
        await page.goto("http://127.0.0.1:8092", wait_until="domcontentloaded")
        btn = await page.wait_for_selector("#btn-launch", timeout=15000)
        await btn.click()
        print("[Test] Clicked launch. Waiting for splash overlay to hide...", flush=True)
        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=30000)

        # Wait for Lua state
        for _ in range(15):
            await asyncio.sleep(1)
            st_count = await page.evaluate("() => window._decidexStateCount || 0")
            if st_count > 0:
                print(f"[Test] Lua state ready (count={st_count})", flush=True)
                break

        print("[Test] Enabling AutoPilot...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        # Run loop for 25 seconds
        seen_states = set()
        played_hand = False
        cashed_out = False

        for step in range(25):
            await asyncio.sleep(1)
            st = await page.evaluate("() => window._lastDecidexState")
            status = await page.evaluate("() => { const b = document.getElementById('autopilot-status'); return b ? b.textContent : ''; }")
            if st:
                sname = st.get("state_name")
                seen_states.add(sname)
                cards = len(st.get("hand", []))
                chips = st.get("chips", 0)
                hands = st.get("hands_left", 0)
                round_num = st.get("round", 0)
                print(f"[{step+1:02d}s] State: {sname:<15} | Hand: {cards} | Chips: {chips} | HandsLeft: {hands} | Round: {round_num} | Status: {status}", flush=True)

                if sname == "SELECTING_HAND" and cards > 0 and chips > 0:
                    played_hand = True
                if sname in ("ROUND_EVAL", "SHOP"):
                    cashed_out = True

        print(f"\n[Test Result] Seen states: {seen_states}", flush=True)
        print(f"[Test Result] Successfully played hand: {played_hand or ('SELECTING_HAND' in seen_states and any(x > 0 for x in [st.get('chips', 0)]))}", flush=True)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
