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
    """
    return content.replace("self.F_SOUND_THREAD = true", "self.F_SOUND_THREAD = false")


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
    return content


def patch_conf(content: str) -> str:
    """
    Set t.window.usedpiscale = false for consistent touch/mouse coordinates.
    """
    if "t.window.usedpiscale" not in content:
        content = content.replace(
            "t.window.minheight = 100",
            "t.window.minheight = 100\n    t.window.usedpiscale = false"
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
