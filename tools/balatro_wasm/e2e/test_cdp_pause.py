import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def run():
    print("[CDP Inspector] Launching browser with CDP debugging...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--use-gl=angle",
                "--use-angle=swiftshader",
            ]
        )
        context = await browser.new_context()
        page = await context.new_page()

        # Attach CDP session
        cdp = await context.new_cdp_session(page)
        await cdp.send("Debugger.enable")

        paused_frames = []

        async def on_paused(event):
            call_frames = event.get("callFrames", [])
            print(f"\n[CDP PAUSED] Reason: {event.get('reason')} | Frames: {len(call_frames)}", flush=True)
            for i, f in enumerate(call_frames[:15]):
                fn_name = f.get("functionName") or "(anonymous)"
                url = f.get("url") or ""
                line = f.get("lineNumber", 0)
                col = f.get("columnNumber", 0)
                print(f"  #{i} {fn_name} at {url}:{line}:{col}", flush=True)
            paused_frames.append(call_frames)
            # Resume after inspecting
            try:
                await cdp.send("Debugger.resume")
            except Exception:
                pass

        cdp.on("Debugger.paused", lambda e: asyncio.create_task(on_paused(e)))

        local_love_path = os.path.abspath("tools/balatro_wasm/web/balatro.love")
        local_wasm_path = os.path.abspath("tools/balatro_wasm/web/love.wasm")
        local_app_path = os.path.abspath("tools/balatro_wasm/web/app.js")

        await page.route("**/balatro.love*", lambda route: route.fulfill(path=local_love_path))
        await page.route("**/love.wasm*", lambda route: route.fulfill(path=local_wasm_path))
        await page.route("**/app.js*", lambda route: route.fulfill(path=local_app_path))

        page.on("console", lambda msg: print(f"[Console] {msg.text}", flush=True) if (
            (msg.type in ['error', 'warning'] or
             any(x in msg.text for x in ["[AutoPilot]", "[DecideX]", "DECIDEX_STATE"])) and
            "LONG DT" not in msg.text
        ) else None)

        print("[CDP Inspector] Navigating to page...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded")

        for _ in range(60):
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if btn_visible:
                break
            await asyncio.sleep(0.5)

        print("[CDP Inspector] Launching Balatro...", flush=True)
        await page.click("#btn-launch")

        for _ in range(30):
            vis = await page.evaluate("() => { const s = document.getElementById('splash-overlay'); return s ? s.classList.contains('hidden') : false; }")
            if vis:
                break
            await asyncio.sleep(0.5)

        print("[CDP Inspector] Game running. Enabling AutoPilot...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        toggle_dispatched = False

        for step in range(40):
            await asyncio.sleep(1)
            try:
                eval_task = page.evaluate("() => ({ state: window._lastDecidexState, status: document.getElementById('autopilot-status')?.textContent })")
                data = await asyncio.wait_for(eval_task, timeout=1.5)
                st = data.get("state")
                sname = st.get("state_name") if st else "NONE"
                status = data.get("status")
                print(f"[{step:02d}s] {sname:<15} | Status: {status}", flush=True)

                if sname == "SHOP" and st.get("can_toggle_shop") == False:
                    toggle_dispatched = True
            except asyncio.TimeoutError:
                print(f"[{step:02d}s] page.evaluate TIMED OUT! Triggering CDP Debugger.pause ...", flush=True)
                try:
                    await cdp.send("Debugger.pause")
                    await asyncio.sleep(0.5)
                except Exception as ex:
                    print(f"Debugger.pause error: {ex}", flush=True)
            except Exception as e:
                print(f"[{step:02d}s] Error: {e}", flush=True)

        await context.close()
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
