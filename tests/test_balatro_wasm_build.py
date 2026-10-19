"""
Unit and regression tests for Balatro WebAssembly (love.js) build pipeline.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BUILD_PATH = ROOT / "tools" / "balatro_wasm" / "build.py"
SERVE_PATH = ROOT / "tools" / "balatro_wasm" / "serve.py"

build_spec = importlib.util.spec_from_file_location("balatro_wasm_build", BUILD_PATH)
build_mod = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(build_mod)

serve_spec = importlib.util.spec_from_file_location("balatro_wasm_serve", SERVE_PATH)
serve_mod = importlib.util.module_from_spec(serve_spec)
serve_spec.loader.exec_module(serve_mod)

patch_hologram_shader = build_mod.patch_hologram_shader
patch_globals = build_mod.patch_globals
patch_main = build_mod.patch_main
patch_conf = build_mod.patch_conf
PATCHES_DIR = build_mod.PATCHES_DIR
DEFAULT_OUTPUT = build_mod.DEFAULT_OUTPUT
check_assets = serve_mod.check_assets
WEB_DIR = serve_mod.WEB_DIR


def test_patches_exist_and_contain_core_symbols():
    bit_lua = PATCHES_DIR / "bit.lua"
    web_patches_lua = PATCHES_DIR / "web_patches.lua"
    bridge_lua = PATCHES_DIR / "decidex_bridge.lua"

    assert bit_lua.exists(), "bit.lua must exist"
    assert web_patches_lua.exists(), "web_patches.lua must exist"
    assert bridge_lua.exists(), "decidex_bridge.lua must exist"

    bit_code = bit_lua.read_text(encoding="utf-8")
    assert "function bit.band" in bit_code
    assert "function bit.bor" in bit_code
    assert "function bit.lshift" in bit_code
    assert "function bit.tobit" in bit_code

    web_code = web_patches_lua.read_text(encoding="utf-8")
    assert 'love.system.getOS = function()' in web_code
    assert 'FakeChannel' in web_code
    assert 'FakeThread' in web_code
    assert 'love.graphics.newImage' in web_code

    bridge_code = bridge_lua.read_text(encoding="utf-8")
    assert '_G.DECIDEX_EXECUTE = decidex_execute' in bridge_code
    assert '[DECIDEX_STATE]' in bridge_code
    assert 'build_state_json' in bridge_code
    assert 'serialize_extra' in bridge_code
    assert 'shop_card_json' in bridge_code
    assert '"deck_count":' in bridge_code
    assert '"discard_count":' in bridge_code
    assert '"joker_slots":' in bridge_code
    assert '"shop":{' in bridge_code
    assert 'buy_card' in bridge_code
    assert 'reroll_shop' in bridge_code
    assert 'toggle_shop' in bridge_code


def test_patch_hologram_shader():
    original = """
    int glow_samples = 4;
    int actual_glow_samples = 0;
    for (int i = -glow_samples; i <= glow_samples; ++i){
        for (int j = -glow_samples; j <= glow_samples; ++j){
            actual_glow_samples += 1;
        }
    }
    """
    patched = patch_hologram_shader(original)
    assert "for (int i = -4; i <= 4; ++i)" in patched
    assert "for (int j = -4; j <= 4; ++j)" in patched
    assert "glow_samples" not in patched.split("for")[1]


def test_patch_globals():
    original = "self.F_SOUND_THREAD = true -- audio thread"
    patched = patch_globals(original)
    assert "self.F_SOUND_THREAD = false" in patched


def test_patch_main_and_idempotency():
    original = 'require "engine/object"\nrequire "bit"\n'
    patched = patch_main(original)
    assert 'require "web_patches"' in patched
    assert 'require "decidex_bridge"' in patched
    assert patched.startswith("-- [[ DecideX WebAssembly Injections ]]")

    # Check idempotency: second call should not re-prepend
    second = patch_main(patched)
    assert second.count('require "web_patches"') == 1


def test_patch_conf():
    original = """
function love.conf(t)
    t.window.minheight = 100
