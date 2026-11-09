"""
Unit and regression tests for Balatro WebAssembly (love.js) build pipeline.
"""

from __future__ import annotations

import importlib.util
import re
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
    # IDBFS write-back must be serialised and back off when the store is slow.
    # A fixed setInterval stacked flushes on a big save (5 concurrent syncfs and
    # repeated "timed out after 5000ms" during long runs).
    assert "writeSyncInFlight" in js_content
    assert "saveSyncIntervalMs" in js_content
    assert "SAVE_SYNC_MAX_MS" in js_content
    assert "setInterval(syncSaveFileSystem" not in js_content
    assert "const timeoutMs = populate ? 15000 : 60000;" in js_content
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


def test_app_js_downloader_uses_short_lived_range_chunks():
    """Cold-start large-file download must not rely on one long-lived stream.

    The Cloudflare edge (and some transparent proxies) kill big HTTP/2
    transfers mid-flight; a full-file retry then re-enters the same doomed
    long stream and the progress bar freezes -- the v127 production probe sat
    at 26.5MB through hours of "Stream blip" while short ranged reads over
    the very same link delivered fine. Every request must therefore be
    capped to a bounded chunk (Range resume picks up exactly at the break),
    and attempts plus wall-clock must be bounded so a dead link fails loudly
    instead of hanging the loader forever.
    """
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "const DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024;" in js_content
    assert "const DOWNLOAD_MAX_ATTEMPTS = 120;" in js_content
    assert "const DOWNLOAD_TOTAL_BUDGET_MS = 1800000;" in js_content
    assert 'Range": `bytes=${loaded}-${loaded + DOWNLOAD_CHUNK_SIZE - 1}`' in js_content
    assert "if (loaded - requestStart >= DOWNLOAD_CHUNK_SIZE)" in js_content
    assert "await reader.cancel();" in js_content
    assert "Download budget exceeded:" in js_content
    assert "Download incomplete:" in js_content


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


def test_bridge_records_actual_action_at_execution_time():
    """§D: play/discard record card identity BEFORE the vanilla animation runs.

    By HAND_PLAYED emit time the selected cards have left G.hand, so the
    snapshot can never read them from there ("打出 0 张"). The bridge must
    capture rank/suit/enhancement at execution time and let build_state_json
    recover the indices by matching against the hand snapshot.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    # capture_actual records card identity from G.hand.highlighted at the
    # moment of the action, into _decidex_actual_action.
    assert "local function capture_actual(kind)" in content
    assert "_G._decidex_actual_action" in content
    assert 'capture_actual("play")' in content
    assert 'capture_actual("discard")' in content
    # Cleared with the rest of the round lifecycle so a stale record cannot
    # leak into the next round's snapshots.
    assert "_G._decidex_actual_action = nil" in content

    # build_state_json backfills actual from the record when no explicit
    # actual was passed, matching rank+suit (+enhancement first) and storing
    # 0-BASED indices into the emitted hand array (the consumers' contract:
    # balatro_web/app.py validates 0 <= idx < len(hand); the state-0002
    # fixture pairs cards [0..4] with A-K-Q-J-10 of spades).
    assert "if actual == nil then" in content
    assert "local rec = _G._decidex_actual_action" in content
    assert "indices[#indices + 1] = found - 1" in content
    assert "if complete then" in content


def test_bridge_copies_drift_is_detected(tmp_path, monkeypatch):
    """A diverged copy must raise instead of building a stale .love."""
    canonical = build_mod.BRIDGE_COPIES[0]
    stale = tmp_path / "decidex_bridge.lua"
    stale.write_bytes(canonical.read_bytes() + b"\n-- drift\n")
    monkeypatch.setattr(build_mod, "BRIDGE_COPIES", [canonical, stale])

    with pytest.raises(RuntimeError, match="drifted apart"):
        build_mod.check_bridge_copies_in_sync()


def test_bridge_keeps_blind_progression_self_healing():
    """The bridge must never be able to strand a run on one blind.

    Three defects combined into a run that could not end: the forced
    ROUND_EVAL -> SHOP shortcut skipped vanilla cash_out's blind-ante tail
    (reset_blinds, blind_ante, blind_tags); a lost
    round_resets.blind == G.P_BLINDS.* identity silently dropped vanilla's
    'Defeated' mark; and vanilla's blind-select builder is queued inside two
    nested events (game.lua:3254-3290), so a pack overlay can delay it into
    the middle of the NEXT round, where the ladder re-picks the blind just
    beaten and create_UIBox_blind_choice() rewrites its state to 'Select'
    (UI_definitions.lua:1489) -- permanently un-defeating it. Observed live:
    rounds 2..8 all facing the same Big blind at ante 1, Boss never faced,
    ante never advanced, no GAME_OVER in 900s.

    A first repair pass inferred "just played" from the
    round_resets.blind == G.P_BLINDS.* identity, but that field survives the
    ante rollover: when AutoPilot skips both Small and Big (Coupon Tag), the
    ladder legitimately re-offers the previous ante's Boss, and the repair
    marked that unplayed Boss 'Defeated' 17 times in one blind select. The
    played blind is therefore recorded per-ante at ROUND_EVAL.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    # Forced cash-out replicates the vanilla tail.
    assert "blind_ante = resets.ante" in content
    assert "resets.blind_tags.Small = get_next_tag_key()" in content
    assert "pcall(reset_blinds)" in content
    assert "[DecideX Blind Guard] reset_blinds failed:" in content

    # The round-eval guard re-derives the mark from the blind actually played,
    # and records it (with its ante) for the blind-select repair.
    assert "local function ensure_played_blind_defeated()" in content
    assert "blind:get_type()" in content
    assert "states[key] = 'Defeated'" in content
    assert "[DecideX Blind Guard] marked" in content
    assert "local function note_played_blind(key)" in content
    assert "note_played_blind(key)" in content
    assert "if rec.ante ~= resets.ante then return nil end" in content
    # ...and only on the ROUND_EVAL entry frame, i.e. before cash_out can
    # have reset the states.
    guard_call = content.index("pcall(ensure_played_blind_defeated)")
    entry_frame = content.rindex("if state_changed then", 0, guard_call)
    assert entry_frame < guard_call
    assert "G.STATE == G.STATES.ROUND_EVAL" in content[entry_frame - 400:entry_frame]

    # The blind-select repair mirrors vanilla's ladder and identity test
    assert "local function repair_blind_select_progress()" in content
    assert "local function drop_blind_select_ui(reason)" in content
    assert "return v == 'Defeated' or v == 'Skipped' or v == 'Hide'" in content
    # Only the blind the ladder is offering may carry 'Select'; any other mark
    # is a leftover from the delayed build.
    assert "if (v == 'Select' or v == 'Current') and key ~= on_deck then" in content
    # The offered blind must not be one this ante already played.
    assert "if last and last == on_deck and (states[last] == 'Select' or states[last] == 'Current') then" in content
    assert "mark_defeated(last, \" (already played)\")" in content
    # An accepted selection (facing_blind) must never be "repaired" away.
    assert "if G.GAME.facing_blind then return end" in content
    assert "states[key] = 'Defeated'" in content
    assert "local expected = blind_ladder(states)" in content
    assert "if G.GAME.blind_on_deck ~= expected then" in content
    # The repaired box is rebuilt from scratch, not patched in place.
    assert "drop_blind_select_ui(\"rebuilding for repaired on_deck\")" in content
    assert "if not rebuilt_needed then return end" in content
    # Vanilla only ever shows this UI in BLIND_SELECT; a leftover copy
    # elsewhere makes repair_nil_state() abandon the real screen.
    assert ("if G.STATE ~= G.STATES.BLIND_SELECT and G.blind_select "
            "and not G.blind_select.REMOVED then") in content
    assert "[DecideX Blind Guard] blind select repaired: " in content
    assert "[DecideX Blind Guard] dropped blind-select UI (" in content
    # The late builder itself is refused outside BLIND_SELECT: it would
    # rewrite blind_states and crash its own prompt-box event.
    assert "local function install_blind_select_build_guard()" in content
    assert "if G.STATES and G.STATE ~= G.STATES.BLIND_SELECT then" in content
    assert "[DecideX Blind Guard] refused blind-select build in " in content
    assert "pcall(install_blind_select_build_guard)" in content
    # The repair is attempted every frame, and its failure cannot abort the
    # watcher (a Lua error there would freeze the whole autopilot).
    repair_call = content.index("pcall(repair_blind_select_progress)")
    assert "err_blind_repair" in content[repair_call:repair_call + 300]


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


