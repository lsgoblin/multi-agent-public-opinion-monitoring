# 功能名称收尾复核 v1

日期：2026-10-09（Asia/Shanghai）  
工作标识：`closure-20261009-functional-names-v1`  
状态：部分验证，未就绪

本版本更新了本机 Web 的业务显示名称和用户演示步骤，未改动 API、任务执行器、预警阈值、仿真执行器、技术路由或既有数据。测试命令 `uv run pytest -q tests/test_ui.py` 返回 7 passed、1 条 Starlette/httpx 弃用提示。

## 当前端到端运行

仅使用当前交接文件 [end-to-end-20261009-functional-names-v1.json](../../final-closure/end-to-end-20261009-functional-names-v1.json) 中记录的端到端运行：案例 `unh_change_20240222_day2_multisource`、图谱 `936c73951d2ab46f8e0ee6bb`、运行 `run_7aff1ceff6934a1da13e`、任务 `job_02aa0aed4a594ebe995c`、报告 `report_26db8f02583eb98bde5f7490`、预警 `alert_19fe8d54fd95ea0876eee8b6323fd0c4`。交接 JSON 记录 3/3 轮、30 项有效决策、20 项带独立记忆的动作、17 项带运行时消息的动作、0 次模型调用和 0 次外部网络请求。数据模式为 `real_historical`，执行模式为 `offline_dynamic_substitute`，图谱来自 `offline_archived_record_projection`。预览为 `dry_run`，没有发送。

这是一条真实历史来源材料驱动的本机离线仿真记录，不是真实模型推理、在线采集或现实走势预测。蓝色风险等级仍属暂定工程规则结果。

交接 JSON 记录的 `ui/app.py` SHA256 与当前文件一致，绑定到同一份界面版本。

## UI 与截图状态

主 Agent 的只读 IAB 复核反馈：当前页面中文显示正常；任务、案例、图谱、报告、预警绑定及 3/3 轮、30 项有效决策、0 项失败、离线执行和 0 次模型/网络调用均正常。该反馈没有提供截图文件路径或可保存的图像。

本 Agent 的浏览器自动化连接失败，备用 Windows UI 自动化被 URL 置信度安全限制终止；Ubuntu Chromium 打开本机页面但缺少中文字体，文字呈方框。本目录没有当前版本 PNG 文件，不能将旧版截图冒充本版本截图。早先的 `2026-10-09-v1` 仅作历史证据保留。

完整复核状态和阻塞说明见[最终报告与 Web 操作复核](../../../docs/records/final-report-web-review.md)。
