# 正式预测产物最小契约（提案）

> 状态：**待总 Agent 确认的提案**，不是已冻结证据，也不是任何分支的既有实现。
> 提出分支：`data-evaluation`；日期：2026-10-11。
> 目的：让标题级情感与事件走势预测能够被 `data-evaluation` 的评测器**逐条准入或拒绝**，
> 而不是靠一个 `qualification` 字符串自述。本文只提出接口，不修改 `simulation-scale`
> 分支代码，也不修改任何共享 schema。

## 1. 为什么需要契约

历史材料里 12 条冻结预测全部是离线常量工程基线（`sentiment` 为 `null`），
因此正式分母为 0。要让分母非零，必须有一份**可追溯、可拒绝、可复算**的预测产物。
评测侧的准入规则已实现于 `src/riskshield/evaluation_prediction_import.py`，
契约即该模块接受的输入形状。

两条硬约束：

1. **白名单而非黑名单。** 执行模式必须在显式正式模式清单内；未知模式一律拒绝，
   绝不默认为正式。
2. **单字符串不能自证。** `qualification`、`complete`、`confidence` 都不构成资格；
   资格只由可解析的证据引用与哈希绑定决定。

## 2. 产物形状

一份预测文档，`predictions` 以 `unit_id` 为键（便于派生确定性证据指针）：

```json
{
  "schema": "riskshield.data_evaluation.prediction_import.v1",
  "task_type": "sentiment_three_class",
  "document_version": "<产物版本>",
  "predictions": {
    "<unit_id>": {
      "unit_id": "<同键>",
      "event_id": "<候选单位的事件 id>",
      "task_type": "sentiment_three_class",
      "label": "positive | neutral | negative",
      "execution_mode": "<正式模式，见 §3>",
      "method_version": "<模型或方法版本>",
      "run_id": "<产出运行 id>",
      "predicted_at": "<ISO-8601 带时区>",
      "cutoff": "<必须等于该单位 cutoff>",
      "input_binding": {
        "input_package_ref": { "path": "<相对材料根>", "pointer": "" },
        "input_package_sha256": "<canonical JSON 的 SHA-256>",
        "cutoff": "<同上>",
        "cutoff_before_label_read": true,
        "text_sha256": "<该单位冻结文本哈希>",
        "source_version": "<该单位来源版本>"
      },
      "production_evidence_ref": { "path": "...", "pointer": "/runs/<run_id>" },
      "freeze_record_ref": { "path": "...", "pointer": "" },
      "labels_accessed": false,
      "outcomes_accessed": false
    }
  }
}
```

### 任务类型

| `task_type` | 指标 | 合法 `label` |
| --- | --- | --- |
| `sentiment_three_class` | 情感三分类、负面识别 | `positive`、`neutral`、`negative` |
| `event_direction` | 走势方向一致率 | `escalation`、`calming` |

两种任务**分开存放、分开准入**。Agent 动作分布、投诉意向、报告风险等级都不能直接充当
情感或走势预测；事件方向预测是事件级结果，多条来源不能抬高它的分母。

### 运行完整性证据

`production_evidence_ref` 指向的对象至少包含：

```json
{ "run_id": "...", "execution_mode": "...", "run_status": "complete",
  "effective_decisions": 0, "failed_decisions": 0, "requested_opportunities": 0,
  "model_calls": 0, "network_requests": 0 }
```

`run_status` 必须是 `complete`。`partial`、超时停止、预算停止的运行不得进入正式候选。

### 冻结记录

`freeze_record_ref` 指向的对象至少包含：

```json
{ "schema": "riskshield.data_evaluation.freeze_record.v1",
  "material_class": "real_frozen_material",
  "frozen_at": "<ISO-8601 带时区>",
  "frozen_by": "<标识>",
  "prediction_document_sha256": "<预测文档 canonical JSON 的 SHA-256>",
  "frozen_inputs_sha256": "<冻结输入包摘要>",
  "labels_read_before_freeze": false,
  "outcomes_read_before_freeze": false }
```

要点：冻结记录绑定**预测文档整体**的摘要；`labels_read_before_freeze` 与
`outcomes_read_before_freeze` 必须显式为 `false`。

## 3. 执行模式

正式白名单（当前）：

- `formal_model_run_v1`
- `sealed_model_run_v1`

工程替身（永不计入正式分母，仅可作为对照）：

