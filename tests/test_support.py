"""Distribution/runtime path checks; no real user profile is modified."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_support import app_paths, preference_path


class SupportTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=runtime)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        assert self.base.is_relative_to(runtime.resolve())

    def test_frozen_windows_data_never_uses_extraction_directory(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)}, clear=True),
        ):
            paths = app_paths()
        self.assertEqual(paths.backups, self.base / "CodexImageCleaner" / "backups")
        self.assertEqual(paths.runtime.parent, self.base / "CodexImageCleaner")

    def test_explicit_data_directory_overrides_source_and_frozen_defaults(self):
        with patch.dict(os.environ, {"CODEX_IMAGE_CLEANER_DATA_DIR": str(self.base)}, clear=True):
            paths = app_paths()
        self.assertEqual(paths.backups, self.base / "backups")

    def test_frozen_macos_data_uses_application_support(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "platform", "darwin"),
            patch("app_support.Path.home", return_value=self.base),
            patch.dict(os.environ, {}, clear=True),
        ):
            paths = app_paths()
        self.assertEqual(
            paths.runtime.parent, self.base / "Library/Application Support/CodexImageCleaner"
        )
        self.assertEqual(paths.backups.parent, paths.runtime.parent)

    def test_invalid_preferences_fall_back_to_a_valid_path(self):
        for value in (None, [], {}, 12, "", "bad\x00path"):
            self.assertEqual(preference_path(value, self.base), self.base)

    def test_macos_prefers_an_installed_app_browser(self):
        import webui

        expected = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        with (
            patch.object(sys, "platform", "darwin"),
            patch("webui.Path.is_file", lambda path: path == expected),
        ):
            self.assertEqual(webui.edge_executable(), expected)

    def test_missing_app_browser_uses_the_default_browser(self):
        import webui

        with (
            patch("webui.edge_executable", return_value=None),
            patch("webui.webbrowser.open", return_value=True) as open_browser,
        ):
            webui.open_window("http://127.0.0.1:12345/", self.base / "browser")
        open_browser.assert_called_once_with("http://127.0.0.1:12345/")

    def test_release_allowlist_does_not_collect_private_state(self):
        spec = importlib.util.spec_from_file_location(
            "release_builder", Path(__file__).resolve().parents[1] / "packaging/build_windows.py"
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        root = Path(__file__).resolve().parents[1]
        files = [str(p.relative_to(root)).replace("\\", "/") for p in builder.source_files()]
        self.assertTrue(files)
        self.assertFalse(
            any(p.startswith((".runtime/", "backups/", ".venv", "build/", "dist/")) for p in files)
        )
        self.assertFalse(any(p.endswith((".jsonl", ".sqlite", ".log")) for p in files))

    def test_self_test_uses_only_synthetic_session_data(self):
        from app import self_test

        output = self.base / "diagnostic.json"
        from desktop import scan as actual_scan

        def isolated_scan(home, *args, **kwargs):
            self.assertTrue(Path(home).resolve().is_relative_to(self.base))
            return actual_scan(home, *args, **kwargs)

        with patch("desktop.scan", side_effect=isolated_scan):
            self_test(output)
        self.assertEqual(json.loads(output.read_bytes())["status"], "ok")

    def test_browser_host_failure_is_reported_without_waiting_for_timeout(self):
        from app import main

        output = self.base / "browser-failed.json"
        with (
            patch.object(sys, "argv", ["app.py", "--browser-self-test", str(output)]),
            patch(
                "runtime_check.browser_host", side_effect=RuntimeError("synthetic startup failure")
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic startup failure"),
        ):
            main()
        self.assertEqual(
            json.loads(output.read_bytes()),
            {"status": "error", "error": "synthetic startup failure"},
        )

    def test_startup_error_page_escapes_messages_and_uses_local_storage(self):
        from app import show_startup_error
        from app_support import AppPaths

        paths = AppPaths(self.base / "profile", self.base / "backups")
        with patch("app.app_paths", return_value=paths), patch("app.webbrowser.open") as open_page:
            show_startup_error("<script>alert('test')</script>")
        page = paths.runtime / "startup-error.html"
        self.assertNotIn("<script>", page.read_text(encoding="utf-8"))
        self.assertIn("&lt;script&gt;", page.read_text(encoding="utf-8"))
        open_page.assert_called_once_with(page.as_uri())


if __name__ == "__main__":
    unittest.main()
