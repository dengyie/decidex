#!/usr/bin/env python3
"""
Balatro WebAssembly (love.js) Build Pipeline
===========================================
Extracts Balatro game assets from Balatro.exe, applies essential WebAssembly,
WebGL shader, audio, and DecideX bridge patches, and bundles them into
balatro.love for execution in love.js (LÖVE 11.5).

Usage:
  python build.py [--exe PATH] [--output PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

DEFAULT_EXE = r"E:\game\gamebox\Balatro v1.0.1\Balatro.exe"
TOOLS_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = TOOLS_DIR / "web" / "balatro.love"
PATCHES_DIR = TOOLS_DIR / "patches"


def patch_hologram_shader(content: str) -> str:
    """
    WebGL 1.0 (GLSL ES 1.00) requires constant loop conditions.
    Replace dynamic variable bounds with constant integers.
    """
    pattern_i = r"for\s*\(\s*int\s+i\s*=\s*-glow_samples\s*;\s*i\s*<=\s*glow_samples\s*;\s*\+\+i\s*\)"
    content = re.sub(pattern_i, "for (int i = -4; i <= 4; ++i)", content)

    pattern_j = r"for\s*\(\s*int\s+j\s*=\s*-glow_samples\s*;\s*j\s*<=\s*glow_samples\s*;\s*\+\+j\s*\)"
    content = re.sub(pattern_j, "for (int j = -4; j <= 4; ++j)", content)
    return content


def patch_globals(content: str) -> str:
    """
    Disable F_SOUND_THREAD so audio processing runs on the main thread
    without triggering pthread / thread blocking errors in WASM.
    Disable F_NO_SAVING and enable F_SKIP_TUTORIAL to eliminate blocking
    synchronous file deflate serialization and modal tutorial popups in browser.
    Force screenmode = 'Windowed' to completely disable fullscreen.
    """
    content = content.replace("self.F_SOUND_THREAD = true", "self.F_SOUND_THREAD = false")
    content = content.replace("self.F_SKIP_TUTORIAL = false", "self.F_SKIP_TUTORIAL = true")
    content = content.replace("self.F_NO_SAVING = false", "self.F_NO_SAVING = true")
    content = content.replace("screenmode = 'Borderless',", "screenmode = 'Windowed',")
    return content


def patch_main(content: str) -> str:
    """
    Prepend web_patches and decidex_bridge requires to main.lua,
    and disable Steam/luasteam initialization that calls love.event.quit().
    """
    prefix = (
        '-- [[ DecideX WebAssembly Injections ]]\n'
        'require "web_patches"\n'
        'require "decidex_bridge"\n'
        'print("[Balatro Trace] 1. web_patches and bridge loaded")\n\n'
    )
    if 'require "web_patches"' not in content:
        content = prefix + content

    content = content.replace(
        'require "challenges"',
        'require "challenges"\nprint("[Balatro Trace] 2. all modules required, G.SEED=" .. tostring(G and G.SEED))'
    )
    content = content.replace(
        'function love.run()',
        'print("[Balatro Trace] 3. love.run registering")\nfunction love.run()'
    )
    content = content.replace(
        'function love.load()',
        'function love.load()\nprint("[Balatro Trace] 4. love.load started")'
    )
    content = content.replace(
        'G:start_up()',
        'print("[Balatro Trace] 5. calling G:start_up()")\nG:start_up()\nprint("[Balatro Trace] 6. G:start_up() completed")'
    )
    content = content.replace(
        'function love.update( dt )',
        'function love.update( dt )\n    if _G.DECIDEX_STEP then _G.DECIDEX_STEP(dt) end'
    )

    # Completely bypass luasteam / Steamworks init on web
    content = content.replace(
        "if os == 'OS X' or os == 'Windows' then",
        "if false and (os == 'OS X' or os == 'Windows') then"
    )

    # Wrap love.run's inner per-frame loop in xpcall so Emscripten main loop never aborts on Lua errors
    orig_run_pattern = (
        "\t-- Main loop time.\n"
        "\treturn function()\n"
        "\t\trun_time = love.timer.getTime()\n"
    )
    new_run_step = (
        "\t-- Main loop time.\n"
        "\tlocal __main_step = function()\n"
        "\t\trun_time = love.timer.getTime()\n"
    )
    if orig_run_pattern in content:
        content = content.replace(orig_run_pattern, new_run_step)
        orig_end = (
            "\t\trun_time = math.min(love.timer.getTime() - run_time, 0.1)\n"
            "\t\tG.FPS_CAP = G.FPS_CAP or 500\n"
            "\t\tif run_time < 1./G.FPS_CAP then love.timer.sleep(1./G.FPS_CAP - run_time) end\n"
            "\tend\n"
            "end\n"
        )
        new_end = (
            "\t\trun_time = math.min(love.timer.getTime() - run_time, 0.1)\n"
            "\t\tG.FPS_CAP = G.FPS_CAP or 500\n"
            "\t\tif run_time < 1./G.FPS_CAP then love.timer.sleep(1./G.FPS_CAP - run_time) end\n"
            "\tend\n"
            "\treturn function()\n"
            "\t\txpcall(__main_step, function(msg)\n"
            "\t\t\tprint('[BALATRO FRAME ERROR]: ' .. tostring(msg))\n"
            "\t\t\tprint(debug.traceback())\n"
            "\t\tend)\n"
            "\tend\n"
            "end\n"
        )
        content = content.replace(orig_end, new_end)

    # Patch love.errhand to avoid infinite-loop freezing browser on uncaught Lua errors
    errhand_pattern = r'function love\.errhand\(msg\).*?\nend\n'
    errhand_replacement = (
        'function love.errhand(msg)\n'
        '    local err_msg = tostring(msg)\n'
        '    local trace = debug.traceback()\n'
        '    print("[BALATRO CRITICAL ERROR]: " .. err_msg)\n'
        '    print(trace)\n'
        '    if G and G.FILE_HANDLER then G.FILE_HANDLER.update_queued = false end\n'
        '    return\n'
        'end\n'
    )
    content = re.sub(errhand_pattern, errhand_replacement, content, flags=re.DOTALL)
    return content


def patch_conf(content: str) -> str:
    """
    Set t.window.usedpiscale = false for consistent touch/mouse coordinates.
    Ensure fullscreen is explicitly disabled in window configuration.
    """
    if "t.window.usedpiscale" not in content:
        content = content.replace(
            "t.window.minheight = 100",
            "t.window.minheight = 100\n    t.window.usedpiscale = false\n    t.window.fullscreen = false"
        )
    return content


def patch_common_events(content: str) -> str:
    """
    Ensure asynchronous scoring animation events in add_round_eval_row
    do not crash when cash_out has already cleaned up G.round_eval.
    """
    content = content.replace(
        "function add_round_eval_row(config)",
        "function add_round_eval_row(config)\n    if not (G and G.round_eval and not G.round_eval.REMOVED) then return end"
    )
    content = content.replace(
        "func = function() \n                        local spacer =",
        "func = function() \n                        if not (G and G.round_eval and not G.round_eval.REMOVED) then return true end\n                        local spacer ="
    )
    content = content.replace(
        "func = function()\n                --Add the far left text and context first:",
        "func = function()\n                if not (G and G.round_eval and not G.round_eval.REMOVED) then return true end\n                --Add the far left text and context first:"
    )
    content = content.replace(
        "func = function()\n                    G.round_eval:add_child(",
        "func = function()\n                    if not (G and G.round_eval and not G.round_eval.REMOVED) then return true end\n                    G.round_eval:add_child("
    )
    content = content.replace(
        "func = function()\n                        if i%30 == 1 then",
        "func = function()\n                        if not (G and G.round_eval and not G.round_eval.REMOVED) then return true end\n                        if i%30 == 1 then"
    )
    return content


def patch_game(content: str) -> str:
    """
    Ensure G.shop, G.round_eval, and UI elements do not trigger nil-indexing crashes
    during rapid transitions or delayed animations.
    """
    content = content.replace(
        "G.shop.alignment.offset.y = -5.3",
        "if G.shop and G.shop.alignment and G.shop.alignment.offset then G.shop.alignment.offset.y = -5.3 end"
    )
    content = content.replace(
        "G.shop.alignment.offset.x = 0",
        "if G.shop and G.shop.alignment and G.shop.alignment.offset then G.shop.alignment.offset.x = 0 end"
    )
    content = content.replace(
        "if math.abs(G.shop.T.y - G.shop.VT.y) < 3 then",
        "if not (G and G.shop and not G.shop.REMOVED and G.shop.T and G.shop.VT) then return true end\n                            if math.abs(G.shop.T.y - G.shop.VT.y) < 3 then"
    )
    content = content.replace(
        "if G.round_eval.alignment.offset.y ~= -7.8 then",
        "if not (G and G.round_eval and not G.round_eval.REMOVED and G.round_eval.alignment and G.round_eval.alignment.offset) then return true end\n                        if G.round_eval.alignment.offset.y ~= -7.8 then"
    )
    content = content.replace(
        "if math.abs(G.round_eval.T.y - G.round_eval.VT.y) < 3 then",
        "if not (G and G.round_eval and not G.round_eval.REMOVED and G.round_eval.T and G.round_eval.VT) then return true end\n                            if math.abs(G.round_eval.T.y - G.round_eval.VT.y) < 3 then"
    )
    return content


def patch_button_callbacks(content: str) -> str:
    """
    Safeguard G.shop and G.SHOP_SIGN during toggle_shop and card consumption.
    Thoroughly prevents nil indexing crashes on G.shop:remove() (line 2489)
    and eliminates re-entrancy race conditions during shop transitions.
    """
    old_toggle_shop_func = (
        "  G.FUNCS.toggle_shop = function(e)\n"
        "    stop_use()\n"
        "    G.CONTROLLER.locks.toggle_shop = true\n"
        "    if G.shop then \n"
        "      for i = 1, #G.jokers.cards do\n"
        "        G.jokers.cards[i]:calculate_joker({ending_shop = true})\n"
        "      end\n"
        "      G.E_MANAGER:add_event(Event({\n"
        "        trigger = 'immediate',\n"
        "        func = function()\n"
        "          G.shop.alignment.offset.y = G.ROOM.T.y + 29\n"
        "          G.SHOP_SIGN.alignment.offset.y = -15\n"
        "          return true\n"
        "        end\n"
        "      })) \n"
        "      G.E_MANAGER:add_event(Event({\n"
        "        trigger = 'after',\n"
        "        delay = 0.5,\n"
        "        func = function()\n"
        "          G.shop:remove()\n"
        "          G.shop = nil\n"
        "          G.SHOP_SIGN:remove()\n"
        "          G.SHOP_SIGN = nil\n"
        "          G.STATE_COMPLETE = false\n"
        "          G.STATE = G.STATES.BLIND_SELECT\n"
        "          G.CONTROLLER.locks.toggle_shop = nil\n"
        "          return true\n"
        "        end\n"
        "      }))\n"
        "    end\n"
        "  end"
    )
    new_toggle_shop_func = (
        "  G.FUNCS.toggle_shop = function(e)\n"
        "    if G.toggling_shop or (G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.toggle_shop) then\n"
        "      return\n"
        "    end\n"
        "    G.toggling_shop = true\n"
        "    stop_use()\n"
        "    if G.CONTROLLER and G.CONTROLLER.locks then\n"
        "      G.CONTROLLER.locks.toggle_shop = true\n"
        "    end\n"
        "    if G.shop and not G.shop.REMOVED then\n"
        "      if G.jokers and G.jokers.cards then\n"
        "        for i = 1, #G.jokers.cards do\n"
        "          if G.jokers.cards[i] and G.jokers.cards[i].calculate_joker then\n"
        "            G.jokers.cards[i]:calculate_joker({ending_shop = true})\n"
        "          end\n"
        "        end\n"
        "      end\n"
        "      G.E_MANAGER:add_event(Event({\n"
        "        trigger = 'immediate',\n"
        "        func = function()\n"
        "          if G.shop and G.shop.alignment and G.shop.alignment.offset then\n"
        "            G.shop.alignment.offset.y = G.ROOM.T.y + 29\n"
        "          end\n"
        "          if G.SHOP_SIGN and G.SHOP_SIGN.alignment and G.SHOP_SIGN.alignment.offset then\n"
        "            G.SHOP_SIGN.alignment.offset.y = -15\n"
        "          end\n"
        "          return true\n"
        "        end\n"
        "      }))\n"
        "      G.E_MANAGER:add_event(Event({\n"
        "        trigger = 'after',\n"
        "        delay = 0.5,\n"
        "        func = function()\n"
        "          if G.shop then\n"
        "            pcall(function() G.shop:remove() end)\n"
        "          end\n"
        "          G.shop = nil\n"
        "          if G.SHOP_SIGN then\n"
        "            pcall(function() G.SHOP_SIGN:remove() end)\n"
        "          end\n"
        "          G.SHOP_SIGN = nil\n"
        "          G.STATE_COMPLETE = false\n"
        "          G.STATE = G.STATES.BLIND_SELECT\n"
        "          G.toggling_shop = nil\n"
        "          if G.CONTROLLER and G.CONTROLLER.locks then\n"
        "            G.CONTROLLER.locks.toggle_shop = nil\n"
        "          end\n"
        "          return true\n"
        "        end\n"
        "      }))\n"
        "    else\n"
        "      G.shop = nil\n"
        "      G.SHOP_SIGN = nil\n"
        "      G.STATE_COMPLETE = false\n"
        "      G.STATE = G.STATES.BLIND_SELECT\n"
        "      G.toggling_shop = nil\n"
        "      if G.CONTROLLER and G.CONTROLLER.locks then\n"
        "        G.CONTROLLER.locks.toggle_shop = nil\n"
        "      end\n"
        "    end\n"
        "  end"
    )
    if old_toggle_shop_func in content:
        content = content.replace(old_toggle_shop_func, new_toggle_shop_func)
    else:
        # Fallback to granular replacement if whitespace varies
        old_clean = (
            "          G.shop:remove()\n"
            "          G.shop = nil\n"
            "          G.SHOP_SIGN:remove()\n"
            "          G.SHOP_SIGN = nil"
        )
        new_clean = (
            "          if G.shop then pcall(function() G.shop:remove() end) end\n"
            "          G.shop = nil\n"
            "          if G.SHOP_SIGN then pcall(function() G.SHOP_SIGN:remove() end) end\n"
            "          G.SHOP_SIGN = nil"
        )
        content = content.replace(old_clean, new_clean)

    old_snap = "G.CONTROLLER:snap_to({node = G.shop:get_UIE_by_ID('next_round_button')})"
    new_snap = "if G.shop then G.CONTROLLER:snap_to({node = G.shop:get_UIE_by_ID('next_round_button')}) end"
    content = content.replace(old_snap, new_snap)

    old_consume = (
        "                  if G.shop then \n"
        "                    G.shop.alignment.offset.y = G.shop.alignment.offset.py\n"
        "                    G.shop.alignment.offset.py = nil\n"
        "                  end"
    )
    new_consume = (
        "                  if G.shop and G.shop.alignment and G.shop.alignment.offset and G.shop.alignment.offset.py then \n"
        "                    G.shop.alignment.offset.y = G.shop.alignment.offset.py\n"
        "                    G.shop.alignment.offset.py = nil\n"
        "                  end"
    )
    content = content.replace(old_consume, new_consume)

    # Force Windowed mode and forbid fullscreen in apply_window_changes
    content = content.replace(
        "{fullscreen = G.SETTINGS.WINDOW.screenmode ~= 'Windowed',",
        "{fullscreen = false,"
    )
    content = content.replace(
        "fullscreentype = (G.SETTINGS.WINDOW.screenmode == 'Borderless' and 'desktop') or (G.SETTINGS.WINDOW.screenmode == 'Fullscreen' and 'exclusive') or nil,",
        "fullscreentype = nil,"
    )
    content = content.replace(
        "G.SETTINGS.WINDOW.screenmode = (G.SETTINGS.QUEUED_CHANGE and G.SETTINGS.QUEUED_CHANGE.screenmode) or G.SETTINGS.WINDOW.screenmode or 'Windowed'",
        "G.SETTINGS.WINDOW.screenmode = 'Windowed'"
    )
    return content


def build_balatro_love(
    exe_path: str | Path,
    output_love: str | Path,
    verbose: bool = True
) -> dict:
    exe_file = Path(exe_path)
    if not exe_file.exists():
        raise FileNotFoundError(f"Balatro.exe not found at: {exe_file}")

    output_path = Path(output_love)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"[*] Extracting Balatro assets from: {exe_file}")

    stats = {
        "files_processed": 0,
        "files_patched": 0,
        "files_injected": 0,
        "uncompressed_bytes": 0,
        "output_size": 0,
        "output_sha256": "",
    }

    # Verify patches exist
    required_patches = [
        "bit.lua",
        "web_patches.lua",
        "decidex_bridge.lua",
        "normalize1.lua",
        "normalize2.lua",
    ]
    for p in required_patches:
        p_path = PATCHES_DIR / p
        if not p_path.exists():
            raise FileNotFoundError(f"Required patch missing: {p_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # 1. Extract files from Balatro.exe (which has game.love zip payload appended)
        with zipfile.ZipFile(exe_file, "r") as z_in:
            for item in z_in.infolist():
                if item.is_dir():
                    continue
                extracted_data = z_in.read(item.filename)
                stats["uncompressed_bytes"] += len(extracted_data)
                stats["files_processed"] += 1

                # 2. Apply patches on targeted files
                if item.filename == "resources/shaders/hologram.fs":
                    text = extracted_data.decode("utf-8", errors="ignore")
                    patched = patch_hologram_shader(text)
                    extracted_data = patched.encode("utf-8")
                    stats["files_patched"] += 1
                elif item.filename == "globals.lua":
                    text = extracted_data.decode("utf-8", errors="ignore")
                    patched = patch_globals(text)
                    extracted_data = patched.encode("utf-8")
                    stats["files_patched"] += 1
                elif item.filename == "main.lua":
                    text = extracted_data.decode("utf-8", errors="ignore")
                    patched = patch_main(text)
                    extracted_data = patched.encode("utf-8")
                    stats["files_patched"] += 1
                elif item.filename == "conf.lua":
                    text = extracted_data.decode("utf-8", errors="ignore")
                    patched = patch_conf(text)
                    extracted_data = patched.encode("utf-8")
                    stats["files_patched"] += 1
                elif item.filename == "functions/common_events.lua":
                    text = extracted_data.decode("utf-8", errors="ignore")
                    patched = patch_common_events(text)
                    extracted_data = patched.encode("utf-8")
                    stats["files_patched"] += 1
                elif item.filename == "game.lua":
                    text = extracted_data.decode("utf-8", errors="ignore")
                    patched = patch_game(text)
                    extracted_data = patched.encode("utf-8")
                    stats["files_patched"] += 1
                elif item.filename == "functions/button_callbacks.lua":
                    text = extracted_data.decode("utf-8", errors="ignore")
                    patched = patch_button_callbacks(text)
                    extracted_data = patched.encode("utf-8")
                    stats["files_patched"] += 1

                target_file = tmp_path / item.filename
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(extracted_data)

        # 3. Inject our runtime patches
        for p in required_patches:
            src = PATCHES_DIR / p
            dst = tmp_path / p
            shutil.copy2(src, dst)
            stats["files_injected"] += 1
            stats["files_processed"] += 1

        # 4. Pack into .love zip archive
        if verbose:
            print(f"[*] Packaging into .love archive: {output_path}")

        sha = hashlib.sha256()
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as z_out:
            for root, _, files in os.walk(tmp_path):
                for f in files:
                    full_p = Path(root) / f
                    arcname = full_p.relative_to(tmp_path).as_posix()
                    z_out.write(full_p, arcname)

    stats["output_size"] = output_path.stat().st_size
    with open(output_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    stats["output_sha256"] = sha.hexdigest()

    if verbose:
        print("[+] Build successful!")
        print(f"    - Output: {output_path} ({stats['output_size'] / (1024*1024):.2f} MB)")
        print(f"    - SHA256: {stats['output_sha256']}")
        print(f"    - Files processed: {stats['files_processed']} (patched: {stats['files_patched']}, injected: {stats['files_injected']})")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Build Balatro for love.js (WebAssembly)")
    parser.add_argument("--exe", default=DEFAULT_EXE, help="Path to Balatro.exe")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output balatro.love path")
    args = parser.parse_args()

    try:
        build_balatro_love(args.exe, args.output)
    except Exception as e:
        print(f"[-] Build failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
