# 最终报告与 Web 操作复核

**复核日期：** 2026-10-08（Asia/Shanghai）  
**交付截图版本：** `artifacts/final-report-web-review/2026-10-08-v2/`  
**结论：** 本机离线工作流和界面改动已验证；G3、G4 状态不由本次复核判定。完整自然语言追问未完成。

## 本次范围

改动限定在 `src/riskshield/day4_report.py`、`ui/app.py`、`tests/test_day4_report.py`、`tests/test_ui.py`，以及本记录和复核产物。API、任务执行器、预警阈值和仿真执行器未修改。

报告首页提供事件概况、暂定风险等级、依据来源、建议行动、数据模式、执行方式和完整性。五个报告模块继续绑定同一 `case_id`、`graph_id` 和 `run_id`。来源证据图来自截止前来源；模拟传播图来自该次运行时消息，二者单独展示。模拟情绪曲线展示范围、轮次与重建依据；关键节点按发送消息数排序，并明确该指标不代表因果影响。

业务建议与技术核查分开。业务建议关联截止前来源，并标出适用条件、建议角色、建议时限和后续指标；角色和时限是流程建议，不是实际指定责任人或 SLA。技术标识放入详情区域。任务页显示完成轮次、有效决策、失败数、云模型调用、外部网络请求和执行模式，且可直接打开绑定图谱、报告和预警。为避免已存在任务时启动新任务造成 Streamlit 状态键冲突，任务选择控件改用独立状态键。

## 实际运行证据

最终截图对应的本机记录如下：

| 字段 | 实际记录 |
|---|---|
| 案例 | `UnitedHealth Group / Change Healthcare 网络事件：SEC 与 Optum 状态页` |
| 数据模式 / 执行方式 | 真实历史材料 / `offline_dynamic_substitute`（本机动态离线替身） |
| Case / Graph / Run | `unh_change_20240222_day2_multisource` / `936c73951d2ab46f8e0ee6bb` / `run_acc82554ca0743be9d1b` |
| Job / Report / Alert | `job_956a680447b84ae49413` / `report_ca8cdf424dc01ef3658bf6fc` / `alert_e68a3acf9c2a6b6c0ce37f46759c9de7` |
| 运行完整性 | 已完成 3/3 轮；有效决策 30；失败决策 0 |
| 传播与来源 | 模拟消息 60 条；来源证据图 7 个节点、6 条关系；截止前来源记录 2 条 |
| 预警展示 | 蓝色（暂定离线规则结果，不代表真实风险校准或正式预警达标） |
| 调用与耗时 | 云模型调用 0；外部网络请求 0；本机记录耗时 0.077642 秒 |

作业输入是本机已有的真实历史归档材料，执行是动态离线替身。没有新增采集、真实模型调用、通知发送、材料外发或部署。作业响应未提供实际账单字段；界面费用为本机费用估算 ¥0，不作为计费系统对账结果。

截图版本与运行产物绑定；本机来源包版本为 `unh-day2-multisource-2026-10-04-v1`，输入 cutoff 为 `2024-02-23T05:48:39+08:00`。结构化统计和截图清单见同目录 `run-summary.json`、`README.md`。

最终截图：

- [任务完成概况](../../artifacts/final-report-web-review/2026-10-08-v2/01-task-complete.png)、[任务进度](../../artifacts/final-report-web-review/2026-10-08-v2/02-task-progress.png)
- [报告概况](../../artifacts/final-report-web-review/2026-10-08-v2/03-report-overview.png)、[模拟传播图](../../artifacts/final-report-web-review/2026-10-08-v2/04-simulated-propagation.png)、[情绪与节点排序](../../artifacts/final-report-web-review/2026-10-08-v2/05-emotion-and-ranking.png)
- [业务建议与来源](../../artifacts/final-report-web-review/2026-10-08-v2/06-business-actions-evidence.png)、[技术核查](../../artifacts/final-report-web-review/2026-10-08-v2/07-technical-checks.png)、[带引用词项查询](../../artifacts/final-report-web-review/2026-10-08-v2/08-lexical-citations-query.png)
- [关联预警](../../artifacts/final-report-web-review/2026-10-08-v2/09-linked-alert.png)、[预警限制](../../artifacts/final-report-web-review/2026-10-08-v2/10-alert-limitations.png)

## 查询能力与主 Agent 集成请求

现有 `POST /graphs/{graph_id}/query` 是词项匹配来源标题/摘要，再返回图邻域与来源引用；不生成自然语言答案。UI 已如实标为“按词项查找引用来源”，展示命中摘要、来源 ID、分数和原始来源链接。完整自然语言追问仍未完成，未以词项检索冒充问答。

