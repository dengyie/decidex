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
    original = (
        'require "engine/object"\n'
        'require "bit"\n'
        '\n'
        'function love.update( dt )\n'
        '\t--Perf monitoring checkpoint\n'
        '    timer_checkpoint(nil, \'update\', true)\n'
        '    G:update(dt)\n'
        'end\n'
    )
    patched = patch_main(original)
    assert 'require "web_patches"' in patched
    assert 'require "decidex_bridge"' in patched
    assert patched.startswith("-- [[ DecideX WebAssembly Injections ]]")

    # Check idempotency: second call should not re-prepend
    second = patch_main(patched)
    assert second.count('require "web_patches"') == 1
    assert second.count("DECIDEX_STEP") == patched.count("DECIDEX_STEP")


def test_patch_main_places_decidex_hook_after_game_update():
    """The bridge must tick AFTER G:update(dt), never before.

    Running the bridge first makes every emitted snapshot one frame stale:
    the bridge reads G.STATE before the game advances it, so asynchronous
    transitions are reported late or missed. The client then acts on a stale
    snapshot and the bridge rejects the action ("not in BLIND_SELECT"), which
    used to burn the 12-strike budget and auto-pause the whole run.
    """
    original = (
        'require "engine/object"\n'
        '\n'
        'function love.update( dt )\n'
        '    G:update(dt)\n'
        'end\n'
        '\n'
        'function love.draw()\n'
        '    G:draw()\n'
        'end\n'
    )
    patched = patch_main(original)
    update_idx = patched.index("function love.update( dt )")
    game_update_idx = patched.index("pcall(G.update, G, dt)", update_idx)
    hook_idx = patched.index("_G.DECIDEX_STEP(dt)", update_idx)
    assert game_update_idx < hook_idx, "DECIDEX_STEP hook must run after G:update(dt)"

    # G:update must be pcall-wrapped: love.run's outer xpcall prints
    # "[BALATRO FRAME ERROR]" and continues, but it unwinds straight past
    # the step hook, so a persistent error inside G:update (crashed event
    # cleanup, half-removed Moveable) used to starve the bridge forever
    # while the game kept erroring every frame.
    assert "local _dx_update_ok, _dx_update_err = pcall(G.update, G, dt)" in patched
    suppress_idx = patched.index("G:update error #%d suppressed", update_idx)
    assert hook_idx < suppress_idx, "the hook must run before the error report"

    # The hook must live inside love.update, not after love.draw.
    draw_idx = patched.index("function love.draw()")
    assert hook_idx < draw_idx


def test_patch_main_hook_tolerates_indentation_styles():
    """The upstream main.lua indents G:update(dt) with spaces.

    A literal tab-anchored needle silently matches nothing, which leaves the
    bridge with no game-loop hook at all and the game unplayable. The patch
    must accept both spaces and tabs.
    """
    for indent in ("    ", "\t", "  "):
        original = (
            "function love.update( dt )\n"
            f"{indent}G:update(dt)\n"
            "end\n"
        )
        patched = patch_main(original)
        assert patched.count("_G.DECIDEX_STEP(dt)") == 1, (
            f"hook not injected for indent {indent!r}"
        )
        assert patched.count("pcall(G.update, G, dt)") == 1, (
            f"G:update not pcall-wrapped for indent {indent!r}"
        )
        assert patched.index("pcall(G.update, G, dt)") < patched.index("_G.DECIDEX_STEP(dt)")


def test_patch_main_requires_update_hook_in_build_path():
    """The real build must fail loudly rather than ship a bridge with no hook."""
    with pytest.raises(RuntimeError, match="love.update"):
        patch_main('require "engine/object"\n')

    # Unit-test fixtures may opt out explicitly.
    assert patch_main('require "engine/object"\n', require_update_hook=False) is not None


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

    # The zip URL aliases to the raw love; assert only where the
    # externally provisioned asset exists (CI checks out git files only).
    legit_love, ctype_love = Handler._resolve_wasm_file("balatro.love.zip")
    if DEFAULT_OUTPUT.exists():
        assert legit_love is not None and legit_love.name == "balatro.love"
        assert ctype_love == "application/octet-stream"
    else:
        assert legit_love is None and ctype_love == ""


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
    # The pause message must carry the underlying error, not a generic label.
    assert "动作连续失败" in js_content
    assert "动作连续失败，自动托管已暂停" not in js_content

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

    # The old balatro_web VNC-console page (with the click coordinate
    # mapping) was removed when /stream became the web-game live view;
    # the love.js letterbox patch above is what /play actually uses.





def test_bridge_copies_are_in_sync():
    """The three DecideX bridge copies must be byte-identical.

    Drift between them silently shipped a stale bridge to production once;
    the build now refuses to run when they diverge.
    """
    build_mod.check_bridge_copies_in_sync()


