import asyncio
from playwright.async_api import async_playwright

async def inspect_g():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        console_logs = []
        page.on('console', lambda msg: console_logs.append(f'[{msg.type}] {msg.text}'))

        await page.goto('http://127.0.0.1:8092', wait_until='domcontentloaded', timeout=15000)
        btn = await page.wait_for_selector('#btn-launch', state='visible', timeout=15000)
        await btn.click()
        await page.wait_for_selector('#splash-overlay', state='hidden', timeout=30000)

        # Wait for Lua boot
        for i in range(15):
            await asyncio.sleep(1)
            cnt = await page.evaluate('() => window._decidexStateCount || 0')
            if cnt > 0:
                break

        # Start run
        await page.evaluate('''() => {
            const FS = window.Module.FS;
            const code = `
                if G.main_menu then G:main_menu() end
                if G.FUNCS and G.FUNCS.start_run then G.FUNCS.start_run(nil, {stake = 1}) end
            `;
            FS.writeFile('/tmp/balatro_eval.lua', code);
        }''')
        await asyncio.sleep(2)

        # Inspect G in BLIND_SELECT
        await page.evaluate('''() => {
            const FS = window.Module.FS;
            const code = `
                print("[LUA_INSPECT] G.STATE = " .. tostring(G.STATE))
                print("[LUA_INSPECT] G.blind_select = " .. tostring(G.blind_select))
                print("[LUA_INSPECT] G.blind_prompt_box = " .. tostring(G.blind_prompt_box))
                print("[LUA_INSPECT] G.blind_select_opts = " .. tostring(G.blind_select_opts))
                if G.blind_select_opts then
                    for k, v in pairs(G.blind_select_opts) do
                        print("[LUA_INSPECT] blind_select_opts[" .. tostring(k) .. "] = " .. tostring(v))
                    end
                end
                print("[LUA_INSPECT] G.FUNCS.select_blind = " .. tostring(G.FUNCS and G.FUNCS.select_blind))
                print("[LUA_INSPECT] G.GAME.blind_on_deck = " .. tostring(G.GAME and G.GAME.blind_on_deck))
                print("[LUA_INSPECT] G.GAME.round_resets.blind_choices = " .. tostring(G.GAME and G.GAME.round_resets and G.GAME.round_resets.blind_choices))
                if G.GAME and G.GAME.round_resets and G.GAME.round_resets.blind_choices then
                    for k, v in pairs(G.GAME.round_resets.blind_choices) do
                        print("[LUA_INSPECT] blind_choice[" .. tostring(k) .. "] = " .. tostring(v))
                    end
                end
            `;
            FS.writeFile('/tmp/balatro_eval.lua', code);
        }''')

        await asyncio.sleep(2)
        logs = [l for l in console_logs if 'LUA_INSPECT' in l]
        for l in logs:
            print(l)

        await browser.close()

asyncio.run(inspect_g())
