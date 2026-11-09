# Contributing / 贡献

欢迎中文或英文 Issue / PR。请说明问题、修改后的行为和验证结果，不要附带真实会话、备份或认证文件。安全问题见 [SECURITY.md](../SECURITY.md)。

Chinese and English contributions are welcome. Include the problem, resulting behavior, and relevant verification. Keep private conversation data out of reports.

## Development

Python 3.11+. The app uses only the standard library; build tools are separate.

```bash
python -m venv .venv-build
# Windows: .venv-build/Scripts/python; macOS: .venv-build/bin/python
python -m pip install -r requirements-build.txt
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -v
node --check ui/app.js
python packaging/verify_web_ui.py
```

Use the virtual environment's interpreter for these commands. Browser checks require Node.js 22+ and Edge (Windows) or Chrome/Edge (Mac); they use synthetic conversations only.

## Structure

| Directory / file | Purpose |
| --- | --- |
| `app.py`, `cleaner.py`, `desktop.py`, `webui.py` | Entry point, cleanup engine, jobs, local server |
| `ui/` | HTML/CSS/JS assets |
| `tests/` | Regression tests and crash worker |
| `packaging/` | Build, browser acceptance, and native Codex probes |
| `docs/` | User guide and preview |

## Build

Build natively on the target OS/architecture. The Codex executable is used with an isolated home and offline model provider; no generation request is sent.

```bash
# Windows x64
python packaging/build_windows.py --verify-codex C:/path/to/codex.exe
# macOS arm64 or x86_64
python3 packaging/build_macos.py --verify-codex /path/to/codex
```

Both builders require static checks, regression/crash tests, and native locking verification, then test the packaged runtime and browser UI. Mac also verifies the extracted ZIP. Repeat `--verify-codex` to test multiple versions.

Source ZIPs use an explicit allowlist. Distribute intended `dist/` artifacts, not the whole working directory. Keep runtime licenses and checksums with the packages. Mac builds are ad-hoc signed, not Developer ID notarized. User-environment acceptance is described in [the guide](../docs/guide.md).

## Changes to data handling

- Preserve text, tool arguments, IDs, ordinals, record counts, JSONL line lengths, and history offsets.
- Back up before mutation; retain recovery markers until terminal manifest state is durable.
- Test failure paths for cleanup, persistence, and restore changes. Never silently accept unknown schemas or remove input limits to make tests pass.
- Render conversation text with `textContent`; retain launch-token, Host/Origin, and request-size checks.