- `offline_constant_escalation_baseline_v1` / `_v13`
- `offline_dynamic_substitute`
- 任何 `synthetic_fixture_control_*`

新增正式模式需要总 Agent 明确批准并同步到白名单；**不得**通过改名绕过。

## 4. 与仿真的关系

- **标题级情感分类不必依赖 500×30 仿真。** 它的完整性条件是「单条冻结标题 + 一次可追溯
  的模型调用 + 冻结记录」，规模运行的 `complete` 不自动赋予它正式资格。
- **事件走势预测**才需要事件级运行证据（同一事件、同一 cutoff 输入、观察窗外的结果不进入
  预测输入）。
- 反向也成立：一个 `complete` 的仿真运行**不能**自动让报告里的风险等级变成正式情感或走势预测。

## 5. 准入判定（评测侧已实现）

每个 `(unit_id, task_type)` 逐条检查：

| 组 | 检查 | 拒绝原因示例 |
| --- | --- | --- |
| 身份 | 单位在候选集内、`event_id` 匹配 | `unit_id_not_a_candidate_unit`、`event_id_does_not_match_unit` |
| 任务 | 文档与行 `task_type` 一致、标签在合法类内 | `label_not_in_formal_sentiment_classes` |
| 模式 | 执行模式在白名单、方法与运行已标识 | `unknown_execution_mode_not_admitted`、`missing_run_id` |
| 输入绑定 | 包引用可解析、包哈希一致、cutoff 等于单位 cutoff、文本哈希与来源版本一致 | `input_package_hash_mismatch`、`prediction_cutoff_mismatch` |
| 运行证据 | 引用可解析、`run_id`/模式一致、`run_status=complete` | `production_run_not_complete` |
| 冻结 | 记录可解析、文档摘要一致、`frozen_at` 存在、两项隔离声明为 false | `freeze_record_document_hash_mismatch` |
| 隔离 | 行声明未读标签与后续结果 | `prediction_labels_access_not_declared_false` |
| 唯一性 | 同单位同任务只有一条；重复即整组拒绝 | `duplicate_conflicting_prediction_for_unit` |

**不做的事：** 不按「最符合标签」挑结果；不把未知模式默认成正式；不用 AI 标签、
常量基线或合成行填补缺失的行。所有被拒绝的行都带原因保留在报告里。

## 6. 时间与标签隔离

契约区分六个时间，不互相冒充：

1. 历史材料可见时间（冻结材料中记录）
2. 本轮拟定回测输入截止时间（`visible_at + 30 分钟`，**派生规则**，非历史冻结证明）
3. 输入包实际冻结时间（当前材料中**不存在**，字段留 `null`）
4. 预测生成时间（`predicted_at`）
5. 预测冻结时间（`frozen_at`）
6. 评分流程读取标签与结果的时间（评测轮记录）

不补造任何时间戳，不把本轮生成的证明写成历史既有记录。
已存在的人工标签可以继续保存；核验重点是**预测是否接触真值**，而不是机械比较
「预测时间早于人工标签创建时间」。

## 7. 现有材料的缺口（供总 Agent 排期）

| 缺口 | 归属 | 说明 |
| --- | --- | --- |
| 5 条标题级正式情感预测 | `simulation-scale` 或统一协调的运行 | 5 个真人双标冻结标题已就绪，唯一缺口 |
| 1 条事件级正式走势预测 | 同上 | 事件 `windows_recall_privacy_controversy_20240607` 还缺观察证据与真人方向标签 |
| 来源使用范围/许可记录 | 各分支按授权 | 现有材料只有署名信息，没有许可记录；署名不等于许可 |
| 27 个人工标注槽位 | 需授权的人工标注 | `v19` 的 14 个标题，每件需 2 名未见预测的独立标注者 |
| 截止后观察窗可比数据 | 需新的采集授权 | 走势指标的前置条件 |

## 8. 兼容性

- 本文只描述**新增的评测侧准入模块**与它接受的产物形状，不修改
  `src/riskshield/evaluation.py` 的 `INPUT_SCHEMA` / `LABEL_SCHEMA`，
  也不修改 `api.py`、`schemas.py`、`store.py`、`tasks.py`、模型网关或依赖。
- `evaluation.py` 的既有口径被复用，未新增第二套指标公式。
- 若总 Agent 决定把该产物形状升级为共享契约，需要同步更新本文与冻结协议版本。
