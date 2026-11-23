"""Tests for tools/ci/production_verify.sh (CI P2-1).

Spin up a local http.server stubbing the production endpoints the script
probes (/api/health, /play, /balatro.love.zip?v=N, /love.wasm.bin?v=N),
then run the real shell script via subprocess and assert exit codes for:

  1. all checks pass            -> exit 0
  2. /play advertises wrong ver -> exit != 0
  3. Content-Length mismatch    -> exit != 0
  4. sha256 mismatch            -> exit != 0

The fixture writes a minimal app.js version-triple source and a minimal
release-assets.json, and points the script at them via
PRODUCTION_VERIFY_APPJS / PRODUCTION_VERIFY_MANIFEST so the repo copies
are never touched.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "ci" / "production_verify.sh"

LOVE_BYTES = b"LOVE-PAYLOAD-" + os.urandom(32)
WASM_BYTES = b"WASM-PAYLOAD-" + os.urandom(32)
LOVE_SIZE = str(len(LOVE_BYTES))
WASM_SIZE = str(len(WASM_BYTES))
VERSION = "v42"

APP_JS = f"""\
  const EXPECTED_LOVE_SIZE = {LOVE_SIZE};
  const EXPECTED_WASM_SIZE = {WASM_SIZE};
  const ASSET_VERSION = "{VERSION}";
  const ASSET_LOVE_URL = "balatro.love.zip?v=42";
  const ASSET_WASM_URL = "love.wasm.bin?v=42";