def test_bridge_copies_drift_is_detected(tmp_path, monkeypatch):
    """A diverged copy must raise instead of building a stale .love."""
    canonical = build_mod.BRIDGE_COPIES[0]
    stale = tmp_path / "decidex_bridge.lua"
    stale.write_bytes(canonical.read_bytes() + b"\n-- drift\n")
    monkeypatch.setattr(build_mod, "BRIDGE_COPIES", [canonical, stale])

    with pytest.raises(RuntimeError, match="drifted apart"):
        build_mod.check_bridge_copies_in_sync()


def test_skip_blind_never_uses_synthetic_button():
    """skip_blind must pass the real UIElement to G.FUNCS.skip_blind.

    The game's skip_blind does `e.UIBox:get_UIE_by_ID('tag_container')`, so a
    synthetic table crashes with "attempt to index field 'UIBox' (a nil value)".
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    # The synthetic fallback that caused the crash must be gone.
    assert "id = 'skip_blind_button'" not in content
    assert "ref_table = tag_obj or opt" not in content

    # The real node is resolved by its tag_<blind> id and validated against
    # the owning UIBox before it reaches the game callback.
    assert "opt:get_UIE_by_ID('tag_' .. on_deck)" in content
    assert "skip_btn.UIBox:get_UIE_by_ID('tag_container')" in content
    assert "if not (skip_btn.UIBox and skip_btn.UIBox.get_UIE_by_ID) then" in content


def test_skip_blind_releases_in_flight_latch():
    """The blind-action latch must never stay latched after a skip.

    _G._select_blind_in_flight was set to true and never cleared, and the
    4s reset was gated on _G._last_select_blind_time which the skip path
    never set - so can_select_blind/can_skip_blind stayed false forever.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    skip_block = content.split('if action == "skip_blind" then', 1)[1]
    skip_block = skip_block.split('if action == "buy_card"', 1)[0]

    assert "_G._last_select_blind_time =" in skip_block
    assert skip_block.count("_G._select_blind_in_flight = nil") >= 2


def test_booster_purchase_uses_use_card_without_double_charge():
    """Booster packs must be opened through use_card, not buy_from_shop.

    buy_from_shop refuses boosters via check_for_buy_space() and its
    fallback emplaces the pack into G.jokers instead of opening it. Charging
    in the bridge as well double-charged the player because Card:open()
    already calls ease_dollars(-cost).
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    booster_block = content.split("Handle Booster pack purchase and opening", 1)[1]
    booster_block = booster_block.split("Handle Voucher purchase", 1)[0]
    # Strip comments so the explanatory note about buy_from_shop does not
    # trip the assertion.
    booster_code = "\n".join(
        ln for ln in booster_block.split("\n") if not ln.strip().startswith("--")
    )

    assert "buy_from_shop" not in booster_code
    assert "ease_dollars(-c.cost)" not in booster_code
    assert "G.FUNCS.use_card" in booster_code


def test_reroll_shop_respects_affordability_gate():
    """reroll_shop must not bypass can_reroll's affordability check."""
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    reroll_block = content.split('if action == "reroll_shop" then', 1)[1]
    reroll_block = reroll_block.split('if action == "toggle_shop" then', 1)[0]

    assert "cannot afford reroll" in reroll_block
    assert "G.GAME.current_round.reroll_cost" in reroll_block
    assert "find_button_by_button_name(G.shop, 'reroll_shop')" in reroll_block


def test_find_uie_by_id_searches_booster_pack():
    """The booster pack overlay must be searchable by id."""
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    find_block = content.split("local function find_uie_by_id(id)", 1)[1]
    find_block = find_block.split("end", 1)[0] if "end" in find_block else find_block
    assert "G.booster_pack" in content.split("local function find_uie_by_id(id)", 1)[1][:1200]


def test_state_exposes_blind_on_deck():
    """The client needs blind_on_deck to detect a successful skip."""
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")
    assert '"blind_on_deck":' in content


def test_autopilot_surfaces_failure_reason():
    """The HUD must show the underlying Lua error instead of a generic label.

    toggle(false) used to overwrite the badge with "已暂停", hiding the
    reason the automation stopped.
    """
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "preserveStatus" in js_content
    assert "this._lastFailureReason" in js_content
    # A hard failure must not refresh the anti-hang watchdog timestamp:
    # doing so keeps the watchdog permanently satisfied while nothing
    # progresses.
    failure_block = js_content.split("    onActionDispatchFailure(action, error, sequence) {", 1)[1]
    failure_block = failure_block.split("    onStateUpdated(state) {", 1)[0]
    # The transient branch (state mismatch / UI transition) returns early and
    # legitimately refreshes the watchdog, because a rejected action proves
    # the game is still moving. Only the hard-failure tail must not.
    hard_failure_block = failure_block.split(
        "this._actionFailureStreak = (this._actionFailureStreak || 0) + 1;", 1
    )[-1]
    hard_failure_code = "\n".join(
        ln for ln in hard_failure_block.split("\n") if not ln.strip().startswith("//")
    )
    assert "_lastProgressTimestamp = Date.now()" not in hard_failure_code