def test_play_search_is_bounded_on_oversized_hands():
    """A 40+ card hand must not trigger the full C(n,5) subset search.

    Endless runs grow the hand past 40 cards; exhaustive enumeration is millions
    of scorings per call, and both the watchdog and the HUD call it several
    times a second. That froze the main thread for minutes (650s rounds, syncfs
    timeouts, in-flight action timeouts).
    """
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "const PLAY_SEARCH_EXHAUSTIVE_MAX_HAND = 13;" in js_content
    assert "const PLAY_SEARCH_POOL_MAX = 12;" in js_content
    assert "const PLAY_SEARCH_EVAL_BUDGET = 3000;" in js_content

    body = js_content.split("function findBestPlays(state) {", 1)[1]
    # The memo must key on the whole scoring context and be consulted first.
    assert "findBestPlays._cache" in body
    assert "if (searchCache.sig === searchSig && searchCache.plays) return searchCache.plays;" in body
    # Enumeration must walk the pruned pool, never the raw hand, and honour the
    # evaluation budget.
    assert "for (let i = start; i < searchN; i++)" in body
    assert "combine(i + 1, [...chosen, searchHand[i]])" in body
    assert "evals > PLAY_SEARCH_EVAL_BUDGET" in body
    assert "Pruned play search: hand=" in body
    # The pool must protect each structure explicitly, not just card values.
    assert "flushSuit" in body
    assert "runRanks" in body


def test_autopilot_escapes_a_wedged_booster_pack():
    """The AutoPilot must have an escape hatch for a pack that never opened."""
    js_content = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "window.executeForceLeavePack" in js_content
    assert 'fn("force_leave_pack")' in js_content

    # The empty-pack branch must escalate to the escape hatch instead of
    # waiting forever when the pack cannot be skipped.
    pack_branch = js_content.split("// Empty pack (pack_cards is [])", 1)[1]
    assert "executeForceLeavePack()" in pack_branch
    # The escape hatch must not fire before the pack had a fair chance, and
    # the wait must be discriminated: a live pack UIBox with consumed cards
    # (can_skip_booster still true) closes immediately, while a pack whose UI
    # never appeared must get the bridge's 4s rebuild window first.
    assert "if (state.pack_closing && timeInPack < 3000) {" in pack_branch
    # Opposite patience by history: a consumed pack (saw cards) closes fast;
    # a never-opened pack gets the bridge's rebuild(~4s)+refill(~5.5s) window.
    assert "const emptyGraceMs = this._packSawCardsThisEpisode ? 800 : 9000;" in pack_branch
    assert "this._packSawCardsThisEpisode = false;" in js_content
    assert "this._packSawCardsThisEpisode = true;" in js_content
    # Before abandoning a pack whose UI may be missing, ask for a forced
    # state dump: the bridge's 4s rebuild can restore G.pack_cards without a
    # fresh snapshot ever reaching the client, which made the AutoPilot throw
    # away a freshly restored pack (probe v121).
    assert "window.decidexReEvaluate();" in pack_branch
    assert "!this._packEmptyDumped" in pack_branch
    # An empty pack is NOT skippable: re-dispatching skip_booster stacks
    # duplicate end_consumeable chains (probe_v121: 75 skips, 8 abandons,
    # ~16% of wall clock across 4 games) and delays the close.
    assert "executeSkipBooster()" not in pack_branch

    # Episode clocks must not outlive an episode. The bridge only bumps
    # pack_epoch when the pack CardArea identity changes, and nil == nil for
    # removed areas, so a stuck epoch carried the 75s cap into fresh packs and
    # force-left them on sight (probe v121: "Pack empty for 295064ms").
    assert "const prevSname = this._lastSeenState;" in js_content
    assert "if (epoch !== this._packEpoch || prevSname !== sname) {" in js_content


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
    # v138: start_run carries its own 12s budget (run-creation sequence
    # emits no snapshots for 4-7s; see test_client_start_run_waits_out_run_creation).
    assert '(this._actionInFlight === "play") ? 6500 : ((this._actionInFlight === "discard") ? 6000 : ((this._actionInFlight === "start_run") ? 12000 : 3800))' in js_content


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
    # Refactored engine: the whole generic-extra block is skipped for
    # condition-gated jokers (chips AND mult), so the exclusion check
    # guards the block and the donor object is jEffect (copy jokers).
    assert "!CONDITIONAL_EXTRA_JOKERS.has(jname)" in content
    assert "jEffect.extra.mult" in content
    for extra_name in ("Scholar", "Walkie Talkie"):
        assert f'"{extra_name}"' in content


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
    assert "jEffect.extra.Xmult" in content


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


