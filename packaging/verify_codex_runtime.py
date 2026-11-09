"""Probe actual Codex locking using an isolated home; never opens real sessions."""

import base64
import contextlib
import json
import os
import queue
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cleaner  # noqa: E402
from cleaner import (  # noqa: E402
    CleanupError,
    SessionBusy,
    clean_session,
    digest,
    inspect_session,
    restore,
    writer_lock,
)


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python packaging/verify_codex_runtime.py <path-to-codex-executable>"
        )
    binary = sys.argv[1]
    runtime = Path(__file__).resolve().parents[1] / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as temp:
        base = Path(temp).resolve()
        assert base.is_relative_to(runtime)
        home = base / "home"
        home.mkdir()
        workspace = base / "workspace"
        workspace.mkdir()
        (home / "config.toml").write_text(
            """model = "offline-probe"
    model_provider = "offline"
    thread_unload_delay_secs = 1
    [model_providers.offline]
    name = "Offline probe"
    base_url = "http://127.0.0.1:1/v1"
    wire_api = "responses"
    requires_openai_auth = false
    """,
            encoding="utf-8",
        )
        env = dict(os.environ, CODEX_HOME=str(home))
        events = queue.Queue()
        with (base / "stderr.log").open("w", encoding="utf-8") as error:
            process = subprocess.Popen(
                [binary, "app-server"],
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=error,
                text=True,
                encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )

            def read():
                for line in process.stdout:
                    with contextlib.suppress(ValueError):
                        events.put(json.loads(line))

            threading.Thread(target=read, daemon=True).start()

            def send(method, params=None, rid=None):
                value = {"method": method}
                if params is not None:
                    value["params"] = params
                if rid is not None:
                    value["id"] = rid
                process.stdin.write(json.dumps(value) + "\n")
                process.stdin.flush()

            def response(rid):
                end = time.monotonic() + 30
                while time.monotonic() < end:
                    value = events.get(timeout=max(0.1, end - time.monotonic()))
                    if value.get("id") == rid:
                        if "error" in value:
                            raise RuntimeError(value["error"])
                        return value["result"]
                raise TimeoutError(str(rid))

            try:
                send(
                    "initialize",
                    {
                        "clientInfo": {"name": "image-cleaner-lock-test", "version": "0.1.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                    1,
                )
                response(1)
                send("initialized")
                send(
                    "thread/start",
                    {
                        "cwd": str(workspace),
                        "model": "offline-probe",
                        "modelProvider": "offline",
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                    2,
                )
                thread = response(2)["thread"]
                tid = thread["id"]

                def chunk(tag, data):
                    return (
                        struct.pack(">I", len(data))
                        + tag
                        + data
                        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
                    )

                png = (
                    b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x22\x44\x66" * 64) * 64))
                    + chunk(b"IEND", b"")
                )
                image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                send(
                    "thread/inject_items",
                    {
                        "threadId": tid,
                        "items": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "Synthetic image cleanup test"},
                                    {"type": "input_image", "image_url": image},
                                ],
                            }
                        ],
                    },
                    4,
                )
                response(4)
                lock = home / "thread-writer-locks" / f"{tid}.lock"
                print(json.dumps({"started": True, "lock_exists": lock.exists()}), flush=True)
                try:
                    with writer_lock(home, tid):
                        blocked = False
                except CleanupError:
                    blocked = True
                print(json.dumps({"loaded_thread_blocked": blocked}), flush=True)
                assert blocked, "Native Codex writer lock is not enforced by cleaner lock"
                send("thread/unsubscribe", {"threadId": tid}, 3)
                print(json.dumps({"unsubscribe": response(3)}), flush=True)
                time.sleep(1.7)
                print(
                    json.dumps(
                        {
                            "lock_file_after_unload": lock.exists(),
                            "coordination_lock": (
                                home / "thread-writer-locks" / ".coordination.lock"
                            ).exists(),
                        }
                    ),
                    flush=True,
                )
                released = False
                end = time.monotonic() + 12
                while time.monotonic() < end:
                    try:
                        with writer_lock(home, tid):
                            released = True
                    except CleanupError:
                        pass
                    if released:
                        break
                    time.sleep(0.25)
                print(
                    json.dumps(
                        {
                            "unloaded_thread_released": released,
                            "other_codex_process_still_alive": process.poll() is None,
                        }
                    ),
                    flush=True,
                )
                assert released
                path = next((home / "sessions").rglob("*" + tid + ".jsonl"))
                original = digest(path)
                send(
                    "thread/start",
                    {
                        "cwd": str(workspace),
                        "model": "offline-probe",
                        "modelProvider": "offline",
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                    5,
                )
                other = response(5)["thread"]["id"]
                copied = cleaner.shutil.copy2

                def copy_while_other_thread_writes(source, destination):
                    send(
                        "thread/inject_items",
                        {
                            "threadId": other,
                            "items": [
                                {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "Other loaded thread can still persist items",
                                        }
                                    ],
                                }
                            ],
                        },
                        6,
                    )
                    response(6)
                    return copied(source, destination)

                with patch.object(cleaner.shutil, "copy2", copy_while_other_thread_writes):
                    backup = clean_session(
                        home, inspect_session(path), base / "backups", online=True
                    )
                assert backup and digest(path) != original
                assert inspect_session(path).images == 0
                restore(home, backup, online=True)
                assert digest(path) == original
                backup = clean_session(home, inspect_session(path), base / "backups", online=True)
                send("thread/resume", {"threadId": tid}, 7)
                response(7)
                try:
                    clean_session(home, inspect_session(path), base / "backups", online=True)
                except SessionBusy:
                    pass
                else:
                    raise AssertionError("A resumed live thread was incorrectly writable")
                print(
                    json.dumps(
                        {
                            "native_online_cleanup_roundtrip": True,
                            "other_loaded_thread_wrote_during_backup": True,
                            "cleaned_thread_resumed_by_codex": True,
                            "resumed_thread_protected": True,
                            "real_user_sessions_modified": False,
                        }
                    ),
                    flush=True,
                )
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=8)
                if os.name != "nt":
                    # Native Codex can spawn plugin-clone helpers. Keep them inside
                    # this probe's process group and stop them before temp cleanup.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                process.stdout.close()


if __name__ == "__main__":
    main()
