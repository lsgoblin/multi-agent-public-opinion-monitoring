# 真实历史记录评测结果（冻结版 v1 · 2026-10-14）

> ⚠️ **口径提示（2026-10-14 晚补注，必读）**：本文件的分母是 **94 条真实归档记录（标题）**，**不是 94 件独立事件**。
> 按 `event_id` 形态拆开后：具名事件 **6** 件、个人投诉贴 12 条、关键词抽样贴 76 条（合计 94）。
> 事件口径的成绩见 [event-level-evaluation-result-v2.md](event-level-evaluation-result-v2.md)：
> 情感 **7 件 = 57.14%（4/7）**、方向 **9 件 = 40.00%（2/5）**。
> 本文件的 84.04% **不得**用作命题"10—20 件独立真实历史事件"的成绩。

> 本文件是**可提交的量化结果**，数据来自本机已完成的正式评测运行，未新增任何模型调用。
> 来源报告 `artifacts/integration/20261013/data-rescore/benchmark-report-v1.json`，
> SHA-256 `857a4553e255099ba8be69522dae1739932d85eb519c88539114d7768c71e819`，
> `claim_level = formal_offline_metric_on_a_frozen_gated_denominator`。
> 所有数字由 [verify_94_event_score.py](verify_94_event_score.py) 从 94 行逐单位明细**独立重算**，与报告记录值逐项一致。
>
> **哈希沿革**：同一路径的该报告共三代 —— `136ffe1d…`（首版）→ `66d40231…`（修正方向 `scored_event_ids` 列表，记录于 `handoff-20261014-v11.md`）→ **本版 `857a4553…`**。三代的情感六项数字与方向 `2/5` 逐项一致，仅方向事件 id 列表与文件哈希变化；引用本结果时请以本版哈希为准。

---

## 1. 情感与负面识别轨：94 条真实归档记录

| 指标 | 命题目标 | 实测 | 分子 / 分母 | 是否达标 |
| --- | --- | --- | --- | --- |
| 情感三分类准确率 | ≥ 88% | **84.04%** | 79 / 94 | 否（差 3.96 个百分点） |
| 负面识别召回率 | ≥ 85% | **89.19%** | 33 / 37 | **是** |
| 负面识别精确率 | ≥ 90% | **80.49%** | 33 / 41 | 否（命题该项公式未裁定） |
| 负面二分类准确率 | 口径未定 | **87.23%** | 82 / 94 | — |

**混淆矩阵**（行=真值，列=预测）

| 真值 \ 预测 | negative | neutral | positive | 合计 |
| --- | ---: | ---: | ---: | ---: |
| negative | **33** | 4 | 0 | 37 |
| neutral | 8 | **44** | 3 | 55 |
| positive | 0 | 0 | **2** | 2 |
| 合计 | 41 | 48 | 5 | **94** |

负面二分类计数：TP 33、FP 8、FN 4、TN 49。

**分母质量（决定该结果能否报告）**

| 检查 | 结果 |
| --- | --- |
| 不同 `unit_id` / `event_id` / 来源记录数 | **94 / 94 / 94**（无同一事件重复计数） |
| 合成数据进入正式分母 | `false` |
| 非历史行被排除数 | 0 |
| 预测执行模式 | **`formal_model_run_v1`**（全部 114 条准入行一致） |
| 预测方法版本 | `bailian-qwen-flash-title-sentiment-v1`（5 件）、`...-expansion-title-sentiment-v1`（85 件）、`...-formal-title-sentiment-v1`（5 件）等 |
| 预测是否接触标签 / 结果 | `labels_accessed = false`、`outcomes_accessed = false`（114/114） |
| 常量替身或离线替身 | **0 条**（`offline_constant_*` 与 `offline_dynamic_substitute` 均在准入表被拒） |

**标注侧**

| 项 | 数值 |
| --- | --- |
| 标注通道（每单位两条独立通道） | `Leo/Dick` 13、`Neo/zhangsan` 52、`wangwu/zhaoliu` 24、`leos/Dick` 4、`Leo/nike` 1 |
| 两条通道完全一致 | **90 件**（95.74%） |
| 经具名裁决 | 4 件，裁决人记为 `project owner` |
| 未解决分歧 | **0 件** |

---

