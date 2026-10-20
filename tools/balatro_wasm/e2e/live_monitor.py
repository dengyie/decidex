import asyncio
import json
import time
from playwright.async_api import async_playwright

async def main():
    print("[Live Monitor] Connecting to Chrome CDP at 127.0.0.1:9222...", flush=True)
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
            print("[Live Monitor] No Balatro page found!", flush=True)
            return

        print(f"[Live Monitor] Monitoring Balatro tab: {target_page.url}", flush=True)

        target_page.on("console", lambda msg: print(f"  [Console] {msg.text}", flush=True) if any(
            k in msg.text for k in [
                "[AutoPilot]", "[DecideX]", "booster", "pack", "card", "play", "discard",
                "cash_out", "GAME_OVER", "GAME_WON", "start_run", "shop"
            ]
        ) and "LONG DT" not in msg.text else None)

        last_state_name = None
        for sec in range(60):
            st = await target_page.evaluate("() => window._lastDecidexState")
            ap = await target_page.evaluate("""() => {
                if (!window.AutoPilot) return null;
                const el = document.getElementById('autopilot-status');
                return {
                    enabled: window.AutoPilot.enabled,
                    actionLock: !!window.AutoPilot._actionLock,
                    statusText: el ? el.textContent : ''
                };
            }""")

            if st:
                sname = st.get("state_name")
                if sname != last_state_name:
                    print(f"[{sec}s] >>> STATE CHANGE: {last_state_name} -> {sname}", flush=True)
                    last_state_name = sname

                if sec % 5 == 0 or sname in ["SHOP", "TAROT_PACK", "PLANET_PACK", "BUFFOON_PACK", "STANDARD_PACK", "SPECTRAL_PACK", "GAME_OVER", "ROUND_EVAL"]:
                    chips = st.get("chips")
                    target = st.get("blind_target") or st.get("blind_chips")
                    hands = st.get("hands_left")
                    discards = st.get("discards_left")
                    dollars = st.get("dollars")
                    ante = st.get("ante")
                    rnd = st.get("round")
                    shop_cards = st.get("shop_cards")
                    pack_cards = st.get("pack_cards")
                    
                    info_extra = []
                    if shop_cards:
                        info_extra.append(f"ShopCards: {len(shop_cards)}")
                    if pack_cards:
                        info_extra.append(f"PackCards: {len(pack_cards)}")
                    extra_str = " | " + " ".join(info_extra) if info_extra else ""

                    print(f"[{sec:02d}s] Ante {ante} R{rnd} [{sname}] Score: {chips}/{target} | Hands: {hands} Disc: {discards} | Money: ${dollars}{extra_str} | AP: {ap}", flush=True)

            # If AutoPilot is disabled, automatically re-enable it
            if ap and not ap.get("enabled"):
                print(f"[{sec:02d}s] AutoPilot became disabled, re-enabling...", flush=True)
                await target_page.evaluate("() => window.AutoPilot && window.AutoPilot.toggle(true)")

            await asyncio.sleep(1)

        print("[Live Monitor] Completed 60s observation.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