def test_autopilot_treats_state_mismatch_as_transient():
    """A stale snapshot must not burn the 12-strike auto-pause budget.

    The client acts on the last snapshot it received; if the game has already
    moved on, the bridge rejects the action with "not in BLIND_SELECT" (or
    another state guard). That is a transient desync, not a broken action.
    Counting it as a hard failure used to trip the 12-strike limit and halt
    the whole run.
    """
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "isStateMismatch" in js_content
    assert "not in (BLIND_SELECT|SHOP|ROUND_EVAL" in js_content
    # The mismatch path must reset the failure streak instead of incrementing it.
    mismatch_block = js_content.split("if (isStateMismatch) {", 1)[1]
    mismatch_block = mismatch_block.split("} else {", 1)[0]
    assert "_actionFailureStreak = 0" in mismatch_block



def test_bridge_dismisses_modal_overlay_that_pauses_the_game():
    """A modal overlay must never freeze an automated run.

    create_unlock_overlay() sets G.SETTINGS.paused = true and opens
    G.OVERLAY_MENU. A paused game gets dt = 0, which freezes G.TIMERS.TOTAL,
    so every pending 'after' event stops firing; Event:handle also returns
    'pause_skip' for events created while unpaused, so the 'immediate' event
    that builds the booster-pack UIBox never runs either.

    The bridge used to deliberately leave the game paused whenever
    G.OVERLAY_MENU existed ("not G.OVERLAY_MENU"), and the autopilot never
    clicked the overlay's continue button -- so an unlock popup appearing
    mid-pack wedged the run permanently.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "local function dismiss_blocking_overlay()" in content
    # The overlay is dismissed through the game's own path, which also
    # unpauses and force-flushes the event queue.
    assert "G.FUNCS.continue_unlock" in content
    assert "G.FUNCS.exit_overlay_menu" in content

    # An overlay raised during a run has to be dismissed, not left on screen.
    assert "G.SETTINGS.paused and G.OVERLAY_MENU and not G.OVERLAY_TUTORIAL" in content
    overlay_branch = content.split(
        "G.SETTINGS.paused and G.OVERLAY_MENU and not G.OVERLAY_TUTORIAL", 1
    )[1]
    overlay_branch = overlay_branch.split("end", 1)[0]
    assert "dismiss_blocking_overlay()" in overlay_branch

    # Dismissing must be scoped to active-run states, so the game-over screen
    # and the run-setup menu are left to the autopilot.
    assert "local function is_active_run_state()" in content
    active = content.split("local function is_active_run_state()", 1)[1]
    active = active.split("local function dismiss_blocking_overlay()", 1)[0]
    for state in ("SELECTING_HAND", "SHOP", "BLIND_SELECT", "ROUND_EVAL", "BUFFOON_PACK"):
        assert state in active
    for state in ("GAME_OVER", "MENU", "SPLASH"):
        assert f"S.{state}" not in active


def test_bridge_has_booster_pack_stall_watchdog():
    """A pack whose UI never appeared must not hang the run forever.

    Game:update_<pack> creates the pack UI exactly once, behind an
    'immediate' event, and only while G.STATE_COMPLETE is false. If that
    event never runs, G.booster_pack and G.pack_cards stay nil while
    G.STATE stays on the pack state -- the autopilot then loops on
    "waiting for pack cards" with no way out.

    The watchdog rebuilds the pack UI directly (bypassing the event queue)
    and, failing that, abandons the pack so the run keeps advancing.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "local function pack_stall_watchdog()" in content
    assert "local function rebuild_pack_ui()" in content
    assert "local function abandon_pack()" in content

    # Every pack state must be covered by the rebuild path.
    rebuild = content.split("local function rebuild_pack_ui()", 1)[1].split("\nend", 1)[0]
    for factory in (
        "create_UIBox_arcana_pack",
        "create_UIBox_celestial_pack",
        "create_UIBox_spectral_pack",
        "create_UIBox_standard_pack",
        "create_UIBox_buffoon_pack",
    ):
        assert factory in rebuild

    # The watchdog must run from the game loop.
    assert "pcall(pack_stall_watchdog)" in content

    # The recovery must be reachable from the action channel too.
    assert 'action == "force_leave_pack"' in content


