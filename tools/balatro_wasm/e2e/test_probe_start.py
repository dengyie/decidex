import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    print("[Probe] Launching Chromium...", flush=True)
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

        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if (
            any(k in msg.text for k in ['[Console]', '[DecideX', '[START_RUN', '[AutoPilot]', 'LÖVE stdout', 'Error'])
        ) else None)
        page.on('pageerror', lambda err: print(f"[PageError] {err}", flush=True))
        page.on('dialog', lambda d: asyncio.create_task(d.dismiss()))

        local_love_path = os.path.abspath("tools/balatro_wasm/web/balatro.love")
        local_wasm_path = os.path.abspath("tools/balatro_wasm/web/love.wasm")
        local_app_path = os.path.abspath("tools/balatro_wasm/web/app.js")

        async def h_love(r): await r.fulfill(path=local_love_path)
        async def h_wasm(r): await r.fulfill(path=local_wasm_path)
        async def h_app(r): await r.fulfill(path=local_app_path)

        await page.route("**/balatro.love*", h_love)
        await page.route("**/love.wasm*", h_wasm)
        await page.route("**/app.js*", h_app)

        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded")
        btn = await page.wait_for_selector("#btn-launch", state="visible")
        await btn.click()
        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=60000)
        print("[Probe] Splash overlay hidden, waiting 3s...", flush=True)
        await asyncio.sleep(3)

        # Call start_run once
        print("[Probe] Calling window.decidexStartRun()...", flush=True)
        await page.evaluate("""() => {
            window.decidexStartRun();
        }""")

        for i in range(12):
            await asyncio.sleep(1)
            st = await page.evaluate("() => window._lastDecidexState")
            if st:
                sname = st.get("state_name")
                can_blind = st.get("can_select_blind")
                print(f"[{i+1:02d}s] state: {sname} | can_select_blind: {can_blind}", flush=True)
            else:
                print(f"[{i+1:02d}s] no state yet", flush=True)

        await browser.close()

if __name__ == '__main__':
    asyncio.run(run())