"""

HEALTH_OK = json.dumps({"ok": True, "wasm_available": True}).encode()
PLAY_OK = f'<html><script src="love.js?v=42"></script><script src="app.js?v=42"></script></html>'.encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StubSite:
    """Configurable single-threaded stub of the production endpoints."""

    def __init__(self, handler_factory):
        self._handler_factory = handler_factory
        self._httpd = None
        self._thread = None

    def start(self, state):
        handler = self._handler_factory(state)
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{port}"

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


def make_handler(state):
    """state keys: health, play, love, wasm (bytes; None -> 404)."""

    class Handler(http.server.BaseHTTPRequestHandler):

        def _asset(self, body):
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                if self.command != "HEAD":
                    self.rfile.write(b"")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, body):
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                if self.command != "HEAD":
                    self.rfile.write(b"")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                self._text(state["health"])
            elif path == "/play":
                self._text(state["play"])
            elif path == "/balatro.love.zip":
                self._asset(state["love"])
            elif path == "/love.wasm.bin":
                self._asset(state["wasm"])
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_HEAD(self):
            # Reuse GET routing; only headers are emitted.
            self.do_GET()

        def log_message(self, *args):  # keep pytest output clean
            pass

    return Handler


@pytest.fixture()
def stub_site():
    site = StubSite(make_handler)
    yield site
    site.stop()


def run_script(base: str, tmp_path: Path, extra_env: dict | None = None):
    env = os.environ.copy()
    env.update(extra_env or {})
    # On Windows a bare "bash" resolves to C:\Windows\System32\bash.exe
    # (the WSL stub, which exits 1 with a UTF-16 banner) because
    # CreateProcess searches System32 before PATH. Always pass the
    # absolute path of the Git Bash / coreutils bash.
    bash = shutil.which("bash")
    if bash:
        bash = os.path.abspath(bash)
        if Path(bash).resolve().as_posix().lower().endswith("/windows/system32/bash.exe"):
            bash = None
    if bash is None:
        pytest.skip("no usable bash on PATH (sha256sum-based verify needs Git Bash/coreutils)")
    return subprocess.run(
        [bash, str(SCRIPT), base],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )


@pytest.fixture()
def bundle(tmp_path):
    """Write minimal app.js + release-assets.json matching the good assets."""
    app_js = tmp_path / "app.js"
    app_js.write_text(APP_JS, encoding="utf-8")
    manifest = tmp_path / "release-assets.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "1.0.0",
                "source": "test fixture",
                "assets": [
                    {
                        "filename": "balatro.love",
                        "source": "artifact://test",
                        "min_size": 1,
                        "sha256": sha256_hex(LOVE_BYTES),
                    },
                    {
                        "filename": "love.wasm",
                        "source": "artifact://test",
                        "min_size": 1,
                        "sha256": sha256_hex(WASM_BYTES),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "env": {
            "PRODUCTION_VERIFY_APPJS": str(app_js),
            "PRODUCTION_VERIFY_MANIFEST": str(manifest),
        }
    }


GOOD_STATE = {
    "health": HEALTH_OK,
    "play": PLAY_OK,
    "love": LOVE_BYTES,
    "wasm": WASM_BYTES,
}


def test_all_checks_pass(stub_site, bundle, tmp_path):
    base = stub_site.start(dict(GOOD_STATE))
    result = run_script(base, tmp_path, bundle["env"])
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "completed successfully" in result.stdout
    assert "asset sha256 match" in result.stdout


def test_version_mismatch_fails(stub_site, bundle, tmp_path):
    state = dict(GOOD_STATE)
    state["play"] = PLAY_OK.replace(b"app.js?v=42", b"app.js?v=41")
    base = stub_site.start(state)
    result = run_script(base, tmp_path, bundle["env"])
    assert result.returncode != 0
    assert "expected app.js?v=42" in result.stdout + result.stderr


def test_size_mismatch_fails(stub_site, bundle, tmp_path):
    state = dict(GOOD_STATE)
    # Serve a truncated love payload so HEAD Content-Length disagrees
    # with EXPECTED_LOVE_SIZE in the fixture app.js.
    state["love"] = LOVE_BYTES[:-8]
    base = stub_site.start(state)
    result = run_script(base, tmp_path, bundle["env"])
    assert result.returncode != 0
    assert "expected" in result.stdout + result.stderr


def test_sha_mismatch_fails(stub_site, bundle, tmp_path):
    state = dict(GOOD_STATE)
    # Same Content-Length as the good payload, different bytes: only the
    # full-download sha256 check can catch this.
    bad_wasm = WASM_BYTES[:-1] + (b"X" if WASM_BYTES[-1:] != b"X" else b"Y")
    assert len(bad_wasm) == len(WASM_BYTES) and bad_wasm != WASM_BYTES
    state["wasm"] = bad_wasm
    base = stub_site.start(state)
    result = run_script(base, tmp_path, bundle["env"])
    assert result.returncode != 0
    assert "sha256" in (result.stdout + result.stderr).lower()


def test_health_not_ok_fails(stub_site, bundle, tmp_path):
    state = dict(GOOD_STATE)
    state["health"] = json.dumps({"ok": False, "wasm_available": True}).encode()
    base = stub_site.start(state)
    result = run_script(base, tmp_path, bundle["env"])
    assert result.returncode != 0
    assert "health" in (result.stdout + result.stderr).lower()


def test_wasm_unavailable_fails(stub_site, bundle, tmp_path):
    state = dict(GOOD_STATE)
    state["health"] = json.dumps({"ok": True, "wasm_available": False}).encode()
    base = stub_site.start(state)
    result = run_script(base, tmp_path, bundle["env"])
    assert result.returncode != 0
    assert "wasm_available" in (result.stdout + result.stderr).lower()


def test_missing_asset_404_fails(stub_site, bundle, tmp_path):
    state = dict(GOOD_STATE)
    state["wasm"] = None  # stub serves 404
    base = stub_site.start(state)
    result = run_script(base, tmp_path, bundle["env"])
    assert result.returncode != 0


def test_manifest_sha_entry_missing_fails(stub_site, bundle, tmp_path):
    """A manifest with no sha256 for love.wasm must fail the verify loudly."""
    state = dict(GOOD_STATE)
    manifest = json.loads(
        Path(bundle["env"]["PRODUCTION_VERIFY_MANIFEST"]).read_text(encoding="utf-8")
    )
    for entry in manifest["assets"]:
        if entry.get("filename") == "love.wasm":
            entry.pop("sha256")
    mpath = tmp_path / "release-assets.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    env = {
        "PRODUCTION_VERIFY_APPJS": bundle["env"]["PRODUCTION_VERIFY_APPJS"],
        "PRODUCTION_VERIFY_MANIFEST": str(mpath),
    }
    base = stub_site.start(state)
    result = run_script(base, tmp_path, env)
    assert result.returncode != 0
