import asyncio
import sys
from playwright.async_api import async_playwright

async def run():
    print("[Diag] Starting Playwright...", flush=True)
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
        page.on("console", lambda msg: print(f"[Console] {msg.text}", flush=True) if "LONG DT" not in msg.text else None)
        page.on("pageerror", lambda err: print(f"[PageError] {err}", flush=True))

        # Route local assets
        import os
        local_love_path = os.path.abspath("tools/balatro_wasm/web/balatro.love")
        local_wasm_path = os.path.abspath("tools/balatro_wasm/web/love.wasm")
        local_app_path = os.path.abspath("tools/balatro_wasm/web/app.js")

        await page.route("**/balatro.love*", lambda route: route.fulfill(path=local_love_path))
        await page.route("**/love.wasm*", lambda route: route.fulfill(path=local_wasm_path))
        await page.route("**/app.js*", lambda route: route.fulfill(path=local_app_path))

        print("[Diag] Navigating...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded")
        
        # Clear any old CacheStorage except v23
        await page.evaluate("""async () => {
            if (window.caches) {
                const keys = await caches.keys();
                for (const k of keys) {
                    if (k !== 'balatro-asset-cache-v23') {
                        await caches.delete(k);
                    }
                }
            }
        }""")

        for _ in range(60):
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if btn_visible:
                break
            await asyncio.sleep(0.5)

        print("[Diag] Clicking launch button...", flush=True)
        await page.click("#btn-launch")

        # Wait for splash hide
        for _ in range(30):
            vis = await page.evaluate("() => { const s = document.getElementById('splash-overlay'); return s ? s.classList.contains('hidden') : false; }")
            if vis:
                break
            await asyncio.sleep(0.5)
        print("[Diag] Splash hidden! Waiting for DecideX state stream...", flush=True)

        # Enable AutoPilot
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        for step in range(35):
            await asyncio.sleep(1)
            try:
                eval_task = page.evaluate("() => ({ state: window._lastDecidexState, status: document.getElementById('autopilot-status')?.textContent })")
                data = await asyncio.wait_for(eval_task, timeout=2.0)
                st = data.get("state")
                status = data.get("status")
                sname = st.get("state_name") if st else "NONE"
                cards = len(st.get("hand", [])) if st else 0
                scored = st.get("blind", {}).get("scored", 0) if st else 0
                target = st.get("blind", {}).get("chips", 0) if st else 0
                hands = st.get("hands_left", 0) if st else 0
                bname = st.get("blind", {}).get("name", "") if st else ""
                flags = f"Cash:{'T' if st and st.get('can_cash_out') else 'F'} Shop:{'T' if st and st.get('can_toggle_shop') else 'F'} Blind:{'T' if st and st.get('can_select_blind') else 'F'}"
                print(f"[{step+1:02d}s] {sname:<15} | Cards:{cards} | Blind:{bname:<11} | Scored:{scored:>3}/{target:<3} | Hands:{hands} | {flags} | Status:{status}", flush=True)
                
                if step >= 25 and (sname == "SHOP" or sname == "BLIND_SELECT"):
                    # Check if next_round_button exists
                    debug_info = await page.evaluate("""() => {
                        return {
                            hasModule: !!window.Module,
                            hasFS: !!(window.Module && window.Module.FS),
                            evalFile: window.Module && window.Module.FS ? (function() {
                                try { return window.Module.FS.readFile('/tmp/balatro_eval.lua', { encoding: 'utf8' }); } catch(e) { return e.toString(); }
                            })() : null
                        };
                    }""")
                    print(f"       Debug: {debug_info}", flush=True)
            except asyncio.TimeoutError:
                print(f"[{step+1:02d}s] page.evaluate TIMED OUT (2s)!", flush=True)
            except Exception as e:
                print(f"[{step+1:02d}s] Eval error: {e}", flush=True)

        await browser.close()
        print("[Diag] Completed diagnostic run.", flush=True)

if __name__ == "__main__":
    asyncio.run(run())