def test_download_size_validated_with_cache_bust_retry():
    """The CF edge caches every balatro.love.zip?v=N URL immutably for 7
    days, and the first fetcher of a fresh version URL can race the origin
    deploy -- the edge then serves the PREVIOUS version's love under the
    new version's URL for a week (production probes booted the v110 bridge
    for four straight releases). The download path must validate the size
    and retry once with a one-shot cache buster.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "downloadLoveValidated" in content
    assert "edge served a stale package, retrying with cache buster" in content
    assert 'ASSET_LOVE_URL + "&bust=" + Date.now()' in content


def test_bridge_auto_closes_consumed_pack():
    """The pack close chain is broken in this WASM build (CardArea:remove()
    nils .cards, vanilla button_callbacks.lua:2283 crashes on the removed
    area, Event Guard drops it), so every consumed pack used to wait for the
    client's empty-pack grace plus a force_leave_pack round trip (~1.3s per
    pack, ~5% wall clock). The bridge now closes consumed packs itself with
    the exact call vanilla makes (button_callbacks.lua:2268), guarded so it
    can never cut a card's own effect chain short, close a half-created
    pack, or double-close into a nil G.STATE.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "local function auto_close_consumed_pack()" in content
    body = content.split("local function auto_close_consumed_pack()", 1)[1].split(
        chr(10) + "local function force_advance_to_shop()", 1)[0]
    # Only close packs whose contents were actually seen (half-created packs
    # are transiently empty while their card-creation events run).
    assert "_G._pack_seen_nonempty" in body
    # Anti-flicker stability window before closing. v131: 0.15s. v137:
    # 0.45s — the queued vanilla restore chain for the last used card
    # lands at ~+0.3s; closing before it fires raced the event (ghost
    # pack state resurrect + button_callbacks.lua:2283 crash on the
    # cascade-removed area, 61 per 40min soak).
    assert "now - _G._pack_empty_confirm < 0.45" in body
    # end_consumeable restores G.STATE from PACK_INTERRUPT and nils it; a
    # second call would write nil into G.STATE.
    # v131b: the PACK_INTERRUPT guard is GONE — the diag build proved that
    # rebuilt+populated packs ALWAYS have it nil at close time (cleared by
    # this episode's abandon_pack and by vanilla's queued 0.3s restore,
    # button_callbacks.lua:2627), so the guard only ever blocked the packs
    # that most need a silent close, each refusal costing a client
    # force_leave round trip and re-buy loops. abandon_pack already falls
    # back to the shop when the interrupt is nil.
    assert "G.GAME.PACK_INTERRUPT" not in body
    assert 'back = (G.GAME and G.GAME.PACK_INTERRUPT) or G.STATES.SHOP' in content
    # The instant exit path (end_consumeable's queued restore chain never
    # lands on this build: 0/7 self-restores in probe v122).
    assert "abandon_pack()" in body
    # A fresh snapshot must follow the close (pack gone, or pack_closing).
    assert "LAST_PACK_SIG = \"\"" in body
    # A rebuilt-but-unpopulated area must NOT be instantly closed: the
    # populate window lands 1.5s after the rebuild.
    assert "if _G._pack_rebuilt_at and not _G._pack_populated then return false end" in body
    # The client must see the closing flag or it double-exits (abandon_pack
    # racing the queued state restore -> nil G.STATE).
    assert '"pack_closing":' in content
    # Wired into the per-frame maintenance path.
    maint = content.split("local function decidex_maintenance()", 1)[1].split(chr(10) + "end", 1)[0]
    assert "pcall(auto_close_consumed_pack)" in maint


def test_event_guard_prints_are_rate_limited():
    """The known-broken vanilla close chain and zombie tarot transform
    events fire hundreds of drops per session (45min soak: 334). The Event
    Guard must print the first occurrence in full and then fold repeats of
    the same signature into a suppressed count, keeping observability
    without flooding the browser console mirror.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "_G._decidex_guard_throttle" in content
    assert "suppressed in last 10s" in content


def test_bridge_populates_dead_pack_creation():
    """Vanilla creates pack cards in queued events (card.lua:1721, delay
    1.3*sqrt(GAMESPEED)) and emplaces them gated on the pack UIBox sliding
    in. When the watchdog rebuilt the UI after those events died into a
    removed area, the rebuilt area stayed empty forever (probe v122: 5/22
    rebuilds ever showed cards; the rest were abandoned at 6s with the
    purchase lost). The bridge must refill the rebuilt area itself,
    mirroring vanilla's sets/voucher odds/seed keys, and trim late vanilla
    emplaces that would duplicate the refill.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "local function populate_empty_pack()" in content
    body = content.split("local function populate_empty_pack()", 1)[1].split(
        chr(10) + "local function force_advance_to_shop()", 1)[0]
    # Only after a rebuild, with a settle window (v131: 0.6s — production
    # showed the vanilla creation chain dead in 79/79 episodes so the old
    # 1.5s wait was pure dead time; a merely-delayed vanilla emplace is
    # handled by trim_overfull_pack), and only while the area is still
    # empty.
    assert "_G._pack_rebuilt_at" in body
    assert "now - _G._pack_rebuilt_at < 0.6" in body
    assert "if G.pack_cards.cards and G.pack_cards.cards[1] then" in body
    # Mirror of vanilla card.lua:1732-1775 (sets, voucher odds, seeds).
    for fragment in ['"Tarot", G.pack_cards', '"Planet", G.pack_cards', '"Spectral", G.pack_cards',
                     '"Enhanced" or "Base"', '"Joker", G.pack_cards', "v_omen_globe",
                     "v_telescope", "poll_edition", "set_edition"]:
        assert fragment in body, fragment
    # Late vanilla emplace would duplicate the refill: trim to pack_size.
    assert "#cards > size" in body
    # A refill (or vanilla delivery) must re-emit so the client sees it.
    assert 'print("[DecideX Pack Watchdog] pack populated from bridge")' in content
    assert "LAST_PACK_SIG = \"\"" in body
    # Wired into the per-frame maintenance path.
    maint = content.split("local function decidex_maintenance()", 1)[1].split(chr(10) + "end", 1)[0]
    assert "pcall(populate_empty_pack)" in maint
    # The rebuild records its timestamp and forces the epoch-carrying emit.
    reb = content.split("local function rebuild_pack_ui()", 1)[1].split(
        chr(10) + "local function populate_empty_pack()", 1)[0]
    assert "_G._pack_rebuilt_at" in reb
    assert 'LAST_PACK_SIG = ""' in reb