def test_autopilot_escapes_a_wedged_booster_pack():
    """The AutoPilot must have an escape hatch for a pack that never opened."""
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "window.executeForceLeavePack" in js_content
    assert 'fn("force_leave_pack")' in js_content

    # The empty-pack branch must escalate to the escape hatch instead of
    # waiting forever when the pack cannot be skipped.
    pack_branch = js_content.split('this.updateStatus("等待补充包卡牌生成..."', 1)[1]
    assert "executeForceLeavePack()" in pack_branch
    # The escape hatch must not fire before the pack had a fair chance.
    assert "timeInPack > 20000" in pack_branch


def test_bridge_latch_hygiene_resets_per_episode():
    """Cross-episode stale latches must reset on state exit, not on re-use.

    Two latch-discipline bugs of the same family: _facing_blind_since only
    reset inside the select gate's guard (so the timestamp survived into the
    NEXT blind select and immediately cleared a legitimately fresh flag,
    double-selecting it), and _draw_hand_full_time never reset when the
    state left DRAW_TO_HAND.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    # The facing_blind timestamp must reset outside BLIND_SELECT, from the
    # maintenance path (which runs every frame regardless of dispatches).
    reset_block = content.split(
        "-- Latch hygiene: _facing_blind_since is only meaningful inside", 1)[1].split(chr(10) + "end", 1)[0]
    assert "if G.STATE ~= G.STATES.BLIND_SELECT then" in reset_block
    assert "_G._facing_blind_since = nil" in reset_block

    # The hand-full latch must reset when DRAW_TO_HAND is left.
    else_branch = content.split(chr(10) + "  else" + chr(10) + "    _G._draw_to_hand_enter_time = nil", 1)[1].split(chr(10) + "  end", 1)[0]
    assert "_G._draw_hand_full_time = nil" in else_branch


def test_bridge_guards_can_skip_booster_and_pack_ui_invariants():
    """The can_skip_booster storm and the rebuild/late-factory loop.

    The pack UI polls can_skip_booster every frame; CardArea:remove() sets
    .cards = nil, so a stale G.pack_cards global made the vanilla predicate
    (button_callbacks.lua:2118) error 3600 frames in a row (~1 minute of
    frozen pack UI). And a queued vanilla factory event firing after the
    watchdog rebuilt a pack replaces the healthy UI with a fresh EMPTY
    area (the card-creation events already fired), re-wedging the pack.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    # Predicate guard: self-heals a removed/stale area instead of erroring.
    assert "local function install_can_skip_booster_guard()" in content
    guard = content.split("local function install_can_skip_booster_guard()", 1)[1].split(chr(10) + "end", 1)[0]
    assert "not G.pack_cards.cards or G.pack_cards.REMOVED" in guard
    assert "G.pack_cards = nil" in guard
    maint = content.split("local function decidex_maintenance()", 1)[1].split(chr(10) + "end", 1)[0]
    assert "pcall(install_can_skip_booster_guard)" in maint

    # Invariant sweep: single pack UI inside pack states, none outside.
    assert "local function sweep_pack_ui_invariants()" in content
    sweep = content.split("local function sweep_pack_ui_invariants()", 1)[1].split(chr(10) + "end", 1)[0]
    assert "removed duplicate (orphaned) pack UIBox" in sweep
    assert "removed pack UI outside a pack state" in sweep
    assert "pcall(sweep_pack_ui_invariants)" in maint

    # Rebuild escalation: a second stall in the same episode must abandon
    # instead of rebuilding into another empty area.
    wd = content.split("local function pack_stall_watchdog()", 1)[1].split(chr(10) + "local function ", 1)[0]
    assert "rebuild already tried this episode; abandoning instead" in wd
    assert "(_G._pack_rebuild_count or 0) < 1" in wd
    assert "_G._pack_rebuild_count = nil" in wd