请主 Agent 后续评估完整、带引用的问答接口。建议请求/响应至少包含：

- 请求：`graph_id`、`question`；可选 `conversation_id` 用于追问上下文。
- 响应：`answer`、`retrieval_kind`、`citations[]`（来源记录 ID、原文片段、可见时间、来源 URL）和无法由截止前证据回答时的 `abstention_reason`。
- 服务端始终限定在图谱 cutoff 合格来源范围内，不将未知历史可见时间、未来结果或 cutoff 后信息放入检索和上下文；答案中的事实句应能回链到引用。

这只是接口需求，当前子任务未修改 API。外部引用跳转通过结果 URL 与 UI 链接路径核对；验证时没有打开外部网站。

## 验证

**已验证：**

- `uv run pytest -q tests/test_day4_report.py tests/test_ui.py`：15 passed，1 条 Starlette/httpx 弃用警告。
- 单测覆盖报告完整性和失败决策、空模拟传播、无匹配查询、带引用查询链接，以及任务到图谱/报告/预警的导航；还覆盖队列已有运行时再次启动历史任务，避免本次发现的控件状态冲突。
- 浏览器操作覆盖本机离线任务完成、任务页进度、任务直接进入图谱/报告/预警、来源链接呈现、词项命中和引用展示。截图只保存于 `2026-10-08-v2/`。

**部分验证：**失败与空数据使用隔离测试数据库和离线替身覆盖；最终浏览器运行本身是成功样本。来源链接的 href 与页面路径已核对，但没有向外部网站发起跳转请求。

**未完成：**完整自然语言追问。截图目录 `2026-10-08-v2/` 中由 `run-summary.json` 列出的十张 PNG 是本次最终版本；同目录未列出的旧命名图和 `2026-10-08-v1/` 都是早期中间截图，不作为本次交付证据。清理旧截图的命令被自动审查阻止（返回 `blocked by policy`），因此保留并明确标注版本优先级。

**关口：**本复核不宣布 G3、G4 或任何正式业务指标达标。G3、G4 仍须由主 Agent 基于对应证据判断。

## 最短复核步骤

1. 在项目根目录运行 `uv run pytest -q tests/test_day4_report.py tests/test_ui.py`。
2. 使用本机 SQLite 路径启动 API 和 Streamlit，打开“处置队列”，选中已完成任务。
3. 点“打开关联图谱”“打开关联报告”“打开关联预警”；在报告页用 `Change Healthcare network interruption` 检索，核对两条来源命中和可点击引用。
4. 核对 UI 的 3/3 轮、30 个有效决策、0 个失败决策、60 条模拟消息、2 条来源记录，以及 0 云模型调用 / 0 外部网络请求。只在本机复放已授权离线任务；不要据此判定 G3/G4。

## 2026-10-09 本机演示补充

为供连续演示复核，本补充使用独立数据库 `D:\temp\riskshield-final-report-web-review-20261009.sqlite3`，从项目已有归档包导入案例，并在本机 8032/8532 端口运行 API / Streamlit。该轮没有联网采集、真实模型调用、外部通知或材料外发；结束后关闭服务。版本截图与摘要见 [2026-10-09-v1](../../artifacts/final-report-web-review/2026-10-09-v1/README.md) 和 [run-summary.json](../../artifacts/final-report-web-review/2026-10-09-v1/run-summary.json)。

运行绑定与统计：

| 字段 | 实际记录 |
|---|---|
| 案例 / 图谱 / 运行 | `unh_change_20240222_day2_multisource` / `936c73951d2ab46f8e0ee6bb` / `run_56e64807a5814fc78929` |
| 任务 / 报告 / 预警 | `job_52805c6e43c54905a6c1` / `report_a220a1e7578935249fc000a4` / `alert_358fb540de828536fb2fe21111ca83d1` |
| 数据模式 / 执行模式 | 真实历史归档材料 / `offline_dynamic_substitute`（本机动态离线替身） |
| 运行完整性 | 已完成 3/3 轮；30 项有效决策；0 项失败；0 次云模型调用；0 次外部网络请求；运行记录耗时 0.148635 秒 |
| 五模块绑定 | `propagation`、`emotion_evolution`、`key_nodes`、`risk`、`recommendations` 均绑定同一 case、graph、run |
| 图与引用 | 运行时模拟传播图 60 条消息、30 条发送关系；来源证据图 7 个节点、6 条关系；截止前来源记录 2 条 |
| 预警 | 蓝色，状态 `provisional_offline_assessment`；暂定规则，不表示真实风险或正式达标 |