def test_bridge_kicks_stuck_event_queue():
    """v131: tag-opened packs lost their creation chain to event crashes.

    Charm/Meteor/Ethereal/Standard/Buffoon Tag open their (mega) pack via
    G.FUNCS.use_card from the blind-select context (tag.lua:209-282). On
    this WASM build the queued chain dies: the update_<pack> factory
    event (game.lua:3333) crashes inside its UIBox definition build and
    the Event Guard drops it BEFORE create_UIBox_*_pack() assigns
    G.pack_cards -- leaving state_complete=true, G.pack_cards=nil and a
    CLEAN queue (base=1 is the by-design eternal splash-spin event,
    game.lua:2276, which never completes on purpose). Production
    evidence: every stall dump read `state_complete=true, queues=base=1`
    for 4+s, and 79/79 packs in the v130 deep probe went through
    stall->rebuild->populate at 6-9s each instead of the native ~1s.

    The watchdog must: trip early (0.8s, healthy fills surface their area
    in <0.3s), try one precise queue kick (walks the base queue with
    vanilla semantics; evicts a permanently-stuck blocking immediate —
    the only case where the chain is merely BLOCKED rather than dead),
    then fall through to the proven rebuild+populate ladder.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "local function pack_stall_watchdog()" in content
    body = content.split("local function pack_stall_watchdog()", 1)[1].split(
        chr(10) + "local function auto_close_consumed_pack()", 1)[0]
    # Stall floor lowered: healthy native fills surface their area in
    # <0.3s; 4s burned 3+ extra seconds on every tag pack before recovery
    # even started (v130 prod: 79/79 packs stalled).
    assert "if stalled_for < 0.8 then return false end" in body
    # Queue kick: walks the base queue with vanilla semantics, evicts a
    # permanently-stuck blocking immediate, revives when pack_cards
    # appears.
    assert "local function kick_pack_queue()" in content
    assert "G.E_MANAGER:update(1/60, true)" not in body  # manual walk, not a blind forced pass
    assert "evicted stuck blocking immediate" in content
    # The kick is bounded per episode (one extra window) before the
    # rebuild ladder: the diag showed the queue is usually already clean
    # (the factory event crashed and was dropped by the Event Guard), so
    # rebuild+populate is the actual recovery, not the kick.
    assert "_G._pack_kick_count" in body
    assert "(_G._pack_kick_count or 0) < 1" in body
    # After a successful kick the populate safety net arms (creation
    # events live behind the same broken chain and may still be dead).
    assert "_G._pack_rebuilt_at = now" in body
    assert "queue kick revived native pack chain" in body
    # Kick counter resets outside pack states (fresh episode budget).
    assert "_G._pack_kick_count = nil" in body
    # v131c: before the kick/rebuild ladder, re-arm STATE_COMPLETE when the
    # vanilla factory provably never ran for this state — that opens the
    # pack NATIVELY (real open animation + vanilla creation events) instead
    # of the abrupt rebuild users reported as "卡 UI". A crashed factory
    # keeps its marker, so the re-arm cannot loop.
    assert "if G.STATE_COMPLETE and _G._pack_factory_seen ~= G.STATE" in body
    assert "(_G._pack_rearm_count or 0) < 1" in body
    assert "G.STATE_COMPLETE = false" in body
    assert "re-arming STATE_COMPLETE for a native open" in body
    assert "_G._pack_rearm_count = nil" in body
    # The factory marker resets per episode (a fresh pack of the same state
    # gets its own native-open attempt).
    assert "_G._pack_factory_seen = nil" in body
    # The marker wrapper installs on the create_UIBox_*_pack globals,
    # preserves vanilla failure semantics (re-raise), and prints the real
    # crash message (Event Guard rate-limiting would otherwise swallow it).
    assert "local function install_pack_factory_markers()" in content
    markers_body = content.split("local function install_pack_factory_markers()", 1)[1].split(
        chr(10) + "local function", 1)[0]
    assert "create_UIBox_celestial_pack" in markers_body
    assert "_G._pack_factory_seen = (G and G.STATE) or true" in markers_body
    assert "error(def, 2)" in markers_body
    assert "[DecideX Pack Diag] factory " in markers_body
    # Wired into the per-frame maintenance path (globals may not exist yet
    # when the bridge boots — install latches and retries per tick).
    maint = content.split("local function decidex_maintenance()", 1)[1].split(chr(10) + "end", 1)[0]
    assert "pcall(install_pack_factory_markers)" in maint


def test_bridge_and_client_refuse_duplicate_pack_use():
    """v133: one USE per pack card per episode.

    The picked card leaves the area via a QUEUED vanilla event. Until it
    lands the emitted snapshot is unchanged, and re-picking the same card
    ran use_card a SECOND time — each run queues its own hand-level-up
    (production report: one planet pack leveled the same hand several
    times). Two independent guards:

    - bridge: sort_id is object-unique, so a dispatched-and-STILL-present
      card is refused ("duplicate use in progress" — transient-classified);
      the mark happens BEFORE use_card so even a mid-body crash cannot be
      re-applied. Legit retries never reach use_card and stay unmarked.
    - client: an identical (name|index) pick while the content signature is
      unchanged waits quietly instead of re-dispatching; reset per episode
      (two packs can hold identical contents).
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")
    sel = content.split('if action == "select_pack_card" then', 1)[1].split(
        chr(10) + '  if action == ', 1)[0]
    # Per-episode reset keyed on the pack area identity.
    assert "if G.pack_cards ~= _G._decidex_used_area then" in sel
    assert "_G._pack_dispatched = {}" in sel
    # The dedupe refusal, transient-classified for the client.
    assert "duplicate use in progress (card already dispatched once this pack)" in sel
    # Marked BEFORE use_card (a mid-body crash must not be re-appliable).
    assert "_G._pack_dispatched[c.sort_id] = true" in sel
    dispatch_pos = sel.find("pcall(G.FUNCS.use_card")
    mark_pos = sel.find("_G._pack_dispatched[c.sort_id] = true")
    assert 0 < mark_pos < dispatch_pos

    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # In-flight pick guard keyed on name|index + content signature.
    assert "const pickId = `${bestCard.name}|${bestCard.index}`;" in js
    assert "this._lastPackPick.sig === packSig" in js
    assert "Pick in flight for" in js
    # Per-episode reset (identical contents in a fresh pack must pick freely).
    assert "this._lastPackPick = null;" in js


