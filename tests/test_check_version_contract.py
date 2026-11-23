"""Smoke tests for tools/check_version_contract.py.

The validator is the last line of defense against version drift (a bumped
ASSET_VERSION with a stale EXPECTED_LOVE_SIZE once made every client purge
and re-download 53.8MB forever). CI runs it as a test-job step; these tests
pin its two critical behaviors directly:

* the current repo passes (catches contract rot the moment it happens),
* a real drift actually FAILS — including the substring false-pass class
  (`app.js?v=16` is a substring of `?v=165`) that the first implementation
  shipped with.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "tools" / "balatro_wasm" / "web"
MANIFEST = ROOT / "tools" / "balatro_wasm" / "release-assets.json"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_version_contract", ROOT / "tools" / "check_version_contract.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_current_repo_passes():
    mod = _load_module()
    assert mod.check(WEB_DIR, MANIFEST) == []


def test_app_js_manifest_version_drift_fails(tmp_path):
    """Rewrite app.js to a different ASSET_VERSION than the manifest."""
    web = tmp_path / "web"
    web.mkdir()
    import re as _re
    for name in ("app.js", "index.html"):
        src = (WEB_DIR / name).read_text(encoding="utf-8")
        src = _re.sub(r'ASSET_VERSION = "v\d+"', 'ASSET_VERSION = "v999"', src)
        (web / name).write_text(src, encoding="utf-8")
    mod = _load_module()
    problems = mod.check(web, MANIFEST)
    assert any("ASSET_VERSION v999" in p for p in problems), problems


def test_index_html_prefix_extension_drift_fails(tmp_path):
    """index.html at ?v=165 while app.js says v16 must NOT pass.

    The original substring containment (`needle not in html`) false-passed
    here because "app.js?v=16" is a substring of "app.js?v=165".
    """
    web = tmp_path / "web"
    web.mkdir()
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    app_js = app_js.replace('ASSET_VERSION = "v165"', 'ASSET_VERSION = "v16"')
    (web / "app.js").write_text(app_js, encoding="utf-8")
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # Simulate the drift: only the html was bumped to a prefix-extended value.
    html = re.sub(r"app\.js\?v=\d+", "app.js?v=165", html)
    (web / "index.html").write_text(html, encoding="utf-8")
    mod = _load_module()
    problems = mod.check(web, MANIFEST)
    assert any("app.js?v=16" in p for p in problems), problems
