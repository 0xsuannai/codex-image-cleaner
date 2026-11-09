"""Constructed-data browser acceptance host. Never opens the user's Codex home."""

import os
import tempfile
import time
from pathlib import Path

from app_support import APP_VERSION, AppPaths
from cleaner import digest, encode, save_json
from desktop import Desktop
from webui import serve


class BrowserAcceptanceDesktop(Desktop):
    def _scan(self):
        # Only this constructed-data host holds a refresh long enough for the
        # browser to observe actual busy feedback. Normal Desktop has no delay.
        if self.busy == "scan":
            time.sleep(4)
        return super()._scan()


def browser_host(report):
    report = Path(report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    save_json(report, {"status": "starting", "stage": "creating_synthetic_data"})
    with tempfile.TemporaryDirectory(dir=report.parent) as folder:
        root = Path(folder)
        sessions = root / "codex/sessions"
        sessions.mkdir(parents=True)
        original = {}
        titles = [
            "制作一段产品介绍视频",
            "根据截图调整网页首页",
            '整理项目文档 <img src=x onerror="window.injected=true">',
        ]
        for index, title in enumerate(titles):
            tid = f"11111111-2222-3333-4444-{index + 1:012d}"
            path = sessions / f"rollout-browser-{tid}.jsonl"
            content = [{"type": "input_text", "text": title}]
            if index < 2:
                content.append(
                    {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 8192}
                )
            records = [
                {"type": "session_meta", "payload": {"id": tid, "cwd": "D:/example-project"}},
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": content},
                },
            ]
            path.write_bytes(b"".join(encode(record) + b"\n" for record in records))
            os.utime(path, (time.time() - index * 3600,) * 2)
            original[path] = digest(path)
        desktop = BrowserAcceptanceDesktop(
            root / "codex", paths=AppPaths(root / "profile", root / "backups"), preferences=False
        )
        save_json(report, {"status": "starting", "stage": "scanning_synthetic_data"})
        desktop._scan()
        save_json(report, {"status": "starting", "stage": "opening_local_server"})
        serve(
            desktop,
            launch_browser=False,
            on_ready=lambda server: save_json(report, {"url": server.url}),
        )
        if any(digest(path) != value for path, value in original.items()):
            raise RuntimeError("Browser acceptance did not restore synthetic data exactly")
        if not desktop.report or not desktop.report.get("completed"):
            raise RuntimeError("Browser acceptance did not complete a synthetic roundtrip")
    save_json(report, {"status": "ok", "version": APP_VERSION, "synthetic_roundtrip": "ok"})
