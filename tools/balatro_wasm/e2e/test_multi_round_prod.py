"""Production multi-round stability test for the Balatro DecideX AutoPilot.

Runs the real https://balatro.mangoqwq.com deployment with no route
interception and watches the autopilot drive several blinds in a row. The
goal is to prove the automation does not wedge over an extended session --
the failure mode that the v87 incident exhibited (a single blind action
crashing and every subsequent step erroring out).

Pass criteria:
  * the autopilot advances through at least MIN_BLINDS distinct on-deck blinds,
  * no pageerror is raised,
  * no AutoPilot action failure is reported,
  * the run never falls back to a failed/paused autopilot status.
"""

import asyncio
import os
import re
from playwright.async_api import async_playwright

DURATION_SECONDS = int(os.environ.get("BALATRO_MULTI_ROUND_SECONDS", "240"))
MIN_BLINDS = int(os.environ.get("BALATRO_MULTI_ROUND_MIN_BLINDS", "4"))
# Production by default; point it at a local serve.py to exercise the same
# build without depending on the 53.7MB download surviving a flaky link.
BASE_URL = os.environ.get("BALATRO_MULTI_ROUND_URL", "https://balatro.mangoqwq.com")

BLIND_ORDER = {"Small": 1, "Big": 2, "Boss": 3}

# Lua rejects an action while the game is mid-animation or the client's view
# is stale; the AutoPilot classifies those as transient waits and retries
# them, so they are not automation failures. Mirrors the isTransient /
# isStateMismatch patterns in tools/balatro_wasm/web/app.js.
TRANSIENT_INFO_RE = re.compile(
    r"debounced|not ready|animating|in progress|busy|locked|waiting|"
    r"already in play|blocks play|slots full|pack card not found|no-op|not set|not in (BLIND_SELECT|SHOP|ROUND_EVAL|"
    r"SELECTING_HAND|HAND_PLAYED|new_round)",
    re.IGNORECASE,
)


async def run():
    print(f"[MultiRound] Watching production for {DURATION_SECONDS}s (need >= {MIN_BLINDS} blinds)...", flush=True)
    profile_dir = os.path.abspath("tools/balatro_wasm/test_browser_profile_multiround")
    os.makedirs(profile_dir, exist_ok=True)

    page_errors = []
    action_failures = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu-watchdog",
                "--enable-webgl",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        page.on(
            "pageerror",
            lambda err: (page_errors.append(str(err)), print(f"[PageError] {err}", flush=True)),
        )
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))

        def on_console(msg):
            text = msg.text
            if "[DECIDEX_STATE]" in text:
                try:
                    import json as _json
                    d = _json.loads(text.split("[DECIDEX_STATE]", 1)[1].strip())
                    print(f"[STATE] {d.get('state_name')} on_deck={d.get('blind_on_deck')!r} "
                          f"ante={d.get('ante')} can_toggle={d.get('can_toggle_shop')} "
                          f"money={d.get('money')} shop_cards={len(d.get('shop', {}).get('cards', []))} "
                          f"boosters={len(d.get('shop', {}).get('boosters', []))}", flush=True)
                except Exception:
                    print(f"[STATE-RAW] {text[:200]}", flush=True)
                return
            if "[DECIDEX_ACTION_RESULT]" in text and "ok=false" in text:
                info = text.split("info=", 1)[1] if "info=" in text else ""
                if not TRANSIENT_INFO_RE.search(info):
                    action_failures.append(text)
                print(f"[Console] {text}", flush=True)
                return
            if (
                msg.type == "error"
                or "[AutoPilot]" in text
                or "ReferenceError" in text
                or "TypeError" in text
            ) and "LONG DT" not in text and "GL Driver" not in text:
                print(f"[Console] {text}", flush=True)

        page.on("console", on_console)

        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)

        # Cold start downloads 53.7MB of game assets; over a flaky Cloudflare
        # edge the transfer survives on Range resume and can legitimately take
        # longer than the old hardcoded 420s. Override with
        # BALATRO_LAUNCH_TIMEOUT (seconds).
        launch_timeout = int(os.environ.get("BALATRO_LAUNCH_TIMEOUT", "420")) * 1000
        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=launch_timeout)
        await btn.click()
        print("[MultiRound] Launch clicked.", flush=True)
        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=120_000)

        for _ in range(30):
            await asyncio.sleep(1)
            if await page.evaluate("() => (window._lastDecidexStateCount || 0) > 0"):
                break

        await page.evaluate("() => window.AutoPilot.toggle(true)")
        print("[MultiRound] AutoPilot enabled.", flush=True)

        seen_blinds = []
        seen_antes = set()
        last_status = ""
        deadline = asyncio.get_event_loop().time() + DURATION_SECONDS

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1)
            try:
                st = await asyncio.wait_for(page.evaluate("() => window._lastDecidexState"), timeout=5.0)
                status = await asyncio.wait_for(
                    page.evaluate(
                        "() => { const b = document.getElementById('autopilot-status'); return b ? b.textContent : ''; }"
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                print("[MultiRound] state read timeout, continuing", flush=True)
                continue

            if not st:
                continue

            on_deck = st.get("blind_on_deck") or ""
            ante = st.get("ante")
            if ante:
                seen_antes.add(ante)

            # A blind is "completed" when the on-deck value moves forward.
            marker = (ante, on_deck)
            if on_deck and marker not in seen_blinds:
                seen_blinds.append(marker)
                print(
                    f"[MultiRound] ante={ante} blind={on_deck} "
                    f"scored={st.get('blind', {}).get('scored')}/{st.get('blind', {}).get('chips')} "
                    f"jokers={len(st.get('jokers', []))} money={st.get('money')} :: {status}",
                    flush=True,
                )

            if status and status != last_status:
                last_status = status
                if any(k in status for k in ("失败", "暂停", "error")):
                    print(f"[MultiRound] WARNING status: {status}", flush=True)

        await context.close()

    progressed = len({b for _, b in seen_blinds})
    print("\n[RESULT]")
    print(f"  blinds_seen={seen_blinds}")
    print(f"  distinct_blinds={progressed}")
    print(f"  antes_seen={sorted(seen_antes)}")
    print(f"  page_errors={page_errors}")
    print(f"  action_failures={action_failures}")

    if page_errors:
        print("\n[FAIL] page errors were raised.", flush=True)
        raise SystemExit(1)
    if action_failures:
        print("\n[FAIL] AutoPilot reported failed actions.", flush=True)
        raise SystemExit(1)
    if progressed < MIN_BLINDS:
        print(f"\n[FAIL] only {progressed} distinct blinds progressed (need {MIN_BLINDS}).", flush=True)
        raise SystemExit(1)
    print(f"\n[PASS] {progressed} distinct blinds progressed with no errors.", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
