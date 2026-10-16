# 运行日志与问题记录（公开脱敏副本）

本目录保存截至 2026-10-16 的排障快照：`runtime_logs_and_incidents.tar.gz` 包含 687 个文本文件，原有日志顺序与失败记录均保留；`manifest.json` 列出每个归档文件的路径、行数、错误关键词命中行数及脱敏后 SHA-256；`failure_index.md` 用于快速定位出错文件。关键词命中行数不等于失败商品数。

归档收集了 `panel_data/logs/`、根目录的运行日志、`logs/`、`outputs/`、`tools/` 中的文本日志，以及盖亚批次回执、抖店批量执行和 API 写入报告等结构化问题记录。浏览器登录数据、原始商品快照、图片、Excel 业务表和密钥均不在归档范围内。

因仓库可能被多人访问，日志里的 URL 被替换为域名标签，本机用户名路径、临时路径、手机号、邮箱及常见凭据值已脱敏。故本副本可追踪时间线与错误类型，但不能用于还原原始 URL 或私有凭据。原始文件仍留在本地 `panel_data/` 等目录。

查看归档：

```sh
tar -tzf diagnostics/runtime_logs_and_incidents.tar.gz
tar -xOf diagnostics/runtime_logs_and_incidents.tar.gz panel_data/logs/launchd.err.log | tail -100
```

在本地重新生成：

```sh
python3 tools/export_public_diagnostics.py
```

新日志不会自动同步到 GitHub；重新导出后需要检查脱敏结果，再提交并推送。不要直接把 `panel_data/` 或浏览器配置目录加入 Git。
