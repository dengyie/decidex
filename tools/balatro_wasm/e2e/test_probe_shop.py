import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def run():
    print("[Probe] Starting Playwright...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--use-gl=angle",
                "--use-angle=swiftshader",
            ]
        )
        page = await browser.new_page()

        page.on("console", lambda msg: (
            print(f"[Console {msg.type}] {msg.text}", flush=True)
            if "LONG DT" not in msg.text else None
        ))
        page.on("pageerror", lambda err: print(f"[PageError] {err}", flush=True))
        page.on("crash", lambda: print("[Page Crash] Page crashed!", flush=True))

        local_love_path = os.path.abspath("tools/balatro_wasm/web/balatro.love")
        local_wasm_path = os.path.abspath("tools/balatro_wasm/web/love.wasm")
        local_app_path = os.path.abspath("tools/balatro_wasm/web/app.js")

        await page.route("**/balatro.love*", lambda r: r.fulfill(path=local_love_path))
        await page.route("**/love.wasm*", lambda r: r.fulfill(path=local_wasm_path))
        await page.route("**/app.js*", lambda r: r.fulfill(path=local_app_path))

        print("[Probe] Navigating to https://balatro.mangoqwq.com ...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded")

        for _ in range(60):
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if btn_visible:
                break
            await asyncio.sleep(0.5)

        print("[Probe] Launching game...", flush=True)
        await page.click("#btn-launch")

        # Wait for splash hide
        for _ in range(30):
            vis = await page.evaluate("() => { const s = document.getElementById('splash-overlay'); return s ? s.classList.contains('hidden') : false; }")
            if vis:
                break
            await asyncio.sleep(0.5)

        print("[Probe] Splash hidden! Enabling AutoPilot...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        in_shop = False
        for step in range(40):
            await asyncio.sleep(1)
            try:
                res = await asyncio.wait_for(
                    page.evaluate("""() => {
                        return {
                            state: window._lastDecidexState,
                            status: document.getElementById('autopilot-status')?.textContent,
                            busy: window.AutoPilot?.busy,
                            rafAlive: Date.now() - (window._lastFrameTime || Date.now())
                        };
                    }"""),
                    timeout=2.0
                )
                st = res.get("state")
                sname = st.get("state_name") if st else "NONE"
                bname = st.get("blind", {}).get("name", "") if st else ""
                scored = st.get("blind", {}).get("scored", 0) if st else 0
                target = st.get("blind", {}).get("chips", 0) if st else 0
                print(f"[{step+1:02d}s] State:{sname:<14} Blind:{bname:<11} Scored:{scored}/{target} Busy:{res.get('busy')} Status:{res.get('status')}", flush=True)

                if sname == "SHOP":
                    in_shop = True
                if in_shop and sname == "BLIND_SELECT":
                    print("[Probe SUCCESS] Successfully reached BLIND_SELECT after SHOP!", flush=True)
                    break
            except asyncio.TimeoutError:
                print(f"[{step+1:02d}s] page.evaluate TIMED OUT (2s)!", flush=True)
                # Check if page is responsive via a tiny eval
                try:
                    t_eval = await asyncio.wait_for(page.evaluate("() => 1 + 1"), timeout=1.0)
                    print(f"       Basic JS test eval returned: {t_eval}", flush=True)
                except Exception as ex:
                    print(f"       Basic JS test eval FAILED: {ex}", flush=True)
            except Exception as e:
                print(f"[{step+1:02d}s] Loop error: {e}", flush=True)

        await browser.close()
        print("[Probe] Finished run.", flush=True)

if __name__ == "__main__":
    asyncio.run(run())
