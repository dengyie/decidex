"""Diagnostic probe: capture FULL console stream (all levels) during a local
full-flow run, to catch the first-cause of the 8s discard lock force-clears.

Mirrors every console message to stdout with wall-clock prefix. Runs until
MIN_SECONDS elapsed. Output: probe_full_console.log
"""
import asyncio
import os
import sys
import time

from playwright.async_api import async_playwright

BASE_URL = os.environ.get("PROBE_URL", "http://127.0.0.1:8099/play")
MIN_SECONDS = int(os.environ.get("PROBE_SECONDS", "900"))
OUT = os.environ.get("PROBE_OUT", "probe_full_console.log")
PROFILE = os.environ.get("PROBE_PROFILE", "E:/profile/balatro_soak/probe_diag")

log = open(OUT, "w", encoding="utf-8", buffering=1)

t0 = time.monotonic()


def ts():
    return f"{time.monotonic() - t0:7.1f}s"


async def main():
    async with async_playwright() as p:
        os.makedirs(PROFILE, exist_ok=True)
        b = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            headless=True,
            viewport={"width": 1920, "height": 1080},
            args=["--no-sandbox", "--disable-dev-shm-usage", "--enable-webgl"],
        )
        page = b.pages[0] if b.pages else await b.new_page()

        def on_console(msg):
            text = msg.text
            log.write(f"[{ts()}][{msg.type}] {text}\n")

        page.on("console", on_console)
        page.on("pageerror", lambda e: log.write(f"[{ts()}][PAGEERROR] {e}\n"))

        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)
        log.write(f"[{ts()}] page loaded, waiting for launch button\n")
        try:
            await page.wait_for_selector("#btn-launch", timeout=900_000)
            await page.click("#btn-launch")
            log.write(f"[{ts()}] launch clicked\n")
        except Exception as e:
            log.write(f"[{ts()}] launch wait failed: {e}\n")

        end = time.monotonic() + MIN_SECONDS
        while time.monotonic() < end:
            await asyncio.sleep(10)
            try:
                st = await page.evaluate(
                    "window._lastDecidexState ? window._lastDecidexState.state_name : 'none'"
                )
                log.write(f"[{ts()}] STATE={st}\n")
            except Exception as e:
                log.write(f"[{ts()}] eval failed: {e}\n")

    log.close()


asyncio.run(main())
