"""Test-only subprocess: terminate at actual persistence boundaries, bypassing finally."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cleaner  # noqa: E402
from journal import marker_path  # noqa: E402


def main():
    action, phase, home, rollout, backup = sys.argv[1:]
    home, rollout, backup = Path(home), Path(rollout), Path(backup)
    save = cleaner.save_json
    replace = cleaner.os.replace
    apply = cleaner.apply_edits
    transaction = cleaner.cache_transaction

    def exit_now():
        print("checkpoint:" + phase, flush=True)
        os._exit(87)

    def saving(path, value):
        save(path, value)
        terminal = "cleaned" if action == "clean" else "restored"
        if (
            phase == "prepared"
            and path.name == "manifest.json"
            and value.get("status") == "prepared"
        ):
            exit_now()
        if phase == "marker" and path == marker_path(rollout):
            exit_now()
        if phase == "finished" and path.name == "manifest.json" and value.get("status") == terminal:
            exit_now()

    def replacing(source, destination):
        result = replace(source, destination)
        if phase == "replaced" and Path(destination) == rollout:
            exit_now()
        return result

    def applying(db, edits, **kw):
        apply(db, edits, **kw)
        if phase == "cache":
            exit_now()

    @contextmanager
    def committing(path):
        with transaction(path) as db:
            yield db
        if phase == "committed":
            exit_now()

    with (
        patch.object(cleaner, "save_json", saving),
        patch.object(cleaner.os, "replace", replacing),
        patch.object(cleaner, "apply_edits", applying),
        patch.object(cleaner, "cache_transaction", committing),
    ):
        if action == "clean":
            cleaner.clean_session(home, cleaner.inspect_session(rollout), backup)
        else:
            cleaner.restore(home, backup)
    raise RuntimeError("The requested crash checkpoint was not reached")


if __name__ == "__main__":
    main()
