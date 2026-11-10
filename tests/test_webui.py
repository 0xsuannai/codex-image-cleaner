"""Local transport and real job roundtrips, using constructed conversations only."""

import http.client
import json
import os
import subprocess
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_cleaner as fixtures

import cleaner
from app_support import AppPaths
from desktop import Desktop
from journal import marker, marker_path
from webui import MAX_BODY, LocalServer, reopen


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CleanerTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.desktop = Desktop(
            self.fixture.home,
            paths=AppPaths(self.fixture.root / "profile", self.fixture.backups),
            preferences=False,
        )
        self.desktop._scan()
        self.lock_probe = patch(
            "desktop.lock_status",
            return_value={
                "requires_close": False,
                "reason": "",
                "can_close": False,
            },
        )
        self.lock_probe.start()
        self.addCleanup(self.lock_probe.stop)
        self.server = LocalServer(self.desktop)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.desktop.close()

    def request(self, method, path, body=None, *, authorized=True, headers=None, raw=None):
        fields = {"Authorization": "Bearer " + self.server.token} if authorized else {}
        if method == "POST":
            fields.update({"Origin": self.server.origin, "Content-Type": "application/json"})
        fields.update(headers or {})
        payload = (
            raw if raw is not None else json.dumps(body).encode() if body is not None else None
        )
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=payload, headers=fields)
            response = connection.getresponse()
            data = response.read()
            value = (
                json.loads(data)
                if response.getheader("Content-Type", "").startswith("application/json")
                else data
            )
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def finish(self):
        self.desktop.worker.join(timeout=10)
        self.assertFalse(self.desktop.worker.is_alive())
        self.assertEqual(self.desktop.busy, "")

    def prepare(self, kind="clean", **kwargs):
        body = {"kind": kind, **kwargs}
        if kind == "clean":
            body["keys"] = list(self.desktop.items)
        status, result, _ = self.request("POST", "/api/prepare", body)
        self.assertEqual(status, 200, result)
        return {"confirmation": result["confirmation"], "acknowledged": True, "mode": "offline"}

    def test_no_conversation_state_without_launch_token(self):
        status, body, _ = self.request("GET", "/api/state", authorized=False)
        self.assertEqual(status, 401)
        self.assertNotIn("sessions", body)

    def test_loopback_binding_does_not_depend_on_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("DNS must not be used")):
            server = LocalServer(self.desktop)
        try:
            self.assertEqual(server.server_name, "127.0.0.1")
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertGreater(server.server_port, 0)
        finally:
            server.server_close()

    def test_cross_origin_read_and_mutation_rejected_even_with_token(self):
        for method, path, body in (("GET", "/api/state", None), ("POST", "/api/scan", {})):
            with self.subTest(method=method):
                self.assertEqual(
                    self.request(method, path, body, headers={"Origin": "https://other.invalid"})[
                        0
                    ],
                    403,
                )

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(
            self.request("GET", "/api/state", headers={"Host": "other.invalid"})[0], 403
        )

    def test_cross_site_fetch_metadata_rejected(self):
        self.assertEqual(
            self.request("GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0], 403
        )

    def test_non_ascii_authentication_and_missing_origin_are_rejected(self):
        self.assertEqual(
            self.request("GET", "/api/state", headers={"Authorization": "Bearer é"})[0], 401
        )
        self.assertEqual(self.request("POST", "/api/scan", {}, headers={"Origin": "null"})[0], 403)

    def test_reopen_only_uses_a_verified_live_loopback_instance(self):
        profile = self.desktop.paths.runtime
        profile.mkdir(parents=True, exist_ok=True)
        cleaner.save_json(
            profile / "web-session.json",
            {
                "port": self.server.server_port,
                "token": self.server.token,
                "url": "https://other.invalid",
            },
        )
        with patch("webui.open_window") as open_window:
            self.assertTrue(reopen(profile))
            open_window.assert_called_once_with(self.server.url, profile / "browser")
        cleaner.save_json(
            profile / "web-session.json", {"port": self.server.server_port, "token": "x" * 43}
        )
        with patch("webui.open_window") as open_window:
            self.assertFalse(reopen(profile))
            open_window.assert_not_called()

    def test_assets_are_fixed_and_do_not_expose_token_or_filesystem(self):
        status, html, headers = self.request("GET", "/", authorized=False)
        self.assertEqual(status, 200)
        self.assertNotIn(self.server.token.encode(), html)
        self.assertNotIn(str(self.fixture.home).encode(), html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        for path in ("/../cleaner.py", "/%2e%2e/cleaner.py", "/auth.json", "/api/unknown"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_json_only_and_bounded_requests(self):
        self.assertEqual(
            self.request("POST", "/api/scan", {}, headers={"Content-Type": "text/plain"})[0], 415
        )
        # The limit must reject the headers before requesting an oversized body.
        # Sending that body while the server closes can race TCP reset delivery.
        self.assertEqual(
            self.request(
                "POST", "/api/scan", raw=b"", headers={"Content-Length": str(MAX_BODY + 1)}
            )[0],
            413,
        )
        self.assertEqual(self.request("POST", "/api/scan", raw=b'{"a":1,"a":2}')[0], 400)
        self.assertEqual(self.request("POST", "/api/scan", [1, 2])[0], 400)

    def test_snapshot_has_opaque_keys_and_revision_reuses_items(self):
        status, body, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["sessions"]), 1)
        self.assertEqual(len(body["sessions"][0]["key"]), 64)
        self.assertTrue(body["sessions"][0]["eligible"])
        _, unchanged, _ = self.request("GET", "/api/state?revision=" + str(body["revision"]))
        self.assertNotIn("sessions", unchanged)
        self.assertIn("status", unchanged)

    def test_execute_requires_preparation_and_explicit_acknowledgement(self):
        self.assertEqual(
            self.request("POST", "/api/execute", {"acknowledged": True, "mode": "offline"})[0], 400
        )
        valid = self.prepare()
        for changed in (
            {"acknowledged": False},
            {"acknowledged": 1},
            {"confirmation": "wrong"},
            {"mode": "unknown"},
        ):
            self.assertEqual(self.request("POST", "/api/execute", {**valid, **changed})[0], 400)
        self.assertEqual(self.fixture.path.read_bytes(), self.fixture.original)

    def test_arbitrary_target_paths_cannot_be_submitted(self):
        for keys in ([str(self.fixture.path)], [{}], [], "invalid", ["x"] * 1001):
            self.assertEqual(self.request("POST", "/api/prepare", {"keys": keys})[0], 400)
        self.assertEqual(
            self.request(
                "POST", "/api/prepare", {"kind": "restore", "key": str(self.fixture.backups)}
            )[0],
            400,
        )

    def test_expired_and_scan_invalidated_confirmations_are_refused(self):
        body = self.prepare()
        self.desktop.confirmation["expires"] = time.monotonic() - 1
        self.assertEqual(self.request("POST", "/api/execute", body)[0], 400)
        body = self.prepare()
        self.desktop.refresh()
        self.finish()
        self.assertEqual(self.request("POST", "/api/execute", body)[0], 400)

    def test_http_cleanup_and_restore_preserve_original_bytes_and_other_threads(self):
        body = self.prepare()
        self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
        self.finish()
        self.assertEqual(cleaner.inspect_session(self.fixture.path).images, 0)
        self.assertEqual(self.fixture.path.stat().st_size, len(self.fixture.original))
        self.assertEqual(self.request("POST", "/api/execute", body)[0], 400)
        self.assertIn("重新打开", self.desktop.report["next_step"])
        _, backups, _ = self.request("GET", "/api/backups")
        self.assertEqual(len(backups["backups"]), 1)
        body = self.prepare("restore", key=backups["backups"][0]["key"])
        self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
        self.finish()
        self.assertEqual(self.fixture.path.read_bytes(), self.fixture.original)
        self.assertEqual(self.fixture.rows(), self.fixture.original_rows)

    def test_new_messages_after_confirmation_are_never_overwritten(self):
        body = self.prepare()
        changed = (
            self.fixture.original
            + cleaner.encode(
                {"type": "event_msg", "payload": {"type": "user_message", "message": "new"}}
            )
            + b"\n"
        )
        self.fixture.path.write_bytes(changed)
        self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
        self.finish()
        self.assertEqual(self.fixture.path.read_bytes(), changed)
        self.assertEqual(self.desktop.report["completed"], [])
        self.assertEqual(len(self.desktop.report["failed"]), 1)

    def deletion(self, backup):
        entries = self.desktop.list_backups()
        key = next(
            item["key"]
            for item in entries
            if self.desktop.backup_paths[item["key"]].parent == backup
        )
        return self.prepare("delete_backup", key=key)

    def test_backup_deletion_preserves_sessions_cache_and_other_backups(self):
        first = self.fixture.clean()
        cleaner.restore(self.fixture.home, first)
        second = self.fixture.clean()
        session, rows = self.fixture.path.read_bytes(), self.fixture.rows()
        for backup in (first, second):
            body = self.deletion(backup)
            self.assertTrue(backup.exists())  # Preparation never removes files.
            self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 200)
            self.assertFalse(backup.exists())
            self.assertEqual(self.fixture.path.read_bytes(), session)
            self.assertEqual(self.fixture.rows(), rows)
            if backup == first:
                self.assertTrue((second / "original.jsonl").is_file())
            self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
        self.assertEqual(self.request("GET", "/api/backups")[1]["backups"], [])

    def test_backup_deletion_requires_its_own_authenticated_confirmation(self):
        backup = self.fixture.clean()
        self.assertEqual(self.request("POST", "/api/backups/delete", {})[0], 400)
        body = self.deletion(backup)
        self.assertEqual(
            self.request("POST", "/api/backups/delete", body, authorized=False)[0], 401
        )
        for changed in ({"acknowledged": False}, {"acknowledged": 1}, {"confirmation": "wrong"}):
            self.assertEqual(
                self.request("POST", "/api/backups/delete", {**body, **changed})[0], 400
            )
        # A deletion confirmation cannot accidentally be interpreted as a restore.
        self.assertEqual(self.request("POST", "/api/execute", body)[0], 400)
        self.desktop.confirmation["expires"] = time.monotonic() - 1
        self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
        body = self.prepare("restore", key=self.desktop.list_backups()[0]["key"])
        self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
        self.assertTrue((backup / "original.jsonl").is_file())

    def test_backup_deletion_refuses_arbitrary_and_stale_targets(self):
        backup = self.fixture.clean()
        self.desktop.list_backups()
        for key in (str(backup), str(self.fixture.backups), "unknown", {}, None):
            self.assertEqual(
                self.request("POST", "/api/prepare", {"kind": "delete_backup", "key": key})[0], 400
            )
        body = self.deletion(backup)
        self.desktop.backup_paths = {}
        self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
        self.assertTrue(backup.exists())

    def test_backup_deletion_refuses_changes_after_confirmation(self):
        backup = self.fixture.clean()
        for name in ("original.jsonl", "summary.json", "manifest.json"):
            body = self.deletion(backup)
            path = backup / name
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
            self.assertEqual(path.read_bytes(), original + b" ")
            path.write_bytes(original)
        body = self.deletion(backup)
        (backup / "keep.txt").write_text("unrelated", encoding="utf-8")
        self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
        self.assertTrue((backup / "original.jsonl").is_file())

    def test_backup_deletion_protects_interrupted_operations(self):
        backup = self.fixture.clean()
        manifest = cleaner.validate_manifest(cleaner.safe_json.read_file(backup / "manifest.json"))
        for status in ("prepared", "restoring"):
            cleaner.save_manifest(backup, {**manifest, "status": status})
            key = self.desktop.list_backups()[0]["key"]
            self.assertEqual(
                self.request("POST", "/api/prepare", {"kind": "delete_backup", "key": key})[0], 400
            )
        cleaner.save_manifest(backup, manifest)
        body = self.deletion(backup)
        cleaner.save_json(
            marker_path(self.fixture.path), marker(self.fixture.path, backup, "clean")
        )
        self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
        self.assertTrue((backup / "original.jsonl").is_file())
        marker_path(self.fixture.path).write_bytes(b"{broken")
        key = self.desktop.list_backups()[0]["key"]
        self.assertEqual(
            self.request("POST", "/api/prepare", {"kind": "delete_backup", "key": key})[0], 400
        )

    def test_backup_deletion_is_blocked_during_other_work(self):
        backup = self.fixture.clean()
        key = self.desktop.list_backups()[0]["key"]
        for busy in ("scan", "clean", "restore"):
            body = self.deletion(backup)
            self.desktop.busy = busy
            try:
                self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
                self.assertEqual(
                    self.request("POST", "/api/prepare", {"kind": "delete_backup", "key": key})[0],
                    400,
                )
            finally:
                self.desktop.busy = ""
        self.assertTrue(backup.exists())

    def test_backup_deletion_refuses_external_directories_and_links(self):
        backup = self.fixture.clean()
        session = self.fixture.path.read_bytes()
        body = self.deletion(backup)
        key = self.desktop.confirmation["key"]
        self.desktop.backup_paths[key] = self.fixture.home / "manifest.json"
        self.assertEqual(self.request("POST", "/api/backups/delete", body)[0], 400)
        self.assertEqual(self.fixture.path.read_bytes(), session)
        if os.name == "nt":
            # Junctions require no symlink privilege on Windows.
            import subprocess

            link = self.fixture.backups / "linked-backup"
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(backup)],
                check=True,
                capture_output=True,
            )
            self.addCleanup(link.rmdir)
        else:
            link = self.fixture.backups / "linked-backup"
            link.symlink_to(backup, target_is_directory=True)
            self.addCleanup(link.unlink)
        key = next(item["key"] for item in self.desktop.list_backups())
        # Use the discovered entry's opaque key, forcing its path to the link
        # since both entries can resolve to the same object ID.
        self.desktop.backup_paths[key] = link / "manifest.json"
        self.assertEqual(
            self.request("POST", "/api/prepare", {"kind": "delete_backup", "key": key})[0], 400
        )
        self.assertTrue((backup / "original.jsonl").is_file())

    def test_occupied_target_is_skipped_and_not_reported_as_cleaned(self):
        body = self.prepare()
        with patch("desktop.clean_session", side_effect=cleaner.SessionBusy("occupied")):
            self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
            self.finish()
        self.assertEqual(self.desktop.report["completed"], [])
        self.assertEqual(len(self.desktop.report["skipped"]), 1)
        self.assertNotIn("重新打开", self.desktop.report["next_step"])
        self.assertEqual(self.fixture.path.read_bytes(), self.fixture.original)
        self.assertEqual(self.desktop.report["retry_keys"], list(self.desktop.items))

    def test_cleanup_can_interrupt_scan_and_validate_an_unscanned_selection(self):
        started = threading.Event()
        cancelled = threading.Event()
        key, info = next(iter(self.desktop.items.items()))
        self.desktop.items[key] = replace(info, scanned=False, sha256="", images=0)

        def slow_scan():
            cancel = self.desktop.cancel
            started.set()
            if not cancel.wait(4):
                raise AssertionError("Cleanup failed to interrupt the global scan")
            cancelled.set()
            raise cleaner.ScanCancelled("stopped")

        with patch.object(self.desktop, "_scan", side_effect=slow_scan):
            self.desktop.refresh()
            self.assertTrue(started.wait(2))
            body = self.prepare()
            self.assertEqual(self.desktop.busy, "scan")
            with patch("desktop.scan", wraps=cleaner.scan) as scan_selected:
                self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
                self.finish()
                self.assertTrue(
                    all(
                        call.kwargs["paths"] == {info.path} for call in scan_selected.call_args_list
                    )
                )
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.desktop.report["completed"]), 1)
        self.assertEqual(cleaner.inspect_session(self.fixture.path).images, 0)

    def test_report_distinguishes_content_sizes_from_disk_sizes(self):
        before = cleaner.inspect_session(self.fixture.path).content_size
        self.assertEqual(self.request("POST", "/api/execute", self.prepare())[0], 200)
        self.finish()
        totals = self.desktop.report["totals"]
        self.assertEqual(totals["before_size"], before)
        self.assertEqual(
            totals["after_size"], cleaner.inspect_session(self.fixture.path).content_size
        )
        self.assertLess(totals["after_size"], totals["before_size"])
        self.assertEqual(totals["disk_before"], totals["disk_after"])
        self.assertGreater(totals["cache_reduced"], 0)
        self.assertEqual(
            self.desktop.snapshot()["sessions"][0]["last_cleanup"]["after_size"],
            totals["after_size"],
        )

    def test_one_click_close_cleans_offline_and_restarts_afterwards(self):
        events = []
        real_clean = cleaner.clean_session

        def clean(*args, **kwargs):
            self.assertFalse(kwargs["online"])
            events.append("clean")
            return real_clean(*args, **kwargs)

        body = {**self.prepare(), "mode": "online", "close_codex": True}
        with (
            patch("desktop.CodexLifecycle") as lifecycle,
            patch("desktop.clean_session", side_effect=clean),
        ):
            lifecycle.return_value.close.side_effect = lambda: events.append("close")
            lifecycle.return_value.restart.side_effect = lambda: events.append("restart")
            self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
            self.finish()
        self.assertEqual(events, ["close", "clean", "restart"])
        self.assertIn("已重新启动", self.desktop.report["next_step"])

    def test_failed_close_never_mutates_and_still_attempts_restart(self):
        body = {**self.prepare(), "close_codex": True}
        with patch("desktop.CodexLifecycle") as lifecycle:
            lifecycle.return_value.close.side_effect = cleaner.CleanupError("退出被取消")
            self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
            self.finish()
            lifecycle.return_value.restart.assert_called_once()
        self.assertEqual(self.fixture.path.read_bytes(), self.fixture.original)
        self.assertEqual(self.desktop.report["completed"], [])
        self.assertEqual(len(self.desktop.report["failed"]), 1)

    def test_windows_exit_timeout_reports_recovery_without_mutating_sessions(self):
        body = {**self.prepare(), "close_codex": True}
        with (
            patch("codex_process.sys.platform", "win32"),
            patch("codex_process.desktop_processes", return_value=[(123, "desktop")]),
            patch(
                "codex_process.run",
                side_effect=subprocess.CalledProcessError(3, ["powershell.exe"]),
            ),
            patch("codex_process.subprocess.Popen") as launch,
            patch("desktop.clean_session") as clean,
        ):
            self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
            self.finish()
            clean.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(self.fixture.path.read_bytes(), self.fixture.original)
        self.assertEqual(self.desktop.report["completed"], [])
        self.assertEqual(len(self.desktop.report["failed"]), 1)
        error = self.desktop.report["failed"][0]["error"]
        self.assertIn("15 秒内未完全退出", error)
        self.assertIn("离线处理", error)
        self.assertNotIn("powershell.exe", error)

    def test_failed_restart_preserves_successful_cleanup_and_reports_error(self):
        body = {**self.prepare(), "close_codex": True}
        with patch("desktop.CodexLifecycle") as lifecycle:
            lifecycle.return_value.restart.side_effect = OSError("启动失败")
            self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
            self.finish()
        self.assertEqual(len(self.desktop.report["completed"]), 1)
        self.assertEqual(len(self.desktop.report["failed"]), 1)
        self.assertIn("手动打开", self.desktop.report["next_step"])

    def test_failed_cleanup_still_restarts_codex(self):
        body = {**self.prepare(), "close_codex": True}
        with (
            patch("desktop.CodexLifecycle") as lifecycle,
            patch("desktop.clean_session", side_effect=cleaner.CleanupError("写入失败")),
        ):
            self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
            self.finish()
            lifecycle.return_value.restart.assert_called_once()
        self.assertEqual(len(self.desktop.report["failed"]), 1)
        self.assertEqual(self.fixture.path.read_bytes(), self.fixture.original)

    def test_busy_jobs_cannot_be_replaced_or_shutdown(self):
        with self.desktop.lock:
            self.desktop.busy = "clean"
        try:
            for endpoint in ("/api/scan", "/api/shutdown", "/api/prepare"):
                self.assertEqual(self.request("POST", endpoint, {})[0], 400)
            self.assertFalse(self.server.closing)
        finally:
            self.desktop.busy = ""

    def test_settings_reject_unsafe_or_missing_directories(self):
        for home, backups in (
            (self.fixture.home, self.fixture.home / "backups"),
            (self.fixture.root / "missing", self.fixture.backups),
            (".", self.fixture.backups),
        ):
            self.assertEqual(
                self.request("POST", "/api/settings", {"home": str(home), "backups": str(backups)})[
                    0
                ],
                400,
            )
        self.assertFalse((self.desktop.paths.runtime / "preferences.json").exists())

    def test_valid_settings_persist_and_restart_scan(self):
        destination = self.fixture.root / "other-backups"
        status, value, _ = self.request(
            "POST", "/api/settings", {"home": str(self.fixture.home), "backups": str(destination)}
        )
        self.assertEqual(status, 200, value)
        self.finish()
        self.assertEqual(self.desktop.backups, destination)
        self.assertTrue((self.desktop.paths.runtime / "preferences.json").is_file())

    def test_diagnostics_are_explicit_and_do_not_include_private_paths(self):
        status, report, _ = self.request("POST", "/api/diagnostics", {})
        self.assertEqual(status, 200)
        self.assertNotIn(str(self.fixture.home), json.dumps(report))
        self.assertNotIn(fixtures.TID, json.dumps(report))

    def test_close_expiry_never_interrupts_mutation(self):
        self.server.last_seen = time.monotonic() - 500
        self.assertTrue(self.server.expired())
        self.desktop.busy = "clean"
        try:
            self.assertFalse(self.server.expired())
        finally:
            self.desktop.busy = ""

    def test_repair_uses_validated_recovery_marker_backup(self):
        from journal import marker, marker_path

        backup = cleaner.clean_session(self.fixture.home, self.fixture.info(), self.fixture.backups)
        cleaner.save_json(
            marker_path(self.fixture.path), marker(self.fixture.path, backup, "clean")
        )
        self.desktop._scan()
        body = self.prepare("repair", key=next(iter(self.desktop.items)))
        self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
        self.finish()
        self.assertEqual(self.fixture.path.read_bytes(), self.fixture.original)
        self.assertFalse(marker_path(self.fixture.path).exists())


if __name__ == "__main__":
    unittest.main()
