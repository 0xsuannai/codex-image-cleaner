# 使用与兼容说明 / User guide

## 中文

### 查找与选择

搜索支持名称、消息、工作区和会话 ID。Ctrl+K / Ctrl+F（Mac 使用 ⌘）聚焦搜索；Escape 清除选择。点击行末箭头查看详情。会话每批显示 60 条，单次最多处理 1000 个；筛选或分页隐藏的选择会在操作区提示。

主任务集中显示，子任务按父会话 ID 归入主任务下，默认折叠，点击“子任务”展开后可独立勾选。选择主任务不会自动选中子任务；“选择有图片的会话”只选择当前已显示的匹配会话。折叠不会取消已有选择，操作区会提示隐藏的选择。搜索子任务时会保留并展开主任务；“只看有图片”也会保留对应主任务作为层级上下文。找不到主任务的子任务单独折叠收纳。

扫描时即可选择会话并清理，包括尚未检查的会话。工具先暂停全局扫描，再只检查和处理所选会话；无需等待全部会话扫描完毕。其余未检查会话可继续选择清理，或点击“重新扫描”。

### 大小与处理结果

列表展示原始内容大小与预计清理后大小，清理后更新为实际值。这里统计 JSONL 日志的有效字节，不包含记录末尾的兼容空白填充；初次检查前只能显示磁盘文件大小，清理后大小显示“待检查”。详情与结果窗单独展示磁盘文件大小。历史缓存的图片内容减少量另行列出，不混入日志大小对比。

清理结束后自动弹出结果窗，展示成功、占用跳过和失败项，以及实际清理前后的大小。工具仍保留日志原长度以兼容 Codex 的历史位置，因此内容减少不等于释放磁盘空间，备份还会额外占用空间。

### 会话锁与自动重启

确认时探测实际系统锁，残留锁文件本身不会被误判为占用。检测到锁或不兼容的锁机制时提示关闭 Codex；识别到桌面应用后可选择“一键关闭 Codex，完成后自动重启”。请先保存工作并停止任务，工具会等待正常退出，再以离线方式清理，并在处理结束后尝试重启原应用。退出失败时不执行清理；清理失败也会尝试重启，重启失败则在结果中提示手动打开。

Windows 支持原生 OpenAI.Codex 应用包和可识别的 Electron Codex 桌面窗口，原生应用通过系统应用包入口重启；Mac 关闭 Codex 桌面应用。CLI、IDE 插件和无法识别的桌面安装需手动退出。操作时仍会再次获取写锁；若确认后又被占用，会跳过并在结果窗提供重试入口。

### 清理范围

扫描 `sessions`、`archived_sessions` 下的 `rollout-*.jsonl`，处理明确标记的图片块、用户图片列表、明文替代历史，以及已知 `thread_history_1.sqlite` 图片缓存。

保留文字、代码、工具参数、ID、顺序、历史位置和素材文件。普通文本中的图片链接或 Base64、加密压缩内容不修改。未知数据库结构拒绝处理。图片计数可能包含缓存副本，减少量是上下文内容估算。

### 备份、恢复与修复

每次修改前保存原会话和恢复清单。恢复会核验文件与缓存，存在新消息或冲突时拒绝覆盖。恢复前同样要关闭目标会话。

在“备份与恢复”中，点击某份备份的“删除备份”，核对会话、备份目录名和占用大小，勾选确认后永久删除。删除只移除该份备份，不修改当前会话或原始图片文件；删除无法撤销，也无法再用这份备份恢复图片上下文。待修复的备份或仍存在恢复标记的会话，必须先完成修复再删除备份。

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
| 会话被占用 | 保存工作并停止任务，使用“一键关闭 Codex，完成后自动重启”；CLI / IDE 插件需手动退出 |
| Codex 在 15 秒内未退出 / 退出码 3 | 本次未执行清理或恢复。保存工作、停止任务，处理退出确认并手动完全退出 Codex，再重新扫描、取消自动关闭选项并选择离线处理。仅关闭窗口可能仍有后台进程 |
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

Subtasks are grouped under their parent conversation and collapsed by default. Expand a group to select subtasks individually; selecting a parent does not select its children. Bulk image selection applies to currently displayed matching conversations. Collapsing a group keeps existing selections and reports them in the action area. Search reveals matching subtasks together with their parent context, and the image filter retains the relevant parent. Subtasks whose parent is unavailable are collected in a separate collapsed group.

### Cleanup and recovery

The cleaner handles typed image blocks, user image lists, plaintext replacement history, and known `thread_history_1.sqlite` image caches for rollouts under `sessions` and `archived_sessions`.

Text, code, tool arguments, IDs, ordering, byte positions, and original asset files are preserved. Image URLs/Base64 in ordinary text and encrypted content are left unchanged. Unknown database structures are refused. Counts may include cache copies; estimated reduction refers to context content.

Every operation backs up the original history and a restore manifest. Restore requires the target to be closed and refuses newer messages or conflicts. After an interruption, keep the backup, select the affected conversation alone, and run repair before reopening it. Do not delete the recovery marker manually.

In Backup and Restore, choose Delete Backup, review the conversation, backup folder name and size, then acknowledge permanent deletion. This releases only the backup's disk space and leaves current conversations and original image files unchanged. Deletion cannot be undone, and that backup can no longer restore image context. Backups needed for interrupted operations must be repaired before deletion.

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
