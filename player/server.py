#!/usr/bin/env python3
"""LOSSLOSS — local hi-res player. ffmpeg decodes, AudioToolbox plays."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from engine import Engine

ROOT = Path(__file__).resolve().parent.parent
UI = Path(__file__).resolve().parent / "ui"
SOURCE = ROOT / "mac" / "window.swift"
BINARY = ROOT / "mac" / "build" / "LOSSLOSS"

ENGINE = Engine()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(ENGINE.snapshot())
            return
        if path.startswith("/art/"):
            self._art(path)
            return
        if path in ("/", "/index.html"):
            self._file(UI / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self._file(UI / "app.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._file(UI / "app.js", "text/javascript; charset=utf-8")
            return
        if path == "/q.png":
            self._file(UI / "q.png", "image/png")
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/library":
                ENGINE.add_paths([str(item) for item in body.get("paths") or []])
                self._json({"ok": True})
            elif path == "/api/play":
                ENGINE.play_index(int(body.get("index", ENGINE.index if ENGINE.index >= 0 else 0)))
                self._json({"ok": True})
            elif path == "/api/toggle":
                ENGINE.toggle()
                self._json({"ok": True})
            elif path == "/api/pause":
                ENGINE.pause()
                self._json({"ok": True})
            elif path == "/api/stop":
                ENGINE.stop()
                self._json({"ok": True})
            elif path == "/api/next":
                ENGINE.next_track()
                self._json({"ok": True})
            elif path == "/api/prev":
                ENGINE.prev_track()
                self._json({"ok": True})
            elif path == "/api/seek":
                ENGINE.seek(float(body.get("seconds", 0)))
                self._json({"ok": True})
            elif path == "/api/volume":
                ENGINE.set_volume(float(body.get("volume", 1)))
                self._json({"ok": True})
            elif path == "/api/device":
                ENGINE.set_device(str(body.get("uid") or ""))
                self._json({"ok": True})
            elif path == "/api/match":
                ENGINE.set_match(bool(body.get("enabled")))
                self._json({"ok": True})
            elif path == "/api/remove":
                ENGINE.remove(int(body.get("index", -1)))
                self._json({"ok": True})
            elif path == "/api/move":
                ENGINE.move(int(body.get("from", -1)), int(body.get("to", -1)))
                self._json({"ok": True})
            elif path == "/api/clear":
                ENGINE.clear()
                self._json({"ok": True})
            elif path == "/api/pick":
                paths = pick(str(body.get("kind") or "files"))
                if paths:
                    ENGINE.add_paths(paths)
                self._json({"ok": True, "paths": paths})
            else:
                self._json({"error": "not found"}, 404)
        except (TypeError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)

    def _art(self, path: str) -> None:
        token = unquote(path.removeprefix("/art/")).split(".", 1)[0]
        if not token.isdigit():
            self._json({"error": "not found"}, 404)
            return
        data = ENGINE.art(int(token))
        if not data:
            self.send_error(404)
            return
        self._bytes(data, "image/jpeg")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode())
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode()
        self._bytes(data, "application/json; charset=utf-8", status)

    def _file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self._bytes(data, content_type)

    def _bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def pick(kind: str) -> list[str]:
    if kind == "folder":
        script = 'POSIX path of (choose folder with prompt "Open a folder")'
    else:
        script = """
set picked to choose file with prompt "Open audio" with multiple selections allowed
set output to ""
repeat with f in picked
    set output to output & POSIX path of f & linefeed
end repeat
output
"""
    try:
        result = subprocess.run(
            ["osascript", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def build_window() -> bool:
    if not SOURCE.exists():
        return False
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    fresh = BINARY.exists() and BINARY.stat().st_mtime >= SOURCE.stat().st_mtime
    if fresh:
        return True
    compiler = subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True, text=True)
    swiftc = compiler.stdout.strip() if compiler.returncode == 0 else ""
    if not swiftc:
        print("swiftc is not available, opening in the browser instead.", flush=True)
        return False
    print("Building the LOSSLOSS window…", flush=True)
    built = subprocess.run(
        [swiftc, "-O", "-framework", "Cocoa", "-framework", "WebKit", "-o", str(BINARY), str(SOURCE)],
        check=False,
    )
    if built.returncode != 0:
        print("The native window failed to build. Opening in the browser.", flush=True)
        return False
    return True


def _on_term(_signum, _frame) -> None:
    raise SystemExit


def main() -> None:
    signal.signal(signal.SIGTERM, _on_term)
    parser = argparse.ArgumentParser(description="LOSSLOSS hi-res player")
    parser.add_argument("paths", nargs="*", help="audio files or folders to load")
    parser.add_argument("--browser", action="store_true", help="open in the browser")
    parser.add_argument("--no-open", action="store_true", help="serve only, do not open a window")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/?shell=1"
    plain = f"http://{host}:{port}/"
    print(f"LOSSLOSS  {plain}", flush=True)
    if args.paths:
        ENGINE.add_paths(args.paths)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    window = None
    try:
        if args.no_open:
            thread.join()
            return
        if not args.browser and build_window():
            window = subprocess.Popen([str(BINARY), url])
            window.wait()
            return
        subprocess.Popen(["open", plain])
        print("Close this terminal to quit and restore the device clock.", flush=True)
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        if window is not None and window.poll() is None:
            window.terminate()
        ENGINE.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
