import asyncio
import os
import json
from playwright.async_api import async_playwright

async def run_verification():
    print("=================================================================", flush=True)
    print("[Verification] Starting Windowed Mode & Coordinate Accuracy Test", flush=True)
    print("Target: https://balatro.mangoqwq.com", flush=True)
    print("=================================================================", flush=True)

    os.makedirs("tools/balatro_wasm/screenshots", exist_ok=True)
    screenshot_path = "tools/balatro_wasm/screenshots/windowed_hud_verified.png"
    profile_dir = os.path.abspath("tools/balatro_wasm/test_browser_profile")
    os.makedirs(profile_dir, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            viewport={"width": 1920, "height": 1080},
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu-watchdog',
                '--enable-webgl'
            ]
        )
        page = context.pages[0] if context.pages else await context.new_page()

        page.on('pageerror', lambda err: print(f"[PageError] {err}", flush=True))
        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True) if (
            msg.type in ['error', 'warning'] or any(
                x in msg.text for x in ["[DecideX]", "[AutoPilot]", "[DIAG]", "[CLICK_RESULT]", "Fullscreen"]
            )
        ) else None)

        print("[Step 1] Navigating to https://balatro.mangoqwq.com ...", flush=True)
        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=45000)

        # Wait for launch button
        for i in range(120):
            st_text = await page.evaluate("() => { const el = document.getElementById('status-text'); return el ? el.textContent : ''; }")
            btn_visible = await page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            if i % 5 == 0 or btn_visible:
                print(f"[Wait Assets] {i}s: {st_text} | Ready: {btn_visible}", flush=True)
            if btn_visible:
                break
            await asyncio.sleep(1)

        # Verify layout before launch
        layout_before = await page.evaluate("""() => {
            const container = document.getElementById('app-container');
            const viewport = document.getElementById('canvas-viewport');
            const hud = document.getElementById('decidex-hud');
            const cStyle = window.getComputedStyle(container);
            const vStyle = window.getComputedStyle(viewport);
            const hStyle = window.getComputedStyle(hud);
            return {
                container_flex_dir: cStyle.flexDirection,
                viewport_width: parseFloat(vStyle.width),
                hud_width: parseFloat(hStyle.width),
                hud_display: hStyle.display,
                hud_position: hStyle.position,
                fullscreen_active: !!document.fullscreenElement
            };
        }""")
        print("[Layout Check Before Launch]:", json.dumps(layout_before, indent=2), flush=True)
        assert layout_before["container_flex_dir"] == "row", "app-container must use row layout!"
        assert layout_before["hud_width"] >= 350, f"HUD width should be ~380px, got {layout_before['hud_width']}"
        assert not layout_before["fullscreen_active"], "Fullscreen must not be active!"

        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=30000)
        await btn.click()
        print("[Step 3] Clicked launch button, waiting for Love engine init...", flush=True)

        await page.wait_for_selector("#splash-overlay.hidden", timeout=40000)
        print("[Step 4] Splash overlay hidden, game engine is running in window!", flush=True)

        # Check fullscreen state after launch gesture
        layout_after = await page.evaluate("""() => {
            const container = document.getElementById('app-container');
            const viewport = document.getElementById('canvas-viewport');
            const hud = document.getElementById('decidex-hud');
            const cStyle = window.getComputedStyle(container);
            const vStyle = window.getComputedStyle(viewport);
            const hStyle = window.getComputedStyle(hud);
            return {
                container_flex_dir: cStyle.flexDirection,
                viewport_width: parseFloat(vStyle.width),
                hud_width: parseFloat(hStyle.width),
                hud_display: hStyle.display,
                hud_position: hStyle.position,
                fullscreen_active: !!document.fullscreenElement
            };
        }""")
        print("[Layout Check After Launch]:", json.dumps(layout_after, indent=2), flush=True)
        assert not layout_after["fullscreen_active"], "Fullscreen must remain completely disabled after launch!"
        assert layout_after["hud_width"] >= 350, f"HUD width must remain visible, got {layout_after['hud_width']}"

        # Disable autopilot temporarily so we can test precise manual mouse coordinates
        await page.evaluate("""() => {
            const toggle = document.getElementById('autopilot-toggle');
            if (toggle && toggle.checked) toggle.click();
        }""")

        # Start a run via Lua
        print("[Step 5] Triggering new run to reach hand selection state...", flush=True)
        await page.evaluate("(code) => window.Module.FS.writeFile('/tmp/balatro_eval.lua', code)",
                            "if G.STATE == G.STATES.SPLASH then G.FUNCS.start_run() end")

        for _ in range(30):
            await asyncio.sleep(0.5)
            s = await page.evaluate("() => window._lastDecidexState")
            if s and s.get("state_name") == "BLIND_SELECT" and s.get("can_select_blind"):
                break

        # Select Small Blind
        await page.evaluate("(code) => window.Module.FS.writeFile('/tmp/balatro_eval.lua', code)",
                            "if G.STATE == G.STATES.BLIND_SELECT then G.FUNCS.select_blind({config = {id = 'bl_small'}}) end")

        for _ in range(40):
            await asyncio.sleep(0.5)
            s = await page.evaluate("() => window._lastDecidexState")
            if s and s.get("state_name") == "SELECTING_HAND":
                break

        print("[Step 6] Game in SELECTING_HAND! Testing mouse coordinate mapping and HUD data...", flush=True)

        # Verify HUD data is populated
        hud_info = await page.evaluate("""() => {
            return {
                ante: document.getElementById('hud-ante')?.textContent?.trim(),
                round: document.getElementById('hud-round')?.textContent?.trim(),
                money: document.getElementById('hud-money')?.textContent?.trim(),
                hands: document.getElementById('hud-hands')?.textContent?.trim(),
                discards: document.getElementById('hud-discards')?.textContent?.trim(),
                deck: document.getElementById('hud-deck')?.textContent?.trim(),
                primary_rec: document.getElementById('primary-rec-container')?.textContent?.trim(),
                candidates: document.getElementById('candidates-container')?.textContent?.trim()
            };
        }""")
        print("[DecideX HUD Live State]:", json.dumps(hud_info, indent=2), flush=True)
        assert hud_info["ante"] and hud_info["ante"] != "--", "HUD should display active Ante!"
        assert hud_info["hands"] and hud_info["hands"] != "--", "HUD should display remaining hands!"

        # Query Card 1 exact target in Love pixels
        lua_card_info = """
        local c1 = G.hand.cards[1]
        local scale = G.TILESCALE * G.TILESIZE
        local room_x = c1.T.x + (c1.container and c1.container.T.x or 0) + c1.T.w * 0.5
        local room_y = c1.T.y + (c1.container and c1.container.T.y or 0) + c1.T.h * 0.5
        local love_px = room_x * scale
        local love_py = room_y * scale
        local res = string.format('{"love_px": %.2f, "love_py": %.2f, "targetW": %d, "targetH": %d, "room_x": %.3f, "room_y": %.3f}',
            love_px, love_py, love.graphics.getWidth(), love.graphics.getHeight(), room_x, room_y)
        local f = io.open('/tmp/c1_target.json', 'w')
        f:write(res)
        f:close()
        """
        await page.evaluate("(code) => window.Module.FS.writeFile('/tmp/balatro_eval.lua', code)", lua_card_info)
        await asyncio.sleep(0.3)
        card1_info = await page.evaluate("() => JSON.parse(window.Module.FS.readFile('/tmp/c1_target.json', {encoding: 'utf8'}))")
        print("[Card 1 Love Window Target Pixels]:", card1_info, flush=True)

        # Calculate corresponding clientX and clientY on the webpage:
        client_coords = await page.evaluate("""(info) => {
            const canvas = document.getElementById('canvas');
            const rect = canvas.getBoundingClientRect();
            const targetW = info.targetW;
            const targetH = info.targetH;
            const bufferAspect = targetW / targetH;
            const domAspect = rect.width / rect.height;

            let renderW = rect.width;
            let renderH = rect.height;
            let offsetX = 0;
            let offsetY = 0;

            if (domAspect > bufferAspect + 0.001) {
                renderW = rect.height * bufferAspect;
                offsetX = (rect.width - renderW) / 2;
            } else if (domAspect < bufferAspect - 0.001) {
                renderH = rect.width / bufferAspect;
                offsetY = (rect.height - renderH) / 2;
            }

            const scaleX = renderW / targetW;
            const scaleY = renderH / targetH;

            const clientX = rect.left + offsetX + info.love_px * scaleX;
            const clientY = rect.top + offsetY + info.love_py * scaleY;

            return {
                clientX: Math.round(clientX),
                clientY: Math.round(clientY),
                rect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
                renderW, renderH, offsetX, offsetY
            };
        }""", card1_info)
        print("[Computed Client Click Target on Webpage]:", client_coords, flush=True)

        # Move mouse and hover
        await page.mouse.move(client_coords["clientX"], client_coords["clientY"])
        await asyncio.sleep(0.4)

        # Verify hovering in Love
        lua_hover = """
        local c1 = G.hand.cards[1]
        local hov = G.CONTROLLER.hovering.target
        local is_c1 = (hov == c1)
        local res = string.format('{"is_c1": %s, "hov_type": "%s"}',
            tostring(is_c1), tostring(hov and hov.config and hov.config.card_key or (hov and hov.type or "nil")))
        local f = io.open('/tmp/hover_res.json', 'w')
        f:write(res)
        f:close()
        """
        await page.evaluate("(code) => window.Module.FS.writeFile('/tmp/balatro_eval.lua', code)", lua_hover)
        await asyncio.sleep(0.3)
        hover_res = await page.evaluate("() => JSON.parse(window.Module.FS.readFile('/tmp/hover_res.json', {encoding: 'utf8'}))")
        print("[Love Engine Hover Feedback]:", hover_res, flush=True)
        assert hover_res["is_c1"], f"Hover target should be Card 1, got {hover_res}"

        # Click to select Card 1
        print(f"[Step 7] Clicking Card 1 at web coordinates ({client_coords['clientX']}, {client_coords['clientY']})...", flush=True)
        await page.mouse.click(client_coords["clientX"], client_coords["clientY"])
        await asyncio.sleep(0.5)

        # Verify Card 1 is highlighted in G.hand
        lua_click = """
        local c1 = G.hand.cards[1]
        local res = string.format('{"c1_highlighted": %s, "num_highlighted": %d}',
            tostring(c1.highlighted), #G.hand.highlighted)
        local f = io.open('/tmp/click_check.json', 'w')
        f:write(res)
        f:close()
        """
        await page.evaluate("(code) => window.Module.FS.writeFile('/tmp/balatro_eval.lua', code)", lua_click)
        await asyncio.sleep(0.3)
        click_check = await page.evaluate("() => JSON.parse(window.Module.FS.readFile('/tmp/click_check.json', {encoding: 'utf8'}))")
        print("[Love Engine Click Verification Result]:", click_check, flush=True)
        assert click_check["c1_highlighted"], "Card 1 MUST be highlighted after clicking! Coordinate mapping verified!"

        # Take screenshot for visual audit
        await page.screenshot(path=screenshot_path)
        print(f"[Step 8] Saved visual proof screenshot to {screenshot_path}", flush=True)

        # Test AutoPilot to make sure automatic pipeline runs smoothly with the new layout
        print("[Step 9] Testing AutoPilot with windowed layout & DecideX HUD...", flush=True)
        await page.evaluate("() => window.AutoPilot.toggle(true)")

        saw_hand_played = False
        for step in range(30):
            await asyncio.sleep(1)
            st = await page.evaluate("() => window._lastDecidexState")
            status = await page.evaluate("() => { const b = document.getElementById('autopilot-status'); return b ? b.textContent : ''; }")
            if st:
                sname = st.get("state_name")
                scored = st.get("blind", {}).get("scored", 0)
                target = st.get("blind", {}).get("chips", 0)
                hands = st.get("hands_left", 0)
                print(f"[AutoPilot Step {step+1:02d}] {sname} | Scored: {scored}/{target} | Hands left: {hands} | Status: {status}", flush=True)
                if scored > 0:
                    saw_hand_played = True
                if (target > 0 and scored >= target) or sname == "ROUND_EVAL":
                    print("[AutoPilot] Successfully cleared blind!", flush=True)
                    break

        assert saw_hand_played, "AutoPilot should play hands successfully!"
        print("=================================================================", flush=True)
        print("[SUCCESS] All windowed mode, HUD display, and mouse coordinate tests PASSED!", flush=True)
        print("=================================================================", flush=True)

        await context.close()

if __name__ == "__main__":
    asyncio.run(run_verification())