end
    """
    patched = patch_conf(original)
    assert "t.window.usedpiscale = false" in patched


def test_packaged_balatro_love_structure():
    if not DEFAULT_OUTPUT.exists():
        pytest.skip(f"balatro.love does not exist at {DEFAULT_OUTPUT} (requires proprietary assets)")
    assert DEFAULT_OUTPUT.stat().st_size > 50 * 1024 * 1024, "balatro.love should be > 50 MB"

    with zipfile.ZipFile(DEFAULT_OUTPUT, "r") as z:
        namelist = set(z.namelist())

        # Essential entrypoints & patches
        assert "main.lua" in namelist
        assert "conf.lua" in namelist
        assert "globals.lua" in namelist
        assert "bit.lua" in namelist
        assert "web_patches.lua" in namelist
        assert "decidex_bridge.lua" in namelist

        # Shaders
        assert "resources/shaders/hologram.fs" in namelist
        assert "resources/shaders/background.fs" in namelist

        # Verify applied patches inside the zip archive
        main_content = z.read("main.lua").decode("utf-8")
        assert 'require "web_patches"' in main_content
        assert 'require "decidex_bridge"' in main_content

        globals_content = z.read("globals.lua").decode("utf-8")
        assert "self.F_SOUND_THREAD = false" in globals_content

        holo_content = z.read("resources/shaders/hologram.fs").decode("utf-8")
        assert "for (int i = -4; i <= 4; ++i)" in holo_content
        assert "for (int j = -4; j <= 4; ++j)" in holo_content

        conf_content = z.read("conf.lua").decode("utf-8")
        assert "t.window.usedpiscale = false" in conf_content


def test_web_assets_integrity():
    assert check_assets(WEB_DIR) is True


def test_cdn_cache_aliases_and_mimetypes():
    from tools.balatro_web.app import Handler
    assert Handler._guess_wasm_ctype("love.wasm.bin") == "application/wasm"
    assert Handler._guess_wasm_ctype("balatro.love.zip") == "application/octet-stream"
    assert Handler._guess_wasm_ctype("balatro.bin") == "application/octet-stream"
    assert Handler._guess_wasm_ctype("style.css") == "text/css; charset=utf-8"
    assert Handler._guess_wasm_ctype("app.js") == "application/javascript; charset=utf-8"


def test_path_traversal_security_defense():
    from tools.balatro_web.app import Handler
    # Legitimate assets should resolve properly
    legit_file, ctype = Handler._resolve_wasm_file("app.js")
    assert legit_file is not None and legit_file.name == "app.js"
    assert "javascript" in ctype

    legit_love, ctype_love = Handler._resolve_wasm_file("balatro.love.zip")
    assert legit_love is not None and legit_love.name == "balatro.love"
    assert ctype_love == "application/octet-stream"

    # Malicious traversal attempts must be strictly rejected (returning None, "")
    traversal_payloads = [
        "../app.py",
        "../../pyproject.toml",
        "sub/../../../../pyproject.toml",
        "foo/../../../../etc/passwd",
        "/etc/passwd",
        ".env",
        ".git/config",
        "sub/../../../data"
    ]
    for payload in traversal_payloads:
        target, ctype = Handler._resolve_wasm_file(payload)
        assert target is None, f"Payload '{payload}' must be rejected by path traversal defense"
        assert ctype == ""


def test_lua_bridge_enhanced_state_signature_and_guards():
    bridge_lua = PATCHES_DIR / "decidex_bridge.lua"
    content = bridge_lua.read_text(encoding="utf-8")

    # State signature enhancements for cards, jokers, and consumables
    assert 'local enh = (c.ability and c.ability.name) or ""' in content
    assert 'local ed = (c.edition and type(c.edition) == "table"' in content
    assert 'local seal = tostring(c.seal or "")' in content
    assert 'sig_parts[#sig_parts + 1] = "J"' in content
    assert 'sig_parts[#sig_parts + 1] = "C"' in content

    # State guards in decidex_execute
    assert 'if G.STATES and G.STATE ~= G.STATES.SHOP then' in content
    assert 'if G.STATES and G.STATE ~= G.STATES.ROUND_EVAL and G.STATE ~= G.STATES.HAND_PLAYED then' in content


def test_dynamic_blind_selection_and_readiness_flags():
    bridge_lua = PATCHES_DIR / "decidex_bridge.lua"
    content = bridge_lua.read_text(encoding="utf-8")

    # Dynamic on-deck blind resolution
    assert 'local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"' in content
    assert 'G.blind_select_opts and G.blind_select_opts[string.lower(on_deck)]' in content

    # UI Action readiness flags
    assert 'local function find_uie_by_id(id)' in content
    assert '"can_cash_out":' in content
    assert '"can_toggle_shop":' in content
    assert '"can_select_blind":' in content
    assert '"won":' in content


def test_frontend_arbitration_and_autopilot():
    import re
    app_js = WEB_DIR / "app.js"
    js_content = app_js.read_text(encoding="utf-8")

    assert "arbitrateAction" in js_content
    assert "AutoPilot" in js_content
    assert re.search(r"(?:decidex-balatro-core-|balatro-asset-cache-)v\d+", js_content) is not None
    assert "executePlayAction" in js_content
    assert "executeDiscardAction" in js_content

    index_html = WEB_DIR / "index.html"
    html_content = index_html.read_text(encoding="utf-8")
    assert "autopilot-toggle" in html_content
    assert re.search(r"app\.js\?v=\d+", html_content) is not None


def test_coordinate_mapping_patches():
    # Verify love.js letterbox/pillarbox coordinate patch
    love_js = WEB_DIR / "love.js"
    love_content = love_js.read_text(encoding="utf-8")
    assert "__calculateTargetCoords" in love_content
    assert "coords.scaleX" in love_content
    assert "coords.scaleY" in love_content

    # Verify balatro_web index.html coordinate mapping fix
    web_index = Path(__file__).resolve().parents[1] / "tools" / "balatro_web" / "index.html"
    web_content = web_index.read_text(encoding="utf-8")
    assert "Math.floor(clampedX * GAME_W / renderW)" in web_content
    assert "Math.floor(clampedY * GAME_H / renderH)" in web_content



