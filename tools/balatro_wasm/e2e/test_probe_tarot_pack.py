"""Diagnose the booster-pack wedge against a live site.

The AutoPilot can get stuck in a pack state with no G.pack_cards. This probe
drives the run until that happens and then dumps the live VM state that
matters:

  * G.SETTINGS.paused and G.OVERLAY_MENU -- a modal overlay pauses the game,
    which freezes G.TIMERS.TOTAL and stops every pending event.
  * The DecideX pack-watchdog bookkeeping, so we can tell whether the
    watchdog saw the stall at all.
  * The head of the E_MANAGER base queue, because that queue is serial and
    head-of-line blocking: one stuck event stops the pack UIBox event.

Set BALATRO_PROBE_URL to point at production or a local serve.py.
"""
import asyncio
import os

from playwright.async_api import async_playwright

BASE_URL = os.environ.get("BALATRO_PROBE_URL", "https://balatro.mangoqwq.com")
DURATION = int(os.environ.get("BALATRO_PROBE_SECONDS", "420"))

# NOTE: this string is a Python raw string, so '\\n' below is a literal
# backslash-n in the Lua/JS source, which is what the eval channel needs.
DUMP_LUA = r"""
print("[DUMP4] G.STATE=" .. tostring(G.STATE) ..
      " state_complete=" .. tostring(G.STATE_COMPLETE) ..
      " paused=" .. tostring(G.SETTINGS and G.SETTINGS.paused) ..
      " overlay_menu=" .. tostring(not not G.OVERLAY_MENU) ..
      " overlay_tutorial=" .. tostring(not not G.OVERLAY_TUTORIAL) ..
      " pack_cards=" .. tostring(G.pack_cards ~= nil) ..
      " booster_pack=" .. tostring(G.booster_pack ~= nil) ..
      " pack_size=" .. tostring(G.GAME and G.GAME.pack_size) ..
      " pack_interrupt=" .. tostring(G.GAME and G.GAME.PACK_INTERRUPT) ..
      " screenwipe=" .. tostring(G.screenwipe ~= nil) ..
      " locks_use=" .. tostring(G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.use))
print("[DUMP4] watchdog stall_time=" .. tostring(_G._pack_stall_time) ..
      " handled=" .. tostring(_G._pack_stall_handled) ..
      " watchdog_fn=" .. tostring(type(_G.DECIDEX_STEP)))
if G.E_MANAGER and G.E_MANAGER.queues then
  for k, v in pairs(G.E_MANAGER.queues) do
    local n = (v and #v or 0)
    print("[DUMP4] queue." .. tostring(k) .. "# = " .. tostring(n))
    if k == 'base' and n > 0 then
      -- The base queue is serial: report the head few events and whether
      -- they are blocking, because one stuck event blocks the rest.
      for i = 1, math.min(n, 6) do
        local e = v[i]
        local trig = '?'
        local delay = 0
        local blocking = true
        local done = false
        pcall(function()
          trig = tostring(e.trigger)
          delay = tostring(e.delay)
          blocking = tostring(e.blocking)
          done = tostring(e.complete)
        end)
        print("[DUMP4]   base[" .. i .. "] trigger=" .. trig ..
              " delay=" .. delay .. " blocking=" .. blocking ..
              " complete=" .. done)
      end
    end
  end
end
print("[DUMP4_END]")
"""


async def run():
    async with async_playwright() as p:
        b = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--enable-webgl"],
        )
        page = await b.new_page()
        page.on("console", lambda m: print(f"[{m.type}] {m.text[:700]}", flush=True))
        page.on(
            "pageerror",
            lambda err: print(f"[PageError] {err}", flush=True),
        )
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)
        launch_timeout = int(os.environ.get("BALATRO_LAUNCH_TIMEOUT", "420")) * 1000
        btn = await page.wait_for_selector("#btn-launch", state="visible", timeout=launch_timeout)
        await btn.click()
        print("[Probe] Launch clicked.", flush=True)
        await page.wait_for_selector("#splash-overlay", state="hidden", timeout=120_000)

        await page.evaluate(
            """() => {
                window.__probeSeq = (window.__probeSeq || 700000) + 1;
                window.__probeLua = (code) => {
                    window.__probeSeq += 1;
                    const FS = window.Module && window.Module.FS;
                    if (!FS) return false;
                    try { FS.unlink('/tmp/balatro_eval_ack.txt'); } catch (_) {}
                    FS.writeFile('/tmp/balatro_eval.lua',
                        '-- DECIDEX_SEQ:' + window.__probeSeq + '\\n' + code);
                    return true;
                };
                // True when the bridge consumed the eval file within `ms`.
                window.__probeWaitConsume = async (ms) => {
                    const FS = window.Module && window.Module.FS;
                    if (!FS) return false;
                    const deadline = Date.now() + ms;
                    while (Date.now() < deadline) {
                        let exists = true;
                        try { exists = FS.analyzePath('/tmp/balatro_eval.lua').exists; } catch (_) {}
                        if (!exists) return true;
                        await new Promise(r => setTimeout(r, 250));
                    }
                    return false;
                };
            }"""
        )

        await page.evaluate("() => window.AutoPilot.toggle(true)")
        print("[Probe] AutoPilot enabled.", flush=True)

        empty_since = None
        for i in range(DURATION):
            await asyncio.sleep(1)
            st = await page.evaluate("() => window._lastDecidexState")
            if not st:
                continue
            sname = st.get("state_name")
            if sname in ("TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK", "STANDARD_PACK", "BUFFOON_PACK"):
                pk = st.get("pack_cards") or []
                if not pk and empty_since is None:
                    empty_since = i
                    print(f"[Probe] PACK EMPTY at t={i}s state={sname} can_skip={st.get('can_skip_booster')}", flush=True)
                if pk and empty_since is not None:
                    print(f"[Probe] RECOVERED at t={i}s pack_cards={len(pk)}", flush=True)
                    empty_since = None
                if empty_since is not None and i - empty_since in (5, 12, 25):
                    print(f"[Probe] dumping VM state at t={i}s (empty for {i-empty_since}s)", flush=True)
                    await page.evaluate("(code) => window.__probeLua(code)", DUMP_LUA)
                    consumed = await page.evaluate("() => window.__probeWaitConsume(4000)")
                    if not consumed:
                        print("[Probe] LUA LOOP DEAD — eval file never consumed; the "
                              "WASM mainloop is wedged (frame error, abort or OOM).", flush=True)
                    await asyncio.sleep(2)
            elif empty_since is not None:
                print(f"[Probe] left pack state at t={i}s -> {sname}", flush=True)
                empty_since = None
        await b.close()


if __name__ == "__main__":
    asyncio.run(run())
