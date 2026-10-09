# 2026-10-09 本机报告与 Web 演示

本目录是本机离线演示版本 `2026-10-09-v1` 的截图与摘要。该运行从已有归档案例包导入数据，使用隔离 SQLite 数据库和本机服务；执行为动态离线替身，不是实时采集或真实模型推理证据。

| 截图 | 内容 |
|---|---|
| [task-detail.png](task-detail.png) | 队列中的完成状态、轮次、有效决策和失败数 |
| [task-linked-actions.png](task-linked-actions.png) | 任务关联报告/图谱/预警 ID 与直接入口 |
| [report-overview.png](report-overview.png) | 报告概况、暂定风险等级和完整性 |
| [report-citations.png](report-citations.png) | 主要依据、来源摘要、可见时间与原始链接 |
| [simulated-propagation.png](simulated-propagation.png) | 区别于来源证据图的模拟传播关系 |
| [report-emotion-activity.png](report-emotion-activity.png) | 情绪曲线轮次、计算说明与按发送数排序的节点 |
| [business-guidance.png](business-guidance.png) | 关联来源的业务建议、适用条件、建议角色/时限和观察指标 |
| [technical-limitations.png](technical-limitations.png) | 技术引用及模拟/GraphRAG 限制 |
| [source-graph-and-query.png](source-graph-and-query.png) | 来源证据图摘要与带引用词项检索结果 |
| [cited-query-results.png](cited-query-results.png) | 两条词项命中与可点击引用链接 |
| [linked-alert.png](linked-alert.png) | 从任务直达的关联预警页 |
| [alert-limitations.png](alert-limitations.png) | 暂定阈值、未送达与 SLA 限制 |
| [alert-preview.png](alert-preview.png) | 企业微信 dry-run 预览、sent_at 为空与网络请求为零 |

## 重放

复用本轮隔离数据库，在项目根目录的两个 PowerShell 终端分别执行：

```powershell
$env:RISKSHIELD_DB = 'D:\temp\riskshield-final-report-web-review-20261009.sqlite3'
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8032
```

```powershell
$env:RISKSHIELD_API_URL = 'http://127.0.0.1:8032'
uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8532 --server.headless true
```

打开 `http://127.0.0.1:8532`，在“处置队列”选择 `UnitedHealth Group / Change Healthcare 网络事件：SEC 与 Optum 状态页`；使用任务页的三个关联按钮，再在报告页以 `Change Healthcare 网络中断` 检索。需要停止时，在两个服务终端分别按 Ctrl+C。任务 ID、报告 ID 与预警 ID 已由任务详情的直接链接承接，无需复制粘贴 ID。

不要用本轮结果宣称真实模型质量、实际公众情绪、实际投诉量或 G3/G4 通过。查询是词项匹配，完整自然语言追问尚未实现；来源原始链接只核对了页面 URL，没有对外打开。
