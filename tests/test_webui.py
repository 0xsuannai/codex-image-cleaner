"""Local transport and real job roundtrips, using constructed conversations only."""

import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_cleaner as fixtures

import cleaner
from app_support import AppPaths
from desktop import Desktop
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

    def test_occupied_target_is_skipped_and_not_reported_as_cleaned(self):
        body = self.prepare()
        with patch("desktop.clean_session", side_effect=cleaner.SessionBusy("occupied")):
            self.assertEqual(self.request("POST", "/api/execute", body)[0], 200)
            self.finish()
        self.assertEqual(self.desktop.report["completed"], [])
        self.assertEqual(len(self.desktop.report["skipped"]), 1)
        self.assertNotIn("重新打开", self.desktop.report["next_step"])
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