def test_bridge_once_dedupe_covers_buy_sell_use():
    """v134: the v133 double-apply class extends beyond pack picks.

    The card/object leaves its area via a QUEUED vanilla event; until it
    lands the snapshot is unchanged and a re-dispatch runs the action a
    SECOND time: buy charges twice, sell refunds twice, a hand consumable
    applies twice (planet level-up, Hermit payout). play/discard already
    have in-flight gates; buy (all three branches), sell and use get a
    shared sort_id once-set:
    - refused message contains "in progress" -> client-classified transient;
    - the mark lands BEFORE the vanilla call (mid-body crash cannot be
      re-applied);
    - buy's graceful no-space refusal UNMARKS (sell-then-rebuy stays legal),
      while an exception throw keeps it (partial effects may exist).
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")
    # Shared helpers exist and cap the set.
    assert "local function decidex_once_refused(c, label)" in content
    assert "local function decidex_once_mark(c)" in content
    assert "local function decidex_once_unmark(c)" in content
    assert "_G._decidex_once_count > 512" in content

    buy = content.split('if action == "buy_card" or action == "buy_from_shop" then', 1)[1].split(
        chr(10) + '  if action == "reroll_shop"', 1)[0]
    # All three branches check + mark.
    assert buy.count('decidex_once_refused(c,') == 3
    # booster: use_card + open; voucher: use_card + redeem; standard: 1 -> 5
    assert buy.count("decidex_once_mark(c)") == 5
    assert "decidex_once_unmark(c)" in buy  # graceful no-space releases the mark

    us = content.split('if action == "use_card" or action == "sell_card" then', 1)[1].split(
        chr(10) + '  if action == "reorder_jokers"', 1)[0]
    assert 'decidex_once_refused(c, action)' in us
    assert us.count("decidex_once_mark(c)") == 2
    # v131: a late vanilla emplace after a bridge populate must not leave
    # unpickable extras in the area.
    assert "local function trim_overfull_pack()" in content
    trim_body = content.split("local function trim_overfull_pack()", 1)[1].split(
        chr(10) + "local function populate_empty_pack()", 1)[0]
    assert "#cards <= size" in trim_body
    assert "trimmed " in content  # trim prints its count
    maint = content.split("local function decidex_maintenance()", 1)[1].split(chr(10) + "end", 1)[0]
    assert "pcall(trim_overfull_pack)" in maint
    assert "pcall(populate_empty_pack)" in maint


def test_bridge_sweeps_stale_stage_ui():
    """v130: "进入下阶段会遗留上阶段的 UI 卡住显示".

    Vanilla removes G.shop in every Game:update_* (game.lua:3042 etc.),
    G.round_eval in cash_out (button_callbacks.lua:2940) and the shop sign
    in toggle_shop (2508) -- all inside queued events the Event Guard drops
    when a sibling crashed. The global then leaks into the next stage and
    its UIBox keeps rendering on top of it (observed: shop panel over the
    next blind, round-eval panel stuck over the shop). The bridge must
    sweep UIBox globals that outlive their state, conservatively.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    assert "local function sweep_stale_ui()" in content
    body = content.split("local function sweep_stale_ui()", 1)[1].split(
        chr(10) + "-- Keep blind progression monotonic", 1)[0]
    # Screen transitions guard (same wipe/load locks as the blind rebuild).
    assert "G.CONTROLLER.locks.wipe or G.CONTROLLER.locks.load" in body
    # 1.2s settle: vanilla's own removal events run first.
    assert "now - _G._sweep_state_enter < 1.2" in body
    # G.shop outside SHOP -- but packs open ON TOP of a live shop and return
    # to it (vanilla end_consumeable slides the same shop back in), so pack
    # states and PLAY_TAROT must be excluded from the sweep.
    assert "G.shop and not G.shop.REMOVED" in body
    assert "local shop_ok = (G.STATE == G.STATES.SHOP) or is_pack_state()" in body
    assert "(G.STATE == G.STATES.PLAY_TAROT)" in body
    assert "if G.shop and not G.shop.REMOVED and not shop_ok then" in body
    # G.round_eval outside ROUND_EVAL, kept during the cash-out animation.
    assert "G.round_eval and not G.round_eval.REMOVED and G.STATE ~= G.STATES.ROUND_EVAL" in body
    assert "not G.cashing_out" in body
    # The shop sign rides along with the panel (vanilla removes it in
    # toggle_shop, button_callbacks.lua:2508).
    assert "G.SHOP_SIGN and not G.SHOP_SIGN.REMOVED" in body
    # Removals are pcall-guarded and the print is throttled.
    assert 'pcall(function() ub:remove() end)' in body
    assert "[DecideX UI Sweep] removed " in body
    assert "now - _G._sweep_last_print > 5.0" in body
    # A cleaned screen must immediately re-emit to the client.
    sig_block = body.split("#victims == 0 then return end", 1)[1]
    for sig in ["LAST_HAND_SIG", "LAST_SHOP_SIG", "LAST_BLIND_SIG", "LAST_EVAL_SIG", "LAST_PACK_SIG"]:
        assert f'{sig} = ""' in sig_block, sig
    # Wired into the emit path (runs per state check, not per frame).
    emit = content.split("local function check_and_emit_state()", 1)[1].split(
        chr(10) + "local function decidex_execute_inner", 1)[0]
    assert "pcall(sweep_stale_ui)" in emit


