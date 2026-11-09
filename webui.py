"""Offline HTML desktop: fixed assets and an authenticated loopback-only job API."""

import contextlib
import http.client
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit

from app_support import APP_VERSION
from cleaner import CleanupError, save_json
from desktop import Desktop
from safe_json import loads, read_file

logger = logging.getLogger(__name__)
ASSETS = Path(__file__).resolve().parent / "ui"
FILES = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
MAX_BODY = 128 * 1024


def edge_executable():
    if sys.platform == "win32":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            folder = os.environ.get(env)
            if folder:
                path = Path(folder) / "Microsoft/Edge/Application/msedge.exe"
                if path.is_file():
                    return path
    elif sys.platform == "darwin":
        for folder in (Path("/Applications"), Path.home() / "Applications"):
            for app, binary in (
                ("Google Chrome", "Google Chrome"),
                ("Microsoft Edge", "Microsoft Edge"),
            ):
                path = folder / f"{app}.app" / "Contents/MacOS" / binary
                if path.is_file():
                    return path
    return None


def open_window(url, profile):
    edge = edge_executable()
    if edge:
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                str(edge),
                "--app=" + url,
                "--user-data-dir=" + str(profile),
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--window-size=1120,850",
            ],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return
    if not webbrowser.open(url):
        raise CleanupError("无法打开界面，请安装或启用 Edge、Chrome 等现代浏览器后重试")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def server_bind(self):
        # HTTPServer normally reverse-resolves the bound address. This local-only
        # service needs no DNS and must start even when macOS name lookup stalls.
        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def __init__(self, desktop):
        self.desktop = desktop
        self.token = secrets.token_urlsafe(32)
        self.last_seen = time.monotonic()
        self.seen = False
        self.closing = False
        self.clients = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)
        self.url = self.origin + "/#token=" + self.token

    def process_request(self, request, client_address):
        if not self.clients.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.clients.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.clients.release()

    def expired(self):
        with self.desktop.lock:
            return not self.desktop.busy and time.monotonic() - self.last_seen > (
                90 if self.seen else 120
            )


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexImageCleaner"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, *_):
        # HTTP paths/headers can contain private content; keep them out of logs.
        pass

    def respond(self, status, value, content_type="application/json"):
        raw = (
            json.dumps(value, ensure_ascii=True).encode()
            if content_type == "application/json"
            else value
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(raw)

    def allowed(self, *, api=False):
        hosts = self.headers.get_all("Host", [])
        if hosts != [urlsplit(self.server.origin).netloc]:
            self.respond(403, {"error": "不允许的本机地址"})
            return False
        origin = self.headers.get_all("Origin", [])
        if origin and origin != [self.server.origin]:
            self.respond(403, {"error": "不允许跨站访问"})
            return False
        if api:
            auth = self.headers.get_all("Authorization", [])
            if (
                len(auth) != 1
                or not auth[0].isascii()
                or not secrets.compare_digest(auth[0], "Bearer " + self.server.token)
            ):
                self.respond(401, {"error": "连接已失效，请重新启动工具"})
                return False
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                self.respond(403, {"error": "不允许跨站访问"})
                return False
        return True

    def do_GET(self):
        path = urlsplit(self.path)
        api = path.path.startswith("/api/")
        if not self.allowed(api=api):
            return
        try:
            if path.path in FILES:
                name, mime = FILES[path.path]
                self.respond(200, (ASSETS / name).read_bytes(), mime)
            elif path.path == "/api/state":
                self.server.last_seen = time.monotonic()
                self.server.seen = True
                self.respond(
                    200,
                    self.server.desktop.snapshot(parse_qs(path.query).get("revision", [None])[0]),
                )
            elif path.path == "/api/ping":
                self.server.last_seen = time.monotonic()
                self.server.seen = True
                self.respond(200, {"version": APP_VERSION})
            elif path.path == "/api/backups":
                self.respond(200, {"backups": self.server.desktop.list_backups()})
            else:
                self.respond(404, {"error": "未找到内容"})
        except (OSError, ValueError, CleanupError) as exc:
            self.respond(400, {"error": str(exc)})

    def do_POST(self):
        if not self.allowed(api=True):
            return
        if self.headers.get_all("Origin", []) != [self.server.origin]:
            self.respond(403, {"error": "请求缺少本机来源"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (
                len(lengths) != 1
                or not lengths[0].isascii()
                or not lengths[0].isdecimal()
                or self.headers.get("Transfer-Encoding")
            ):
                raise ValueError("不支持的请求长度")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                self.respond(413, {"error": "请求过大"})
                return
            if self.headers.get_content_type() != "application/json":
                self.respond(415, {"error": "只接受 JSON 请求"})
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求不完整")
            body = loads(raw)
            if not isinstance(body, dict):
                raise ValueError("请求格式错误")
            desktop = self.server.desktop
            path = urlsplit(self.path).path
            result = {"ok": True}
            if path == "/api/scan":
                desktop.refresh()
            elif path == "/api/stop":
                desktop.stop()
            elif path == "/api/prepare":
                result = desktop.prepare(body)
            elif path == "/api/execute":
                desktop.execute(body)
            elif path == "/api/settings":
                desktop.settings(body)
            elif path == "/api/diagnostics":
                from diagnostics import collect

                result = collect(desktop.home)
            elif path == "/api/shutdown":
                with desktop.lock:
                    if desktop.busy:
                        raise CleanupError("正在处理会话，请等待完成后退出")
                    self.server.closing = True
            else:
                self.respond(404, {"error": "未找到操作"})
                return
            self.respond(200, result)
        except (CleanupError, OSError, ValueError, RecursionError, TypeError) as exc:
            self.respond(400, {"error": str(exc)})
        except Exception:
            logger.exception("Local UI request failed")
            self.respond(500, {"error": "操作未完成，请稍后重试或导出诊断"})


def reopen(profile):
    """Reopen a live authenticated local UI; never navigate to an arbitrary saved URL."""
    try:
        state = read_file(profile / "web-session.json", limit=4096)
        port, token = state["port"], state["token"]
        if (
            type(port) is not int
            or not 1 <= port <= 65535
            or not isinstance(token, str)
            or len(token) != 43
        ):
            return False
        with contextlib.closing(
            http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        ) as connection:
            connection.request("GET", "/api/ping", headers={"Authorization": "Bearer " + token})
            response = connection.getresponse()
            payload = response.read(4096)
            if response.status != 200 or loads(payload).get("version") != APP_VERSION:
                return False
        open_window(f"http://127.0.0.1:{port}/#token={token}", profile / "browser")
        return True
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return False


def serve(desktop, *, launch_browser=True, on_ready=None):
    desktop.paths.runtime.mkdir(parents=True, exist_ok=True)
    session_file = desktop.paths.runtime / "web-session.json"
    with LocalServer(desktop) as server:
        server.timeout = 0.25
        save_json(session_file, {"port": server.server_port, "token": server.token})
        try:
            if on_ready:
                on_ready(server)
            if launch_browser:
                open_window(server.url, desktop.paths.runtime / "browser")
            while not server.closing and not server.expired():
                server.handle_request()
        finally:
            desktop.close()
            session_file.unlink(missing_ok=True)


def launch(home):
    desktop = Desktop(home)
    desktop.refresh()
    serve(desktop)
