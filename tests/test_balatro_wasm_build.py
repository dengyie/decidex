"""
Unit and regression tests for Balatro WebAssembly (love.js) build pipeline.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
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
verify_release_assets = serve_mod.verify_release_assets
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


def test_web_asset_check_excludes_proprietary_binaries(tmp_path):
    for name in ("index.html", "style.css", "app.js", "viz.js", "viz.css", "love.js"):
        (tmp_path / name).write_bytes(b"x" * 200000)
    assert check_assets(tmp_path) is True


def test_release_asset_check_requires_manifest_and_external_assets(tmp_path):
    manifest = tmp_path / "release-assets.json"
    manifest.write_text("{}", encoding="utf-8")
    assert verify_release_assets(tmp_path, manifest) is False


def test_release_asset_check_validates_hash_and_source(tmp_path):
    import hashlib
    import json

    love = tmp_path / "balatro.love"
    wasm = tmp_path / "love.wasm"
    love.write_bytes(b"love-release")
    wasm.write_bytes(b"wasm-release")
    manifest = tmp_path / "release-assets.json"
    manifest.write_text(json.dumps({
        "version": "test-1",
        "source": "controlled artifact store",
        "assets": [
            {"filename": "balatro.love", "source": "artifact://love", "min_size": 1,
             "sha256": hashlib.sha256(love.read_bytes()).hexdigest()},
            {"filename": "love.wasm", "source": "artifact://wasm", "min_size": 1,
             "sha256": hashlib.sha256(wasm.read_bytes()).hexdigest()},
        ],
    }), encoding="utf-8")
    assert verify_release_assets(tmp_path, manifest) is True
    wasm.write_bytes(b"tampered")
    assert verify_release_assets(tmp_path, manifest) is False


def test_server_refuses_to_start_when_controlled_assets_are_missing(monkeypatch):
    monkeypatch.setattr(serve_mod, "check_assets", lambda: False)
    monkeypatch.setattr(serve_mod.socketserver, "TCPServer",
                        lambda *args, **kwargs: pytest.fail("server must not start"))
    assert serve_mod.run_server() == 1


def test_static_asset_range_responses(tmp_path, monkeypatch):
    from tools.balatro_web.app import Handler

    target = tmp_path / "bundle.love"
    target.write_bytes(b"0123456789")
    monkeypatch.setattr("tools.balatro_web.app.WASM_WEB_DIR", tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = urllib.request.Request(base + "/bundle.love", headers={"Range": "bytes=3-6"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 3-6/10"
            assert response.read() == b"3456"

        request = urllib.request.Request(base + "/bundle.love", headers={"Range": "bytes=20-"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 416
        assert error.value.headers["Content-Range"] == "bytes */10"
        assert error.value.headers["Content-Length"] == "0"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_wasm_server_serves_byte_ranges(tmp_path, monkeypatch):
    (tmp_path / "game.love").write_bytes(b"0123456789")
    monkeypatch.setattr(serve_mod, "WEB_DIR", tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve_mod.WasmHTTPRequestHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = urllib.request.Request(base + "/game.love", headers={"Range": "bytes=4-7"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 4-7/10"
            assert response.read() == b"4567"

        request = urllib.request.Request(base + "/game.love", headers={"Range": "bytes=-3"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 206
            assert response.read() == b"789"

        request = urllib.request.Request(base + "/game.love", headers={"Range": "bytes=99-"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 416
        assert error.value.headers["Content-Range"] == "bytes */10"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_frame_response_body_matches_etag(tmp_path, monkeypatch):
    from tools.balatro_web import app as web_app

    frame_path = tmp_path / "frame.jpg"
    frame_data = b"frame-bytes"
    frame_path.write_bytes(frame_data)
    monkeypatch.setattr(web_app, "FRAME_PATH", frame_path)
    monkeypatch.setattr(web_app, "_FRAME_ETAG", "")
    server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/api/frame.jpg", timeout=5) as response:
            body = response.read()
            assert body == frame_data
            assert response.headers["ETag"] == f'"{__import__("hashlib").sha1(body).hexdigest()[:16]}"'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_web_credentials_are_unset_without_environment(monkeypatch):
    monkeypatch.delenv("BALATRO_WEB_USER", raising=False)
    monkeypatch.delenv("BALATRO_WEB_PASSWORD", raising=False)
    app_path = ROOT / "tools" / "balatro_web" / "app.py"
    spec = importlib.util.spec_from_file_location("balatro_web_app_credentials", app_path)
    web_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web_app)
    assert web_app.WEB_USER == ""
    assert web_app.WEB_PASSWORD == ""
    assert not web_app._check_credentials("", "")


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
    assert 'shop_items_sig' in content

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
    assert 'played_hands' in content
    assert 'most_played_hand' in content
    assert 'DECIDEX_SEQ' in content
    assert 'write_action_ack' in content
    assert 'played_this_round' in content


def test_frontend_arbitration_and_autopilot():
    import re
    app_js = WEB_DIR / "app.js"
    js_content = app_js.read_text(encoding="utf-8")

    assert "arbitrateAction" in js_content
    assert "AutoPilot" in js_content
    assert re.search(r"(?:decidex-balatro-core-|balatro-asset-cache-)v\d+", js_content) is not None
    assert "executePlayAction" in js_content
    assert "executeDiscardAction" in js_content

    # Mailbox failures must reach AutoPilot instead of leaving an in-flight
    # action invisible until the watchdog fires.
    assert "onActionDispatchFailure" in js_content
    assert "Lua action ${label} #${sequence} failed" in js_content
    assert "return dispatchLuaAction(luaCode, \"play\")" in js_content
    assert "return dispatchLuaAction(luaCode, \"discard\")" in js_content
    assert "this._actionFailureStreak" in js_content
    assert "动作连续失败，自动托管已暂停" in js_content

    # IDBFS initialization must observe love.js's single runtime sync rather
    # than depending on unavailable Emscripten dependency hooks or racing it.
    assert "FS._decidex_syncfs_wrapped" in js_content
    assert "FS.syncfs timed out after ${timeoutMs}ms" in js_content
    assert "window.Module.addRunDependency" not in js_content
    assert "non-fatal" not in js_content
    assert "if (isLoveRunning || initialSyncState !== \"ready\") return;" in js_content
    assert "preRun: [setupFileSystem]" in js_content
    assert "postRun: [onInitSuccess]" in js_content
    version_match = re.search(r'const ASSET_VERSION = "v(\d+)";', js_content)
    assert version_match is not None, "ASSET_VERSION constant not found in app.js"
    asset_num = version_match.group(1)
    assert f'balatro.love.zip?v={asset_num}' in js_content
    assert f'love.wasm.bin?v={asset_num}' in js_content

    index_html = WEB_DIR / "index.html"
    html_content = index_html.read_text(encoding="utf-8")
    assert "autopilot-toggle" in html_content
    assert f"app.js?v={asset_num}" in html_content
    assert f"love.js?v={asset_num}" in html_content


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



