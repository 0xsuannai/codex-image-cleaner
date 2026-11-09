"""Build an allowlisted Windows bundle. Never copies local sessions or preferences."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT))
from quality import run_gates  # noqa: E402

from app_support import APP_VERSION  # noqa: E402

BUNDLE_FILES = (
    "README.md",
    "README.en.md",
    "docs/guide.md",
    "docs/images/workspace.png",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)

SOURCE_FILES = (
    *BUNDLE_FILES,
    "app.py",
    "app_support.py",
    "safe_json.py",
    "journal.py",
    "instance.py",
    "diagnostics.py",
    "packaging/quality.py",
    "tests/crash_worker.py",
    "cleaner.py",
    "desktop.py",
    "codex_process.py",
    "webui.py",
    "runtime_check.py",
    "packaging/verify_web_ui.py",
    "tests/test_webui.py",
    "tests/test_codex_process.py",
    "ui/index.html",
    "ui/style.css",
    "ui/app.js",
    "ui/icon.svg",
    "tests/test_cleaner.py",
    "tests/test_support.py",
    "tests/test_resilience.py",
    "packaging/verify_codex_runtime.py",
    "SECURITY.md",
    ".github/CONTRIBUTING.md",
    "requirements-build.txt",
    "pyproject.toml",
    "启动工具.cmd",
    "packaging/build_windows.py",
    "packaging/build_macos.py",
    "启动工具.command",
    "packaging/windows_version.txt",
    "packaging/browser_smoke.cjs",
    "packaging/acceptance.ps1",
    "packaging/验收工具.cmd",
    ".github/workflows/checks.yml",
    ".github/workflows/macos.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md",
    ".gitignore",
    ".gitattributes",
)


def safe_output(name: str) -> Path:
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise RuntimeError("Build output escaped the project directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_files():
    files = [ROOT / name for name in SOURCE_FILES]
    missing = [str(p.relative_to(ROOT)) for p in files if not p.is_file()]
    if missing:
        raise RuntimeError("Missing release source files: " + ", ".join(missing))
    return files


def licenses(destination: Path):
    destination.mkdir(exist_ok=True)
    sources = {
        "Python-LICENSE.txt": Path(sys.base_prefix) / "LICENSE.txt",
    }
    distribution = importlib.metadata.distribution("pyinstaller")
    license_file = next(p for p in distribution.files if str(p).endswith("licenses/COPYING.txt"))
    sources["PyInstaller-COPYING.txt"] = distribution.locate_file(license_file)
    for name, path in sources.items():
        if not path.is_file():
            raise RuntimeError("Runtime license missing: " + str(path))
        shutil.copyfile(path, destination / name)


def build(codex_binaries):
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise SystemExit("Build this Windows x64 distribution using 64-bit Python on Windows.")
    files = source_files()
    evidence = run_gates(codex_binaries)
    dist, work = safe_output("dist"), safe_output("build")
    temp = safe_output(".runtime/build-temp")
    env = dict(
        os.environ,
        TEMP=str(temp),
        TMP=str(temp),
        PYINSTALLER_CONFIG_DIR=str(safe_output("build/pyinstaller-cache")),
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            "CodexImageCleaner",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(work),
            "--version-file",
            str(ROOT / "packaging/windows_version.txt"),
            "--add-data",
            str(ROOT / "ui") + os.pathsep + "ui",
            str(ROOT / "app.py"),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    executable = dist / "CodexImageCleaner.exe"
    report = work / "packaged-self-test.json"
    report.unlink(missing_ok=True)
    env["CODEX_IMAGE_CLEANER_DATA_DIR"] = str(work / "self-test-profile")
    isolated = safe_output("build/portable-smoke/独立目录")
    isolated_exe = isolated / executable.name
    shutil.copyfile(executable, isolated_exe)
    system_root = Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
    env["PATH"] = os.pathsep.join((str(system_root / "System32"), str(system_root)))
    env["PYTHONPATH"] = ""
    env["PYTHONHOME"] = str(work / "no-system-python")
    env.pop("TCL_LIBRARY", None)
    env.pop("TK_LIBRARY", None)
    subprocess.run(
        [str(isolated_exe), "--self-test", str(report)],
        cwd=isolated,
        env=env,
        check=True,
        timeout=60,
    )
    result = json.loads(report.read_bytes())
    if result.get("status") != "ok" or not result.get("frozen"):
        raise RuntimeError("Packaged self-test did not pass")
    diagnostic_report = work / "packaged-cli-diagnostics.json"
    diagnostic_report.unlink(missing_ok=True)
    subprocess.run(
        [
            str(isolated_exe),
            "--home",
            str(work / "unused-codex-home"),
            "--diagnostics",
            str(diagnostic_report),
        ],
        cwd=isolated,
        env=env,
        check=True,
        timeout=60,
    )
    if json.loads(diagnostic_report.read_bytes()).get("application_version") != APP_VERSION:
        raise RuntimeError("Packaged diagnostic export did not pass")
    result["diagnostic_export"] = "ok"
    evidence["packaged_runtime"] = result
    from verify_web_ui import verify

    evidence["browser_acceptance"] = verify(executable)
    bundle_info = {
        "application_version": APP_VERSION,
        "file_name": executable.name,
        "bytes": executable.stat().st_size,
        "sha256": sha256(executable),
    }
    bundle_info_path = dist / "BUNDLE_INFO.json"
    bundle_info_path.write_text(json.dumps(bundle_info, indent=2), encoding="utf-8")
    shutil.copyfile(bundle_info_path, isolated / "BUNDLE_INFO.json")
    acceptance_directory = safe_output("build/acceptance-local")
    powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "packaging/acceptance.ps1"),
            "-BundleDirectory",
            str(isolated),
            "-ReportDirectory",
            str(acceptance_directory),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=90,
    )
    acceptance = json.loads((acceptance_directory / "acceptance-summary.json").read_bytes())
    if (
        acceptance.get("status") != "passed"
        or acceptance.get("clean_machine_user_confirmed") is not False
    ):
        raise RuntimeError("Local acceptance harness failed or misreported a clean machine")
    evidence["acceptance_harness"] = {"status": "passed", "clean_machine_user_confirmed": False}
    notices = safe_output("build/runtime-licenses")
    licenses(notices)
    source_zip = dist / f"codex-image-cleaner-{APP_VERSION}-source.zip"
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            name = path.relative_to(ROOT).as_posix()
            if path.suffix == ".command":
                info = zipfile.ZipInfo.from_file(path, name)
                info.create_system = 3
                info.external_attr = 0o100755 << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
            else:
                archive.write(path, name)
    portable_zip = dist / f"codex-image-cleaner-{APP_VERSION}-windows-x64.zip"
    with zipfile.ZipFile(portable_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(executable, executable.name)
        for name in BUNDLE_FILES:
            archive.write(ROOT / name, name)
        archive.write(bundle_info_path, "BUNDLE_INFO.json")
        archive.write(ROOT / "packaging/acceptance.ps1", "acceptance.ps1")
        archive.write(ROOT / "packaging/验收工具.cmd", "验收工具.cmd")
        for path in notices.iterdir():
            archive.write(path, "licenses/" + path.name)
    evidence_path = dist / f"release-evidence-{APP_VERSION}.json"
    evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    artifacts = [executable, source_zip, portable_zip, evidence_path]
    (dist / "SHA256SUMS.txt").write_text(
        "\n".join(f"{sha256(p)}  {p.name}" for p in artifacts) + "\n", encoding="ascii"
    )
    print(
        json.dumps(
            {
                "version": APP_VERSION,
                "self_test": result,
                "artifacts": [
                    {"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)}
                    for p in artifacts
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build only after regression and native compatibility gates pass"
    )
    parser.add_argument(
        "--verify-codex",
        action="append",
        required=True,
        type=Path,
        help="Codex binary for isolated validation; repeat to verify multiple versions",
    )
    build(parser.parse_args().verify_codex)
