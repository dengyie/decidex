import asyncio
import os
import tempfile
from playwright.async_api import async_playwright

async def run():
    print("[Diag] Launching Chromium to diagnose BLIND_SELECT...", flush=True)
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

        page.on('console', lambda msg: print(f"[Console] {msg.text}", flush=True))
        page.on('pageerror', lambda err: print(f"[PageError] {err}", flush=True))
        page.on('dialog', lambda dialog: asyncio.create_task(dialog.dismiss()))

        local_love_path = os.path.abspath("tools/balatro_wasm/web/balatro.love")
        local_wasm_path = os.path.abspath("tools/balatro_wasm/web/love.wasm")

        async def handle_love(route):
            await route.fulfill(path=local_love_path)

        async def handle_wasm(route):
            await route.fulfill(path=local_wasm_path)

        await page.route("**/balatro.love*", handle_love)
        await page.route("**/love.wasm*", handle_wasm)

        await page.goto("https://balatro.mangoqwq.com", wait_until="domcontentloaded", timeout=45000)

        # Wait for launch button
        await page.wait_for_selector("#btn-launch", state="visible", timeout=30000)
        btn = await page.wait_for_selector("#btn-launch")
        await btn.click()
        print("[Diag] Clicked launch...", flush=True)

        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=60000)
        print("[Diag] Splash hidden! Waiting 4s for game loop...", flush=True)
        await asyncio.sleep(4)

        # Trigger start_run
        print("[Diag] Triggering start_run...", flush=True)
        await page.evaluate("""() => {
            window.Module.FS.writeFile('/tmp/balatro_eval.lua', 'if G.FUNCS and G.FUNCS.start_run then G.FUNCS.start_run(nil, {stake=1}) print("[Diag Lua] start_run called") end');
        }""")

        # Wait 3s for BLIND_SELECT
        await asyncio.sleep(3)

        # Now probe all objects in BLIND_SELECT
        print("[Diag] Probing BLIND_SELECT internals...", flush=True)
        probe_lua = """
        local info = {}
        table.insert(info, "STATE=" .. tostring(G.STATE))
        table.insert(info, "STATES.BLIND_SELECT=" .. tostring(G.STATES and G.STATES.BLIND_SELECT))
        table.insert(info, "facing_blind=" .. tostring(G.GAME and G.GAME.facing_blind))
        table.insert(info, "blind_select=" .. tostring(G.blind_select ~= nil))
        table.insert(info, "blind_prompt_box=" .. tostring(G.blind_prompt_box ~= nil))
        if G.blind_prompt_box then
            local p1 = G.blind_prompt_box.get_UIE_by_ID and G.blind_prompt_box:get_UIE_by_ID('prompt_dynatext1')
            local p2 = G.blind_prompt_box.get_UIE_by_ID and G.blind_prompt_box:get_UIE_by_ID('prompt_dynatext2')
            table.insert(info, "p1=" .. tostring(p1 ~= nil) .. ", p1.config=" .. tostring(p1 and p1.config ~= nil) .. ", p1.obj=" .. tostring(p1 and p1.config and p1.config.object ~= nil))
            table.insert(info, "p2=" .. tostring(p2 ~= nil) .. ", p2.config=" .. tostring(p2 and p2.config ~= nil) .. ", p2.obj=" .. tostring(p2 and p2.config and p2.config.object ~= nil))
        end
        local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "nil"
        table.insert(info, "on_deck=" .. tostring(on_deck))
        if G.blind_select_opts then
            table.insert(info, "opts.small=" .. tostring(G.blind_select_opts.small ~= nil) .. ", opts.big=" .. tostring(G.blind_select_opts.big ~= nil))
        else
            table.insert(info, "blind_select_opts=nil")
        end
        print("[Diag Probe]: " .. table.concat(info, " | "))
        """
        await page.evaluate(f"""() => {{
            window.Module.FS.writeFile('/tmp/balatro_eval.lua', {repr(probe_lua)});
        }}""")

        # Wait 3s and inspect
        await asyncio.sleep(3)
        await context.close()
        await browser.close()
        print("[Diag] Done!", flush=True)

if __name__ == "__main__":
    asyncio.run(run())