def test_autopilot_transition_pacing_floors():
    """v130: "自动化太快了，游戏的ui会很卡".

    The 150-350ms scheduling cadence re-entered mid-transition, stacked
    dispatches onto a main thread still running the previous stage's ease
    events, and the 100ms no-op unlock re-entry starved it further. The
    floors must sit above the vanilla transition times (screen wipe ~0.3s,
    panel slide ~0.5s at GAMESPEED 4).
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    floors = content.split("getDelayForState(sname) {", 1)[1]
    assert re.search(r'SPLASH"\s*\|\|\s*sname === "MENU"\s*\)\s*return 300;', floors)
    assert 'if (sname === "BLIND_SELECT") return 400;' in floors
    assert 'if (sname === "ROUND_EVAL") return 350;' in floors
    assert 'if (sname === "SHOP") return 400;' in floors
    assert 'if (sname === "SELECTING_HAND") return 450;' in floors
    assert 'return 400;' in floors
    # The no-op unlock re-entry must not be 100ms.
    assert "const delay = executedAction ? unlockCooldownMs : 250;" in content
    # The watchdog 350ms nudge must respect the pacing floor instead of
    # bypassing it.
    nudge = content.split("// never more than twice per pacing floor", 1)[1].split("}, 350);", 1)[0]
    assert "sinceStep >= floor" in nudge
    assert "Math.max(600, AutoPilot.getDelayForState(sname))" in nudge


def test_client_enforces_eye_mouth_hand_type_gates():
    """v136 root fix: The Eye / The Mouth "blocks play" rejection loop.

    Both constraints were DETECTED in detectConstraints but never consumed:
    vanilla sets blind.block_play for a forbidden type, the bridge refuses
    with "blind blocks play", the client transient-classifies it and re-
    dispatches the SAME top-ranked (forbidden) play forever — the round can
    never advance and no strike counter ever trips. The root fix bans the
    forbidden types at RANKING time in findBestPlays, from the bridge's
    authoritative per-type played_this_round counts.

    - shared helper bannedHandTypeReason (The Eye: repeats; The Mouth: any
      type other than the played one once one exists);
    - findBestPlays zeroes a banned candidate (rawTotalScore keeps the
      physical prediction for calibration);
    - optimizePlayWithJunkFilter must not attach junk that converts a legal
      play into a banned type (Pair -> Two Pair under The Mouth);
    - the client resyncs the snapshot on a "blocks play" rejection so the
      ban applies on the very next ranking.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    helper = content.split("function bannedHandTypeReason(state, constraints, handName)", 1)[1]
    assert "constraints.noRepeatedHands" in helper
    assert "The Eye 禁止重复牌型" in helper
    assert "constraints.singleHandType" in helper
    assert "The Mouth 本轮只允许" in helper
    assert "hand_played_round" in helper

    # Ranking-time gate inside findBestPlays' combine loop.
    search = content.split("function findBestPlays(state)", 1)[1].split(
        "function findBestDiscards", 1)[0]
    assert "const bannedReason = bannedHandTypeReason(state, constraints, evalRes.handName);" in search
    banned_pos = search.find("if (bannedReason) {")
    push_pos = search.find("candidates.push(evalRes);")
    assert 0 < banned_pos < push_pos
    assert "evalRes.totalScore = 0;" in search

    # Junk filter must not create a forbidden type.
    junk = content.split("function optimizePlayWithJunkFilter(basePlay, state)", 1)[1].split(
        "function arbitrateAction", 1)[0]
    assert "if (bannedHandTypeReason(state, constraints, testEval.handName)) continue;" in junk

    # The "blocks play" transient rejection triggers a snapshot resync.
    failure = content.split("onActionDispatchFailure(action, error, sequence) {", 1)[1].split(
        "onStateUpdated(state) {", 1)[0]
    assert 'errMsg.includes("blocks play")' in failure
    assert "window.decidexReEvaluate();" in failure


