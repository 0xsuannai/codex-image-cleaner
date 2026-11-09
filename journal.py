"""Small recovery pointers next to rollouts, independent of the configured backup folder."""

from pathlib import Path

from safe_json import read_file


def marker_path(rollout: Path) -> Path:
    return rollout.with_name("." + rollout.name + ".image-cleaner.json")


def read_marker(rollout: Path):
    path = marker_path(rollout)
    if not path.exists():
        return None
    value = read_file(path, limit=16 * 1024)
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("intent") not in {"clean", "restore"}
        or not isinstance(value.get("backup"), str)
        or not Path(value["backup"]).is_absolute()
        or value.get("path") != str(rollout.resolve())
    ):
        raise ValueError("未完成操作的恢复标记损坏，请保留文件并检查备份")
    return value


def marker(rollout: Path, backup: Path, intent: str):
    return {
        "version": 1,
        "intent": intent,
        "path": str(rollout.resolve()),
        "backup": str(backup.resolve()),
    }


def backup_summary(manifest: Path):
    try:
        value = read_file(manifest.parent / "summary.json", limit=64 * 1024)
        stat = manifest.stat()
        if (
            isinstance(value, dict)
            and value.get("source_signature") == [stat.st_size, stat.st_mtime_ns]
            and isinstance(value.get("title"), str)
            and isinstance(value.get("status"), str)
        ):
            return value
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    return read_file(manifest)
