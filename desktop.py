"""UI-independent jobs and explicit confirmation for the local web desktop."""

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import asdict
from pathlib import Path

from app_support import APP_VERSION, app_paths, preference_path
from cleaner import (
    CleanupError,
    ScanCancelled,
    SessionBusy,
    clean_session,
    default_home,
    restore,
    save_json,
    scan,
    validate_manifest,
)
from journal import backup_summary
from safe_json import read_file

logger = logging.getLogger(__name__)


def object_id(path):
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def eligible(info):
    return info.scanned and not info.error and info.images + info.cache_images > 0


def optional_json(path, fallback, limit=16 * 1024 * 1024):
    try:
        return read_file(path, limit=limit)
    except (OSError, ValueError, RecursionError):
        return fallback


class Desktop:
    def __init__(self, home, *, paths=None, preferences=True):
        self.paths = paths or app_paths()
        prefs = optional_json(self.paths.runtime / "preferences.json", {}) if preferences else {}
        prefs = prefs if isinstance(prefs, dict) else {}
        self.home = Path(home).expanduser().resolve()
        if self.home == default_home():
            self.home = preference_path(prefs.get("home"), self.home)
        self.backups = preference_path(prefs.get("backups"), self.paths.backups)
        cached = optional_json(self.paths.runtime / "scan-cache.json", {}) if preferences else {}
        self.cache = (
            cached.get("files", {})
            if isinstance(cached, dict) and cached.get("version") == 2
            else {}
        )
        if not isinstance(self.cache, dict):
            self.cache = {}
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.items = {}
        self.backup_paths = {}
        self.confirmation = None
        self.revision = 0
        self.busy = ""
        self.status = "准备读取本地会话"
        self.progress = [0, 0]
        self.report = None
        self.worker = None

    def snapshot(self, revision=None):
        with self.lock:
            result = {
                "version": APP_VERSION,
                "revision": self.revision,
                "busy": self.busy,
                "status": self.status,
                "progress": self.progress[:],
                "report": self.report,
                "home": str(self.home),
                "backups": str(self.backups),
            }
            if revision != str(self.revision):
                result["sessions"] = [
                    dict(asdict(info), key=key, eligible=eligible(info))
                    for key, info in self.items.items()
                ]
            return result

    def _start(self, operation, job):
        with self.lock:
            if self.busy:
                raise CleanupError("正在处理会话，请等待当前操作完成")
            self.busy = operation
            self.confirmation = None
            self.cancel.clear()
            self.progress = [0, 0]
            self.status = {
                "scan": "正在查找本地会话…",
                "clean": "正在备份并清理…",
                "restore": "正在恢复图片…",
            }[operation]

            def run():
                try:
                    job()
                except ScanCancelled:
                    with self.lock:
                        self.status = "扫描已停止，已完成的结果仍可使用"
                except Exception as exc:
                    logger.exception("Desktop job failed: %s", operation)
                    with self.lock:
                        self.status = "操作未完成，请查看详情"
                        self.report = {
                            "message": self.status,
                            "completed": [],
                            "skipped": [],
                            "failed": [{"title": "会话处理", "error": str(exc)}],
                        }
                finally:
                    with self.lock:
                        self.busy = ""
                        self.revision += 1

            self.worker = threading.Thread(target=run, daemon=False, name="cleaner-" + operation)
            self.worker.start()

    def _catalog(self, items):
        with self.lock:
            self.items = {object_id(info.path): info for info in items}
            self.revision += 1

    def _session(self, info):
        with self.lock:
            self.items[object_id(info.path)] = info
            self.revision += 1

    def _progress(self, done, total):
        with self.lock:
            self.progress = [done, total]
            self.status = f"正在检查图片 {done} / {total}"

    def _scan(self):
        if not any((self.home / folder).is_dir() for folder in ("sessions", "archived_sessions")):
            self._catalog([])
            with self.lock:
                self.status = "没有找到会话，可从“更多 → 目录设置”选择位置"
            return
        try:
            items = scan(
                self.home,
                self._progress,
                on_catalog=self._catalog,
                on_session=self._session,
                cancel=self.cancel,
                cache=self.cache,
            )
            self._catalog(items)
            with self.lock:
                self.status = (
                    f"已找到 {len(items)} 个会话 · {sum(eligible(i) for i in items)} 个可清理图片"
                )
        finally:
            try:
                self.paths.runtime.mkdir(parents=True, exist_ok=True)
                save_json(
                    self.paths.runtime / "scan-cache.json", {"version": 2, "files": self.cache}
                )
            except OSError:
                logger.warning("Scan cache could not be saved")

    def refresh(self):
        self._start("scan", self._scan)

    def stop(self):
        with self.lock:
            if self.busy == "scan":
                self.cancel.set()
                self.status = "正在停止扫描…"

    def settings(self, body):
        with self.lock:
            if self.busy:
                raise CleanupError("请等待当前操作完成")
            values = []
            for name in ("home", "backups"):
                value = body.get(name)
                if not isinstance(value, str) or not value.strip() or "\0" in value:
                    raise CleanupError("请填写完整的目录路径")
                path = Path(value).expanduser()
                if not path.is_absolute():
                    raise CleanupError("请使用完整的目录路径")
                values.append(path.resolve())
            home, backups = values
            if not any((home / folder).is_dir() for folder in ("sessions", "archived_sessions")):
                raise CleanupError("没有找到会话目录，请选择包含 sessions 的 Codex 目录")
            if backups.is_relative_to(home):
                raise CleanupError("请把备份放在 Codex 目录以外")
            self.paths.runtime.mkdir(parents=True, exist_ok=True)
            save_json(
                self.paths.runtime / "preferences.json",
                {"home": str(home), "backups": str(backups)},
            )
            self.home, self.backups = home, backups
            self.backup_paths = {}
            self.confirmation = None
            self.items = {}
            self.revision += 1
            self.refresh()

    def list_backups(self):
        with self.lock:
            if self.busy:
                raise CleanupError("请等待当前操作完成")
            entries, paths = [], {}
            for index, path in enumerate(
                sorted(self.backups.glob("*/manifest.json"), reverse=True)
            ):
                if index >= 5000:
                    break
                try:
                    item = backup_summary(path)
                    if not isinstance(item, dict):
                        continue
                    key = object_id(path.resolve())
                    entries.append(
                        {
                            "key": key,
                            "title": str(item.get("title") or "未命名会话"),
                            "status": str(item.get("status", "unknown")),
                            "modified": path.stat().st_mtime,
                        }
                    )
                    paths[key] = path
                except (OSError, ValueError, TypeError, RecursionError):
                    continue
            self.backup_paths = paths
            return entries

    def prepare(self, body):
        with self.lock:
            if self.busy:
                raise CleanupError("请等待当前操作完成")
            kind = body.get("kind", "clean")
            if kind == "clean":
                keys = body.get("keys")
                if (
                    not isinstance(keys, list)
                    or not 1 <= len(keys) <= 1000
                    or any(not isinstance(k, str) for k in keys)
                ):
                    raise CleanupError("请选择需要清理的会话（每批最多 1000 个）")
                chosen = [self.items.get(key) for key in dict.fromkeys(keys)]
                if any(info is None or not eligible(info) for info in chosen):
                    raise CleanupError("部分会话没有可清理图片、尚未检查或需要修复，请重新选择")
                target = chosen
                titles = [i.title for i in chosen]
            elif kind in ("restore", "repair"):
                key = body.get("key")
                if not isinstance(key, str):
                    raise CleanupError("请选择备份")
                if kind == "repair":
                    info = self.items.get(key)
                    path = (
                        Path(info.recovery_backup) / "manifest.json"
                        if info and info.recovery_backup
                        else None
                    )
                else:
                    path = self.backup_paths.get(key)
                if path is None:
                    raise CleanupError("备份已变化，请刷新后重新选择")
                manifest = validate_manifest(read_file(path))
                target = path.parent
                titles = [manifest.get("title") or "未命名会话"]
            else:
                raise CleanupError("不支持的操作")
            nonce = secrets.token_urlsafe(32)
            self.confirmation = {
                "nonce": nonce,
                "kind": kind,
                "target": target,
                "titles": titles,
                "expires": time.monotonic() + 180,
            }
            return {"confirmation": nonce, "kind": kind, "titles": titles}

    def execute(self, body):
        with self.lock:
            challenge = self.confirmation
            if (
                self.busy
                or not challenge
                or body.get("acknowledged") is not True
                or not isinstance(body.get("confirmation"), str)
                or not secrets.compare_digest(challenge["nonce"], body["confirmation"])
                or time.monotonic() > challenge["expires"]
                or body.get("mode") not in ("online", "offline")
            ):
                raise CleanupError("请重新确认目标会话已关闭")
            self.confirmation = None
            online = body["mode"] == "online"

            def job():
                completed, skipped, failed = [], [], []
                if challenge["kind"] == "clean":
                    for index, info in enumerate(challenge["target"]):
                        self._progress(index, len(challenge["target"]))
                        with self.lock:
                            self.status = f"正在备份并清理 {index + 1} / {len(challenge['target'])}"
                        try:
                            backup = clean_session(self.home, info, self.backups, online=online)
                            completed.append(
                                {
                                    "title": info.title,
                                    "backup": str(backup) if backup else "无需清理",
                                }
                            )
                            self.cache.pop(info.path, None)
                        except SessionBusy as exc:
                            skipped.append({"title": info.title, "error": str(exc)})
                        except Exception as exc:
                            logger.exception("Cleanup failed")
                            failed.append({"title": info.title, "error": str(exc)})
                    message = (
                        f"已清理 {len(completed)} 个会话的历史图片"
                        if completed
                        else "本次没有清理成功的会话"
                    )
                else:
                    restore(self.home, challenge["target"], online=online)
                    completed.append(
                        {"title": challenge["titles"][0], "backup": str(challenge["target"])}
                    )
                    self.cache.clear()
                    message = "已恢复会话图片"
                if skipped:
                    message += f" · {len(skipped)} 个仍被占用，已跳过"
                if failed:
                    message += f" · {len(failed)} 个未完成"
                next_step = (
                    "重新打开已处理的会话，再尝试发送消息。"
                    if completed
                    else "请查看详情；占用的会话关闭后可重试。"
                )
                with self.lock:
                    self.report = {
                        "message": message,
                        "next_step": next_step,
                        "completed": completed,
                        "skipped": skipped,
                        "failed": failed,
                    }
                    self.status = next_step
                self._scan()
                with self.lock:
                    self.status = next_step

            self._start("clean" if challenge["kind"] == "clean" else "restore", job)

    def close(self):
        self.stop()
        if self.worker and self.worker.is_alive():
            self.worker.join()