def test_bridge_repairs_nil_state_midrun():
    """A nil G.STATE mid-run must be repaired from the visible UI.

    Vanilla restore events assign `G.STATE = prev_state` (use_card) and
    `G.STATE = G.GAME.PACK_INTERRUPT` (end_consumeable) from delayed events;
    when they fire after a DecideX recovery path cleared the captured field,
    G.STATE becomes nil. Game:update dispatches on G.STATE, so nil matches
    no branch and nothing ever sets it again -- the emit gate returns early
    and the autopilot freezes on its last snapshot.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "local function repair_nil_state()" in content
    # Repair only fires inside a run (G.GAME present) and only for nil.
    body = content.split("local function repair_nil_state()", 1)[1]
    body = body.split("\nend", 1)[0]
    assert 'if not (G and G.GAME and G.STATES) then return false end' in body
    assert 'if G.STATE ~= nil then return false end' in body
    # Every visible-UI fallback must map to a real state.
    for ui, state in (
        ("G.blind_select", "G.STATES.BLIND_SELECT"),
        ("G.shop", "G.STATES.SHOP"),
        ("G.round_eval", "G.STATES.ROUND_EVAL"),
        ("G.booster_pack", "G.GAME.PACK_INTERRUPT or G.STATES.SHOP"),
        ("G.hand", "G.STATES.SELECTING_HAND"),
    ):
        assert ui in body and state in body
    # delete_run's deliberate -1 limbo must never be touched.
    assert "-1" not in body
    # The repair must run from the maintenance path (pcall'd).
    maint = content.split("local function decidex_maintenance()", 1)[1]
    maint = maint.split("\nend", 1)[0]
    assert "pcall(repair_nil_state)" in maint


def test_bridge_frame_loop_crash_guard():
    """A Lua error in the game's own frame code must not kill the WASM mainloop.

    love.update used to call the game update and the bridge step unguarded, so
    a single escaping error (crashed event cleanup, half-removed UIBox, broken
    overlay) froze the whole game: every JS->Lua eval then timed out with
    "acknowledgement timed out" and the automation died with it. The frame
    guard pcalls the game update, the bridge step and the bridge maintenance
    separately, so the eval channel stays reachable no matter what throws.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "local function safe_decidex_step" in content
    assert "local function report_frame_error" in content

    # The love.update fallback must pcall the game's own update, not call it
    # raw: an error there is what killed the whole mainloop.
    tail = content.split("local orig_update = love.update", 1)[1]
    assert "pcall(orig_update, dt)" in tail

    # Maintenance (tutorial suppression, overlay dismissal, watchdogs) runs
    # before the eval consume and must not be able to starve it.
    assert "pcall(decidex_maintenance)" in content
    step = content.split("local function decidex_step(dt)", 1)[1]
    maint_pos = step.find("pcall(decidex_maintenance)")
    eval_pos = step.find('io.open("/tmp/balatro_eval.lua"')
    assert 0 < maint_pos < eval_pos

    # The game-loop hook injected by build.py resolves DECIDEX_STEP at call
    # time, so it must be bound to the guarded wrapper.
    assert "_G.DECIDEX_STEP = safe_decidex_step" in content


def test_app_js_surfaces_bridge_selfheal_events():
    """Watchdog recoveries must be visible to the operator, not just in stdout.

    The pack watchdog and the event/frame guards print Lua-side markers; if
    app.js never surfaces them, a run that is being repeatedly self-healed
    looks identical to a healthy one in the HUD.
    """
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for marker in (
        "[DecideX Pack Watchdog] ",
        "[DecideX Event Guard] ",
        "[DecideX Frame Guard] ",
    ):
        assert marker in js_content
    assert "[DecideX Recovery]" in js_content


def test_lua_bridge_blocks_stay_balanced():
    """The bridge patch must not introduce an unbalanced Lua block.

    There is no Lua interpreter in CI, so a cheap opener/end counter is used
    to catch the class of mistake an unbalanced `end` would cause.
    """
    checker = ROOT / "tools" / "balatro_wasm" / "check_lua_balance.py"
    assert checker.exists()

    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("check_lua_balance", checker)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for path in build_mod.BRIDGE_COPIES:
        assert mod.check(str(path)) == 0, f"unbalanced Lua blocks in {path}"


def test_transient_rejection_clears_inflight_lock():
    """v109: a transient dispatch rejection must clear the in-flight lock for
    play/discard too. The bridge's transient windows are short (discard
    debounce 1.5s, in-progress gate 5s) while keeping the lock stranded the
    action until the interval watchdog force-cleared it at 8s/15s -- the
    40+ "discard stuck for ~8s" idle locks per soak in v108.
    """
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "the interval watchdog then reported" in js_content
    # The transient branch clears unconditionally, blind actions keep only
    # their own sent-flag reset.
    assert "this._actionInFlight = null;" in js_content
    assert 'if (action === "select_blind" || action === "skip_blind") {' in js_content


def test_play_discard_wait_gates_aligned_with_bridge_latches():
    """v109: the executeStep wait gates and lifecycle fallbacks must stay in
    the same range as the bridge's own reject windows (play 5s latch gate,
    discard 1.5s debounce + 5s in-progress gate) so a retry happens from the
    normal cooldown path instead of the watchdog force-clear.
    """
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert 'this._actionInFlight === "play" && (now - (this._lastPlayActionTime || 0) < 6000)' in js_content
    assert 'this._actionInFlight === "discard" && (now - (this._lastDiscardActionTime || 0) < 5500)' in js_content
    assert "now - (this._lastPlayActionTime || 0) > 6000" in js_content
    assert "now - (this._lastDiscardActionTime || 0) > 5500" in js_content
    # The interval watchdog budgets must match (no more 15s/8s stranding).
    assert '(this._actionInFlight === "play") ? 6500 : ((this._actionInFlight === "discard") ? 6000 : 3800)' in js_content


