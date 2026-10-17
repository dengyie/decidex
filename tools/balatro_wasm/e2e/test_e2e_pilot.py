import asyncio
import time
from playwright.async_api import async_playwright

async def main():
    print("[E2E] Starting test...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        console_logs = []
        def on_console(msg):
            t = msg.text
            if any(k in t for k in ["[AutoPilot]", "[DecideX]", "DECIDEX_STATE", "BLIND_SELECT", "SELECTING_HAND", "ROUND_EVAL", "LUA"]):
                print(f"[Browser Console] {t}", flush=True)
            console_logs.append(t)

        page.on('console', on_console)

        print("[E2E] Navigating to http://127.0.0.1:8092...", flush=True)
        await page.goto('http://127.0.0.1:8092', wait_until='domcontentloaded', timeout=20000)
        
        btn = await page.wait_for_selector('#btn-launch', state='visible', timeout=15000)
        await btn.click()
        print("[E2E] Clicked launch. Waiting for splash to hide...", flush=True)
        await page.wait_for_selector('#splash-overlay', state='hidden', timeout=30000)
        print("[E2E] Splash hidden! Waiting for Lua engine...", flush=True)

        for i in range(20):
            await asyncio.sleep(1)
            cnt = await page.evaluate('() => window._decidexStateCount || 0')
            if cnt > 0:
                print(f"[E2E] Lua state detected after {i+1}s! Count={cnt}", flush=True)
                break

        print("[E2E] Toggling AutoPilot ON...", flush=True)
        await page.evaluate('() => window.AutoPilot.toggle(true)')

        start_time = time.time()
        for i in range(25):
            await asyncio.sleep(1)
            st = await page.evaluate('() => window._lastDecidexState')
            status = await page.evaluate('() => { const b = document.getElementById("autopilot-status"); return b ? b.textContent : ""; }')
            sname = st.get('state_name') if st else 'None'
            chips = st.get('chips', 0) if st else 0
            hand_len = len(st.get('hand', [])) if st else 0
            hands_left = st.get('hands_left', 0) if st else 0
            need = (st.get('blind', {}).get('chips', 0) - st.get('blind', {}).get('scored', 0)) if st else 0
            print(f"[{i+1}s] State: {sname} | Hand: {hand_len} cards | HandsLeft: {hands_left} | Scored: {chips} | Need: {need} | Status: {status}", flush=True)
            
            # If we played cards or finished a round, success!
            if sname in ("ROUND_EVAL", "SHOP") or chips > 0:
                print(f"[E2E SUCCESS] Successfully progressed gameplay via AutoPilot! Current chips: {chips}", flush=True)
                if sname in ("ROUND_EVAL", "SHOP"):
                    print("[E2E SUCCESS] Round completed and evaluated!", flush=True)
                    break

        await browser.close()
        print("[E2E] Finished test.", flush=True)

if __name__ == '__main__':
    asyncio.run(main())
