# 历史事件评测分母核查报告

> 分支 `data-evaluation`，2026-10-11。本文只报告**本次实际重算**的结果，并明确区分正式口径与对照口径。
> 复核入口：`src/riskshield/evaluation_alignment.py`、`tests/test_evaluation_alignment.py`，
> 以及主工作区本机证据 `artifacts/branch-work/data-evaluation/evidence/20261011/v01/`（被 Git 忽略）。

## 1. 本次结论

| 指标 | 正式结果 | 说明 |
| --- | --- | --- |
| 情感三分类准确率（目标 ≥88%） | **0/0，不可计算** | 19 个候选单位中，没有任何单位同时具备独立人工双标与配对的正式情感预测。 |
| 负面识别 precision / recall / binary accuracy（≥90% / ≥85%） | **不可计算**（TP/FP/FN/TN 全 0，分母 0） | 全零混淆矩阵不等于 0%。 |
| 走势方向一致率（目标 ≥70%） | **0/0，不可计算** | 合格走势证据 0，独立真人走势标签 0，与冻结走势预测配对的单位 0。 |

本轮把「逐单位配对」做成可复算的检查，结论是：**正式分母仍为 0**。
这不是 0% 或达标，而是样本条件不齐导致不可计算。核查同时确认了卡点位置，见第 4 节。

## 2. 检查的六个条件

只有六项同时成立，单位才允许进入正式分母：

1. 来源许可与可核验版本、公开可见时间；
2. 事件身份在来源、标签、预测三类文件间一致（标题/原文哈希可复核）；
3. 存在历史输入 `cutoff`，且可见来源不晚于该 `cutoff`；
4. 走势评分所需的截止后观察证据；
5. 至少两名未见预测的独立人工标注；
6. 标签与走势读取之前冻结的正式预测。

## 3. 逐单位配对结果

### 3.1 五个真人双标冻结标题

| unit_id | 事件 | 来源版本 | 可见时间 → cutoff | 原文哈希 | 独立双标 | 配对正式预测 | 可评分 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `sent-fnf-1821s44` | `fnf_cyberattack_20231123` | `arctic-shift-title-snapshot-v1` | 2023-11-23T13:59:55Z → 14:29:55Z | 匹配归档哈希 | 是（leos、Dick，均声明未见预测） | **无** | 否 |
| `sent-anthem-1h6mj0x` | `anthem_anesthesia_proposal_20241204` | 同上 + 索引原文 | 2024-12-04T17:59:34Z → 18:29:34Z | 匹配 | 是 | **无** | 否 |
| `sent-cigna-159ce8g` | `cigna_algorithm_denial_lawsuit_20230725` | `arctic-shift-title-snapshot-v1` | 2023-07-25T15:53:54Z → 16:23:54Z | 匹配 | 是 | **无** | 否 |
| `sent-humana-ars-20231213` | `humana_algorithm_care_lawsuit_20231214` | Ars Technica 原始标题 | 2023-12-13T21:40:00Z → 22:10:00Z | **无归档标题哈希** | 是 | **无** | 否 |
| `sent-aflac-1lg33ey` | `aflac_cybersecurity_incident_20250620` | `arctic-shift-title-snapshot-v1` | 2025-06-20T12:46:30Z → 13:16:30Z | 匹配 | 是 | **无** | 否 |

统一排除原因：`missing_paired_sentiment_prediction`。
`sent-humana-ars-20231213` 额外排除原因：`no_archived_title_hash_evidence`（该单位来自新闻原始标题，冻结材料中没有独立归档的标题哈希）。
5 名标注者的标签、时间与证据指针记录在配对报告 JSON 的 `sentiment_units[].annotations[]`。

### 3.2 v19 的 14 个待标注标题

事件身份、来源版本、可见时间与 `cutoff` 齐备，但 `annotator_slots` 全部为空，
每个单位都缺两名独立真人标注；排除原因 `fewer_than_two_independent_human_labels`。

### 3.3 走势证据

冻结材料中仅 1 个事件带走势证据（`windows_recall_privacy_controversy_20240607`，`holdout_candidate`）：
真人方向标签为 `null`，证据自评为不满足当前内部协议，且属跨域事件。合格走势事件因此为 0。

### 3.4 冻结预测清单

12 条冻结预测（v8 四条 + v13 一条 + 事件级方向预测）全部是
`offline_constant_escalation_baseline_v1/v13` 常量工程基线，`qualification` 自述
`engineering_baseline_only_not_formal_forecast`，`sentiment` 均为 `null`。
其中可计为正式模型预测者为 **0** 条，标题级情感预测 **0** 条。

## 4. 卡点

- **情感指标**：唯一缺口是「与五个冻结标题配对的正式标题级情感预测」。
  标题、来源哈希、cutoff、真人双标都已齐备；补齐该预测即可让正式分母非零。
- **走势指标**：缺合格观察证据与独立真人走势标签，两项都需另行取证。
- 本轮**没有**新增联网采集、收费模型调用、真人标注或走势证据，因此分母维持 0。

## 5. 反向对照（证明门槛与计分器确实会计数，非正式结果）

为避免「0/0 无法区分是缺样本还是计分器坏了」，本轮用一份**显式标注为非正式**的常量离线替身预测跑同一套门槛与计分器：

| 对照项 | 结果 | 含义 |
| --- | --- | --- |
| 常量替身配对 | 5/5 单位通过结构门槛，3/5 与真人标签一致 | 门槛与计分器确实会计数 |
| 标签替换 | 把首个单位的一名标注由 `neutral` 改为 `positive` 后，分子由 3 降为 2 | 计分器按标签计分，未恒真 |

对照预测存放在本机证据目录的 `control-predictions-v1.json`，其 `qualification` 为
`engineering_control_only_not_a_formal_prediction`，**不进入任何正式分母**。

## 6. 复现命令

```powershell
$env:PYTHONPATH = 'C:\Users\李\.codex\worktrees\riskshield-data-evaluation\多智能体仿真舆情监测与投诉预警\src'
python src/riskshield/evaluation_alignment.py `
  --material-root 'D:\test\多智能体仿真舆情监测与投诉预警' `
  --out-dir 'D:\test\多智能体仿真舆情监测与投诉预警\artifacts\branch-work\data-evaluation\evidence\20261011\v01'
```

工具只读取冻结材料并计算哈希，不写入冻结目录；标签只进入标签文档，不进入 Agent 输入；
后续走势证据只用于指标计算。

## 7. 边界

- 正式分母为 0 是「不可计算」，不是 0% 或达标。
- AI 标注、常量离线基线、`offline_dynamic_substitute` 与 `dry_run` 都不计作正式结果。
- 五平台持续采集与延迟、真实通知送达、完整内网出口隔离仍未验证，不在本报告范围内。
