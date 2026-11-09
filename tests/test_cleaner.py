"""Synthetic sessions only: never modifies the user's Codex history."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import cleaner

TID = "11111111-2222-3333-4444-555555555555"
IMAGE = "data:image/png;base64," + "A" * 8192


class CleanerTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=runtime)
        assert Path(self.tmp.name).resolve().is_relative_to(runtime.resolve())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex"
        self.folder = self.home / "sessions" / "2026" / "11" / "08"
        self.folder.mkdir(parents=True)
        self.path = self.folder / f"rollout-test-{TID}.jsonl"
        self.backups = self.root / "backups"
        self.records = [
            {"type": "session_meta", "payload": {"id": TID, "cwd": "D:/example"}},
            {
                "type": "response_item",
                "ordinal": 1,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "保留这段文字：" + IMAGE[:50]},
                        {"type": "input_image", "image_url": IMAGE, "detail": "high"},
                    ],
                },
            },
            {
                "type": "response_item",
                "ordinal": 2,
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": [
                        {"type": "input_image", "image_url": IMAGE},
                        {"type": "input_text", "text": "tool text"},
                    ],
                },
            },
            {
                "type": "compacted",
                "ordinal": 3,
                "payload": {
                    "message": "summary",
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_image", "image_url": IMAGE}],
                        }
                    ],
                },
            },
            {
                "type": "response_item",
                "ordinal": 4,
                "payload": {
                    "type": "function_call",
                    "name": "keep_me",
                    "arguments": '{"image_url":"do not touch"}',
                },
            },
            {
                "type": "event_msg",
                "ordinal": 5,
                "payload": {
                    "type": "user_message",
                    "message": "纯图片说明",
                    "images": [IMAGE],
                    "local_images": ["D:/input.png"],
                },
            },
        ]
        self.raw_lines = [cleaner.encode(r) + b"\r\n" for r in self.records]
        self.original = b"".join(self.raw_lines)
        self.path.write_bytes(self.original)
        self.history = self.home / "thread_history_1.sqlite"
        self.start = len(self.raw_lines[0])
        self.end = sum(map(len, self.raw_lines[:3]))
        with closing(sqlite3.connect(self.history)) as db, db:
            db.executescript("""
                CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,rollout_byte_offset INTEGER,
                    rollout_end_byte_offset INTEGER,rollout_ordinal INTEGER,PRIMARY KEY(thread_id,turn_id));
                CREATE TABLE thread_items (thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT,
                    PRIMARY KEY(thread_id,turn_id,item_id));
                CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY,
                    next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);
                CREATE TABLE thread_realtime_items (thread_id TEXT,item_id TEXT,item_json TEXT,
                    PRIMARY KEY(thread_id,item_id));
            """)
            db.execute(
                "INSERT INTO thread_turns VALUES (?,?,?,?,?)",
                (TID, "turn-1", self.start, self.end, 1),
            )
            db.execute(
                "INSERT INTO thread_history_projection_state VALUES (?,?,?)",
                (TID, len(self.original), 6),
            )
            self.cached = json.dumps(
                {
                    "type": "userMessage",
                    "id": "item-1",
                    "content": [
                        {"type": "text", "text": "中文缓存原文", "text_elements": []},
                        {"type": "image", "url": IMAGE, "detail": None},
                    ],
                }
            )
            db.execute(
                "INSERT INTO thread_items VALUES (?,?,?,?)", (TID, "turn-1", "item-1", self.cached)
            )
            db.execute(
                "INSERT INTO thread_items VALUES (?,?,?,?)",
                ("other-thread", "other-turn", "other-item", self.cached),
            )
            db.execute(
                "INSERT INTO thread_items VALUES (?,?,?,?)",
                (
                    TID,
                    "turn-1",
                    "generated",
                    json.dumps(
                        {
                            "type": "imageGeneration",
                            "id": "generated",
                            "result": "B" * 4096,
                            "inputImages": ["D:/a.png"],
                            "savedPath": "D:/keep.png",
                            "status": "completed",
                        }
                    ),
                ),
            )
        self.original_rows = self.rows()

    def rows(self):
        with closing(sqlite3.connect(self.history)) as db, db:
            return {
                table: db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                for table in cleaner.CACHE_TABLES
            }

    def info(self):
        return cleaner.inspect_session(self.path)

    def clean(self):
        return cleaner.clean_session(self.home, self.info(), self.backups)

    def test_clean_preserves_text_structure_ordinals_and_offsets_then_restores_exactly(self):
        info = self.info()
        self.assertEqual(info.images, 5)
        mtime = self.path.stat().st_mtime_ns
        backup = self.clean()
        self.assertEqual((backup / "original.jsonl").read_bytes(), self.original)
        raw = self.path.read_bytes()
        lines = raw.splitlines(keepends=True)
        records = [json.loads(line) for line in lines]
        self.assertEqual(len(records), len(self.records))
        self.assertEqual(
            [r.get("ordinal") for r in records], [r.get("ordinal") for r in self.records]
        )
        self.assertEqual(
            records[1]["payload"]["content"][0], self.records[1]["payload"]["content"][0]
        )
        self.assertEqual(records[4], self.records[4])
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(IMAGE.encode(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        with closing(sqlite3.connect(self.history)) as db, db:
            start, end = db.execute(
                "SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns"
            ).fetchone()
            self.assertEqual(start, len(lines[0]))
            self.assertEqual(end, sum(map(len, lines[:3])))
            self.assertEqual(
                db.execute(
                    "SELECT next_rollout_byte_offset FROM thread_history_projection_state"
                ).fetchone()[0],
                len(raw),
            )
            cached = db.execute(
                "SELECT item_json FROM thread_items WHERE thread_id=? AND item_id='item-1'", (TID,)
            ).fetchone()[0]
            self.assertNotIn(IMAGE, cached)
            self.assertEqual(json.loads(cached)["content"][0]["text"], "中文缓存原文")
            self.assertEqual(
                db.execute(
                    "SELECT item_json FROM thread_items WHERE thread_id='other-thread'"
                ).fetchone()[0],
                self.cached,
            )
        self.assertIsNone(self.clean())  # Idempotent cleanup, no duplicate backup.
        cleaner.restore(self.home, backup)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.rows(), self.original_rows)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertEqual(json.loads((backup / "manifest.json").read_bytes())["status"], "restored")

    def test_stale_scan_rejected(self):
        info = self.info()
        self.path.write_bytes(self.original + b"{}\n")
        with self.assertRaisesRegex(cleaner.CleanupError, "变化"):
            cleaner.clean_session(self.home, info, self.backups)
        self.assertFalse(self.backups.exists())

    def test_malformed_file_never_modified(self):
        self.path.write_bytes(self.original + b'{"unfinished":')
        before = self.path.read_bytes()
        self.assertTrue(self.info().error)
        with self.assertRaises(cleaner.CleanupError):
            self.clean()
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_cache_offset_aborts_without_mutation(self):
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute("UPDATE thread_turns SET rollout_byte_offset=17")
        before = self.rows()
        with self.assertRaisesRegex(cleaner.CleanupError, "缓存位置"):
            self.clean()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.rows(), before)

    def test_new_database_version_refused(self):
        (self.home / "thread_history_2.sqlite").touch()
        with self.assertRaisesRegex(cleaner.CleanupError, "版本"):
            self.clean()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_new_cache_offset_column_refused(self):
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute("ALTER TABLE thread_turns ADD COLUMN another_byte_offset INTEGER")
        with self.assertRaisesRegex(cleaner.CleanupError, "未知历史位置"):
            self.clean()

    def test_restore_refuses_new_messages(self):
        backup = self.clean()
        newer = (
            self.path.read_bytes() + b'{"type":"event_msg","payload":{"message":"new message"}}\n'
        )
        self.path.write_bytes(newer)
        with self.assertRaisesRegex(cleaner.CleanupError, "新内容"):
            cleaner.restore(self.home, backup)
        self.assertEqual(self.path.read_bytes(), newer)

    def test_restore_cache_conflict_rolls_back(self):
        backup = self.clean()
        cleaned = self.path.read_bytes()
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute(
                "UPDATE thread_items SET item_json='{}' WHERE thread_id=? AND item_id='item-1'",
                (TID,),
            )
        before = self.rows()
        with self.assertRaisesRegex(cleaner.CleanupError, "缓存已被更新"):
            cleaner.restore(self.home, backup)
        self.assertEqual(self.path.read_bytes(), cleaned)
        self.assertEqual(self.rows(), before)

    def test_database_failure_after_file_replace_rolls_both_back(self):
        transaction = cleaner.cache_transaction

        @contextmanager
        def failing_transaction(path):
            with transaction(path) as db:
                yield db
                raise OSError("simulated commit failure")

        with (
            patch.object(cleaner, "cache_transaction", failing_transaction),
            self.assertRaisesRegex(OSError, "simulated"),
        ):
            self.clean()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.rows(), self.original_rows)

    def test_crash_after_replacement_before_commit_can_restore(self):
        backup = self.clean()
        manifest = json.loads((backup / "manifest.json").read_bytes())
        with cleaner.cache_transaction(self.history) as db:
            cleaner.apply_edits(db, manifest["cache_edits"], reverse=True)
        manifest["status"] = "prepared"
        cleaner.save_json(backup / "manifest.json", manifest)
        cleaner.restore(self.home, backup)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.rows(), self.original_rows)

    def test_backup_tamper_rejected(self):
        backup = self.clean()
        (backup / "original.jsonl").write_bytes(b"corrupt")
        with self.assertRaisesRegex(cleaner.CleanupError, "备份校验"):
            cleaner.restore(self.home, backup)

    def test_no_cache_and_no_final_newline(self):
        self.history.unlink()
        self.path.write_bytes(self.original.rstrip(b"\r\n"))
        original = self.path.read_bytes()
        backup = self.clean()
        self.assertFalse(self.path.read_bytes().endswith(b"\n"))
        cleaner.restore(self.home, backup)
        self.assertEqual(self.path.read_bytes(), original)

    def test_cache_only_image_cleanup(self):
        self.clean()
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute(
                "UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id='item-1'",
                (self.cached, TID),
            )
        info = self.info()
        self.assertEqual(info.images, 0)
        second = cleaner.clean_session(self.home, info, self.backups)
        self.assertIsNotNone(second)
        self.assertEqual(cleaner.digest(self.path), info.sha256)
        cleaner.restore(self.home, second)

    def test_active_writer_lock_refused(self):
        script = (
            "import sys; from pathlib import Path; import cleaner; "
            "h=Path(sys.argv[1]); cleaner.clean_session(h,cleaner.inspect_session(Path(sys.argv[2])),Path(sys.argv[3]))"
        )
        with cleaner.writer_lock(self.home, TID):
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.home), str(self.path), str(self.backups)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SessionBusy", result.stderr)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_scan_includes_cache_and_titles(self):
        (self.home / "session_index.jsonl").write_text(
            json.dumps({"id": TID, "thread_name": "测试会话"}), encoding="utf-8"
        )
        items = cleaner.scan(self.home)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "测试会话")
        self.assertEqual(items[0].images, 5)
        self.assertEqual(items[0].cache_images, 3)

    def test_image_text_and_metadata_examples_not_scrubbed(self):
        sample = {
            "type": "session_meta",
            "payload": {"example": {"type": "input_image", "image_url": IMAGE}},
        }
        raw = cleaner.encode(sample) + b"\n"
        self.assertEqual(cleaner.clean_line(raw), (raw, 0))
        text = {
            "type": "input_text",
            "text": json.dumps({"type": "input_image", "image_url": IMAGE}),
        }
        self.assertEqual(cleaner.scrub(text), (text, 0))

    def test_backup_inside_codex_home_refused(self):
        with self.assertRaisesRegex(cleaner.CleanupError, "Codex 目录以外"):
            cleaner.clean_session(self.home, self.info(), self.home / "backup")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_durable_image_references_use_valid_text_or_empty_inline_variant(self):
        value = {"type": "image", "fileId": "file-image", "detail": "high"}
        clean, count = cleaner.scrub(value, cache=True)
        self.assertEqual(count, 1)
        self.assertEqual(clean["type"], "text")
        self.assertNotIn("fileId", clean)
        generated = {
            "type": "imageGeneration",
            "id": "g1",
            "status": "completed",
            "fileId": "file-result",
            "result": "",
            "inputImages": [],
        }
        clean, count = cleaner.scrub(generated, cache=True)
        self.assertEqual(count, 1)
        self.assertNotIn("fileId", clean)
        self.assertEqual(clean["result"], "")
        raw = {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c",
                "output": [{"type": "input_image", "file_id": "file-input"}],
            },
        }
        clean, count = cleaner.clean_line(cleaner.encode(raw))
        self.assertEqual(count, 1)
        self.assertEqual(json.loads(clean)["payload"]["output"][0]["type"], "input_text")

    def test_name_only_database_and_long_unicode_title(self):
        title = "新版完整会话名称：" + "保留长标题不要显示成会话编号" * 20
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT,name TEXT,first_user_message TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?,?)", (TID, title, "原始用户消息"))
        entry = cleaner.catalog(self.home)[0]
        self.assertEqual(entry.title, title)
        self.assertEqual(entry.preview, "原始用户消息")
        self.assertEqual(entry.title_source, "会话名称")
        self.assertFalse(entry.scanned)

    def test_bom_name_index_rename_wins_and_empty_entry_does_not_erase(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT,title TEXT,name TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?,?)", (TID, "旧自动标题", "旧名称"))
        lines = [
            "broken line",
            json.dumps({"id": TID, "thread_name": "手动改过的新名称"}),
            json.dumps({"id": TID, "thread_name": ""}),
        ]
        (self.home / "session_index.jsonl").write_text("\n".join(lines), encoding="utf-8-sig")
        self.assertEqual(cleaner.read_titles(self.home)[TID], "手动改过的新名称")

    def test_missing_title_uses_user_request_after_workspace_instructions(self):
        records = [
            self.records[0],
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "# AGENTS.md instructions for workspace",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "<environment_context>system data</environment_context>",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "# Context from my IDE setup:\nactive file: demo.py\n\n## My request:\n帮我修好会话名称显示",
                        }
                    ],
                },
            },
        ]
        self.path.write_bytes(b"".join(cleaner.encode(r) + b"\n" for r in records))
        entry = cleaner.catalog(self.home)[0]
        self.assertEqual(entry.title, "帮我修好会话名称显示")
        self.assertEqual(entry.title_source, "首条用户消息")

    def test_missing_subagent_title_uses_nickname(self):
        meta = {
            **self.records[0]["payload"],
            "source": {"subagent": {}},
            "agent_nickname": "测试助手",
        }
        self.path.write_bytes(cleaner.encode({"type": "session_meta", "payload": meta}) + b"\n")
        entry = cleaner.catalog(self.home)[0]
        self.assertTrue(entry.is_subagent)
        self.assertEqual(entry.title, "子任务 · 测试助手")
        self.assertNotEqual(entry.title, TID)

    def test_catalog_retains_parent_relationship_for_native_subtasks(self):
        parent = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        meta = {
            **self.records[0]["payload"],
            "parent_thread_id": parent,
            "source": {"subagent": {"other": "worker"}},
        }
        self.path.write_bytes(cleaner.encode({"type": "session_meta", "payload": meta}) + b"\n")
        entry = cleaner.scan(self.home)[0]
        self.assertTrue(entry.is_subagent)
        self.assertEqual(entry.parent_thread_id, parent)

    def test_catalog_supports_thread_spawn_parent_metadata(self):
        parent = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        meta = {
            **self.records[0]["payload"],
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}},
        }
        self.path.write_bytes(cleaner.encode({"type": "session_meta", "payload": meta}) + b"\n")
        self.assertEqual(cleaner.catalog(self.home)[0].parent_thread_id, parent)

    def test_invalid_and_self_parent_ids_are_not_used(self):
        for parent in (TID, "invalid", {}, 123):
            meta = {**self.records[0]["payload"], "parent_thread_id": parent}
            self.path.write_bytes(cleaner.encode({"type": "session_meta", "payload": meta}) + b"\n")
            self.assertEqual(cleaner.catalog(self.home)[0].parent_thread_id, "")

    def test_catalog_is_delivered_before_image_analysis_and_cache_is_reused(self):
        cache, events = {}, []
        result = cleaner.scan(
            self.home,
            cache=cache,
            on_catalog=lambda entries: events.append(("catalog", entries[0].scanned)),
            on_session=lambda entry: events.append(("session", entry.scanned)),
        )
        self.assertEqual(events[0], ("catalog", False))
        self.assertEqual(events[1], ("session", True))
        with patch.object(
            cleaner,
            "inspect_session",
            side_effect=AssertionError("unchanged file should not be decoded again"),
        ):
            again = cleaner.scan(self.home, cache=cache)
        self.assertEqual(result[0].sha256, again[0].sha256)
        self.assertEqual(result[0].cache_images, again[0].cache_images)
        self.assertEqual(again[0].cache_images, 3)

    def test_scan_cancel_does_not_modify_sessions(self):
        event = threading.Event()
        with self.assertRaises(cleaner.ScanCancelled):
            cleaner.scan(self.home, cancel=event, on_catalog=lambda _: event.set())
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.rows(), self.original_rows)
        with self.assertRaises(cleaner.ScanCancelled):
            cleaner.inspect_session(self.path, cancel=event)

    @contextmanager
    def held_native_style_lock(self, thread_id):
        directory = self.home / "thread-writer-locks"
        directory.mkdir(exist_ok=True)
        (directory / ".coordination.lock").touch(exist_ok=True)
        lock = directory / f"{thread_id}.lock"
        script = (
            "import sys;from pathlib import Path;import cleaner; "
            "ctx=cleaner.file_lock(Path(sys.argv[1]),'busy');ctx.__enter__();"
            "print('ready',flush=True);sys.stdin.readline();ctx.__exit__(None,None,None)"
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(lock)],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            yield lock
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_online_mode_refuses_unknown_lock_protocol(self):
        with self.assertRaisesRegex(cleaner.CleanupError, "写锁机制"):
            cleaner.clean_session(self.home, self.info(), self.backups, online=True)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.rows(), self.original_rows)
        self.assertFalse(self.backups.exists())

    def test_online_mode_skips_loaded_target_without_removing_its_lock(self):
        with self.held_native_style_lock(TID) as path:
            with self.assertRaisesRegex(cleaner.SessionBusy, "目标会话"):
                cleaner.clean_session(self.home, self.info(), self.backups, online=True)
            self.assertTrue(path.exists())
            self.assertEqual(self.path.read_bytes(), self.original)
            self.assertEqual(self.rows(), self.original_rows)

    def test_online_cleanup_and_restore_allow_unrelated_loaded_thread(self):
        other = "99999999-2222-3333-4444-555555555555"
        with self.held_native_style_lock(other) as other_lock:
            backup = cleaner.clean_session(self.home, self.info(), self.backups, online=True)
            self.assertNotEqual(self.path.read_bytes(), self.original)
            self.assertTrue(other_lock.exists())
            self.assertFalse((self.home / "thread-writer-locks" / f"{TID}.lock").exists())
            cleaner.restore(self.home, backup, online=True)
            self.assertEqual(self.path.read_bytes(), self.original)
            self.assertEqual(self.rows(), self.original_rows)
            self.assertTrue(other_lock.exists())

    def test_backup_does_not_hold_database_write_lock(self):
        copy = cleaner.shutil.copy2
        checks = []

        def copying(source, destination):
            with closing(sqlite3.connect(self.history, timeout=0.1)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE thread_items SET item_json=item_json WHERE thread_id='other-thread'"
                )
                checks.append(True)
            return copy(source, destination)

        with patch.object(cleaner.shutil, "copy2", copying):
            backup = self.clean()
        self.assertEqual(checks, [True])
        cleaner.restore(self.home, backup)
        self.assertEqual(self.rows(), self.original_rows)

    def test_cache_change_after_snapshot_aborts_before_rollout_replacement(self):
        copy = cleaner.shutil.copy2

        def copying(source, destination):
            with closing(sqlite3.connect(self.history)) as db, db:
                db.execute(
                    "UPDATE thread_items SET item_json='{}' WHERE thread_id=? AND item_id='item-1'",
                    (TID,),
                )
            return copy(source, destination)

        with (
            patch.object(cleaner.shutil, "copy2", copying),
            self.assertRaisesRegex(cleaner.CleanupError, "缓存已被更新"),
        ):
            self.clean()
        self.assertEqual(self.path.read_bytes(), self.original)
        with closing(sqlite3.connect(self.history)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT item_json FROM thread_items WHERE thread_id=? AND item_id='item-1'",
                    (TID,),
                ).fetchone()[0],
                "{}",
            )
            self.assertEqual(
                db.execute("SELECT rollout_end_byte_offset FROM thread_turns").fetchone()[0],
                self.end,
            )

    def test_scrub_preserves_tool_arguments_and_structured_business_data(self):
        arguments = {"type": "image", "url": "https://example.com/catalog-item"}
        business = {"type": "image", "data": {"sku": "example"}}
        item = {
            "type": "mcpToolCall",
            "arguments": arguments,
            "extra_metadata": arguments,
            "result": {
                "structuredContent": business,
                "content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"}],
            },
        }
        clean, count = cleaner.scrub(item, cache=True)
        self.assertEqual(clean["arguments"], arguments)
        self.assertEqual(clean["extra_metadata"], arguments)
        self.assertEqual(clean["result"]["structuredContent"], business)
        self.assertEqual(count, 1)

    def test_restore_rejects_manifest_with_a_different_thread_id(self):
        backup = self.clean()
        before, rows = self.path.read_bytes(), self.rows()
        manifest = json.loads((backup / "manifest.json").read_bytes())
        manifest["thread_id"] = "99999999-2222-3333-4444-555555555555"
        cleaner.save_json(backup / "manifest.json", manifest)
        with self.assertRaisesRegex(cleaner.CleanupError, "会话.*不匹配"):
            cleaner.restore(self.home, backup)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), rows)

    def test_atomic_json_writes_do_not_share_a_temporary_file(self):
        destination = self.root / "settings.json"
        replace = cleaner.os.replace
        entered = False

        def competing_replace(source, target):
            nonlocal entered
            if Path(target) == destination and not entered:
                entered = True
                cleaner.save_json(destination, {"writer": "second"})
            return replace(source, target)

        with patch.object(cleaner.os, "replace", competing_replace):
            cleaner.save_json(destination, {"writer": "first"})
        self.assertEqual(json.loads(destination.read_bytes()), {"writer": "first"})

    def test_interrupted_restore_preserves_cleaned_rollback_snapshot(self):
        backup = self.clean()
        cleaned, cleaned_rows = self.path.read_bytes(), self.rows()
        manifest = json.loads((backup / "manifest.json").read_bytes())
        manifest["status"] = "restoring"
        cleaner.save_json(backup / "manifest.json", manifest)
        (backup / "restore-previous.jsonl").write_bytes(cleaned)
        self.path.write_bytes(self.original)
        transaction = cleaner.cache_transaction

        @contextmanager
        def failing_transaction(path):
            with transaction(path) as db:
                yield db
                raise OSError("simulated restore commit failure")

        with (
            patch.object(cleaner, "cache_transaction", failing_transaction),
            self.assertRaisesRegex(OSError, "simulated"),
        ):
            cleaner.restore(self.home, backup)
        self.assertEqual((backup / "restore-previous.jsonl").read_bytes(), cleaned)
        self.assertEqual(self.path.read_bytes(), cleaned)
        self.assertEqual(self.rows(), cleaned_rows)
        cleaner.restore(self.home, backup)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_scan_tolerates_a_session_removed_after_catalog(self):
        items = cleaner.scan(self.home, on_catalog=lambda _: self.path.unlink())
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].error)

    def test_invalid_cache_stats_are_rescanned(self):
        stat = self.path.stat()
        cache = {
            str(self.path.resolve()): {"signature": [stat.st_size, stat.st_mtime_ns], "stats": {}}
        }
        items = cleaner.scan(self.home, cache=cache)
        self.assertEqual(items[0].images, 5)
        self.assertTrue(items[0].sha256)

    def test_restore_rejects_cache_edits_for_another_thread(self):
        backup = self.clean()
        before, rows = self.path.read_bytes(), self.rows()
        manifest = json.loads((backup / "manifest.json").read_bytes())
        manifest["cache_edits"][0]["keys"]["thread_id"] = "other-thread"
        cleaner.save_json(backup / "manifest.json", manifest)
        with self.assertRaisesRegex(cleaner.CleanupError, "会话 ID 不匹配"):
            cleaner.restore(self.home, backup)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), rows)

    def test_null_session_metadata_is_reported_without_crashing(self):
        self.path.write_bytes(cleaner.encode({"type": "session_meta", "payload": None}) + b"\n")
        self.assertTrue(self.info().error)


if __name__ == "__main__":
    unittest.main()