def test_pack_selection_emits_content_progress():
    """v109: pack card selection must release the in-flight lock via content
    progress. Two halves: the bridge includes the highlighted flag in both
    the pack signature (snapshot emission) and shop_card_json (the snapshot
    payload); app.js diffs consecutive snapshots and unlocks non-combat
    in-flight actions when the selection signature flips.
    """
    bridge = Path(build_mod.BRIDGE_COPIES[0]).read_text(encoding="utf-8")
    # signature carries the highlight bit
    assert 'i .. ":" .. cname .. ":" .. hl' in bridge
    assert "local hl = (c.highlighted and 1 or 0)" in bridge
    # snapshot payload carries the highlight flag (single-quoted Lua string,
    # built via chr(39) to keep this file free of nested quote gymnastics)
    assert chr(39) + '"highlighted":' + chr(39) + ' .. (c.highlighted and "true" or "false")' in bridge

    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "onContentProgress" in js_content
    assert "(c.highlighted ? 1 : 0)" in js_content


def test_pack_ui_sweep_never_removes_pack_cards_area():
    """v109: the outside-a-pack-state sweep must drop the G.pack_cards
    reference without CardArea:remove(), which nils .cards and poisons every
    queued use_card restore event still holding the area
    (button_callbacks.lua:2283 crashed 18+ times per soak). abandon_pack
    already documents this exact vanilla-semantics constraint.
    """
    bridge = Path(build_mod.BRIDGE_COPIES[0]).read_text(encoding="utf-8")
    sweep = bridge[bridge.index("local function sweep_pack_ui_invariants"):]
    sweep = sweep[:sweep.index("end\n\n--[[") if "end\n\n--[[" in sweep else len(sweep)]
    # No :remove() on pack_cards inside the sweep anymore; reference drop only.
    assert "G.pack_cards:remove()" not in sweep
    assert "Drop the reference WITHOUT removing the area" in sweep


def test_draw_to_hand_tracks_hand_count_not_queue_noise():
    """v109: the DRAW_TO_HAND escape must arm on hand-COUNT progress, not
    queue-length activity. The base queue churns on unrelated background
    events during post-pack chaos (base=30-38 observed mid-stall), which
    kept the old queue-progress timer refreshed while zero cards were dealt
    for 49s (soak19 986s stall: STATE=3, STATE_COMPLETE=true, hand=0).
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")
    assert "_last_draw_hand_count" in content
    assert "_last_draw_hand_progress" in content
    assert "re-triggering draw_from_deck_to_hand" in content
    # Latch hygiene: both new latches reset when the state exits.
    tail = content[content.index("G.STATES.DRAW_TO_HAND then"):]
    tail = tail[:tail.index("\n  end", tail.index("_last_draw_queue_progress = nil"))]
    assert "_last_draw_hand_count = nil" in tail
    assert "_last_draw_hand_progress = nil" in tail


def test_pack_select_arms_watchdog_and_breaks_futile_retries():
    """v109: select_pack_card must (a) arm the watchdog clock per attempt --
    the dispatch path never refreshed _lastProgressTimestamp, so repeated
    slots-full rejections accumulated an ever-staler anchor (4.2s -> 9.8s
    warnings in soak21) -- and (b) stop retrying the same refused selection:
    three consecutive transient rejections trip a breaker that leaves the
    pack instead of churning (v111 widened slots-full to ALL rejection
    classes -- poisoned packs crash every pick with "use_card failed").
    """
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "instead of each attempt getting its own window" in js_content
    assert "this._packSelectFailStreak = (this._packSelectFailStreak || 0) + 1" in js_content
    assert "forcing pack leave" in js_content
    assert "_skipCurrentPack && state.can_skip_booster" in js_content
    # The breaker must reset per pack episode, keyed by state name.
    assert "_packStateSeen" in js_content


def test_scoring_applies_joker_effects_in_order():
    """Vanilla evaluate_play applies each joker's +mult before its ×mult,
    left-to-right. The old code accumulated all xmult and multiplied at the
    end, which mis-scored any run mixing +mult and ×mult jokers.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "const totalMult = mult * xmult" not in content
    # In-order application inside the joker loop
    assert "chips += jChips;" in content
    assert "mult += jMult;" in content
    assert "mult *= jXmult;" in content
    # Played-card editions/glass, then held-in-hand, then jokers
    assert "mult *= cardXMult;" in content
    assert "mult += heldMult;" in content
    assert "mult *= heldXMult;" in content


