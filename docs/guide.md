# 使用与兼容说明 / User guide

## 中文

### 查找与选择

搜索支持名称、消息、工作区和会话 ID。Ctrl+K / Ctrl+F（Mac 使用 ⌘）聚焦搜索；Escape 清除选择。点击行末箭头查看详情。会话每批显示 60 条，单次最多处理 1000 个；筛选或分页隐藏的选择会在操作区提示。

### 清理范围

扫描 `sessions`、`archived_sessions` 下的 `rollout-*.jsonl`，处理明确标记的图片块、用户图片列表、明文替代历史，以及已知 `thread_history_1.sqlite` 图片缓存。

保留文字、代码、工具参数、ID、顺序、历史位置和素材文件。普通文本中的图片链接或 Base64、加密压缩内容不修改。未知数据库结构拒绝处理。图片计数可能包含缓存副本，减少量是上下文内容估算。

### 备份、恢复与修复

每次修改前保存原会话和恢复清单。恢复会核验文件与缓存，存在新消息或冲突时拒绝覆盖。恢复前同样要关闭目标会话。

操作中断后保留备份，单独选择该会话，点击“修复上次中断”。修复完成前不要在 Codex 中重新打开它，也不要手动删除恢复标记。

### 数据位置

| 运行方式 | 工具设置与备份 |
| --- | --- |
| 源码 | 项目下 `.runtime/` 和 `backups/` |
| Windows EXE | `%LOCALAPPDATA%\CodexImageCleaner/` |
| Mac `.app` | `~/Library/Application Support/CodexImageCleaner/` |

`CODEX_IMAGE_CLEANER_DATA_DIR` 覆盖工具的数据目录；`CODEX_HOME` 指定被读取的 Codex 目录，两者用途不同。备份也可单独设置，必须放在 Codex 目录之外。

### 常见问题

| 问题 | 处理方法 |
| --- | --- |
| 找不到会话 | 在“目录设置”中核对 `.codex` 或 `CODEX_HOME` |
| 会话被占用 | 停止任务并关闭目标会话，稍等后重新扫描；必要时完全退出 Codex 使用离线模式 |
| 本地界面断连 | 等待自动重连或手动重试；后台已退出时重新启动工具 |
| Mac 无法首次打开 | 应用为 ad-hoc 签名、未公证。按 Apple 官方流程允许可信应用，不要关闭全局安全保护 |
| 清理后磁盘没变小 | 正常。日志保留原长度和历史位置，备份也保留原数据 |

Windows 的 Edge、Mac 的 Chrome/Edge 优先使用独立窗口，缺少时回退到默认浏览器。退出工具可从“更多”操作；关闭页面后无任务时约 90 秒自动退出。

### 兼容与验收

Windows 10/11 x64；Mac 原生 arm64 / x86_64，以 macOS 15 为 CI 基线；Linux 暂未支持。源码 CI 覆盖 Windows Python 3.11–3.13 和 Mac Python 3.12。原生 Codex 验证使用隔离目录，支持范围见发布附件的 `release-evidence*.json`。

单条记录上限 64 MiB，单会话缓存编辑预算 128 MiB，最多一百万条记录，恢复清单上限 192 MiB。损坏数据或未知格式会拒绝处理。

在其他电脑验收时，不复制私人会话：Windows 包可运行 `验收工具.cmd`；Mac 可用隔离数据目录运行 `.app/Contents/MacOS/CodexImageCleaner --self-test <报告路径>`。再手动检查首次启动、浏览器、搜索、选择和恢复。CI 不能替代首次下载体验和长期使用反馈。

## English

### Finding conversations

Search matches titles, messages, workspaces, and conversation IDs. Ctrl+K / Ctrl+F (⌘ on Mac) focuses search; Escape clears selection. Row arrows open details. Lists load 60 rows at a time, with up to 1,000 targets per operation. Hidden selections are disclosed in the action area.

### Cleanup and recovery

The cleaner handles typed image blocks, user image lists, plaintext replacement history, and known `thread_history_1.sqlite` image caches for rollouts under `sessions` and `archived_sessions`.

Text, code, tool arguments, IDs, ordering, byte positions, and original asset files are preserved. Image URLs/Base64 in ordinary text and encrypted content are left unchanged. Unknown database structures are refused. Counts may include cache copies; estimated reduction refers to context content.

Every operation backs up the original history and a restore manifest. Restore requires the target to be closed and refuses newer messages or conflicts. After an interruption, keep the backup, select the affected conversation alone, and run repair before reopening it. Do not delete the recovery marker manually.

### Local storage

Source runs use project `.runtime/` and `backups/`. Packaged Windows uses `%LOCALAPPDATA%\CodexImageCleaner/`; Mac uses `~/Library/Application Support/CodexImageCleaner/`.

`CODEX_IMAGE_CLEANER_DATA_DIR` overrides cleaner storage; `CODEX_HOME` selects the Codex conversations being read. A separate backup directory must be outside the Codex home.

### Troubleshooting

| Symptom | Action |
| --- | --- |
| No conversations | Check the configured Codex home |
| Target occupied | Stop and close it, wait, rescan; fully exit Codex and choose offline mode if needed |
| UI disconnected | Retry or relaunch if the backend exited |
| Mac first launch blocked | Bundles are ad-hoc signed, not notarized. Follow Apple's trusted-app process without disabling global protections |
| Disk use unchanged | Expected: historical lengths/positions and backups are retained |

Edge on Windows and Chrome/Edge on Mac use an app-style window when available; otherwise the default browser opens. Use More → Quit, or close the pages and let the idle backend exit after about 90 seconds.

### Compatibility and acceptance

Targets are Windows 10/11 x64 and native Mac arm64/x86_64, with macOS 15 as the Mac CI baseline. Linux is not supported. CI covers Windows Python 3.11–3.13 and Mac Python 3.12. Native Codex checks use an isolated home; consult `release-evidence*.json` for verified versions.

Limits: 64 MiB per record, 128 MiB cache-edit budget per conversation, one million records, and 192 MiB per restore manifest. Invalid data and unknown formats are refused.

For another-machine acceptance, use synthetic conversations. Run the Windows acceptance tool or the Mac bundle's `--self-test` with an isolated `CODEX_IMAGE_CLEANER_DATA_DIR`. Also check first launch, browser behavior, search, selection, and restore manually. CI does not replace first-download or long-term field testing.
