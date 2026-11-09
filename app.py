"""GUI entry point and diagnostics for source and frozen distributions."""

import html
import logging
import platform
import sqlite3
import sys
import tempfile
import webbrowser
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_support import APP_VERSION, app_paths
from instance import AlreadyRunning, SingleInstance

logger = logging.getLogger(__name__)


def configure_logging():
    path = app_paths().runtime
    path.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path / "application.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)


def self_test(report: Path):
    """Check the current web runtime without reading real Codex sessions."""
    from cleaner import clean_session, digest, encode, inspect_session, restore, save_json
    from desktop import Desktop

    result = {
        "version": APP_VERSION,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=report.parent) as scratch:
            scratch_path = Path(scratch).resolve()
            if not scratch_path.is_relative_to(report.parent.resolve()):
                raise RuntimeError("Diagnostic scratch directory escaped the report location")
            home = scratch_path / "codex"
            sessions = home / "sessions"
            sessions.mkdir(parents=True)
            tid = "11111111-2222-3333-4444-555555555555"
            path = sessions / f"rollout-diagnostic-{tid}.jsonl"
            records = [
                {"type": "session_meta", "payload": {"id": tid}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "synthetic diagnostic"},
                            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                        ],
                    },
                },
            ]
            path.write_bytes(b"".join(encode(r) + b"\n" for r in records))
            from app_support import AppPaths

            desktop = Desktop(
                home,
                paths=AppPaths(scratch_path / "profile", scratch_path / "backups"),
                preferences=False,
            )
            try:
                desktop._scan()
                snapshot = desktop.snapshot()
                if len(snapshot["sessions"]) != 1 or not snapshot["sessions"][0]["eligible"]:
                    raise RuntimeError("Packaged web session listing failed")
            finally:
                desktop.close()
            original = digest(path)
            original_size = path.stat().st_size
            backup = clean_session(home, inspect_session(path), scratch_path / "backups")
            if not backup or inspect_session(path).images:
                raise RuntimeError("Packaged cleanup diagnostic failed")
            if path.stat().st_size != original_size:
                raise RuntimeError("Packaged layout preservation diagnostic failed")
            restore(home, backup)
            if digest(path) != original:
                raise RuntimeError("Packaged restore diagnostic failed")
            with SingleInstance(scratch_path / "desktop"):
                try:
                    with SingleInstance(scratch_path / "desktop"):
                        raise RuntimeError("Packaged instance ownership diagnostic failed")
                except AlreadyRunning:
                    pass
        connection = sqlite3.connect(":memory:")
        try:
            if connection.execute("SELECT 1").fetchone()[0] != 1:
                raise RuntimeError("SQLite diagnostic failed")
        finally:
            connection.close()
        result.update(
            status="ok",
            storage_roundtrip="ok",
            sqlite=sqlite3.sqlite_version,
            instance_ownership="ok",
            layout_preservation="ok",
        )
        from webui import ASSETS, FILES

        if not all((ASSETS / name).read_bytes() for name, _ in FILES.values()):
            raise RuntimeError("Packaged web assets are missing")
        result.update(interface="local_web", web_assets="ok")
    except Exception as exc:
        result.update(status="error", error=str(exc))
        raise
    finally:
        report.parent.mkdir(parents=True, exist_ok=True)
        save_json(report, result)


def show_startup_error(message):
    """Use the existing browser for errors; do not bundle a second GUI toolkit."""
    page = app_paths().runtime / "startup-error.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '<!doctype html><meta charset="utf-8"><title>Codex Image Cleaner</title>'
        '<body style="font:16px/1.7 system-ui;padding:48px;max-width:720px">'
        '<h1>无法启动 Codex Image Cleaner</h1><pre style="white-space:pre-wrap">'
        + html.escape(str(message))
        + "</pre></body>",
        encoding="utf-8",
    )
    webbrowser.open(page.as_uri())


def main():
    if "--browser-self-test" in sys.argv:
        if len(sys.argv) != 3 or sys.argv[1] != "--browser-self-test":
            raise SystemExit("Usage: CodexImageCleaner --browser-self-test <report.json>")
        try:
            from cleaner import save_json

            save_json(Path(sys.argv[2]), {"status": "starting", "stage": "loading_fixture_host"})
            from runtime_check import browser_host

            browser_host(sys.argv[2])
        except Exception as exc:
            from cleaner import save_json

            save_json(Path(sys.argv[2]), {"status": "error", "error": str(exc)})
            raise
        return
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        if len(sys.argv) != index + 2:
            raise SystemExit("Usage: CodexImageCleaner --self-test <report.json>")
        self_test(Path(sys.argv[index + 1]).expanduser().resolve())
        return
    try:
        from cleaner import main as cleaner_main

        if any(arg in sys.argv for arg in ("--scan", "--diagnostics", "--version", "--help", "-h")):
            configure_logging()
            cleaner_main()
        else:
            try:
                with SingleInstance(app_paths().runtime):
                    configure_logging()
                    cleaner_main()
            except AlreadyRunning:
                from webui import reopen

                if not reopen(app_paths().runtime):
                    show_startup_error("清理工具已经在运行，请切换到已有窗口或稍后重新启动。")
    except Exception as exc:
        logger.exception("Application startup failed")
        if sys.stderr is not None:
            print(f"Startup failed: {exc}", file=sys.stderr)
        with suppress(Exception):
            show_startup_error(f"{exc}\n\n诊断日志：{app_paths().runtime / 'application.log'}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