def test_client_avoids_the_ox_and_predicts_the_flint():
    """v136: two detected-but-unused boss constraints wired into decisions.

    - The Ox wipes ALL money when the most-played hand is played. While the
      Ox hand cannot win the blind outright it is downgraded to 30% so a
      legal alternative accumulates instead; a win-outright Ox hand keeps
      its full score (survival beats cash). most_played_hand also joins the
      findBestPlays memo signature so a changed target re-ranks.
    - The Flint halves base Chips/Mult — evaluateHandScore must halve the
      hand-level base too, or every steady-accumulation verdict and the
      calibration stream overestimate ~2x.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    search = content.split("function findBestPlays(state)", 1)[1].split(
        "function findBestDiscards", 1)[0]
    assert "state.idol_rank, state.idol_suit, state.most_played_hand," in search
    assert "constraints.wipesMoneyOnMostPlayed && state.most_played_hand" in search
    assert "evalRes.handName === state.most_played_hand" in search
    assert "evalRes.totalScore * 0.3" in search
    # Downgrade only when the hand does not win outright.
    ox_block = search.split("constraints.wipesMoneyOnMostPlayed", 1)[1]
    assert "if (evalRes.totalScore < oxNeed)" in ox_block

    eval_fn = content.split("function evaluateHandScore(selectedCards, jokers, blind, stateContext)", 1)[1].split(
        "function findBestPlays(state)", 1)[0]
    assert "constraints.halvedBase" in eval_fn
    assert "{ chips: lvlBase.chips / 2, mult: lvlBase.mult / 2 }" in eval_fn
    # Halving must happen to a COPY of the level base (POKER_HANDS is the
    # shared module-level table — mutating it in place permanently halved
    # the hand for every later evaluation in the session), BEFORE card
    # chips are added.
    assert "const base = constraints.halvedBase" in eval_fn
    flint_pos = eval_fn.find("constraints.halvedBase")
    chips_pos = eval_fn.find("let chips = base.chips + cardChips;")
    assert 0 < flint_pos < chips_pos


def test_client_use_card_rejection_breaker():
    """v136: use_card transient-refusal loop breaker.

    The v134 once-mark is set BEFORE vanilla use_card runs, so a mid-body
    failure leaves the card marked AND still in its area: the client then
    re-selected it forever ("duplicate use_card in progress" is transient-
    classified), and because consumable use outranks play (combat) and
    shop-toggle, the state wedged with no strike counter to save it.

    - every consumable dispatch stamps _pendingConsKey for attribution;
    - a "duplicate" refusal skips the card for this episode immediately;
    - other transient refusals skip after 2 strikes;
    - the skip set clears on every state change (targets/slots may appear)
      and on toggle; the evaluators honour it.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # Evaluators take and honour the skip set.
    assert "function findBestConsumableToUse(state, skipSet) {" in content
    assert "function findBestNonCombatConsumableToUse(state, skipSet) {" in content
    assert content.count('if (skipSet && skipSet.has(name + "|" + idx)) continue;') == 2

    # All three dispatch sites stamp the pending key.
    assert content.count("this._pendingConsKey = nonCombatCons.card.name") == 2
    assert "this._pendingConsKey = bestCons.card.name" in content

    # Breaker in the transient branch of onActionDispatchFailure.
    failure = content.split("onActionDispatchFailure(action, error, sequence) {", 1)[1].split(
        "onStateUpdated(state) {", 1)[0]
    dup_pos = failure.find('errMsg.includes("duplicate")')
    strike_pos = failure.find("this._consUseFailStreak[consKey] >= 2")
    pending_pos = failure.find("const consKey = this._pendingConsKey;")
    assert 0 < pending_pos < dup_pos < strike_pos
    assert 'this._consUseSkipped.add(consKey);' in failure

    # Success clears the pending attribution; state change clears all.
    assert 'if (action === "use_card") this._pendingConsKey = null;' in content
    reset = content.split('if (sname !== "SELECTING_HAND") this._handsExhaustedSince = null;', 1)[1]
    assert "this._consUseSkipped.clear();" in reset
    assert "this._consUseFailStreak = {};" in reset


def test_client_step_error_streak_breaker():
    """v136: decision-layer exception loop breaker.

    executeStep's catch used to retry at 150-250ms forever with no
    escalation (v135: "money is not defined" wedged every shop for 75s+
    until a human read the logs). Consecutive step exceptions now resync
    the snapshot at 5 and auto-pause with the visible reason at 12 (same
    budget as hard action failures); one clean step resets the streak.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    step = content.split("async executeStep() {", 1)[1].split("window.AutoPilot = AutoPilot;", 1)[0]
    assert "let stepThrew = false;" in step
    assert "stepThrew = true;" in step
    assert "this._stepErrorStreak >= 12" in step
    assert "决策连续异常" in step
    assert "this.toggle(false, { preserveStatus: true });" in step
    assert "this._stepErrorStreak >= 5" in step
    finally_pos = step.find("if (!stepThrew) this._stepErrorStreak = 0;")
    catch_pos = step.find("stepThrew = true;")
    assert 0 < catch_pos < finally_pos
def test_bridge_abandon_leaves_valid_pack_interrupt():
    """v137: abandon_pack must NOT nil G.GAME.PACK_INTERRUPT.

    Vanilla's queued end_consumeable tail (button_callbacks.lua:2625)
    reads G.GAME.PACK_INTERRUPT at FIRE time (+0.2s) and writes it
    straight into G.STATE. After the abandon nil'd the field, that tail
    wrote nil into G.STATE and the nil-STATE repair fired 10+ times per
    episode (soak v137-diag: 13 repairs per 40min). Leaving the state we
    are returning to keeps the tail's write valid; the tail then
    self-clears the field exactly as vanilla designed.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")
    body = content.split("local function abandon_pack()", 1)[1].split(
        chr(10) + "local function ", 1)[0]

    # The interrupt step normalizes to the return state instead of nil.
    assert "step(\"pack_interrupt\", function()" in body
    assert "G.GAME.PACK_INTERRUPT = back" in body
    assert "G.GAME.PACK_INTERRUPT = nil" not in body
    # back is computed before the steps run (used by both the state step
    # and the interrupt step).
    assert "local back = (G.GAME and G.GAME.PACK_INTERRUPT) or G.STATES.SHOP" in body
    # The interrupt step must come after `back` is bound (order sanity:
    # back-pos < interrupt-pos).
    back_pos = body.index("local back = (G.GAME and G.GAME.PACK_INTERRUPT) or G.STATES.SHOP")
    int_pos = body.index("G.GAME.PACK_INTERRUPT = back")
    assert back_pos < int_pos

    # v137 F4: the cascade-removed pack area becomes a zombie with a
    # living empty table, so a late use_card restore evaluates
    # area.cards[1] as falsy instead of crashing (button_callbacks
    # :2283, 61 per 40min soak when the congested queue delivers the
    # restore after the abandon).
    assert "local dying_area = G.pack_cards" in body
    assert "pack_cards_zombie" in body
    assert "if dying_area and dying_area.cards == nil then" in body
    assert "dying_area.cards = {}" in body
    # Capture must precede the ref-nil step (capture-pos < ref-pos).
    cap_pos = body.index("local dying_area = G.pack_cards")
    ref_pos = body.index('step("pack_cards_ref", function() G.pack_cards = nil end)')
    assert cap_pos < ref_pos


