# E 预测与 Day 5 评测字段映射 v8

**用途：**将 `qualified-inputs-v8.json` 的 cutoff-only `events[]` 交给 E 预测；若后续需要把预测结果适配到现有 `riskshield.day5.evaluation-inputs.v1`，按本表建立一条每事件的评测行。C 不修改评分实现，也不在此文件填预测或标签。

## 字段映射

| v8 来源字段 | E / Day 5 目标字段 | 规则 |
| --- | --- | --- |
| `events[]` | `cases[]` | 每个底层事件最多一条走势预测行；来源、帖子、多个 case_id 不拆成独立事件。 |
| `event_id` | `event_id` | 原样保留。 |
| `case_id` | `evaluation_id`（可加 `__direction__v8` 后缀） | 评测行唯一 ID，不作为事件数。 |
| 固定字符串 `event_cutoff_summary_v8` | `unit_id` | 走势预测是事件级单位，不生成帖子级方向样本。 |
| 选择的 cutoff 主来源 `cutoff_sources[].record_id` | `target_record_id` | 必须同时保留在 `sources[]` 中。 |
| `data_mode: real_historical_public_sources` | `data_mode: real_historical` | 只对确为历史公开输入的行映射；人工真值和未来材料不放进 case。 |
| `dataset_version` | case `source_version` | 保留输入批次版本。 |
| `cutoff` | `cutoff` | 原样保留带时区值。 |
| `cutoff` 的本地 JSON Pointer | `cutoff_evidence_ref` | `path=data/evaluation/final-benchmark/inputs/qualified-inputs-v8.json`，`pointer=/events/{index}/cutoff`。 |
| `cutoff_sources[]` | case `sources[]` | 原样带上 `record_id`、`publisher`、`source_version`、`published_at` 和 `available_at`。不把 cutoff 后记录加入。 |
| cutoff source 对象内的 `record_id` / `source_version` / `available_at` | `source_record_ref` / `source_version_ref` / `source_available_at_ref` / `visibility_evidence_ref` | 指回同一个 cutoff source JSON 对象的相应字段；另保留 `source_url`、`time_evidence`、`available_at_evidence_url` 供人工核查。若可见时间只有日期或不明，不编造时区/时分，改为不合格或等待补证。 |
| `input_text` | E 的预测内容字段 | 只在 E 预测运行时读取。不要将它当作原帖情感标注单位。 |
| `labels/trajectory-evidence-v8.json` | Day 5 `future-labels-v1` 的事件方向证据 | 仅在预测输出及输入哈希封存后，由评分侧读取；v8 状态为 unknown，不生成方向真值。 |
| `labels/blind-sentiment-packet-v6.json` | Day 5 `sentiment_labels[]` 的独立材料来源 | 每条仍须真人双标、无预测暴露；当前 0/10。标题情感不得由事件摘要替代。 |

## 当前关键限制

- v8 文件是 C 的来源充分性/版本数据包，不是 Day 5 evaluator 的直接 `cases[]` 文件。E 可直接按 `events[]` 读取 cutoff 输入；如调用评测器，再按上表由 E/主 Agent 生成本地适配行。
- FNF SEC 来源保留的提交时间为外部交叉报告时间，现有输入包含时间 URL 与说明；Louisiana AP 页面无历史字节副本；State Farm 官方页无历史字节快照/修订记录。适配器不得把“时间字段结构化”误当成“历史内容版本已正式接纳”。
- 评测器 `predictions.direction` 只接受 escalation/calming；在协议冻结、证据不足或人工标签未回收时必须留空并说明，不将 stable/unknown 改造成方向标签。
- 这份映射不授权修改 `src/riskshield/day5_evaluation.py`，也不把文档映射等同于一次实际 E 预测或正式评分。
