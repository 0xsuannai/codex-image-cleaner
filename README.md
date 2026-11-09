# Codex Image Cleaner

**简体中文** · [English](README.en.md)

清理 Codex 会话历史中的图片上下文，保留文字、代码和原始图片文件。全部本地处理，修改前自动备份。

![界面预览（演示数据）](docs/images/workspace.png)

## 下载

| 系统 | 下载 |
| --- | --- |
| Windows 10/11 x64 | [Windows 版](https://github.com/0xsuannai/codex-image-cleaner/releases/download/v0.4.0rc4/codex-image-cleaner-0.4.0rc4-windows-x64.zip) |
| Mac · Apple Silicon | [M 系列芯片版](https://github.com/0xsuannai/codex-image-cleaner/releases/download/v0.4.0rc4/codex-image-cleaner-0.4.0rc4-macos-arm64.zip) |
| Mac · Intel | [Intel 版](https://github.com/0xsuannai/codex-image-cleaner/releases/download/v0.4.0rc4/codex-image-cleaner-0.4.0rc4-macos-x86_64.zip) |

解压后运行 `CodexImageCleaner.exe`，或将 `.app` 拖入“应用程序”后打开。无需安装 Python，需要现代浏览器。当前为候选版；Mac 以 macOS 15 验证，应用尚未做 Apple 公证。

[所有版本与校验文件](https://github.com/0xsuannai/codex-image-cleaner/releases)

## 使用

1. 停止任务并关闭要清理的 Codex 会话，其他会话可以保持打开。
2. 打开工具，搜索会话，或勾选“只看有图片”。
3. 选中会话，点击“清理会话图片”，核对名称并确认。
4. 清理完成后重新打开会话。需要撤销时，在“备份与恢复”中恢复。

默认读取 `~/.codex` 或 `CODEX_HOME`，可在“目录设置”中修改。在线锁机制不兼容时，完全退出 Codex（含 IDE 插件），再选择离线处理。

## 使用前了解

- 清理的是图片上下文，**不会释放磁盘空间**，也不保证解决网络或服务端导致的重连。
- 清理后若会话已有新消息，旧备份会拒绝覆盖。操作中断时，先使用“修复上次中断”。
- 本工具非 OpenAI 官方产品。备份包含会话内容，请勿公开上传。

[详细使用与兼容说明](docs/guide.md) · [反馈问题](https://github.com/0xsuannai/codex-image-cleaner/issues)

[MIT License](LICENSE) · [安全报告](SECURITY.md)
