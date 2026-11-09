"""Lock and app lifecycle checks never close the developer's real desktop."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cleaner import CleanupError, SessionBusy
from codex_process import CodexLifecycle, desktop_processes, lock_status, windows_desktop


class LifecycleTests(unittest.TestCase):
    def test_native_codex_package_is_recognized_and_restarted_through_activation(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "WindowsApps/OpenAI.Codex_1.0_x64__publisher"
            (package / "app").mkdir(parents=True)
            executable = package / "app/ChatGPT.exe"
            (package / "AppxManifest.xml").write_text(
                '<Package xmlns="urn:test"><Identity Name="OpenAI.Codex"/>'
                '<Applications><Application Id="App"/></Applications></Package>',
                encoding="utf-8",
            )
            self.assertTrue(windows_desktop(executable))
            self.assertFalse(windows_desktop(package / "app/other.exe"))
            with (
                patch("codex_process.sys.platform", "win32"),
                patch("codex_process.desktop_processes", return_value=[]),
                patch("codex_process.subprocess.Popen") as launch,
            ):
                lifecycle = CodexLifecycle()
                lifecycle.targets = [(1, str(executable))]
                lifecycle.restart()
                self.assertEqual(
                    launch.call_args.args[0],
                    ["explorer.exe", "shell:AppsFolder\\OpenAI.Codex_publisher!App"],
                )

    def test_stale_lock_file_is_not_treated_as_occupied(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            locks = home / "thread-writer-locks"
            locks.mkdir()
            (locks / ".coordination.lock").touch()
            (locks / "example.lock").touch()
            with patch("codex_process.desktop_processes") as processes:
                self.assertFalse(lock_status(home, ["example"])["requires_close"])
                processes.assert_not_called()

    def test_held_lock_offers_close_only_for_identified_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            locks = home / "thread-writer-locks"
            locks.mkdir()
            (locks / ".coordination.lock").touch()
            with (
                patch("codex_process.file_lock", side_effect=SessionBusy("会话锁")),
                patch("codex_process.desktop_processes", return_value=[(1, "desktop")]),
            ):
                status = lock_status(home, [])
            self.assertTrue(status["requires_close"])
            self.assertTrue(status["can_close"])

    def test_no_desktop_does_not_attempt_to_close_cli(self):
        with (
            patch("codex_process.desktop_processes", return_value=[]),
            patch("codex_process.run") as run,
        ):
            lifecycle = CodexLifecycle()
            with self.assertRaises(CleanupError):
                lifecycle.close()
            lifecycle.restart()
            run.assert_not_called()

    def test_windows_cli_without_electron_resources_is_excluded(self):
        with (
            patch("codex_process.sys.platform", "win32"),
            patch("codex_process.run", return_value='[{"Id":123,"Path":"C:/tools/codex.exe"}]'),
            patch("codex_process.Path.is_file", return_value=False),
        ):
            self.assertEqual(desktop_processes(), [])

    def test_windows_waits_for_exit_and_restarts_captured_executable(self):
        executable = str(Path("C:/apps/Codex.exe"))
        target = (123, executable)
        with (
            patch("codex_process.sys.platform", "win32"),
            patch("codex_process.desktop_processes", side_effect=[[target], [target], [], []]),
            patch("codex_process.run") as run,
            patch("codex_process.subprocess.Popen") as launch,
        ):
            lifecycle = CodexLifecycle()
            lifecycle.close()
            self.assertIn("CloseMainWindow", run.call_args.args[0][-1])
            self.assertIn("WaitForExit", run.call_args.args[0][-1])
            lifecycle.restart()
            self.assertEqual(launch.call_args.args[0], [executable])

    def test_restart_does_not_duplicate_a_desktop_that_cancelled_exit(self):
        with (
            patch("codex_process.desktop_processes", return_value=[(1, "desktop")]),
            patch("codex_process.subprocess.Popen") as launch,
        ):
            lifecycle = CodexLifecycle()
            lifecycle.targets = [(1, "desktop")]
            lifecycle.restart()
            launch.assert_not_called()

    def test_mac_reopens_the_captured_app_bundle(self):
        with (
            patch("codex_process.sys.platform", "darwin"),
            patch("codex_process.desktop_processes", return_value=[]),
            patch("codex_process.run") as run,
        ):
            lifecycle = CodexLifecycle()
            lifecycle.targets = [(1, "/Applications/Codex.app/Contents/MacOS/Codex")]
            lifecycle.restart()
            run.assert_called_once_with(["open", "-a", "/Applications/Codex.app"])