def test_bridge_ghost_state_reabandon_guard():
    """v137: a stale vanilla restore event re-enters the pack we just
    abandoned (use_card's +0.3s "G.STATE = prev_state"). Full recovery
    churn on that ghost (stall tripwire -> STATE_COMPLETE re-arm ->
    rebuild -> populate -> auto-close) costs seconds per cycle and ends
    in another abandon anyway. The watchdog must re-abandon in one frame
    when the exit window is active, no pack UI exists, and the area is
    gone/empty — before any of the recovery machinery runs. Dispatched
    actions clear the window, so a freshly purchased pack is never a
    ghost.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")
    body = content.split("local function pack_stall_watchdog()", 1)[1].split(
        chr(10) + "local function ", 1)[0]

    # The ghost guard is present and uses the exit window + UI absence.
    assert "_G._decidex_pack_exit_until and now < _G._decidex_pack_exit_until" in body
    assert "and not G.booster_pack then" in body
    assert "if not gpc or not gpc.cards or not gpc.cards[1] then" in body
    assert "ghost pack state after abandon" in body
    assert "abandon_pack()" in body
    # The guard runs BEFORE the empty-area health logic and the stall
    # tripwire (guard-pos < G.pack_cards health check).
    guard_pos = body.index("_G._decidex_pack_exit_until and now < _G._decidex_pack_exit_until")
    health_pos = body.index("if G.pack_cards then")
    assert guard_pos < health_pos
    # And before the stall tripwire print.
    trip_pos = body.index("pack stalled for")
    assert guard_pos < trip_pos


def test_bridge_dispatch_clears_pack_exit_window():
    """v137: any dispatched action proves a live decision-maker, so
    decidex_execute_inner must clear the post-abandon exit window --
    otherwise the ghost-state guard in pack_stall_watchdog would kill a
    pack legitimately purchased seconds after an abandon. select_pack_card
    is exempt: its own gate ("pack exit in progress") must keep refusing
    a stuck retry that lands in a resurrected empty pack.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")
    body = content.split("local function decidex_execute_inner(action, args)", 1)[1].split(
        chr(10) + "local function ", 1)[0]

    assert 'if action ~= "select_pack_card" then' in body
    assert "_G._decidex_pack_exit_until = nil" in body
    clear_pos = body.index("_G._decidex_pack_exit_until = nil")
    # The clear runs before the first action branch (§D capture helpers
    # are defined at the top; the first action handler follows it).
    first_action = body.index('if action == "play" or action == "discard" then')
    assert clear_pos < first_action

    # The select_pack_card gate survives.
    gate = content.split('if action == "select_pack_card" then', 1)[1]
    assert '_G._decidex_pack_exit_until and now_gate < _G._decidex_pack_exit_until' in gate
    assert '"pack exit in progress"' in gate
def test_client_start_run_waits_out_run_creation():
    """v138: every game start was force-cleared at ~3.8s ("Action start_run
    stuck") because the run-creation sequence (wipe_on -> delete_run -1
    limbo -> G:start_run full init -> BLIND_SELECT) emits no snapshots for
    4-7s (soak: 7s when a deep dead run is deleted first) while the
    anti-hang watchdog used the 3800ms default budget. start_run now gets
    its own 12s budget (same carve-out class as select_blind), and the
    3s busy-lock auto-clear preserves its in-flight lock like the blind
    actions.
    """
    content = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # Own anti-hang budget, positioned after the play/discard ternaries.
    assert '(this._actionInFlight === "start_run") ? 12000 : 3800' in content
    budget_pos = content.index('(this._actionInFlight === "start_run") ? 12000 : 3800')
    assert "v138" in content[budget_pos - 700:budget_pos]
    # The budget sits inside the anti-hang watchdog (after the comment
    # block that explains the force-clear discipline).
    assert content.index("const maxStuckTime") < budget_pos

    # The 3s busy-lock auto-clear must not drop a start_run in-flight lock.
    guard = 'this._actionInFlight !== "discard" && this._actionInFlight !== "start_run"'
    assert guard in content
    guard_pos = content.index(guard)
    assert "v138" in content[guard_pos:guard_pos + 300]
    # It stays inside the busy-lock watchdog (before the anti-hang section).
    assert guard_pos < budget_pos
def test_bridge_stall_waits_for_pending_factory():
    """v139: G.STATE_COMPLETE == true with no factory marker means vanilla
    consumed the state-entry gate and the factory event is queued but
    pending behind the purchase/open chain (diag139: base=40 events; can
    legitimately take >0.8s to reach). The old 0.8s tripwire re-armed
    STATE_COMPLETE there, queueing a SECOND factory behind the first and
    forcing the rebuild+populate detour on ~60% of packs. The watchdog
    must now wait up to 2.5s for a pending factory before treating it as
    lost, and the stall print carries factory_seen for observability.
    """
    content = (PATCHES_DIR / "decidex_bridge.lua").read_text(encoding="utf-8")

    # The pending-factory wait sits BEFORE the 0.8s tripwire and before
    # the stall handling print.
    assert ("if G.STATE_COMPLETE == true and not _G._pack_factory_seen "
            "and stalled_for < 2.5 then") in content
    pending_pos = content.index(
        "if G.STATE_COMPLETE == true and not _G._pack_factory_seen "
        "and stalled_for < 2.5 then")
    trip_pos = content.index("if stalled_for < 0.8 then return false end",
                             pending_pos)
    print_pos = content.index('print("[DecideX Pack Watchdog] pack stalled for')
    assert pending_pos < trip_pos
    assert trip_pos < print_pos

    # The v131 0.8s tripwire stays (non-pending path: factory ran but
    # produced no area, or COMPLETE=false and nothing was queued).
    assert "if stalled_for < 0.8 then return false end" in content

    # Observability: the stall print now carries factory_seen.
    assert 'factory_seen=" .. tostring(_G._pack_factory_seen ~= nil)' in content


def test_build_patches_tarot_conversion_guards():
    """v139: the queued tarot/spectral conversion events (card.lua use_card
    chain) index G.hand.highlighted[i] at fire time; the bridge's combat
    actions can remove highlighted cards within the 0.4s conversion
    window, crashing each event ("attempt to index field '?'", 20 per
    40min soak) and PARTIALLY applying the tarot. build.py now guards all
    six queued event bodies so a vanished card is skipped and the chain
    still lands.
    """
    vanilla = ROOT / ".pytest_tmp" / "vanilla" / "card.lua"
    content = vanilla.read_text(encoding="utf-8")
    patched = build_mod.patch_card_conversion_guards(content)
    assert patched != content
    # 4 guarded single-line conversions (flip / suit_conv / set_ability /
    # tarot2 flip).
    assert patched.count("if G.hand.highlighted[i] then") == 4
    # Death copy guard + Strength rank-up guard.
    assert ("if G.hand.highlighted[i] and G.hand.highlighted[i] ~= rightmost"
            ) in patched
    assert "if not card then return true end" in patched
