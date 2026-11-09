# Codex Image Cleaner

[简体中文](README.md) · **English**

Remove image context from local Codex conversation history while keeping text, code, and original image files. Everything runs locally, with automatic backups before changes.

![Workspace preview using synthetic data](docs/images/workspace.png)

## Download

| Platform | Download |
| --- | --- |
| Windows 10/11 x64 | [Windows](https://github.com/0xsuannai/codex-image-cleaner/releases/download/v0.4.0rc4/codex-image-cleaner-0.4.0rc4-windows-x64.zip) |
| Mac · Apple Silicon | [M-series](https://github.com/0xsuannai/codex-image-cleaner/releases/download/v0.4.0rc4/codex-image-cleaner-0.4.0rc4-macos-arm64.zip) |
| Mac · Intel | [Intel](https://github.com/0xsuannai/codex-image-cleaner/releases/download/v0.4.0rc4/codex-image-cleaner-0.4.0rc4-macos-x86_64.zip) |

Extract and run `CodexImageCleaner.exe`, or move the `.app` into Applications and open it. Python is bundled; a modern browser is required. This is a release candidate. Mac builds are verified on macOS 15 and are not Apple-notarized. The application UI is currently Simplified Chinese.

[All releases and checksums](https://github.com/0xsuannai/codex-image-cleaner/releases)

## Usage

1. Stop the task and close the target Codex conversation. Other conversations can stay open.
2. Open the cleaner, search for the conversation, or filter to conversations with images.
3. Select it, click **清理会话图片** (clean images), and confirm the target names.
4. Reopen the conversation afterward. To undo, use **备份与恢复** (backups and restore).

The default conversation home is `~/.codex` or `CODEX_HOME`; change it in directory settings. If online locking is incompatible, fully exit Codex and its IDE extensions, then choose offline processing.

## Before you use it

- This reduces image context, **not disk usage**, and cannot guarantee a fix for network or backend reconnects.
- Restore refuses to overwrite messages added after cleanup. After an interruption, use the repair action first.
- This is an independent tool, not an official OpenAI product. Backups contain conversation data; keep them private.

[Usage and compatibility](docs/guide.md#english) · [Issues](https://github.com/0xsuannai/codex-image-cleaner/issues)

[MIT License](LICENSE) · [Security](SECURITY.md)
