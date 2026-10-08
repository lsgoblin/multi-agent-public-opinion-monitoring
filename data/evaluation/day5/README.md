# Day 5 离线评测数据

`inputs-v1.json` 只包含 cutoff 输入、来源/可见时间证据引用和预测记录。`future-labels-v1.json` 单独保存人工情感标签与未来走势标签；评测器仅读取它计算结果，不会把它写入 Store、图谱、Agent 记忆或推演上下文。

## 输入字段

- `evaluation_id`：唯一评测行 ID；`event_id`：去重后的真实事件 ID；`unit_id` 与 `target_record_id`：该情感预测对应的来源记录。
- `data_mode`：正式指标仅接收 `real_historical`。合成行会标为排除，不进入任何正式分母。
- `cutoff`、`cutoff_evidence_ref`：带时区 cutoff 与证明该值的本地 JSON 文件/JSON Pointer。
- `sources[]`：每个来源记录的 `record_id`、来源版本、可见时间、`source_record_ref`、`source_version_ref`、`source_available_at_ref` 和 `visibility_evidence_ref`。引用必须指向存在的本地 JSON 值；来源版本和记录可见时间必须与来源快照一致，公开可见时间证据也必须与填写时间相符，且不晚于 cutoff。
- `predictions.sentiment`：三分类预测、执行模式、结果证据引用。无预测时 `label: null` 并说明原因。
- `predictions.direction`：只接受 `escalation` 或 `calming`；稳定、弃权、无预测暂记为不可计算，待 V2-Q05 正式口径确认。

## 标签字段

- `sentiment_labels[]` 按 `evaluation_id` 关联输入。至少两位不同标注者的独立标签、均未见目标预测、且标签一致，才形成可计分真值；分歧不自动投票。
- `events[]` 按 `event_id` 提供 `observed_direction`、证据引用和状态。无法根据未来证据形成明确二分类标签时使用 `null` 并写明原因。
- 未来标签文件只用于评测。请勿将其导入任何图、检索索引、Agent 记忆或运行输入。

从仓库根目录复算本地数据：

```powershell
uv run python -m riskshield.day5_evaluation --inputs data/evaluation/day5/inputs-v1.json --future-labels data/evaluation/day5/future-labels-v1.json --out artifacts/day5-evaluation/local-run-v1.json
```

空模板见 `inputs-template.json` 与 `future-labels-template.json`。模板字段规则以本说明和评测器版本 `riskshield.day5.evaluation-inputs.v1` / `riskshield.day5.future-labels.v1` 为准。