本轮浏览器流程实际打开任务关联图谱/报告与预警，并在报告中查询“Change Healthcare 网络中断”。词项匹配返回 2 条带来源 ID、匹配分数和原始来源 URL 的记录；链接目标已核对，因本轮限于离线复核，没有打开外部网站。完整自然语言追问仍未实现。

在预警页生成的企业微信预览为 `preview_d7efe267551a46708d80`，`delivery_status=dry_run`、`sent_at=null`、`network_requests=0`、`recipient=null`。`previewed_at` 为 2026-10-09 00:41:06.293473 +08:00；`discovery_to_preview_ms=459711.686` 仅表示本机规则评估至本机生成预览的时间，不能解释为历史事件发现延迟或送达 SLA。真实送达未发生。

**补充验证：**再次运行 `uv run pytest -q tests/test_day4_report.py tests/test_ui.py`，结果 15 passed、1 条 Starlette/httpx 弃用警告，11.83 秒。测试覆盖失败任务、空模拟传播、无匹配查询、引用 URL 展示与任务到关联图谱/报告/预警的导航；这些失败和空路径由隔离测试数据覆盖，不代表本轮浏览器任务失败。

**当前限制与集成请求：**完整自然语言追问未完成；现有查询仍是词项匹配。主 Agent 若推进完整问答，需另行设计带 `graph_id`、`question`、引用片段、来源可见时间及拒答原因的接口，服务端严格使用 cutoff 合格证据。本机演示不判定 G3/G4；当前 G3/G4 仍未通过。

## 2026-10-09 功能名称收尾复核

**工作标识：** `closure-20261009-functional-names-v1`。本轮仅修改 Web 界面用语、UI 测试和用户指南。页面标题、侧栏说明、历史案例导入按钮、证据能力说明和情绪计算说明改用业务名称；技术路由、任务 ID、数据库字段和关口状态保持原样。用户指南新增一条连续演示路径，说明从案例导入、离线执行到任务直达图谱/报告/预警，不需复制 ID。

**当前验证：** `uv run pytest -q tests/test_ui.py` 返回 **7 passed**、1 条 Starlette/httpx 弃用提示。覆盖导入、报告、预警、成功任务、失败任务、空传播数据、无匹配检索及引用结果展示。引用链接的实际浏览器跳转没有在本轮尝试；没有向外部来源发请求。

最新端到端交接证据见 [本轮集成运行](../../artifacts/final-closure/end-to-end-20261009-functional-names-v1.json)：案例 `unh_change_20240222_day2_multisource`、图谱 `936c73951d2ab46f8e0ee6bb`、运行 `run_7aff1ceff6934a1da13e`、任务 `job_02aa0aed4a594ebe995c`、报告 `report_26db8f02583eb98bde5f7490`、预警 `alert_19fe8d54fd95ea0876eee8b6323fd0c4`。该 JSON 记录 3/3 轮、30 项有效决策、0 次模型调用、0 次外部网络请求、真实历史归档输入和 `offline_dynamic_substitute` 执行；五个模块共用同一案例/图谱/运行。交接 JSON 中的 `ui/app.py` SHA256 与本轮当前文件一致。仿真消息、情绪与蓝色等级均为离线结果，不代表真实平台活动或现实风险。

另对本机只读 API 做过绑定核对，但所用隔离数据库副本来自早先 `2026-10-09-v1` 演示，运行 ID 为 `run_56e64807a5814fc78929`；它不是上方最新端到端运行，不应混作同一截图版本。主 Agent 随后反馈其 IAB 只读复核看到当前页面中文正常，并核对了任务进度、ID、离线执行方式和 0 次模型/网络调用。该反馈没有附带可落盘截图文件，故仅作为主 Agent 的观察记录。

**截图状态：未验证。**本轮没有保存当前界面截图。浏览器自动化运行环境返回 `privileged native pipe bridge is not available; browser-client is not trusted`；备用 Windows UI 自动化因无法高置信判断当前浏览器 URL 而停止。Ubuntu Chromium 的本机页面因缺少中文字体显示方框，不作为交付截图。旧版 `2026-10-09-v1` 截图继续保留为历史版本，不代表本轮业务名称更新后的页面。复核摘要与阻塞证据见 [本轮记录目录](../../artifacts/final-report-web-review/2026-10-09-functional-names-v1/README.md)。

**交接状态：部分验证，未就绪。**本轮 `artifacts/final-closure/b-ready.json` 标记截图阻塞和现有测试证据；当前截图缺失、完整自然语言追问未实现。G3/G4 与正式业务指标仍由主 Agent 根据证据判断。
