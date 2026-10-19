#!/usr/bin/env python3
"""
DecideX Balatro WebAssembly Local Server
========================================
Serves the tools/balatro_wasm/web directory with correct MIME types for
WebAssembly (.wasm), .love game bundles, and cross-origin security headers.

Usage:
  python serve.py [--port 8092] [--host 127.0.0.1] [--check]
"""

from __future__ import annotations

import argparse
import http.server
import mimetypes
import os
import socketserver
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"


class WasmHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        # Enable Cross-Origin Isolation for high-resolution timers / WASM threads if needed
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def translate_path(self, path):
        # Alias CDN-cacheable paths back to disk files
        clean_path = path.split("?", 1)[0]
        if clean_path.endswith("/balatro.love.zip"):
            return str(WEB_DIR / "balatro.love")
        if clean_path.endswith("/love.wasm.bin"):
            return str(WEB_DIR / "love.wasm")
        return super().translate_path(path)

    def guess_type(self, path):
        mimetypes.add_type("application/wasm", ".wasm")
        mimetypes.add_type("application/wasm", ".bin")
        mimetypes.add_type("application/octet-stream", ".love")
        mimetypes.add_type("application/octet-stream", ".zip")
        mimetypes.add_type("application/javascript", ".js")
        mimetypes.add_type("application/json", ".json")
        return super().guess_type(path)

    def send_head(self):
        # A previous HEAD request may have set this without copying a body.
        self.__dict__.pop("_range_remaining", None)
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            file_size = os.path.getsize(path)
            file_obj = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        range_header = self.headers.get("Range", "")
        if not range_header.startswith("bytes=") or "," in range_header:
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Length", str(file_size))
            self.send_header("Last-Modified", self.date_time_string(os.path.getmtime(path)))
            self.end_headers()
            return file_obj

        try:
            spec = range_header[6:].strip()
            first, last = spec.split("-", 1)
            if first:
                start = int(first)
                end = min(int(last), file_size - 1) if last else file_size - 1
            else:
                suffix = int(last)
                if suffix <= 0:
                    raise ValueError
                start = max(file_size - suffix, 0)
                end = file_size - 1
            if start < 0 or start >= file_size or end < start:
                raise ValueError
        except (ValueError, TypeError):
            file_obj.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        file_obj.seek(start)
        self._range_remaining = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(self._range_remaining))
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(os.path.getmtime(path)))
        self.end_headers()
        return file_obj

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_range_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        try:
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        finally:
            del self._range_remaining


def check_assets(web_dir: Path = WEB_DIR) -> bool:
    """Check repository-controlled frontend assets, not proprietary game files."""
    required = {
        "index.html": 500,
        "style.css": 500,
        "app.js": 1000,
        "viz.js": 1000,
        "viz.css": 500,
        "love.js": 100000,
    }

    all_ok = True
    print(f"[*] Checking Balatro WASM web directory: {web_dir}")
    for fname, min_size in required.items():
        p = web_dir / fname
        if not p.exists():
            print(f"  [-] MISSING: {fname}")
            all_ok = False
        else:
            sz = p.stat().st_size
            if sz < min_size:
                print(f"  [-] TOO SMALL: {fname} ({sz} bytes < {min_size})")
                all_ok = False
            else:
                print(f"  [+] OK: {fname} ({sz / (1024*1024):.2f} MB)")

    return all_ok


def run_server(host: str = "127.0.0.1", port: int = 8092):
    if not check_assets():
        print("[-] Some required web assets are missing. Run build.py first.", file=sys.stderr)
        return 1

    handler = WasmHTTPRequestHandler
    with socketserver.TCPServer((host, port), handler) as httpd:
        print(f"\n[+] DecideX Balatro Web server running at:")
        print(f"    -> Local:   http://localhost:{port}")
        print(f"    -> Network: http://127.0.0.1:{port}")
        print("    Press Ctrl+C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[*] Server stopped.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Serve Balatro WebAssembly build")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8092, help="Bind port (default: 8092)")
    parser.add_argument("--check", action="store_true", help="Only check asset integrity and exit")
    args = parser.parse_args()

    if args.check:
        ok = check_assets()
        sys.exit(0 if ok else 1)

    sys.exit(run_server(args.host, args.port))


if __name__ == "__main__":
    main()
