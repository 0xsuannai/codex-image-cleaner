"""Gracefully close and reopen the desktop app, never kill CLI/IDE processes."""

import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path, PurePosixPath

from cleaner import CleanupError, SessionBusy, file_lock


def windows_package(executable):
    """Identify the native Store desktop separately from ChatGPT and Codex CLI."""
    path = Path(executable)
    if path.name.lower() != "chatgpt.exe" or path.parent.name.lower() != "app":
        return None
    package = path.parent.parent
    if (
        package.parent.name.lower() != "windowsapps"
        or not package.name.startswith("OpenAI.Codex_")
        or "__" not in package.name
    ):
        return None
    manifest = package / "AppxManifest.xml"
    root = ET.fromstring(manifest.read_bytes())
    identity = next((node for node in root if node.tag.endswith("}Identity")), None)
    application = next((node for node in root.iter() if node.tag.endswith("}Application")), None)
    if (
        identity is None
        or identity.get("Name") != "OpenAI.Codex"
        or application is None
        or not application.get("Id")
    ):
        return None
    family = "OpenAI.Codex_" + package.name.rsplit("__", 1)[1]
    return family + "!" + application.get("Id")


def windows_desktop(executable):
    path = Path(executable)
    try:
        return bool(windows_package(path)) or (
            path.name.lower() == "codex.exe" and (path.parent / "resources" / "app.asar").is_file()
        )
    except (OSError, ValueError, ET.ParseError):
        return False


def run(arguments, *, timeout=20):
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ).stdout


def desktop_processes():
    """Only return identifiable Electron desktop instances with a restart path."""
    if sys.platform == "win32":
        output = run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
                "@(Get-Process -Name Codex,ChatGPT -ErrorAction SilentlyContinue | "
                "Where-Object { $_.MainWindowHandle -ne 0 } | "
                "Select-Object Id,Path) | ConvertTo-Json -Compress",
            ]
        )
        rows = json.loads(output) if output.strip() else []
        if isinstance(rows, dict):
            rows = [rows]
        return [
            (int(row["Id"]), str(Path(row["Path"]).resolve()))
            for row in rows
            if row.get("Path") and windows_desktop(row["Path"])
        ]
    if sys.platform == "darwin":
        # ps is read-only and does not launch an installed but stopped application.
        rows = run(["ps", "-axo", "pid=,comm="]).splitlines()
        result = []
        for row in rows:
            parts = row.strip().split(None, 1)
            if len(parts) == 2 and (
                parts[1].endswith(".app/Contents/MacOS/Codex")
                or parts[1].endswith("/Codex.app/Contents/MacOS/ChatGPT")
            ):
                result.append((int(parts[0]), parts[1]))
        return result
    return []


def lock_status(home, thread_ids):
    """Probe actual OS locks, not the existence of a stale lock file."""
    directory = home / "thread-writer-locks"
    coordination = directory / ".coordination.lock"
    reason = ""
    try:
        if not coordination.is_file():
            reason = "未检测到兼容的会话锁。请关闭 Codex 后清理。"
        else:
            with file_lock(coordination, "Codex 正在切换会话，请关闭 Codex 后清理。"):
                for tid in thread_ids:
                    path = directory / f"{tid}.lock"
                    if path.is_file():
                        with file_lock(path, "检测到会话锁，请关闭 Codex 后清理。"):
                            pass
    except (SessionBusy, OSError) as exc:
        reason = str(exc)
    # Process enumeration is only necessary when offering to close the app.
    can_close = False
    if reason:
        with suppress(OSError, ValueError, subprocess.SubprocessError):
            can_close = bool(desktop_processes())
    return {"requires_close": bool(reason), "reason": reason, "can_close": can_close}


class CodexLifecycle:
    def __init__(self):
        self.targets = []

    def close(self):
        self.targets = desktop_processes()
        if not self.targets:
            raise CleanupError("没有找到可自动重启的 Codex 桌面进程，请手动退出后重试。")
        if sys.platform == "win32":
            for pid, executable in self.targets:
                # A second inventory prevents closing a reused PID or a different app.
                if (pid, executable) not in desktop_processes():
                    continue
                run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                        "if ($p) { if (-not $p.CloseMainWindow()) { exit 2 }; "
                        "if (-not $p.WaitForExit(15000)) { exit 3 } }",
                    ]
                )
        elif sys.platform == "darwin":
            run(["osascript", "-e", 'tell application id "com.openai.codex" to quit'])
        deadline = time.monotonic() + 15
        while any(target in desktop_processes() for target in self.targets):
            if time.monotonic() >= deadline:
                raise CleanupError("Codex 尚未退出，未执行清理。请处理退出提示后重试。")
            time.sleep(0.25)

    def restart(self):
        running = {path for _, path in desktop_processes()} if self.targets else set()
        for executable in dict.fromkeys(path for _, path in self.targets):
            if executable in running:
                continue
            if sys.platform == "win32":
                package = windows_package(executable)
                if package:
                    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + package])
                else:
                    subprocess.Popen([executable], cwd=str(Path(executable).parent))
            elif sys.platform == "darwin":
                run(["open", "-a", str(PurePosixPath(executable).parents[2])])
