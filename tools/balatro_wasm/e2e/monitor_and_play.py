import asyncio
import json
import time
from playwright.async_api import async_playwright

async def run():
    print("[1/4] Attaching to Chrome 9222 Balatro tab...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        target_page = None
        for ctx in browser.contexts:
            for page in ctx.pages:
                if "balatro" in page.url:
                    target_page = page
                    break
        if not target_page:
            print("Error: Balatro page not found!")
            return

        def on_console(msg):
            txt = msg.text
            if any(k in txt for k in ["[DecideX", "[AutoPilot]", "[LÖVE", "FRAME ERROR", "Bridge", "loadstring", "DECIDEX_ACTION_RESULT", "DECIDEX_STATE", "Error:"]):
                if "DECIDEX_STATE" in txt:
                    try:
                        raw = txt.split("[DECIDEX_STATE]")[1].strip()
                        st = json.loads(raw)
                        b = st.get("blind", {})
                        print(f"  [Game State] {st.get('state_name')} | Chips: {b.get('scored')}/{b.get('chips')} | Hands: {st.get('hands_left')} | Discards: {st.get('discards_left')} | Money: ${st.get('money')}", flush=True)
                    except Exception:
                        print(f"  [Console] {txt[:160]}...", flush=True)
                elif len(txt) > 200:
                    print(f"  [Console] {txt[:200]}...", flush=True)
                else:
                    print(f"  [Console] {txt}", flush=True)

        target_page.on("console", on_console)
        target_page.on("pageerror", lambda err: print(f"  [PageError] {err}", flush=True))

        print("[2/4] Waiting for active download to finish...", flush=True)
        ready = False
        for i in range(120): # up to 6 minutes
            info = await target_page.evaluate("""() => {
                const progress = document.getElementById("progress-bar");
                const status = document.getElementById("status-text");
                const btn = document.getElementById("btn-launch");
                const is100 = progress && progress.style.width === "100%";
                const isBtnVisible = btn && window.getComputedStyle(btn).display !== "none" && !btn.disabled;
                return {
                    progress: progress ? progress.style.width : "",
                    status: status ? status.innerText : "",
                    ready: is100 && isBtnVisible
                };
            }""")
            if info.get("ready"):
                print(f"\n[Download Complete!] Status: {info.get('status')}", flush=True)
                ready = True
                break
            if i % 3 == 0:
                print(f"  [{i*3:03d}s] Progress: {info.get('progress')} | {info.get('status')}", flush=True)
            await asyncio.sleep(3)

        if not ready:
            print("Download timed out or failed!")
            return

        print("\n[3/4] Clicking Launch Button...", flush=True)
        await asyncio.sleep(1)
        await target_page.evaluate("() => document.getElementById('btn-launch').click()")

        print("Waiting for game engine to boot into SPLASH state...", flush=True)
        splash_ready = False
        for i in range(40):
            await asyncio.sleep(1)
            state_info = await target_page.evaluate("""() => {
                const overlay = document.getElementById("splash-overlay");
                return {
                    splashHidden: overlay ? overlay.classList.contains("hidden") : false,
                    state: window._lastDecidexState ? window._lastDecidexState.state_name : null,
                    canvasW: document.getElementById("canvas") ? document.getElementById("canvas").width : 0
                };
            }""")
            if i % 3 == 0 or state_info.get("state") == "SPLASH":
                print(f"  [{i:02d}s] Engine: {state_info}", flush=True)
            if state_info.get("state") == "SPLASH":
                splash_ready = True
                break

        if not splash_ready:
            print("Failed to reach SPLASH state after launch!")
            return

        print("\n[4/4] SPLASH reached! Enabling AutoPilot and tracking game progress...", flush=True)
        await asyncio.sleep(1)
        await target_page.evaluate("""() => {
            if (window.AutoPilot && !window.AutoPilot.enabled) {
                window.AutoPilot.toggle(true);
            }
        }""")

        last_state = None
        for i in range(90):
            await asyncio.sleep(1)
            info = await target_page.evaluate("""() => {
                const st = window._lastDecidexState || {};
                const blind = st.blind || {};
                const badge = document.getElementById("autopilot-status");
                return {
                    state: st.state_name,
                    chips: blind.chips,
                    scored: blind.scored,
                    hands: st.hands_left,
                    discards: st.discards_left,
                    money: st.money,
                    autopilotStatus: badge ? badge.innerText : ""
                };
            }""")
            sname = info.get("state")
            if sname != last_state:
                print(f"\n>>> STATE CHANGED: {last_state} -> {sname} (HUD: {info.get('autopilotStatus')})", flush=True)
                last_state = sname
            elif i % 5 == 0:
                print(f"  [{i:02d}s] State: {sname} | Chips: {info.get('scored')}/{info.get('chips')} | Hands: {info.get('hands')} | Money: ${info.get('money')}", flush=True)

            if sname == "SHOP":
                print("\n=========================================================")
                print("🏆 SUCCESS! Reached SHOP! Automated playthrough confirmed!")
                print("=========================================================", flush=True)
                break

if __name__ == "__main__":
    asyncio.run(run())
