"""Browser-rendered acceptance against source or the frozen EXE, with no real sessions."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from safe_json import read_file  # noqa: E402
from webui import edge_executable  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def verify(executable=None, output=None):
    node = shutil.which("node")
    edge = edge_executable()
    if not node or not edge:
        raise RuntimeError(
            "Browser acceptance requires Node.js 22+ and Edge (Windows) or Chrome/Edge (macOS)"
        )
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as temporary:
        scratch = Path(temporary)
        ready = scratch / "browser-ready.json"
        destination = Path(output).resolve() if output else scratch / "screenshots"
        destination.mkdir(parents=True, exist_ok=True)
        command = (
            [str(Path(executable).resolve())]
            if executable
            else [getattr(sys, "_base_executable", sys.executable), str(ROOT / "app.py")]
        )
        process = subprocess.Popen(
            command + ["--browser-self-test", str(ready)],
            cwd=ROOT,
            env=dict(os.environ, PYTHONUTF8="1"),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        url = None
        last_stage = "process_start"
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Acceptance host exited before becoming ready")
                if ready.exists():
                    data = read_file(ready, limit=4096)
                    last_stage = data.get("stage", last_stage)
                    if data.get("status") == "error":
                        raise RuntimeError(
                            "Acceptance host failed: " + data.get("error", "unknown error")
                        )
                    url = data.get("url")
                    if url:
                        break
                time.sleep(0.05)
            if not url:
                raise RuntimeError("Acceptance host did not start; last stage: " + last_stage)
            browser_result = subprocess.run(
                [
                    node,
                    str(ROOT / "packaging/browser_smoke.cjs"),
                    url,
                    str(edge),
                    str(scratch / "edge"),
                    str(destination),
                ],
                cwd=ROOT,
                check=False,
                timeout=120,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if browser_result.returncode:
                raise RuntimeError("Browser interaction failed: " + browser_result.stderr[-4000:])
            browser_checks = json.loads(browser_result.stdout.strip().splitlines()[-1])
            if (
                any(
                    browser_checks.get(name) != "ok"
                    for name in (
                        "rendered_ui",
                        "search_selection",
                        "confirmation_gate",
                        "cleanup_restore",
                        "small_layout",
                        "fixed_viewport",
                        "list_scroll",
                        "motion_feedback",
                        "activity_state",
                        "reduced_motion",
                        "selection_scope",
                        "recovery_guidance",
                        "empty_states",
                        "connection_recovery",
                        "pagination",
                    )
                )
                or browser_checks.get("script_errors") != 0
                or browser_checks.get("external_requests") != 0
            ):
                raise RuntimeError("Browser interaction evidence is incomplete")
            process.wait(timeout=20)
            if process.returncode or read_file(ready).get("synthetic_roundtrip") != "ok":
                raise RuntimeError("Browser acceptance did not restore original data")
            return {
                "status": "passed",
                "rendered_ui": "ok",
                "synthetic_roundtrip": "ok",
                "real_user_sessions_modified": False,
                "checks": browser_checks,
            }
        finally:
            if process.poll() is None and url:
                from urllib.parse import urlsplit

                parsed = urlsplit(url)
                token = parsed.fragment.removeprefix("token=")
                request = urllib.request.Request(
                    f"http://{parsed.netloc}/api/shutdown",
                    data=b"{}",
                    headers={
                        "Authorization": "Bearer " + token,
                        "Origin": f"http://{parsed.netloc}",
                        "Content-Type": "application/json",
                    },
                )
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        request, timeout=5
                    ):
                        pass
                    process.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify local web UI with constructed data")
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.exe, arguments.output)))
