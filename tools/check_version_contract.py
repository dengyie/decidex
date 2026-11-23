#!/usr/bin/env python3
"""Cross-check the DecideX web version contract.

The version triple lives in several files that must agree exactly:

* ``tools/balatro_wasm/web/app.js``      — ASSET_VERSION, EXPECTED_LOVE_SIZE,
  EXPECTED_WASM_SIZE, ``?v=N`` asset URLs
* ``tools/balatro_wasm/web/index.html``  — ``app.js?v=N`` / ``love.js?v=N``
* ``tools/balatro_wasm/release-assets.json`` — decidex_version + per-asset
  sha256/min_size of what is actually on disk
* ``tools/balatro_wasm/web/version.json`` — the build-time record of the
  last build (version/love_size/love_sha256)

Any drift silently shipped a stale asset to production once (a bumped
version with a stale EXPECTED_LOVE_SIZE made every client purge and
re-download forever). This script fails loudly instead; pytest and the CI
test job both call it.

Usage:  python tools/check_version_contract.py [--web-dir PATH]
Exit 0 = consistent, exit 1 = mismatches found (each printed with details).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEB_DIR = ROOT / "tools" / "balatro_wasm" / "web"
MANIFEST_PATH = ROOT / "tools" / "balatro_wasm" / "release-assets.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def check(web_dir: Path, manifest_path: Path) -> list[str]:
    problems: list[str] = []

    app_js_path = web_dir / "app.js"
    index_path = web_dir / "index.html"
    version_path = web_dir / "version.json"

    # --- app.js ----------------------------------------------------------
    app_js = app_js_path.read_text(encoding="utf-8")

    def pick(pattern: str, label: str) -> str | None:
        m = re.search(pattern, app_js)
        if not m:
            problems.append(f"app.js: missing {label}")
            return None
        return m.group(1)

    asset_version = pick(r'const ASSET_VERSION = "(v\d+)";', "ASSET_VERSION")
    love_size = pick(r"const EXPECTED_LOVE_SIZE = (\d+);", "EXPECTED_LOVE_SIZE")
    wasm_size = pick(r"const EXPECTED_WASM_SIZE = (\d+);", "EXPECTED_WASM_SIZE")
    love_url = pick(r'const ASSET_LOVE_URL = "balatro\.love\.zip\?v=(\d+)";', "ASSET_LOVE_URL")
    wasm_url = pick(r'const ASSET_WASM_URL = "love\.wasm\.bin\?v=(\d+)";', "ASSET_WASM_URL")
    love_js_v = pick(r'love\.js\?v=(\d+)', "love.js?v=")

    if asset_version and love_url and asset_version.lstrip("v") != love_url:
        problems.append(f"app.js: ASSET_VERSION {asset_version} != ASSET_LOVE_URL ?v={love_url}")
    if asset_version and wasm_url and asset_version.lstrip("v") != wasm_url:
        problems.append(f"app.js: ASSET_VERSION {asset_version} != ASSET_WASM_URL ?v={wasm_url}")
    if asset_version and love_js_v and asset_version.lstrip("v") != love_js_v:
        problems.append(f"app.js: ASSET_VERSION {asset_version} != love.js?v={love_js_v}")
    if not (love_size and wasm_size):
        problems.append("app.js: EXPECTED_*_SIZE constants missing")
        love_size = wasm_size = None

    # --- index.html ------------------------------------------------------
    if index_path.exists():
        html = index_path.read_text(encoding="utf-8")
        if asset_version:
            num = asset_version.lstrip("v")
            # Exact token match: a plain `needle not in html` false-passes
            # when html is a prefix-extension of app.js (v16 vs ?v=165).
            for asset in ("app.js", "love.js"):
                if not re.search(rf"{re.escape(asset)}\?v={num}(?!\d)", html):
                    problems.append(f"index.html: missing {asset}?v={num} (exact)")
        else:
            problems.append("index.html: cannot check ?v= without app.js ASSET_VERSION")
    else:
        problems.append(f"index.html not found: {index_path}")

    # --- release-assets.json --------------------------------------------
    manifest = None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"release-assets.json unreadable: {exc}")

    # CI checkouts carry no proprietary binaries. balatro.love is a build
    # product that lives outside Git (53.7MB); love.wasm IS tracked. So a
    # bare checkout always has love.wasm but never balatro.love — the
    # old "any sibling present means the tree is complete" rule flagged
    # every CI run as a broken deploy tree. Each artifact's asset-level
    # (size/sha) checks therefore run INDEPENDENTLY: whichever file exists
    # gets verified against its manifest entry, and absence of the
    # untracked balatro.love alone is not an error.
    love_path = web_dir / "balatro.love"
    wasm_path = web_dir / "love.wasm"
    artifacts_present = love_path.is_file() and wasm_path.is_file()
    any_artifact_present = love_path.is_file() or wasm_path.is_file()

    if manifest is not None:
        declared_version = manifest.get("decidex_version")
        if not declared_version:
            problems.append("release-assets.json: missing decidex_version "
                            "(re-run the build to write it)")
        elif asset_version and declared_version != asset_version:
            problems.append(f"release-assets.json: decidex_version {declared_version} "
                            f"!= app.js ASSET_VERSION {asset_version}")

        assets = {e.get("filename"): e for e in manifest.get("assets", [])
                  if isinstance(e, dict)}
        for filename in ("balatro.love", "love.wasm"):
            entry = assets.get(filename)
            if not entry:
                problems.append(f"release-assets.json: missing asset {filename}")
                continue
            path = web_dir / filename
            if not path.is_file():
                continue  # untracked build product absent (e.g. CI checkout): per-file checks below run for whatever IS present
            actual_size = path.stat().st_size
            min_size = entry.get("min_size")
            if not isinstance(min_size, int) or min_size <= 0:
                problems.append(f"release-assets.json: {filename} min_size invalid: {min_size!r}")
            elif actual_size != min_size:
                problems.append(f"release-assets.json: {filename} min_size {min_size} != "
                                f"on-disk bytes {actual_size} (re-run the build)")
            expected_sha = entry.get("sha256")
            if not expected_sha:
                problems.append(f"release-assets.json: {filename} missing sha256")
            else:
                actual_sha = _sha256(path)
                if actual_sha != expected_sha.lower():
                    problems.append(f"release-assets.json: {filename} sha256 mismatch "
                                    f"(manifest {expected_sha[:16]}… != disk {actual_sha[:16]}…)")

    # --- web/version.json -------------------------------------------------
    if not version_path.exists():
        if not any_artifact_present:
            print("[i] artifact-less checkout: skipping web/version.json check")
        else:
            problems.append(f"web/version.json not found (re-run the build): {version_path}")
    else:
        try:
            vinfo = json.loads(version_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"web/version.json unreadable: {exc}")
            vinfo = None
        if vinfo:
            vver = vinfo.get("version")
            if asset_version and vver != asset_version:
                problems.append(f"web/version.json: version {vver} != app.js {asset_version}")
            love_path = web_dir / "balatro.love"
            if love_path.is_file():
                disk_love = love_path.stat().st_size
                declared_love = vinfo.get("love_size")
                if not isinstance(declared_love, int) or declared_love != disk_love:
                    problems.append(f"web/version.json: love_size {declared_love} != "
                                    f"on-disk {disk_love}")
                if love_size and declared_love and int(love_size) != declared_love:
                    problems.append(f"web/version.json: love_size {declared_love} != "
                                    f"app.js EXPECTED_LOVE_SIZE {love_size}")
            declared_sha = vinfo.get("love_sha256")
            if love_path.is_file() and declared_sha:
                disk_sha = _sha256(love_path)
                if disk_sha != declared_sha.lower():
                    problems.append(f"web/version.json: love_sha256 mismatch "
                                    f"({declared_sha[:16]}… != disk {disk_sha[:16]}…)")
            elif love_path.is_file() and not declared_sha:
                problems.append("web/version.json: missing love_sha256")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-dir", type=Path, default=DEFAULT_WEB_DIR,
                        help="web asset directory (default: tools/balatro_wasm/web)")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH,
                        help="release-assets.json path")
    args = parser.parse_args()

    problems = check(args.web_dir, args.manifest)
    if problems:
        print(f"[-] Version contract violated ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("[+] Version contract OK: app.js / index.html / release-assets.json / "
          "version.json all agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
