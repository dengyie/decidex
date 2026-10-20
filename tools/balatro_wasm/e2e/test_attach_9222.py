import asyncio
import sys
from playwright.async_api import async_playwright

async def main():
    print("[Attach 9222] Connecting to Chrome CDP at 127.0.0.1:9222...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        target_page = None
        for ctx in browser.contexts:
            for page in ctx.pages:
                if "balatro" in page.url:
                    target_page = page
                    break
            if target_page:
                break

        if not target_page:
            print("[Attach 9222] No balatro tab found, creating new page...", flush=True)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            target_page = await ctx.new_page()
            await target_page.goto("https://balatro.mangoqwq.com/?v=72", wait_until="domcontentloaded")
        else:
            print(f"[Attach 9222] Navigating target tab to v72: {target_page.url}", flush=True)
            await target_page.goto("https://balatro.mangoqwq.com/?v=72", wait_until="domcontentloaded")

        print(f"[Attach 9222] Target tab found: {target_page.url}", flush=True)

        target_page.on("pageerror", lambda err: print(f"[Browser Error] {err}", flush=True))
        target_page.on("console", lambda msg: print(f"[Console] {msg.text}", flush=True) if any(
            k in msg.text for k in [
                "[AutoPilot]", "[DecideX]", "opened booster", "redeemed", "GAME_OVER",
                "GAME_WON", "start_run", "STATE_NUM", "go_to_menu", "booster pack",
                "buying booster", "selecting pack", "TAROT_PACK", "PLANET_PACK", "BUFFOON_PACK"
            ]
        ) else None)

        # Wait for asset download / launch ready
        print("[Attach 9222] Waiting for asset download / launch ready...", flush=True)
        for i in range(300):
            st_text = await target_page.evaluate("() => { const el = document.getElementById('status-text'); return el ? el.textContent : ''; }")
            btn_visible = await target_page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
            splash_hidden = await target_page.evaluate("() => { const el = document.getElementById('splash-overlay'); return !el || el.classList.contains('hidden') || window.getComputedStyle(el).visibility === 'hidden' || window.getComputedStyle(el).display === 'none'; }")
            game_ready = await target_page.evaluate("() => !!window._lastDecidexState || (window.AutoPilot && window.AutoPilot.enabled !== undefined)")
            
            if btn_visible or (splash_hidden and game_ready):
                print(f"[Attach 9222] Ready condition met! btn_visible={btn_visible}, splash_hidden={splash_hidden}, game_ready={bool(game_ready)}", flush=True)
                break
                
            if i % 5 == 0:
                bar_w = await target_page.evaluate("() => { const el = document.getElementById('progress-bar'); return el ? el.style.width : '0%'; }")
                print(f"[Attach 9222] Progress ({i}s): {st_text} ({bar_w})", flush=True)
            await asyncio.sleep(1)

        # If launch button visible, click it
        btn_visible = await target_page.evaluate("() => { const el = document.getElementById('btn-launch'); return el && window.getComputedStyle(el).display !== 'none'; }")
        if btn_visible:
            print("[Attach 9222] Launch button visible, clicking #btn-launch ...", flush=True)
            launch_btn = await target_page.query_selector("#btn-launch")
            if launch_btn:
                await launch_btn.click()

        # Wait for splash overlay to hide and engine to be ready
        print("[Attach 9222] Waiting for splash overlay to hide...", flush=True)
        for i in range(120):
            splash_hidden = await target_page.evaluate("() => { const el = document.getElementById('splash-overlay'); return !el || el.classList.contains('hidden') || window.getComputedStyle(el).visibility === 'hidden' || window.getComputedStyle(el).display === 'none'; }")
            game_ready = await target_page.evaluate("() => !!window._lastDecidexState || (window.AutoPilot && window.AutoPilot.enabled !== undefined)")
            if splash_hidden and game_ready:
                print(f"[Attach 9222] Game active at {i}s! Splash hidden: {splash_hidden}", flush=True)
                break
            if i % 5 == 0:
                print(f"[Attach 9222] Booting engine... ({i}s) splash_hidden={splash_hidden}, game_ready={bool(game_ready)}", flush=True)
            await asyncio.sleep(1)

        # Check AutoPilot status and enable
        is_ap_enabled = await target_page.evaluate("() => window.AutoPilot && window.AutoPilot.enabled")
        print(f"[Attach 9222] Current AutoPilot enabled: {is_ap_enabled}", flush=True)
        if not is_ap_enabled:
            print("[Attach 9222] Enabling AutoPilot...", flush=True)
            await target_page.evaluate("() => window.AutoPilot && window.AutoPilot.toggle(true)")

        # Verify start_run if at splash/menu
        state_info = await target_page.evaluate("() => window._lastDecidexState || null")
        print(f"[Attach 9222] Current Game State: {state_info.get('state_name') if state_info else 'None'}", flush=True)
        if state_info and state_info.get('state_name') in ['SPLASH', 'MENU']:
            print("[Attach 9222] In SPLASH/MENU, calling decidexStartRun...", flush=True)
            await target_page.evaluate("() => window.decidexStartRun && window.decidexStartRun()")

        # Monitor gameplay for 240 seconds to witness blinds, shop, card packs, and game over handling
        print("[Attach 9222] Monitoring gameplay for 240 seconds...", flush=True)
        last_sname = None
        for sec in range(240):
            await asyncio.sleep(1)
            cur_state = await target_page.evaluate("() => window._lastDecidexState || null")
            ap_status = await target_page.evaluate("() => { const el = document.getElementById('autopilot-status'); return el ? el.textContent : ''; }")
            if cur_state:
                sname = cur_state.get('state_name')
                if sname != last_sname:
                    print(f"[{sec}s] State Transition: {last_sname} -> {sname}", flush=True)
                    last_sname = sname

                if sec % 10 == 0 or sname in ['ROUND_EVAL', 'SHOP', 'TAROT_PACK', 'PLANET_PACK', 'BUFFOON_PACK', 'STANDARD_PACK', 'SPECTRAL_PACK', 'GAME_OVER', 'GAME_WON']:
                    blind = cur_state.get('blind') or {}
                    chips = blind.get('scored', 0)
                    blind_req = blind.get('chips', 0)
                    hands = cur_state.get('hands_left')
                    discards = cur_state.get('discards_left')
                    dollars = cur_state.get('money')
                    ante = cur_state.get('ante')
                    round_num = cur_state.get('round')
                    pack_cards = cur_state.get('pack_cards')
                    pack_info = f" | pack_cards={len(pack_cards)}" if pack_cards else ""
                    print(f"[{sec}s] Ante {ante} R{round_num} | State={sname} | ${dollars} | Chips={chips}/{blind_req} | H:{hands} D:{discards}{pack_info} | AP={ap_status}", flush=True)

        print("[Attach 9222] Monitoring phase complete.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