## 2. 走势方向轨：5 件可评事件

| 指标 | 命题目标 | 实测 | 分子 / 分母 |
| --- | --- | --- | --- |
| 走势方向一致率（二分类） | ≥ 70% | **40.00%** | **2 / 5** |
| 走势三分类诊断（含 stable） | 非正式门 | 22.22% | 2 / 9 |

- 二分类包含的事件：`aflac_cybersecurity_incident_20250620`、`anthem_anesthesia_proposal_20241204`、`humana_algorithm_care_lawsuit_20231214`、`unitedhealth_algorithm_care_lawsuit_20231115`、`unitedhealth_ceo_shooting_20241204`。
- 混淆矩阵（实际 × 预测）：`calming→calming` 2、`calming→escalation` 3；`escalation` 实际为 **0 件**。
- **4 件被排除而非记零**：`cigna_algorithm_denial_lawsuit_20230725`、`fnf_cyberattack_20231123`、`ftc_pbm_insulin_complaint_20240920`、`unh_change_cyberattack_20240222`，原因均为 `missing_or_unresolved_observed_direction_label`。
- 类别支持度 `calming 5 / escalation 0 / stable 4`：本冻结集内**没有任何一件事件走向升级**，因此 escalation 的召回率是**未定义**，不是 0%。

> 走势轨与情感轨是**两个分母**，按评测器定义不得合并。本文件不把它们相加成任何"综合准确率"。

---

## 3. 必须随结果一起报告的限制

1. **标注通道不是真人。** 每单位由两条独立标注通道填写，4 件分歧由项目负责人具名裁决。本结果**不把任何一条通道描述为人工标注员**。这是一处与内部协议（原要求两名真人独立标注）不同的口径，用户已明确授权以 AI 通道承担最终验收；此处如实记录，供评估方判断。
2. **分母数量高于命题区间，但口径不同。** 命题为"不少于 10—20 个历史真实舆情事件"，其确切下限在本项目内一直记为 `unresolved`；本节的分母是 **94 条记录/标题**，不是事件，因此**不声称落在该区间内**，也不声称满足该要求。事件口径见 v2 报告。
3. **两项未达标。** 情感三分类 84.04% < 88%；负面精确率 80.49% < 90%（该项正式公式未裁定，precision 与二分类准确率并列报告）。负面召回率 89.19% ≥ 85% 达标。
4. **走势轨样本量小。** 5 件可评、2 件一致，40% 距 70% 有明显差距；且实际类别中无升级事件，指标的两类都不可比。
5. **以下命题项不由本结果证明**，需各自证据：五平台覆盖与 ≤15 分钟采集延迟、真实历史事件上的 500 Agent × 30 轮、事件输入至五模块报告 ≤60 分钟、红色预警发现至推送 ≤30 分钟、Docker 域内运行与数据不出域。
6. **未新增调用。** 本结果全部来自既有冻结运行，本次冻结过程没有发起任何模型调用或网络请求。

---

## 4. 复现方式

```powershell
$m = "D:\test\多智能体仿真舆情监测与投诉预警"
$env:PYTHONIOENCODING = "utf-8"
& "$m\.venv\Scripts\python.exe" "$m\docs\delivery\event-eval-20261014\verify_94_event_score.py"
# -> 逐项 recomputed vs recorded 全部 OK，结尾 VERDICT: all checks pass
& "$m\.venv\Scripts\python.exe" "$m\docs\delivery\event-eval-20261014\freeze_result.py"
# -> 重新生成 frozen-result-94-events-v1.json 与 per-unit-rows-v1.csv
```

## 5. 本目录文件

| 文件 | 用途 |
| --- | --- |
| `real-historical-evaluation-result-v1.md` | 本文件，可提交的结果说明 |
| `frozen-result-94-events-v1.json` | 机器可读结果：指标、分子分母、混淆矩阵、标注侧、准入来源、限制 |
| `per-unit-rows-v1.csv` | 94 行逐单位明细：unit_id / event_id / 真值 / 预测 / 是否正确 / 标注依据 / 标注通道 |
| `verify_94_event_score.py` | 从逐单位明细独立重算全部指标并与记录值比对的核验脚本 |
| `freeze_result.py` | 由来源报告生成本目录结果文件与明细表 |