def test_scoring_gates_conditional_xmult_jokers_by_hand_type():
    """The Duo family x_mult must only fire when the played hand contains the
    required type (game: next(context.poker_hands[ability.type])). Previously
    the generic ability.x_mult path applied them unconditionally, and Acrobat
    fired on every hand instead of the last one.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert '"The Duo": "Pair", "The Trio": "Three of a Kind", "The Family": "Four of a Kind"' in content
    assert '"The Order": "Straight", "The Tribe": "Flush"' in content
    assert "DUO_HAND_GATE[jname] && !containsHand[DUO_HAND_GATE[jname]]" in content
    assert 'jname === "Acrobat" && Number(stateContext && stateContext.hands_left) !== 1' in content


def test_scoring_conditional_extra_jokers_not_double_counted():
    """Jolly-family/Half/Mystic Summit carry their bonus in extra but only score
    when their condition holds. The automatic extra path must skip them so a
    non-matching hand isn't credited their mult/chips.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "CONDITIONAL_EXTRA_JOKERS" in content
    for name in ("Jolly Joker", "Half Joker", "Mystic Summit", "Crafty Joker"):
        assert f'"{name}"' in content
    assert "j.extra.mult && !CONDITIONAL_EXTRA_JOKERS.has(jname)" in content


def test_scoring_covers_held_in_hand_and_face_jokers():
    """Baron / Shoot the Moon / Raised Fist / Mime / Blackboard / Photograph /
    Triboulet / Flower Pot / Baseball Card / Cavendish were absent from the
    prediction entirely — the main source of 'score missed the jokers'.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert 'j.name === "Baron"' in content
    assert 'j.name === "Shoot the Moon"' in content
    assert 'j.name === "Raised Fist"' in content
    assert 'jokerFlags.mime ? 2 : 1' in content
    assert 'jname === "Blackboard"' in content
    assert "allHeldBlack" in content
    assert 'jname === "Photograph"' in content
    assert 'jname === "Triboulet"' in content
    assert 'jname === "Flower Pot"' in content
    assert 'jname === "Baseball Card"' in content
    # Cavendish stores its multiplier in extra.Xmult (capital X), which the
    # generic extra.x_mult path never read.
    assert "j.extra.Xmult" in content


def test_scoring_covers_retriggers_and_pareidolia():
    """Red-seal plus Hack/Hanging Chad/Sock and Buskin/Dusk retrigger scoring
    cards; Pareidolia makes every card a face card for trigger purposes.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "cardRepeats" in content
    assert 'jokerFlags.hack && ["2", "3", "4", "5"].includes(c.rank)' in content
    assert 'jokerFlags.hangingChad && ci === 0' in content
    assert "jokerFlags.sockBuskin && isCardFace(c)" in content
    assert "jokerFlags.dusk" in content
    assert "jokerFlags.pareidolia" in content


def test_scoring_four_fingers_shortcut_classification():
    """Four Fingers legalizes 4-card flushes/straights; Shortcut bridges
    single-rank gaps. Both classification and scoring-card extraction must
    agree, or the hand is scored as High Card (everything lost).
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "function isStraight(cards, jokers)" in content
    assert "const isFlush = maxSuitCount >= (jokerFlags.fourFingers ? 4 : 5);" in content
    assert "const need = fourFingers ? 4 : 5;" in content
    assert "const gap = shortcut ? 1 : 0;" in content


def test_scoring_uses_leveled_hand_values_from_bridge():
    """Planet-levelled hands must be scored at their levelled chips/mult, not
    the level-1 POKER_HANDS table. The bridge emits hand_levels for every hand
    above level 1.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    bridge = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert '"hand_levels":{' in bridge
    assert "hlvl > 1" in bridge
    assert "stateContext.hand_levels" in content
    assert "hand_levels[handName]" in content


def test_pack_cards_hand_requirement_gated_before_pack_state():
    """Familiar/Grim/Incantation/Immolate/Cryptid/Sigil/Ouija consume the
    hand itself and crash inside vanilla when the hand is empty
    (pseudorandom_element -> math.random(0)). The check must sit BEFORE the
    pack-state branches: recovery paths can put foreign-set cards into any
    pack state, and a SPECTRAL_PACK-gated check would miss them.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    fn_start = content.index("function canUsePackCard(")
    fn_end = content.index("}", content.index('if (name === "The Fool") return false;', fn_start))
    body = content[fn_start:fn_end]

    for name in ("Familiar", "Grim", "Incantation", "Immolate", "Cryptid", "Sigil", "Ouija"):
        assert name in body, f"{name} missing from canUsePackCard hand gate"
    assert 'if (name === "The Fool") return false;' in body
    # The hand gate must precede the first pack-state branch (BUFFOON_PACK).
    assert body.index('["Familiar"') < body.index("BUFFOON_PACK")


def test_pack_select_failure_breaker_covers_all_rejections():
    """The pack selection circuit breaker must trip on ANY rejection class,
    not just slots-full: a poisoned pack crashes every pick with
    "use_card failed" (transient-classified), so a slots-full-gated counter
    never trips and the client churns until the pack is eaten card by card.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert 'errMsg.includes("slots full")' not in content
    assert "action === \"select_pack_card\" && !isStateMismatch" in content
    assert "this._packSelectFailStreak >= 3" in content
    assert "forcing pack leave" in content


