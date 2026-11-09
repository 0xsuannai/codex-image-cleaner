"""Opt-in diagnostics: versions, counts and schema compatibility, never user content/paths."""

import platform
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import safe_json
from app_support import APP_VERSION
from cleaner import CACHE_TABLES, OFFSET_COLUMNS


def collect(home: Path):
    home = home.expanduser().resolve()
    report = {
        "application_version": APP_VERSION,
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "privacy": "No conversation content, thread IDs, account data or local paths are collected.",
        "limits": {
            "record_bytes": safe_json.MAX_RECORD_BYTES,
            "cache_edit_bytes": safe_json.MAX_CACHE_EDIT_BYTES,
        },
        "session_counts": {},
        "history_databases": {},
        "observed_codex_versions": [],
    }
    for folder in ("sessions", "archived_sessions"):
        try:
            report["session_counts"][folder] = sum(
                1 for _ in (home / folder).rglob("rollout-*.jsonl")
            )
        except OSError:
            report["session_counts"][folder] = "unreadable"
    for path in home.glob("thread_history_*.sqlite"):
        if not re.fullmatch(r"thread_history_\d+\.sqlite", path.name):
            continue
        info = {"known_database_version": path.name == "thread_history_1.sqlite", "tables": {}}
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                tables = {
                    r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                info["unknown_thread_table_count"] = sum(
                    t.startswith("thread_") and t not in CACHE_TABLES for t in tables
                )
                for table, keys in CACHE_TABLES.items():
                    if table not in tables:
                        continue
                    columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                    info["tables"][table] = {
                        "keys_present": set(keys) <= columns,
                        "unknown_offset_fields": sum(
                            "byte_offset" in c and c not in OFFSET_COLUMNS for c in columns
                        ),
                        "rows": db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0],
                    }
        except sqlite3.Error:
            info["status"] = "unreadable"
        report["history_databases"][path.name] = info
    versions = set()
    for path in home.glob("state_*.sqlite"):
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                for row in db.execute("SELECT DISTINCT cli_version FROM threads LIMIT 50"):
                    if isinstance(row[0], str) and re.fullmatch(
                        r"[0-9][0-9A-Za-z.+-]{0,79}", row[0]
                    ):
                        versions.add(row[0])
        except sqlite3.Error:
            continue
    report["observed_codex_versions"] = sorted(versions)
    return report
