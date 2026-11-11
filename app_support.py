"""Version and persistent paths, independent of a frozen executable's extraction path."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_VERSION = "0.4.0rc6"
DATA_ENV = "CODEX_IMAGE_CLEANER_DATA_DIR"


@dataclass(frozen=True)
class AppPaths:
    runtime: Path
    backups: Path


def app_paths() -> AppPaths:
    override = os.environ.get(DATA_ENV)
    if override:
        base = Path(override).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = (
                Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
                / "CodexImageCleaner"
            )
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "CodexImageCleaner"
        else:
            base = (
                Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
                / "codex-image-cleaner"
            )
    else:
        # Keep existing source users' backups/preferences in place. Release ZIPs
        # use an allowlist and never include either of these private directories.
        base = Path(__file__).resolve().parent
    return AppPaths(runtime=base / ".runtime", backups=base / "backups")


def preference_path(value, default: Path) -> Path:
    if isinstance(value, str) and value.strip() and "\x00" not in value:
        try:
            return Path(value).expanduser().resolve()
        except (OSError, ValueError):
            pass
    return default
