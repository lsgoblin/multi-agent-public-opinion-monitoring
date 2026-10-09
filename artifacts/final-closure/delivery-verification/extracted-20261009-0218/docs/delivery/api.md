# API 参考

## 范围与入口

当前 API 由 [FastAPI 应用](../../src/riskshield/api.py) 提供，版本 `0.5.0`。主机默认地址为 `http://127.0.0.1:8000`；Compose 内 Web 使用 `http://api:8000`，由 local_gateway 向本机发布入口；实测独立项目 `riskshieldaccept` 的主机 API 端口为 18000、Web 为 18501。默认端口仍为 8000/8501，详见[部署文档](deployment.md)。启动后可访问 FastAPI 生成的 [`/docs`](http://127.0.0.1:8000/docs) 和 `/openapi.json` 查看本次运行的完整 OpenAPI 描述。应用未配置用户认证，当前端口仅绑定本机回环地址；不要将其直接发布到不受信任网络。

以下重点描述任务、报告、预警和 UI 使用的接口。原有案例、采集、标注和仿真路由也保留在代码中，详见接口清单和运行时 OpenAPI。响应均为 JSON；错误详情由当前代码返回，具体字段以 `/docs` 为准。

## 健康与能力

| 方法 | 路径 | 请求 | 成功响应 |
| --- | --- | --- | --- |
| `GET` | `/health` | 无 | `{"status":"ok","stage":"day4_v2","version":"0.5.0"}` |
| `GET` | `/readiness` | 无 | 工程关口布尔值、功能能力和 blocker 列表。代码将 G3/G4 标记为未通过；该接口不是本次运行或正式签收证明。 |
| `GET` | `/channels/status` | 无 | 读取仓库内 `data/research/channel_checks_v2.json`。渠道路线/核查状态不等于在线接入。 |
| `GET` | `/contracts` | 无 | 返回案例导入、遗留日投诉、Agent 决策和仿真创建的 Pydantic JSON Schema。 |

## 案例与证据图路由

| 方法 | 路径 | 请求/响应摘要 |
| --- | --- | --- |
| `POST` | `/cases` | 请求体为 `CaseImport`；创建案例，版本冲突返回 409。案例限定为单一 `real_historical` 或 `synthetic` 数据模式。 |
| `GET` | `/cases` | 返回案例列表。 |
| `GET` | `/cases/{case_id}` | 返回案例元数据。 |
| `GET` | `/cases/{case_id}/snapshot` | 返回 cutoff 快照及排除统计。 |
| `GET` | `/evaluations/cases/{case_id}/evidence` | 返回 `evaluation_only` 记录，明确标记 `not_for_agent_context=true`。 |
| `POST` | `/cases/{case_id}/observations/collect` | 请求体为 `CollectRequest`；受限采集入口。来源/联网权限需另按项目授权边界核对。 |
| `GET` | `/cases/{case_id}/observations` | 返回该案例观察记录。 |
| `POST` / `GET` | `/cases/{case_id}/labels` | POST 使用 `LabelRequest` 提交标签；GET 返回标签。 |
| `POST` | `/cases/{case_id}/graph` | 从符合条件的案例材料构建截止证据图，返回图对象和 `graph_id`。 |
| `GET` | `/graphs/{graph_id}` | 返回证据图。Web 将其画成截止快照证据关系图，不是实际社交传播图。 |
| `POST` | `/graphs/{graph_id}/query` | 请求体 `GraphQuery`，包含图查询问题；返回图检索结果。 |

`/graphs/{graph_id}/query` 当前按问题词项做本地图谱邻域匹配并返回来源引用；它不生成自然语言答案，也不保存多轮追问上下文，不能计作命题要求的 Web 自然语言追问能力。

`CollectRequest`、`LabelRequest` 和 `GraphQuery` 的字段以运行时 OpenAPI 为准。未知可见时间、未来结果或 `evaluation_only` 内容不得作为 cutoff Agent 输入。投诉样本不代表公司全渠道投诉量。

## 模型与仿真路由

| 方法 | 路径 | 请求/响应摘要 |
| --- | --- | --- |
| `GET` | `/models/status` | 返回当前模型配置状态，不证明凭据有效或任务质量。 |
| `POST` | `/models/probe` | 执行模型连通探测；模型不可用时返回 503。该路由可能触发收费/外部模型调用，不属于离线任务流程；本手册没有授权执行它。 |
| `POST` | `/simulations` | 请求 `SimulationCreate` 并返回新建运行摘要。字段：`case_id`、`graph_id`、`agent_count`（1–500）、`rounds`（1–30）、可选 `seed`、`role_offset`、`concurrency`（1–128）、`budget_cny`（大于 0 且不超过 ¥30）。只创建运行配置，初始状态为 `created`，不会由此端点启动 Day 4 后台任务。 |
| `GET` | `/simulations` | 返回 `{ "runs": [...] }`。 |
| `GET` | `/simulations/{run_id}` | 返回运行摘要：`run_id`、案例/图 ID、配置、状态、完成轮数、有效参与者数、费用估算、用量和逐轮统计。 |
| `GET` | `/simulations/{run_id}/trajectory` | 返回 `{ "run_id": ..., "actions": [...] }`，包括动作、用量、请求诊断和观测引用。 |

仿真创建时校验案例、图谱、cutoff 和来源版本匹配。`budget_cny` 是单次运行硬上限字段；配置值不证明消费了该预算或正式成本目标已达标。

## Day 4 任务接口

### 准备纯合成演示

`POST /demos/day4/run`（HTTP 201）

```json
{
  "agent_count": 10,
  "rounds": 3,
  "concurrency": 4
}
```

字段可省略，代码默认 10/3/4；约束分别为 Agent 1–100、轮数 1–10、并发 1–32。返回仿真摘要（含 `run_id`），**只准备运行，不启动**。案例为虚构 Harborlight 纯合成场景。

### 直接启动真实历史材料离线任务

`POST /day4/historical/jobs`（HTTP 202）

```json
{
  "case_id": "unh_change_20240222_day2_multisource",
  "agent_count": 10,
  "rounds": 3,
  "concurrency": 4
}
```

必须指定本机已导入的 `real_historical` 案例。`case_id` 仅允许字母、数字、下划线和连字符，长度 1–100；默认 Agent/轮数/并发为 10/3/4，范围分别为 1–100、1–10、1–32。实现先尝试 cutoff 图构建；若没有可用观察记录，则可能采用明确标注的 `offline_archived_record_projection` 本地图投影。未知可见时间、未来来源、非输入记录、跨案例或版本不匹配会在 Agent 读取上下文前拒绝。

### 启动已有运行

`POST /jobs`（HTTP 202）

```json
{"run_id":"run_0123456789abcdefabcd"}
```

`run_id` 必须符合 `run_` 加 20 位小写十六进制字符格式。运行必须处于 `created` 状态且案例数据模式匹配。接口排入本机后台线程；同一 `run_id` 重复启动复用原任务记录，不创建第二个 job。当前执行器固定为 `offline_dynamic_substitute`，模型调用和外部网络请求账本为 0。

### 查询任务

| 方法 | 路径 | 成功响应 |
| --- | --- | --- |
| `GET` | `/jobs` | `{ "jobs": [...] }`，按最近排队时间倒序。 |
| `GET` | `/jobs/{job_id}` | 单个任务记录。常用字段如下。 |

任务对象包含 `job_id`、`run_id`、`status`、`mode`、`queued_at`、`started_at`、`completed_at`、`updated_at`、`alert_id`、`report_id`、`failure_category`、`model_calls`、`network_requests`、`input_received_at`、`report_generated_at`、`elapsed_seconds`，以及从运行摘要派生的 `case_id`、`graph_id`、`data_mode`、`execution_mode`、`completed_rounds`、`target_rounds`、`simulation_status`、`progress`、`result_state`、`all_decisions_valid`、`action_counts` 和 `error`。尚未发生的时间和产物 ID 可为 `null`；响应状态可能已快速前进到 `running` 或 `complete`。

任务状态：

- `queued`：已排队。
- `running`：后台线程执行中。
- `complete`：全部配置轮次及全部决策有效，已生成关联预警与报告。
- `failed`：执行、完整性校验或后续产物生成失败。`error`/`failure_category` 为受限类别，可能是 `simulation_error`、`alert_error`、`report_error`、`data_integrity_error`、`unexpected_error`、`interrupted_on_restart` 或 `thread_start_error`；不返回异常堆栈。

`result_state` 进一步区分 `success`、`partial`（轮次结束但有无效决策）、`stopped`（仿真为 `partial`，仍有未派发/未完成部分）和 `failed`（执行异常或完整性/产物失败）。排队/运行中沿用 `queued`/`running`。`simulation_status=complete` 仅说明轮次和请求机会结束，必须同时检查 `all_decisions_valid`，不能据此单独判成功。

API 不提供自动轮询。Web 的“刷新任务进度”会重新读取 SQLite 中的状态。服务初始化时仍处于排队/运行中的旧任务会标为 `failed` / `interrupted_on_restart`，不会自动续跑。

## 报告接口

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| `POST` | `/reports` | 请求 `{ "run_id": "..." }`，HTTP 201。需要至少完成一轮的 `complete` 或 `partial` 运行；自动先按当前规则判级，再构建并保存报告。 |
| `GET` | `/reports/{report_id}` | 返回保存的报告 JSON。 |

报告根字段含 `report_id`、`run_id`、`case_id`、`graph_id`、`cutoff`、`source_version`、`data_mode`、`execution_mode`、`graph_build_mode`、`run_status`、轮数、`binding`、`modules`、`evidence` 和 `limitations`。五个模块为：

- `propagation`：运行时模拟消息边、消息数和消息 ID；不是平台实测传播。
- `emotion_evolution`：可校验时为仿真状态重建曲线，否则可能退回动作分布代理；不是真实公众情绪或原始逐轮观测快照。
- `key_nodes`：按模拟消息活跃度排序，不表示因果影响。
- `risk`：暂定预警对象；若未提供则标为 `unassessed`。
- `recommendations`：保留来源、动作、消息 ID 引用。

根对象和每个模块都绑定同一 `run_id`、`case_id`、`graph_id`。报告的 `evidence` 还列出合格来源 ID、动作引用来源 ID、失败动作 ID 和图构建模式。真实历史材料经替身处理时，报告会注明它不是真实模型推理；投影图也会注明未发生在线 GraphRAG 检索。

## 预警与通知预览接口

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| `POST` | `/alerts/assess` | 请求 `{ "run_id": "..." }`，HTTP 201。至少需有一轮和最新轮有效决策；当前判级可评估部分运行，但会附不完整/失败限制。 |
| `GET` | `/alerts/{alert_id}` | 返回预警记录。 |
| `POST` | `/alerts/{alert_id}/previews` | 请求 `{"channel":"wecom"}` 或 `{"channel":"email"}`，HTTP 201；只保存离线预览。 |

预警 `level` 为 `red`/`orange`/`yellow`/`blue`。暂定比例为最后一轮有效决策中 `express_complaint_intent` 的数量除以该轮有效决策数：红 ≥40%、橙 ≥20%、黄 ≥5%、其余蓝。该指标是模拟投诉意向，不是实际投诉量。预警记录含 `rule_version`、`metrics`、`discovered_at`、`sent_at` 和 `evidence_refs`；`discovered_at` 是本机规则评估时刻，`sent_at` 当前为 `null`。`evidence_refs` 绑定案例、图谱、cutoff、来源版本、观察到的来源 ID 与同一运行/轮次/Agent 的动作引用；动作引用可能截断至 20 条，并由 `action_refs_truncated` 标识。

预览响应含 `preview_id`、关联 ID、渠道、`delivery_status="dry_run"`、预览时间、评估到预览毫秒数、`sent_at=null`、`network_requests=0`、`recipient=null` 和载荷。企微载荷结构为 `msgtype=text`；邮件预览载荷包含 `[OFFLINE PREVIEW]` 主题、正文和空别名收件人。**没有真实通知发送接口或送达回执。**

## HTTP 错误

| 状态码 | 当前实现中的情形 |
| --- | --- |
| `404` | 案例、图、仿真运行、任务、报告或预警 ID 不存在；历史任务的案例不存在。响应 `detail` 使用对应路由的中文错误文本。 |
| `409` | 导入案例发生版本/内容冲突。 |
| `422` | Pydantic 请求字段无效；不支持的数据模式/任务参数；采集、仿真、报告、预警或预览的业务校验失败。 |
| `503` | `/models/probe` 中模型不可用。 |

Web 将 HTTP 错误显示为“API 返回 {状态码}：{detail}”；无法连接 API 时提示检查服务。不要依赖错误正文以外的内部堆栈。具体路由条件以源码及 `/docs` 为准。
