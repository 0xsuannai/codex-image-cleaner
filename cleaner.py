"""Local-only Codex image cleanup. Standard library only; no network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing, contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

import safe_json
from journal import marker, marker_path, read_marker
from safe_json import loads as safe_loads

PLACEHOLDER = "[图片已由本地清理工具移除]"
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
MARKERS = (
    b"input_image",
    b"output_image",
    b'"image"',
    b'"image_url"',
    b'"user_message"',
    b'"image_generation_call"',
    b'"imageGeneration"',
)
CACHE_TABLES = {
    "thread_turns": ("thread_id", "turn_id"),
    "thread_items": ("thread_id", "turn_id", "item_id"),
    "thread_realtime_items": ("thread_id", "item_id"),
    "thread_history_projection_state": ("thread_id",),
}
OFFSET_COLUMNS = {"rollout_byte_offset", "rollout_end_byte_offset", "next_rollout_byte_offset"}
OPAQUE_FIELDS = {
    "arguments",
    "input_schema",
    "inputSchema",
    "structuredContent",
    "parameters",
    "$defs",
    "definitions",
}
SHA256 = re.compile(r"[0-9a-f]{64}$")


class CleanupError(Exception):
    pass


class ScanCancelled(CleanupError):
    pass


class SessionBusy(CleanupError):
    """A live Codex writer or coordination operation owns the relevant lock."""


class RecoveryRequired(CleanupError):
    """An interrupted operation must be resolved before another cleanup."""


def default_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_json(path: Path, value):
    """Durable manifest update, including during interrupted operations."""
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            raw = encode(value)
            if len(raw) > safe_json.MAX_MANIFEST_BYTES:
                raise CleanupError("备份清单超过安全大小上限，未修改会话")
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_manifest(backup: Path, value):
    path = backup / "manifest.json"
    save_json(path, value)
    # Optional small sidecar: listing hundreds of backups must not parse all of
    # their original inline image data. Restoration still validates the full file.
    with suppress(OSError):
        stat = path.stat()
        summary = {key: value.get(key) for key in ("title", "thread_id", "status", "original_size")}
        summary["source_signature"] = [stat.st_size, stat.st_mtime_ns]
        save_json(backup / "summary.json", summary)


def scrub(value, *, cache=False, _image_slot=True, replacement=PLACEHOLDER):
    """Replace typed image blocks only. Never regex-replace conversation text."""
    count = 0
    if isinstance(value, list):
        result = []
        for item in value:
            clean, n = scrub(item, cache=cache, _image_slot=_image_slot, replacement=replacement)
            result.append(clean)
            count += n
        return result, count
    if not isinstance(value, dict):
        return value, 0
    kind = value.get("type")
    if (
        _image_slot
        and kind in ("input_image", "output_image")
        and any(k in value for k in ("image_url", "file_id", "image", "data"))
    ):
        return {"type": "input_text", "text": replacement}, 1
    if _image_slot and kind == "image_url" and "image_url" in value:
        return {"type": "text", "text": replacement}, 1
    if (
        _image_slot
        and kind == "image"
        and any(isinstance(value.get(k), str) for k in ("url", "fileId", "data"))
    ):
        result = {"type": "text", "text": replacement}
        if cache:
            result["text_elements"] = []
        return result, 1
    result = {}
    for key, item in value.items():
        if key in OPAQUE_FIELDS:
            result[key] = item
        elif _image_slot and kind == "imageGeneration" and key == "fileId":
            # File IDs take precedence over inline result in the current Codex schema.
            # Switch to the valid empty inline variant, preserving the generation item.
            result["result"] = ""
            count += 1
        elif (
            _image_slot
            and kind == "user_message"
            and key in ("images", "local_images")
            and isinstance(item, list)
        ):
            result[key] = []
            count += len(item)
        elif (
            _image_slot
            and kind in ("image_generation_call", "imageGeneration")
            and key == "result"
            and isinstance(item, str)
            and item
        ):
            result[key] = ""
            count += 1
        elif (
            _image_slot
            and kind == "imageGeneration"
            and key == "inputImages"
            and isinstance(item, list)
        ):
            result[key] = []
            count += len(item)
        else:
            result[key], n = scrub(
                item,
                cache=cache,
                _image_slot=key in {"content", "output", "replacement_history", "item"},
                replacement=replacement,
            )
            count += n
    if _image_slot and kind == "imageGeneration" and "fileId" in value:
        result["result"] = ""
    return result, count


def clean_line(raw: bytes):
    if not any(marker in raw for marker in MARKERS):
        return raw, 0
    value = safe_loads(raw)
    # Metadata and arbitrary records may contain example tool schemas. Leave them alone.
    if value.get("type") not in ("response_item", "event_msg", "compacted"):
        return raw, 0
    payload, count = scrub(value.get("payload"))
    if not count:
        return raw, 0
    value["payload"] = payload
    newline = b"\r\n" if raw.endswith(b"\r\n") else b"\n" if raw.endswith(b"\n") else b""
    clean = encode(value) + newline
    if len(clean) > len(raw):
        # Durable image IDs may be shorter than a translated placeholder. A short
        # marker keeps even those records compatible with position-preserving IO.
        value["payload"], _ = scrub(safe_loads(raw).get("payload"), replacement="[img]")
        clean = encode(value) + newline
    if len(clean) > len(raw):
        raise CleanupError("替换后记录增大，无法保持历史位置，已停止以保护会话")
    return clean, count


def preserve_layout(original: bytes, clean: bytes):
    """Keep line boundaries and every SQLite byte offset valid, even across a crash."""
    if len(clean) > len(original):
        raise CleanupError("不能扩大历史记录")
    ending = b"\r\n" if clean.endswith(b"\r\n") else b"\n" if clean.endswith(b"\n") else b""
    body = clean[: -len(ending)] if ending else clean
    return body + b" " * (len(original) - len(clean)) + ending


def file_signature(stat):
    # Atomic replacement can intentionally preserve both length and mtime. File
    # identity is therefore part of cache validity, not just size/timestamp.
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_ctime_ns]


@dataclass
class Session:
    path: str
    thread_id: str = ""
    title: str = ""
    cwd: str = ""
    modified: float = 0
    size: int = 0
    images: int = 0
    saving: int = 0
    cache_images: int = 0
    cache_saving: int = 0
    sha256: str = ""
    error: str = ""
    preview: str = ""
    title_source: str = ""
    archived: bool = False
    is_subagent: bool = False
    agent_name: str = ""
    scanned: bool = True
    recovery_backup: str = ""


def inspect_session(path: Path, cancel=None) -> Session:
    info = Session(str(path.resolve()))
    hasher = hashlib.sha256()
    try:
        stat = path.stat()
        info.modified, info.size = stat.st_mtime, stat.st_size
        with path.open("rb") as stream:
            for index, raw in enumerate(safe_json.records(stream)):
                if cancel is not None and cancel.is_set():
                    raise ScanCancelled("扫描已停止，已完成的结果仍可使用")
                hasher.update(raw)
                # Validate every line before offering a file for mutation.
                value = safe_loads(raw)
                if not isinstance(value, dict):
                    raise CleanupError("会话包含非对象记录")
                if index == 0:
                    meta = value.get("payload", {})
                    if not isinstance(meta, dict):
                        raise CleanupError("不支持的会话元数据")
                    if value.get("type") != "session_meta" or not UUID.fullmatch(
                        str(meta.get("id", ""))
                    ):
                        raise CleanupError("不支持的会话元数据")
                    info.thread_id = meta["id"]
                    info.cwd = meta.get("cwd", "")
                clean, count = clean_line(raw)
                info.images += count
                info.saving += len(raw) - len(clean)
        if not info.thread_id:
            raise CleanupError("空会话")
        if (stat.st_size, stat.st_mtime_ns) != (path.stat().st_size, path.stat().st_mtime_ns):
            raise CleanupError("会话扫描期间发生变化，请刷新")
        info.sha256 = hasher.hexdigest()
    except ScanCancelled:
        raise
    except (ValueError, OSError, CleanupError, RecursionError) as exc:
        info.error = f"无法安全读取：{exc}"
    return info


def meaningful_title(value):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split()).strip()
    return "" if UUID.fullmatch(value) else value


def user_text(text):
    """Skip injected workspace instructions when deriving a human-readable title."""
    if not isinstance(text, str):
        return ""
    request = re.search(r"(?im)^\s*#{1,4}\s*(?:My request|我的请求|用户请求)\s*[:：]?\s*", text)
    if request:
        text = text[request.end() :]
    elif text.lstrip().startswith(
        (
            "# AGENTS.md",
            "<environment_context>",
            "<external_codex_apps_",
            "<permissions instructions>",
        )
    ):
        return ""
    return text.strip()[:3000]


def title_details(home: Path):
    """Prefer explicit names; metadata gaps fall back to actual user messages."""
    home = home.expanduser().resolve()
    details = {}
    databases = sorted(
        home.glob("state_*.sqlite"),
        key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1,
    )
    for path in databases:
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                if "id" not in columns:
                    continue
                wanted = [
                    c
                    for c in ("id", "name", "title", "first_user_message", "preview")
                    if c in columns
                ]
                for row in db.execute("SELECT " + ",".join(wanted) + " FROM threads"):
                    row = dict(row)
                    item = details.setdefault(row["id"], {})
                    for column in ("name", "title"):
                        if value := meaningful_title(row.get(column)):
                            item.update(
                                title=value,
                                title_source="会话名称" if column == "name" else "会话标题",
                            )
                            break
                    preview = user_text(row.get("first_user_message")) or user_text(
                        row.get("preview")
                    )
                    if preview:
                        item["preview"] = preview
        except sqlite3.Error:
            continue
    # The append-only name index reflects explicit renames and wins over generated titles.
    index = home / "session_index.jsonl"
    if index.exists():
        with suppress(OSError, ValueError), index.open("rb") as stream:
            for line in safe_json.records(stream):
                try:
                    row = safe_loads(line)
                    if not isinstance(row, dict):
                        continue
                    tid = row.get("id") or row.get("thread_id")
                    title = meaningful_title(
                        row.get("thread_name") or row.get("name") or row.get("title")
                    )
                    if tid and title:
                        details.setdefault(tid, {}).update(title=title, title_source="会话名称")
                except (ValueError, KeyError, TypeError, RecursionError):
                    continue
    return details


def read_titles(home: Path):
    return {tid: item["title"] for tid, item in title_details(home).items() if item.get("title")}


def catalog(home: Path):
    """Load useful names quickly, before decoding gigabytes of image history."""
    home = home.expanduser().resolve()
    details = title_details(home)
    paths = {}
    for folder in ("sessions", "archived_sessions"):
        for candidate in (home / folder).rglob("rollout-*.jsonl"):
            try:
                paths[candidate] = candidate.stat()
            except OSError:
                continue  # A live client may archive/delete a session during enumeration.
    result = []
    for path in sorted(paths, key=lambda p: paths[p].st_mtime, reverse=True):
        stat = paths[path]
        info = Session(
            path=str(path.resolve()),
            modified=stat.st_mtime,
            size=stat.st_size,
            archived=path.is_relative_to(home / "archived_sessions"),
            scanned=False,
        )
        try:
            with path.open("rb") as stream:
                raw = safe_json.read_record(stream)
                meta_record = safe_loads(raw)
                meta = meta_record.get("payload", {})
                if meta_record.get("type") != "session_meta" or not UUID.fullmatch(
                    str(meta.get("id", ""))
                ):
                    raise CleanupError("缺少有效会话元数据")
                info.thread_id = meta["id"]
                info.cwd = meta.get("cwd", "")
                source = meta.get("source")
                info.is_subagent = isinstance(source, dict) and "subagent" in source
                info.agent_name = (
                    meta.get("agent_nickname")
                    or str(meta.get("agent_path") or "").rsplit("/", 1)[-1]
                )
                item = details.get(info.thread_id, {})
                info.title = item.get("title", "")
                info.title_source = item.get("title_source", "")
                info.preview = item.get("preview", "")
                if not info.preview:
                    # Bound startup work. Full validation and image counting happen later.
                    budget = 2 * 1024 * 1024
                    for _ in range(100):
                        raw = stream.readline(min(budget + 1, 256 * 1024))
                        budget -= len(raw)
                        if not raw or budget <= 0 or not raw.endswith(b"\n"):
                            break
                        record = safe_loads(raw)
                        if not isinstance(record, dict) or not isinstance(
                            record.get("payload"), dict
                        ):
                            continue
                        payload = record.get("payload", {})
                        text = ""
                        if payload.get("type") == "user_message":
                            text = payload.get("message", "")
                        elif payload.get("type") == "message" and payload.get("role") == "user":
                            text = "\n".join(
                                v.get("text", "")
                                for v in payload.get("content", [])
                                if isinstance(v, dict) and v.get("type") in ("input_text", "text")
                            )
                        if preview := user_text(text):
                            info.preview = preview
                            break
                if not info.title and info.preview:
                    info.title = " ".join(info.preview.split())[:160]
                    info.title_source = "首条用户消息"
                if not info.title:
                    if info.is_subagent:
                        info.title = "子任务 · " + (info.agent_name or "未命名")
                    else:
                        workspace = Path(info.cwd.replace("\\", "/")).name or "本地"
                        info.title = (
                            workspace
                            + "会话 · "
                            + datetime.fromtimestamp(info.modified).strftime("%m-%d %H:%M")
                        )
                    info.title_source = "本地摘要"
        except (
            ValueError,
            OSError,
            CleanupError,
            AttributeError,
            TypeError,
            RecursionError,
        ) as exc:
            info.error = f"无法读取会话信息：{exc}"
            info.title = "无法读取的会话 · " + datetime.fromtimestamp(info.modified).strftime(
                "%m-%d %H:%M"
            )
        try:
            pending = read_marker(path)
            if pending:
                info.recovery_backup = pending["backup"]
                info.error = "检测到未完成的清理或恢复，请先修复此会话"
        except (ValueError, OSError, RecursionError):
            info.error = "恢复标记无法读取，请保留文件并检查备份"
        result.append(info)
    return result


def scan(home: Path, progress=None, *, on_catalog=None, on_session=None, cancel=None, cache=None):
    home = home.expanduser().resolve()
    entries = catalog(home)
    if on_catalog:
        on_catalog(entries)
    result = []
    for index, entry in enumerate(entries):
        if cancel is not None and cancel.is_set():
            raise ScanCancelled("扫描已停止，已完成的结果仍可使用")
        path = Path(entry.path)
        try:
            stat = path.stat()
            signature = file_signature(stat)
        except OSError:
            signature = None
        previous = cache.get(entry.path, {}) if cache is not None else {}
        if not isinstance(previous, dict) or not isinstance(previous.get("stats", {}), dict):
            previous = {}
        stats = previous.get("stats", {})
        valid_stats = (
            isinstance(stats.get("sha256"), str)
            and bool(SHA256.fullmatch(stats["sha256"]))
            and type(stats.get("images")) is int
            and stats["images"] >= 0
            and type(stats.get("saving")) is int
            and stats.get("error") == ""
        )
        if signature is not None and valid_stats and previous.get("signature") == signature:
            cached = previous.get("stats", {})
            info = replace(
                entry,
                **{
                    key: cached[key]
                    for key in ("images", "saving", "sha256", "error")
                    if key in cached
                },
                scanned=True,
            )
        else:
            checked = inspect_session(path, cancel)
            info = replace(
                entry,
                images=checked.images,
                saving=checked.saving,
                sha256=checked.sha256,
                error=checked.error or entry.error,
                scanned=True,
            )
            if cache is not None and not info.error:
                cache[entry.path] = {
                    "signature": signature,
                    "stats": {
                        key: getattr(info, key) for key in ("images", "saving", "sha256", "error")
                    },
                }
        info.error = info.error or entry.error
        result.append(info)
        if on_session:
            on_session(info)
        if progress:
            progress(index + 1, len(entries))
    history = cache_path(home)
    if history:
        by_id = {info.thread_id: info for info in result}
        with closing(sqlite3.connect(history.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("thread_items", "thread_realtime_items"):
                if table not in tables:
                    continue
                query = f'SELECT thread_id, CASE WHEN length(CAST(item_json AS BLOB))<=? THEN item_json ELSE NULL END FROM "{table}"'
                for tid, raw in db.execute(query, (safe_json.MAX_RECORD_BYTES,)):
                    if cancel is not None and cancel.is_set():
                        raise ScanCancelled("扫描已停止，已完成的结果仍可使用")
                    info = by_id.get(tid)
                    if info is None:
                        continue
                    if not isinstance(raw, str):
                        info.error = info.error or "历史缓存单条记录超过安全上限，请保留原会话"
                        continue
                    try:
                        parsed = safe_loads(raw)
                        if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
                            raise ValueError("无效的历史缓存记录")
                    except (ValueError, TypeError, RecursionError):
                        info.error = info.error or "历史缓存记录损坏，已禁止清理此会话"
                        continue
                    if not any(
                        marker in raw
                        for marker in (
                            '"image"',
                            '"input_image"',
                            '"output_image"',
                            '"imageGeneration"',
                            '"image_url"',
                        )
                    ):
                        continue
                    try:
                        clean, n = scrub(parsed, cache=True)
                    except RecursionError:
                        info.error = info.error or "历史缓存嵌套过深，已禁止清理此会话"
                        continue
                    info.cache_images += n
                    if n:
                        info.cache_saving += len(raw.encode("utf-8")) - len(encode(clean))
    if on_session:
        for info in result:
            on_session(info)
    return result


def checked_session_path(home: Path, path: Path):
    home, path = home.resolve(), path.resolve()
    if path.suffix != ".jsonl" or not path.name.startswith("rollout-"):
        raise CleanupError("只允许处理 rollout 会话文件")
    if not any(path.is_relative_to(home / folder) for folder in ("sessions", "archived_sessions")):
        raise CleanupError("会话路径超出选定的 Codex 目录（不支持外链目录）")
    return path


def check_identity(path: Path, thread_id: str, *, rollout=True):
    try:
        with path.open("rb") as stream:
            record = safe_loads(safe_json.read_record(stream))
        actual = record.get("payload", {}).get("id")
        if (
            record.get("type") != "session_meta"
            or actual != thread_id
            or not UUID.fullmatch(thread_id)
            or (rollout and not path.stem.endswith(thread_id))
        ):
            raise CleanupError("备份、文件名与当前会话 ID 不匹配")
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise CleanupError("无法校验会话身份，已停止操作") from exc


def validate_manifest(value):
    """Treat imported backup manifests as untrusted input, including SQL row keys."""
    try:
        if type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("version")
        for key in ("home", "path", "thread_id", "status", "original_sha256", "clean_sha256"):
            if not isinstance(value[key], str) or not value[key]:
                raise ValueError(key)
        if not UUID.fullmatch(value["thread_id"]):
            raise ValueError("thread_id")
        if not all(SHA256.fullmatch(value[k]) for k in ("original_sha256", "clean_sha256")):
            raise ValueError("sha256")
        if value["status"] not in {"prepared", "cleaned", "restoring", "restored", "rolled_back"}:
            raise ValueError("status")
        if type(value["original_mtime_ns"]) is not int or value["original_mtime_ns"] < 0:
            raise ValueError("mtime")
        if value["history_path"] is not None and not isinstance(value["history_path"], str):
            raise ValueError("history_path")
        if not isinstance(value["cache_edits"], list):
            raise ValueError("cache_edits")
        for edit in value["cache_edits"]:
            table = edit["table"]
            if table not in CACHE_TABLES or set(edit["keys"]) != set(CACHE_TABLES[table]):
                raise ValueError("table keys")
            if edit["keys"]["thread_id"] != value["thread_id"]:
                raise ValueError("cache thread_id")
            if not all(isinstance(v, str) and v for v in edit["keys"].values()):
                raise ValueError("row keys")
            before, after = edit["before"], edit["after"]
            if (
                not before
                or set(before) != set(after)
                or not set(before) <= OFFSET_COLUMNS | {"item_json"}
            ):
                raise ValueError("edit columns")
            for fields in (before, after):
                for key, item in fields.items():
                    if key == "item_json":
                        if not isinstance(item, str):
                            raise ValueError("item_json")
                        safe_loads(item)
                    elif type(item) is not int or item < 0:
                        raise ValueError("offset")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CleanupError("备份清单无效，或会话 ID 不匹配；已停止操作") from exc
    return value


def pending_operation(path: Path, *, restoring_from=None):
    try:
        value = read_marker(path)
    except (ValueError, OSError, RecursionError) as exc:
        if restoring_from is not None:
            # restore() has independently validated the backup's identity and will
            # verify rollout hashes and every cache field before replacing anything.
            return {"backup": str(restoring_from), "damaged_marker": True}
        raise RecoveryRequired("恢复标记损坏或无法读取，请保留文件并检查备份") from exc
    if value and (
        restoring_from is None or Path(value["backup"]).resolve() != restoring_from.resolve()
    ):
        raise RecoveryRequired(f"此会话有未完成操作，请先恢复备份：{value['backup']}")
    return value


@contextmanager
def file_lock(path: Path, message: str):
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SessionBusy(message) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def writer_lock(home: Path, thread_id: str, *, online=False):
    """Hold coordination while owning the target lock, including its file lifecycle.

    Codex deletes per-thread lock files on unload. Coordination prevents a resumed
    writer from locking a different inode while cleanup replaces the rollout.
    Other already-loaded threads keep their own locks and can continue writing.
    """
    if not UUID.fullmatch(thread_id):
        raise CleanupError("无效会话 ID")
    directory = home / "thread-writer-locks"
    coordination = directory / ".coordination.lock"
    if online and not coordination.is_file():
        raise CleanupError(
            "未检测到兼容的 Codex 写锁机制，不能确认在线清理安全。请完全退出 Codex 后选择离线模式。"
        )
    directory.mkdir(exist_ok=True)
    with file_lock(coordination, "Codex 正在切换或释放会话，本次已跳过，请稍后重试。"):
        path = directory / f"{thread_id}.lock"
        acquired = False
        try:
            with file_lock(
                path,
                "目标会话仍被 Codex 加载或使用，本次已跳过。请停止任务并关闭该会话，稍后重试；其他会话可继续使用。",
            ):
                acquired = True
                yield
        finally:
            if acquired:
                # Keep the coordination lock until the handle is closed and the
                # temporary per-thread lock file is removed, matching Codex's order.
                with suppress(OSError):
                    path.unlink(missing_ok=True)


def cache_path(home: Path):
    paths = list(home.glob("thread_history_*.sqlite"))
    if any(p.name != "thread_history_1.sqlite" for p in paths):
        raise CleanupError("发现尚未支持的历史数据库版本，已停止以保护会话")
    return paths[0] if paths else None


def cache_edits(db, thread_id, offsets):
    """Plan in-place projection updates; ordinals, IDs and record counts stay intact."""
    if db is None:
        return []
    db.row_factory = sqlite3.Row
    edits = []
    edit_bytes = 0
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Unknown per-thread tables could carry additional offsets. Refuse new schema versions.
    for table in present - set(CACHE_TABLES) - {"_sqlx_migrations", "sqlite_sequence"}:
        if table.startswith("thread_"):
            raise CleanupError(f"历史数据库包含不支持的表：{table}")
    for table, keys in CACHE_TABLES.items():
        if table not in present:
            continue
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if not set(keys) <= columns:
            raise CleanupError(f"历史表结构不支持：{table}")
        if any("byte_offset" in c and c not in OFFSET_COLUMNS for c in columns):
            raise CleanupError(f"发现未知历史位置字段：{table}")
        for row in db.execute(f'SELECT * FROM "{table}" WHERE thread_id=?', (thread_id,)):
            before, after = {}, {}
            for column in columns & OFFSET_COLUMNS:
                old = row[column]
                if old is None:
                    continue
                if old not in offsets:
                    raise CleanupError("历史缓存位置未对应完整的会话记录，请勿手动修改文件")
                new = offsets[old]
                if new != old:
                    before[column], after[column] = old, new
            if "item_json" in columns:
                old = row["item_json"]
                if (
                    not isinstance(old, str)
                    or len(old.encode("utf-8")) > safe_json.MAX_RECORD_BYTES
                ):
                    raise CleanupError("单条历史缓存超过安全上限或损坏，已停止操作")
                parsed = safe_loads(old)
                if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
                    raise CleanupError("无效的历史缓存记录，已停止操作")
                clean, count = scrub(parsed, cache=True)
                if count:
                    before["item_json"], after["item_json"] = old, encode(clean).decode("utf-8")
            if before:
                edit_bytes += len(encode(before)) + len(encode(after))
                if edit_bytes > safe_json.MAX_CACHE_EDIT_BYTES:
                    raise CleanupError("此会话的缓存编辑量超过 128 MiB 安全上限，未修改原会话")
                edits.append(
                    {
                        "table": table,
                        "keys": {key: row[key] for key in keys},
                        "before": before,
                        "after": after,
                    }
                )
    return edits


def apply_edits(db, edits, *, reverse=False):
    if not edits:
        return
    if db is None:
        raise CleanupError("恢复所需的历史数据库不存在")
    for edit in edits:
        table = edit["table"]
        if table not in CACHE_TABLES or set(edit["keys"]) != set(CACHE_TABLES[table]):
            raise CleanupError("备份历史表标识不合法")
        before, after = (
            (edit["after"], edit["before"]) if reverse else (edit["before"], edit["after"])
        )
        allowed = OFFSET_COLUMNS | {"item_json"}
        if set(before) != set(after) or not set(before) <= allowed:
            raise CleanupError("备份历史字段不合法")
        keys = edit["keys"]
        where = " AND ".join(f'"{key}" IS ?' for key in keys)
        row = db.execute(f'SELECT * FROM "{table}" WHERE {where}', tuple(keys.values())).fetchone()
        if row is None:
            raise CleanupError("历史缓存已变化，拒绝覆盖")
        current = dict(row)
        if all(current[column] == value for column, value in after.items()):
            continue  # Idempotent crash recovery.
        if not all(current[column] == value for column, value in before.items()):
            raise CleanupError("历史缓存已被更新，拒绝覆盖")
        setting = ",".join(f'"{column}"=?' for column in after)
        db.execute(
            f'UPDATE "{table}" SET {setting} WHERE {where}', (*after.values(), *keys.values())
        )


@contextmanager
def cache_transaction(path):
    if path is None:
        yield None
        return
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def cache_snapshot(path):
    if path is None:
        yield None
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        try:
            yield db
        finally:
            db.rollback()


def clean_session(home: Path, info: Session, backup_root: Path, *, online=False) -> Path | None:
    home = home.resolve()
    path = checked_session_path(home, Path(info.path))
    if info.error or not info.sha256:
        raise CleanupError(info.error or "请先扫描会话")
    check_identity(path, info.thread_id)
    with writer_lock(home, info.thread_id, online=online):
        pending_operation(path)
        if digest(path) != info.sha256:
            raise CleanupError("会话已发生变化，请刷新列表后重试")
        history = cache_path(home)
        # Same filesystem as rollout: replacement must be atomic.
        fd, name = tempfile.mkstemp(prefix=".image-cleaner-", suffix=".tmp", dir=path.parent)
        staged = Path(name)
        offsets = {0: 0}
        old_position = new_position = count = 0
        backup = None
        replaced = committed = False
        manifest = None
        try:
            with os.fdopen(fd, "wb") as output, path.open("rb") as source:
                for raw in safe_json.records(source):
                    safe_loads(raw)  # Fail closed on malformed or ambiguous JSONL.
                    clean, n = clean_line(raw)
                    if n:
                        clean = preserve_layout(raw, clean)
                    output.write(clean)
                    count += n
                    old_position += len(raw)
                    new_position += len(clean)
                    offsets[old_position] = new_position
                output.flush()
                os.fsync(output.fileno())
            # Plan from a read snapshot and write backups before taking SQLite's
            # database-wide write lock. Other sessions can write during this work.
            with cache_snapshot(history) as db:
                edits = cache_edits(db, info.thread_id, offsets)
            if not count and not edits:
                return None
            backup_root = backup_root.resolve()
            if backup_root.is_relative_to(home):
                raise CleanupError("请将备份保存在 Codex 目录以外")
            backup = backup_root / (
                datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            )
            backup.mkdir(parents=True)
            shutil.copy2(path, backup / "original.jsonl")
            if digest(backup / "original.jsonl") != info.sha256 or digest(path) != info.sha256:
                raise CleanupError("备份期间会话发生变化，已停止")
            with (backup / "original.jsonl").open("r+b") as stream:
                os.fsync(stream.fileno())
            manifest = {
                "version": 1,
                "status": "prepared",
                "home": str(home),
                "path": str(path),
                "thread_id": info.thread_id,
                "title": info.title,
                "original_sha256": info.sha256,
                "clean_sha256": digest(staged),
                "original_size": path.stat().st_size,
                "clean_size": staged.stat().st_size,
                "original_mtime_ns": path.stat().st_mtime_ns,
                "images": count,
                "cache_edits": edits,
                "history_path": str(history) if history else None,
                "layout": "byte_preserving",
            }
            save_manifest(backup, manifest)
            save_json(marker_path(path), marker(path, backup, "clean"))
            with cache_transaction(history) as db:
                apply_edits(db, edits)
                os.replace(staged, path)
                replaced = True
                # Preserve ordering in the Codex sidebar; cache offsets already match new bytes.
                os.utime(path, ns=(path.stat().st_atime_ns, manifest["original_mtime_ns"]))
            committed = True
            manifest["status"] = "cleaned"
            save_manifest(backup, manifest)
            marker_path(path).unlink(missing_ok=True)
            return backup
        except BaseException as exc:
            # SQLite rolled back if commit failed. Put the matching rollout back too.
            if replaced and not committed:
                shutil.copy2(backup / "original.jsonl", staged)
                with staged.open("r+b") as stream:
                    os.fsync(stream.fileno())
                os.replace(staged, path)
            if manifest is not None:
                manifest["status"] = "cleaned" if committed else "rolled_back"
                with suppress(OSError):
                    save_manifest(backup, manifest)
                    marker_path(path).unlink(missing_ok=True)
            if committed:
                raise CleanupError(f"会话已清理，但备份状态写入失败；请保留 {backup}") from exc
            raise
        finally:
            staged.unlink(missing_ok=True)


def restore(home: Path, backup: Path, *, online=False):
    home, backup = home.resolve(), backup.resolve()
    try:
        manifest = validate_manifest(safe_json.read_file(backup / "manifest.json"))
    except (OSError, ValueError) as exc:
        raise CleanupError("无法读取备份清单") from exc
    if manifest.get("version") != 1 or Path(manifest["home"]).resolve() != home:
        raise CleanupError("备份版本或 Codex 目录不匹配")
    path = checked_session_path(home, Path(manifest["path"]))
    original = backup / "original.jsonl"
    check_identity(path, manifest["thread_id"])
    if digest(original) != manifest["original_sha256"]:
        raise CleanupError("备份校验失败")
    check_identity(original, manifest["thread_id"], rollout=False)
    history = cache_path(home)
    if (str(history) if history else None) != manifest["history_path"]:
        raise CleanupError("历史数据库位置已变化")
    with writer_lock(home, manifest["thread_id"], online=online):
        pending = pending_operation(path, restoring_from=backup)
        if manifest["status"] == "restored" and not pending:
            raise CleanupError("此备份已经恢复")
        current_hash = digest(path)
        if current_hash not in (manifest["clean_sha256"], manifest["original_sha256"]):
            raise CleanupError("清理后会话已有新内容，拒绝用旧备份覆盖")
        fd, name = tempfile.mkstemp(prefix=".image-restore-", suffix=".tmp", dir=path.parent)
        staged = Path(name)
        os.close(fd)
        replaced = committed = False
        previous = backup / "restore-previous.jsonl"
        try:
            if manifest["status"] == "restoring" and previous.exists():
                if digest(previous) not in (manifest["clean_sha256"], manifest["original_sha256"]):
                    raise CleanupError("恢复回滚副本校验失败，请保留备份")
            else:
                shutil.copy2(path, previous)
                with previous.open("r+b") as stream:
                    os.fsync(stream.fileno())
            shutil.copyfile(original, staged)
            if digest(staged) != manifest["original_sha256"]:
                raise CleanupError("备份在恢复期间发生变化，已停止操作")
            with staged.open("r+b") as stream:
                os.fsync(stream.fileno())
            manifest["status"] = "restoring"
            save_manifest(backup, manifest)
            save_json(marker_path(path), marker(path, backup, "restore"))
            with cache_transaction(history) as db:
                apply_edits(db, manifest["cache_edits"], reverse=True)
                if digest(path) != current_hash:
                    raise CleanupError("恢复期间会话发生变化")
                os.replace(staged, path)
                replaced = True
                os.utime(path, ns=(path.stat().st_atime_ns, manifest["original_mtime_ns"]))
            committed = True
            manifest["status"] = "restored"
            save_manifest(backup, manifest)
            marker_path(path).unlink(missing_ok=True)
        except BaseException as exc:
            if replaced and not committed:
                shutil.copy2(previous, staged)
                with staged.open("r+b") as stream:
                    os.fsync(stream.fileno())
                os.replace(staged, path)
            if committed:
                raise CleanupError(f"会话已恢复，但备份状态保存失败；请保留 {backup}") from exc
            raise
        finally:
            staged.unlink(missing_ok=True)


def main():
    from app_support import APP_VERSION

    parser = argparse.ArgumentParser(description="Codex 会话图片清理（本地处理，自动备份）")
    parser.add_argument("--home", type=Path, default=default_home())
    parser.add_argument("--version", action="version", version=APP_VERSION)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scan", action="store_true", help="只读扫描并输出 JSON")
    mode.add_argument("--diagnostics", type=Path, help="导出不含会话正文和本机路径的诊断 JSON")
    parser.add_argument("--output", type=Path, help="扫描结果保存路径（会包含私人会话标题和路径）")
    args = parser.parse_args()
    if args.output and not args.scan:
        parser.error("--output 需要与 --scan 一起使用")
    if args.diagnostics:
        from diagnostics import collect

        output = args.diagnostics.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        save_json(output, collect(args.home))
        return
    if args.scan:
        if args.output is None and sys.stdout is None:
            raise CleanupError("此窗口程序没有控制台，请用 --output 指定扫描结果保存路径")
        result = [asdict(s) for s in scan(args.home)]
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            save_json(output, result)
        else:
            print(json.dumps(result, ensure_ascii=True, indent=2))
    else:
        from webui import launch

        launch(args.home)


if __name__ == "__main__":
    main()