def test_pack_no_progress_breaker_forces_leave():
    """A pack whose content signature never changes is wedged even when
    every action 'succeeds' (the bridge watchdog ignores non-empty packs).
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "45000" in content
    assert "packSig" in content
    assert content.count("window.executeForceLeavePack()") >= 3


def test_pack_unskippable_escalates_to_force_leave():
    """When the breaker trips but can_skip_booster is false (destroy-family
    picks in the shop, hand empty), waiting can never improve the situation:
    the pack must be abandoned instead of retried forever.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "this._skipCurrentPack && !state.can_skip_booster && packCards.length > 0" in content


def test_bridge_select_pack_card_prevalidates_hand_consumers():
    """The bridge must reject hand-consuming pack cards on an empty hand
    BEFORE vanilla use_card: vanilla removes the card from the pack area
    before crashing, so every failed attempt eats one pack card.
    """
    bridge = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "hand_needs_cards" in bridge
    for name in ("Familiar", "Grim", "Incantation", "Immolate", "Cryptid", "Sigil", "Ouija"):
        assert name in bridge
    assert "needs a non-empty hand" in bridge


def test_bridge_watchdog_abandons_poisoned_pack():
    """An abandon race can pair the OLD pack contents with the NEW pack
    state (probe_diag_v110: PLANET_PACK holding spectrals). The watchdog
    must detect the set mismatch and abandon instead of rebuilding/waiting.
    """
    bridge = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert 'expected = "Tarot"' in bridge
    assert 'expected = "Planet"' in bridge
    assert 'expected = "Spectral"' in bridge
    assert 'expected = "Standard"' in bridge
    assert 'expected = "Joker"' in bridge
    assert "poisoned pack" in bridge


def test_pack_hard_episode_cap():
    """Cards flickering in and out of the snapshot reset both the empty-pack
    timer and the no-progress breaker (one production episode dragged 106s).
    A pack state that outlives 75s -- the soak's own stall budget -- must be
    force-left unconditionally.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "Pack episode exceeded 75s (epoch" in content
    assert "now - this._packEpochSince > 75000" in content


def test_force_end_round_escape_hatch():
    """A round whose resolution chain broke (dropped scoring event) strands
    SELECTING_HAND with zero hands forever -- vanilla only ends a round
    through the final played hand or the all-areas-empty auto-end. The
    client must force vanilla end_round after 20s.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    bridge = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "window.executeForceEndRound" in content
    assert "SELECTING_HAND with 0 hands for 20s; forcing end_round" in content
    assert "this._handsExhaustedSince" in content
    assert 'if action == "force_end_round" then' in bridge
    assert "actions still available" in bridge
    assert "pcall(end_round)" in bridge


def test_pack_epoch_and_hand_sig_emission():
    """Two production stalls traced to per-episode and staleness bugs:
    (a) the client cannot tell pack episodes apart by JS-side state names
    (steps skipped while busy leaked _packEnterTime across packs, insta-
    abandoning freshly bought packs) -- the bridge must number episodes by
    pack-area identity (pack_epoch); (b) cards can be drawn into the hand
    DURING a pack and the pack-state emit must re-fire on hand changes, or
    the client judges every hand-consuming card unusable against a stale
    empty-hand snapshot (v114 spectral-pack stall: snapshot hand=0, real
    G.hand=8).
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    bridge = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert '"pack_epoch":' in bridge
    assert "_G._decidex_epoch_area" in bridge
    assert "epoch !== this._packEpoch" in content
    assert "Pack episode exceeded 75s (epoch" in content
    assert "now - this._packEpochSince > 75000" in content
    # pack emit must include the hand signature
    assert "pack_sig_parts[#pack_sig_parts + 1] = \"H\" .. tostring(#h_cards)" in bridge
    assert "h\" .. i .. \":\" .. r" in bridge


def test_stale_pending_state_replay_guard():
    """The busy-window state replay re-processed snapshots the game had long
    left (production: a pack snapshot from 436s earlier drove force_leave
    against a live round). Replays must be dropped when any newer snapshot
    arrived while the step ran.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "this.pendingStateCount = window._lastDecidexStateCount || 0;" in content
    assert "(window._lastDecidexStateCount || 0) - queuedCount <= 1" in content
