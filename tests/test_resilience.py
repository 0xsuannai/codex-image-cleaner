"""Real process-crash recovery, resource limits, data corruption and ownership checks."""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_cleaner as fixtures

import cleaner
import safe_json
from instance import AlreadyRunning, SingleInstance
from journal import marker_path


class ResilienceTests(unittest.TestCase):
    def fixture(self):
        value = fixtures.CleanerTests("runTest")
        value.setUp()
        self.addCleanup(value.doCleanups)
        return value

    def crash(self, fixture, action, phase, backup):
        result = subprocess.run(
            [
                sys.executable,
                "tests/crash_worker.py",
                action,
                phase,
                str(fixture.home),
                str(fixture.path),
                str(backup),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            env=dict(os.environ, PYTHONUTF8="1"),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(result.returncode, 87, result.stderr)
        self.assertIn("checkpoint:" + phase, result.stdout)

    def verify_original(self, fixture):
        self.assertEqual(fixture.path.read_bytes(), fixture.original)
        self.assertEqual(fixture.rows(), fixture.original_rows)
        self.assertFalse(marker_path(fixture.path).exists())
        with closing(sqlite3.connect(fixture.history)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def verify_positions(self, fixture):
        boundaries = {0}
        offset = 0
        for line in fixture.path.read_bytes().splitlines(keepends=True):
            offset += len(line)
            boundaries.add(offset)
        with closing(sqlite3.connect(fixture.history)) as db:
            for start, end in db.execute(
                "SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns"
            ):
                self.assertIn(start, boundaries)
                self.assertIn(end, boundaries)

    def test_cleanup_process_crashes_recover_at_every_persistence_boundary(self):
        for phase in ("prepared", "marker", "cache", "replaced", "committed", "finished"):
            with self.subTest(phase=phase):
                f = self.fixture()
                self.crash(f, "clean", phase, f.backups)
                self.verify_positions(f)
                backup = next(f.backups.iterdir())
                if phase != "prepared":
                    self.assertTrue(marker_path(f.path).exists())
                    entry = cleaner.catalog(f.home)[0]
                    self.assertEqual(Path(entry.recovery_backup), backup)
                    with self.assertRaises(cleaner.RecoveryRequired):
                        cleaner.clean_session(f.home, f.info(), f.backups)
                cleaner.restore(f.home, backup)
                self.verify_original(f)

    def test_restore_process_crashes_recover_at_every_persistence_boundary(self):
        for phase in ("marker", "cache", "replaced", "committed", "finished"):
            with self.subTest(phase=phase):
                f = self.fixture()
                backup = f.clean()
                self.crash(f, "restore", phase, backup)
                self.verify_positions(f)
                self.assertTrue(marker_path(f.path).exists())
                cleaner.restore(f.home, backup)
                self.verify_original(f)

    def test_recovery_blocks_a_different_backup(self):
        f = self.fixture()
        backup = f.clean()
        self.crash(f, "restore", "marker", backup)
        other = f.root / "other-backup"
        other.mkdir()
        import shutil

        shutil.copyfile(backup / "manifest.json", other / "manifest.json")
        shutil.copyfile(backup / "original.jsonl", other / "original.jsonl")
        before = f.path.read_bytes()
        with self.assertRaises(cleaner.RecoveryRequired):
            cleaner.restore(f.home, other)
        self.assertEqual(f.path.read_bytes(), before)
        cleaner.restore(f.home, backup)
        self.verify_original(f)

    def test_corrupted_recovery_marker_fails_closed(self):
        f = self.fixture()
        marker_path(f.path).write_bytes(b"{broken")
        with self.assertRaises(cleaner.RecoveryRequired):
            cleaner.clean_session(f.home, f.info(), f.backups)
        self.assertEqual(f.path.read_bytes(), f.original)
        self.assertTrue(cleaner.catalog(f.home)[0].error)

    def test_pending_error_survives_a_warm_scan_cache(self):
        f = self.fixture()
        cache = {}
        cleaner.scan(f.home, cache=cache)
        self.crash(f, "clean", "marker", f.backups)
        entry = cleaner.scan(f.home, cache=cache)[0]
        self.assertTrue(entry.error)
        self.assertTrue(entry.recovery_backup)

    def test_oversized_record_is_rejected_before_mutation(self):
        f = self.fixture()
        with patch.object(safe_json, "MAX_RECORD_BYTES", 1024):
            self.assertTrue(f.info().error)
            with self.assertRaises(cleaner.CleanupError):
                f.clean()
        self.assertEqual(f.path.read_bytes(), f.original)
        self.assertFalse(f.backups.exists())

    def test_record_count_limit_prevents_unbounded_offset_maps(self):
        f = self.fixture()
        with patch.object(safe_json, "MAX_RECORDS", 2):
            self.assertTrue(f.info().error)
        self.assertEqual(f.path.read_bytes(), f.original)

    def test_duplicate_json_keys_are_rejected_and_preserve_original(self):
        f = self.fixture()
        data = f.original + b'{"type":"event_msg","payload":{},"payload":null}\n'
        f.path.write_bytes(data)
        self.assertTrue(f.info().error)
        with self.assertRaises(cleaner.CleanupError):
            f.clean()
        self.assertEqual(f.path.read_bytes(), data)

    def test_non_finite_json_and_deep_nesting_fail_closed(self):
        f = self.fixture()
        for record in (
            b'{"type":"event_msg","payload":{"value":NaN}}\n',
            b'{"type":"event_msg","payload":{"value":1e999}}\n',
            b"[" * 1200 + b"0" + b"]" * 1200 + b"\n",
        ):
            with self.subTest(record_length=len(record)):
                f.path.write_bytes(f.original + record)
                self.assertTrue(f.info().error)

    def test_invalid_unicode_never_reaches_the_interface(self):
        f = self.fixture()
        for value in (b'{"key":"\\ud800"}', b'["\\udfff"]', b'{"\\ud800":0}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                safe_json.loads(value)
        f.path.write_bytes(
            b'{"type":"session_meta","payload":{"id":"'
            + fixtures.TID.encode()
            + b'","cwd":"\\ud800"}}\n'
        )
        self.assertTrue(cleaner.catalog(f.home)[0].error)

    def test_bad_cache_row_isolated_from_other_sessions(self):
        f = self.fixture()
        with closing(sqlite3.connect(f.history)) as db, db:
            db.execute(
                "UPDATE thread_items SET item_json='[broken' WHERE thread_id=? AND item_id='item-1'",
                (fixtures.TID,),
            )
        entry = cleaner.scan(f.home)[0]
        self.assertTrue(entry.error)
        with self.assertRaises(cleaner.CleanupError):
            cleaner.clean_session(f.home, entry, f.backups)
        self.assertEqual(f.path.read_bytes(), f.original)

    def test_cache_edit_budget_rejected_before_backup_or_mutation(self):
        f = self.fixture()
        with (
            patch.object(safe_json, "MAX_CACHE_EDIT_BYTES", 100),
            self.assertRaises(cleaner.CleanupError),
        ):
            f.clean()
        self.assertEqual(f.path.read_bytes(), f.original)
        self.assertEqual(f.rows(), f.original_rows)

    def test_repeated_roundtrips_are_exact(self):
        f = self.fixture()
        for _ in range(40):
            backup = f.clean()
            cleaner.restore(f.home, backup)
            self.verify_original(f)

    def test_verified_backup_can_repair_a_damaged_recovery_marker(self):
        f = self.fixture()
        backup = f.clean()
        marker_path(f.path).write_bytes(b"{damaged")
        cleaner.restore(f.home, backup)
        self.verify_original(f)

    def test_backup_summary_falls_back_when_stale_or_damaged(self):
        from journal import backup_summary

        f = self.fixture()
        backup = f.clean()
        self.assertEqual(backup_summary(backup / "manifest.json")["status"], "cleaned")
        (backup / "summary.json").write_bytes(b"{bad")
        self.assertEqual(backup_summary(backup / "manifest.json")["status"], "cleaned")
        cleaner.restore(f.home, backup)
        self.assertEqual(backup_summary(backup / "manifest.json")["status"], "restored")

    def test_cleanup_preserves_byte_positions_but_removes_inline_images(self):
        f = self.fixture()
        previous_lines = [len(line) for line in f.original.splitlines(keepends=True)]
        backup = f.clean()
        self.assertEqual(
            [len(line) for line in f.path.read_bytes().splitlines(keepends=True)], previous_lines
        )
        self.assertNotIn(fixtures.IMAGE.encode(), f.path.read_bytes())
        self.assertEqual(f.info().images, 0)
        self.verify_positions(f)
        cleaner.restore(f.home, backup)
        self.verify_original(f)

    def test_atomic_replacement_invalidates_cached_images_even_when_size_and_mtime_match(self):
        f = self.fixture()
        cache = {}
        cleaner.scan(f.home, cache=cache)
        original_size, original_mtime = f.path.stat().st_size, f.path.stat().st_mtime_ns
        f.clean()
        self.assertEqual(f.path.stat().st_size, original_size)
        self.assertEqual(f.path.stat().st_mtime_ns, original_mtime)
        self.assertEqual(cleaner.scan(f.home, cache=cache)[0].images, 0)

    def test_legacy_compact_backup_remains_restorable(self):
        import shutil

        f = self.fixture()
        backup = f.backups / "legacy"
        backup.mkdir(parents=True)
        shutil.copy2(f.path, backup / "original.jsonl")
        offsets, old_position, new_position, parts = {0: 0}, 0, 0, []
        for raw in f.original.splitlines(keepends=True):
            clean, _ = cleaner.clean_line(raw)
            parts.append(clean)
            old_position += len(raw)
            new_position += len(clean)
            offsets[old_position] = new_position
        with cleaner.cache_transaction(f.history) as db:
            edits = cleaner.cache_edits(db, fixtures.TID, offsets)
            cleaner.apply_edits(db, edits)
        clean = b"".join(parts)
        manifest = {
            "version": 1,
            "status": "cleaned",
            "home": str(f.home),
            "path": str(f.path),
            "thread_id": fixtures.TID,
            "title": "legacy",
            "original_sha256": cleaner.digest(f.path),
            "clean_sha256": __import__("hashlib").sha256(clean).hexdigest(),
            "original_mtime_ns": f.path.stat().st_mtime_ns,
            "cache_edits": edits,
            "history_path": str(f.history),
        }
        cleaner.save_json(backup / "manifest.json", manifest)
        f.path.write_bytes(clean)
        cleaner.restore(f.home, backup)
        self.verify_original(f)

    def test_source_cli_can_export_scan_and_diagnostics_without_a_desktop(self):
        f = self.fixture()
        profile = f.root / "profile"
        env = dict(os.environ, CODEX_IMAGE_CLEANER_DATA_DIR=str(profile), PYTHONUTF8="1")
        with SingleInstance(profile / ".runtime"):
            for mode in ("scan", "diagnostics"):
                output = f.root / (mode + ".json")
                arguments = (
                    ["--scan", "--output", str(output)]
                    if mode == "scan"
                    else ["--diagnostics", str(output)]
                )
                result = subprocess.run(
                    [sys.executable, "app.py", "--home", str(f.home), *arguments],
                    cwd=Path(__file__).resolve().parents[1],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(output.is_file())
                value = json.loads(output.read_bytes())
                if mode == "scan":
                    self.assertEqual(value[0]["thread_id"], fixtures.TID)
                else:
                    self.assertNotIn(str(f.home), json.dumps(value))

    def test_rollback_failure_retains_a_repairable_transaction(self):
        f = self.fixture()
        transaction, copy = cleaner.cache_transaction, cleaner.shutil.copy2
        from contextlib import contextmanager

        @contextmanager
        def failing_transaction(path):
            with transaction(path) as db:
                yield db
                raise OSError("simulated commit failure")

        def failing_copy(source, destination):
            if Path(source).name == "original.jsonl":
                raise OSError("simulated full disk during rollback")
            return copy(source, destination)

        with (
            patch.object(cleaner, "cache_transaction", failing_transaction),
            patch.object(cleaner.shutil, "copy2", failing_copy),
            self.assertRaises(OSError),
        ):
            f.clean()
        self.assertTrue(marker_path(f.path).exists())
        backup = next(f.backups.iterdir())
        cleaner.restore(f.home, backup)
        self.verify_original(f)

    def test_diagnostics_excludes_contents_ids_and_paths(self):
        from diagnostics import collect

        f = self.fixture()
        secret = "private-sentinel-do-not-export"
        (f.home / "auth.json").write_text(secret)
        with closing(sqlite3.connect(f.home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id TEXT,title TEXT,cli_version TEXT)")
            db.execute(
                "INSERT INTO threads VALUES(?,?,?)", (fixtures.TID, secret, "0.164.0-alpha.11.1")
            )
            db.execute("INSERT INTO threads VALUES(?,?,?)", ("private-id", secret, str(f.home)))
        report = collect(f.home)
        rendered = json.dumps(report)
        for private in (secret, fixtures.TID, str(f.home), "private-id", "data:image"):
            self.assertNotIn(private, rendered)
        self.assertEqual(report["observed_codex_versions"], ["0.164.0-alpha.11.1"])

    def test_diagnostics_handles_corrupted_databases(self):
        from diagnostics import collect

        f = self.fixture()
        f.history.write_bytes(b"broken sqlite")
        report = collect(f.home)
        self.assertEqual(
            report["history_databases"]["thread_history_1.sqlite"]["status"], "unreadable"
        )

    def test_second_desktop_instance_is_rejected_and_ownership_released(self):
        f = self.fixture()
        with SingleInstance(f.root / "profile"):
            with self.assertRaises(AlreadyRunning), SingleInstance(f.root / "profile"):
                self.fail("A second desktop instance was accepted")
            with SingleInstance(f.root / "different-profile"):
                pass
        with SingleInstance(f.root / "profile"):
            pass

    def test_desktop_ownership_released_after_process_termination(self):
        f = self.fixture()
        for restart in range(12):
            with self.subTest(restart=restart):
                self.desktop_crash_roundtrip(f)

    def desktop_crash_roundtrip(self, f):
        script = "from pathlib import Path;import os,sys;from instance import SingleInstance;ctx=SingleInstance(Path(sys.argv[1]));ctx.__enter__();print('ready:'+str(os.getpid()),flush=True);sys.stdin.readline()"
        process = subprocess.Popen(
            # Windows venv python.exe can redirect to a separate worker. Start
            # the base interpreter so kill()/wait() refer to the lock owner.
            [
                getattr(sys, "_base_executable", sys.executable),
                "-u",
                "-c",
                script,
                str(f.root / "profile"),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready:" + str(process.pid))
            with self.assertRaises(AlreadyRunning), SingleInstance(f.root / "profile"):
                self.fail("Accepted a concurrent desktop")
            process.kill()
            process.wait(timeout=5)
            with SingleInstance(f.root / "profile"):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
