"""Release gates with sanitized evidence. Does not publish artifacts or run real user threads."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app_support import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]


def command(name, arguments, *, env=None, timeout=240):
    print("Checking:", name, flush=True)
    started = time.monotonic()
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(name + " did not pass; artifacts were not approved")
    return {
        "check": name,
        "status": "passed",
        "seconds": round(time.monotonic() - started, 2),
    }, result


def run_gates(codex_binaries):
    env = dict(os.environ, PYTHONUTF8="1")
    checks = []
    for name, args in (
        ("static_analysis", [sys.executable, "-m", "ruff", "check", "."]),
        ("formatting", [sys.executable, "-m", "ruff", "format", "--check", "."]),
        (
            "regression_and_process_crashes",
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-q",
                "-s",
                "tests",
            ],
        ),
    ):
        check, result = command(name, args, env=env)
        if name == "regression_and_process_crashes":
            output = result.stdout + result.stderr
            count = re.search(r"Ran (\d+) tests?", output)
            if not count or "skipped=" in output:
                raise RuntimeError("Regression gate did not run all required tests")
            check["test_count"] = int(count.group(1))
        checks.append(check)
    if not codex_binaries:
        raise RuntimeError("A release needs at least one verified Codex executable")
    matrix = []
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    for binary in codex_binaries:
        binary = Path(binary).expanduser().resolve()
        if not binary.is_file():
            raise RuntimeError("A requested Codex test executable was not found")
        with tempfile.TemporaryDirectory(dir=runtime) as temporary:
            if not Path(temporary).resolve().is_relative_to(runtime.resolve()):
                raise RuntimeError("Version probe escaped runtime directory")
            version_env = dict(env, CODEX_HOME=temporary)
            version_result = subprocess.run(
                [str(binary), "--version"],
                cwd=ROOT,
                env=version_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=20,
            )
        match = re.fullmatch(
            r"codex-cli ([0-9][0-9A-Za-z.+-]{0,79})", version_result.stdout.strip()
        )
        if not match:
            raise RuntimeError("Unrecognized Codex version response")
        version = match.group(1)
        check, result = command(
            "native_codex_" + version,
            [sys.executable, "packaging/verify_codex_runtime.py", str(binary)],
            env=env,
            timeout=90,
        )
        last = json.loads(result.stdout.strip().splitlines()[-1])
        required = (
            "native_online_cleanup_roundtrip",
            "other_loaded_thread_wrote_during_backup",
            "cleaned_thread_resumed_by_codex",
            "resumed_thread_protected",
        )
        if (
            not all(last.get(key) is True for key in required)
            or last.get("real_user_sessions_modified") is not False
        ):
            raise RuntimeError("Native compatibility evidence is incomplete")
        matrix.append({"version": version, "status": "passed", "seconds": check["seconds"]})
    return {
        "version": APP_VERSION,
        "local_checks": checks,
        "codex_matrix": matrix,
        "external_acceptance": {
            "clean_second_machine": "not_run",
            "long_term_field_use": "not_run",
        },
        "release_status": "candidate_external_acceptance_pending",
    }
