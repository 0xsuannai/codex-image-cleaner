# Security / 安全报告

Use **Security → Report a vulnerability** when private reporting is available. Otherwise, open a minimal issue asking for a private channel, without vulnerability details. / 优先使用私密漏洞报告；不可用时，先提交不含细节的问题询问私下渠道。

Include versions, the affected operation, and a synthetic reproduction. The app's diagnostic export is suitable for public bug reports. / 可提供版本、受影响操作、构造样例和工具导出的脱敏诊断。

Do not publicly attach real rollouts, databases, credentials, backups, browser profiles, `.runtime/`, or unreviewed logs and screenshots. / 请勿公开真实会话、数据库、凭据、备份、浏览器配置及未经检查的日志、截图。

The current candidate is maintained on `main`; compatibility is described in [the guide](docs/guide.md). Restore only trusted backups of your own data. / 当前维护 `main`，只恢复自己可信的备份。
