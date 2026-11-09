"""Build and verify a native macOS app bundle using synthetic data only."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from build_windows import BUNDLE_FILES, ROOT, safe_output, sha256, source_files
from quality import run_gates
from verify_web_ui import verify

from app_support import APP_VERSION


def runtime_licenses(destination):
    destination.mkdir(parents=True, exist_ok=True)
    package = importlib.metadata.distribution("pyinstaller")
    license_file = next(p for p in package.files if str(p).endswith("licenses/COPYING.txt"))
    shutil.copyfile(package.locate_file(license_file), destination / "PyInstaller-COPYING.txt")
    # These are license texts only. Application/runtime code comes from the locked build tools.
    upstream = {
        "Python-LICENSE.txt": f"https://raw.githubusercontent.com/python/cpython/v{platform.python_version()}/LICENSE",
    }
    for name, url in upstream.items():
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read(1024 * 1024)
        if b"copyright" not in content.lower():
            raise RuntimeError("Runtime license text could not be verified: " + name)
        (destination / name).write_bytes(content)


def build(codex_binaries):
    architecture = platform.machine()
    if sys.platform != "darwin" or architecture not in ("arm64", "x86_64"):
        raise SystemExit("Build on a native Apple Silicon or Intel Mac.")
    source_files()
    evidence = run_gates(codex_binaries)
    dist, work = safe_output("dist"), safe_output("build/macos")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            "--name",
            "CodexImageCleaner",
            "--target-arch",
            architecture,
            "--osx-bundle-identifier",
            "io.github.suannai.codex-image-cleaner",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(work),
            "--add-data",
            str(ROOT / "ui") + os.pathsep + "ui",
            str(ROOT / "app.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    app = dist / "CodexImageCleaner.app"
    binary = app / "Contents/MacOS/CodexImageCleaner"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    report = work / "packaged-runtime.json"
    report.unlink(missing_ok=True)
    env = dict(
        os.environ,
        CODEX_IMAGE_CLEANER_DATA_DIR=str(work / "isolated-profile"),
        PYTHONPATH="",
        PYTHONHOME=str(work / "no-system-python"),
        PATH="/usr/bin:/bin:/usr/sbin:/sbin",
    )
    subprocess.run(
        [str(binary), "--self-test", str(report)], env=env, cwd=work, check=True, timeout=90
    )
    result = json.loads(report.read_bytes())
    if result.get("status") != "ok" or not result.get("frozen"):
        raise RuntimeError("macOS packaged runtime verification failed")
    evidence.update(
        platform="macOS",
        architecture=architecture,
        packaged_runtime=result,
        browser_acceptance=verify(binary),
        signing="ad-hoc; not Developer ID notarized",
    )
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        staging = Path(temporary)
        shutil.copytree(app, staging / app.name, symlinks=True)
        for name in BUNDLE_FILES:
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        runtime_licenses(staging / "licenses")
        archive = dist / f"codex-image-cleaner-{APP_VERSION}-macos-{architecture}.zip"
        # ditto preserves executable modes, symlinks and the bundle's ad-hoc signature.
        subprocess.run(
            ["ditto", "-c", "-k", "--sequesterRsrc", str(staging), str(archive)], check=True
        )
    evidence_path = dist / f"release-evidence-{APP_VERSION}-macos-{architecture}.json"
    evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    checksum = dist / f"SHA256SUMS-macos-{architecture}.txt"
    checksum.write_text(
        "\n".join(f"{sha256(p)}  {p.name}" for p in (archive, evidence_path)) + "\n",
        encoding="ascii",
    )
    # Verify the actual archive after extraction, not just the build directory.
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        extracted = Path(temporary)
        subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
        executable = extracted / app.name / "Contents/MacOS/CodexImageCleaner"
        roundtrip_report = work / "archive-runtime.json"
        subprocess.run(
            [str(executable), "--self-test", str(roundtrip_report)],
            env=env,
            cwd=work,
            check=True,
            timeout=90,
        )
        if json.loads(roundtrip_report.read_bytes()).get("status") != "ok":
            raise RuntimeError("Extracted macOS archive did not pass runtime checks")
    print(
        json.dumps(
            {
                "version": APP_VERSION,
                "architecture": architecture,
                "status": "passed",
                "artifacts": [archive.name, evidence_path.name, checksum.name],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-codex", action="append", required=True, type=Path)
    build(parser.parse_args().verify_codex)
