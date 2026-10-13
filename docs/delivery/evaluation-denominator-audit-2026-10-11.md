# 历史事件评测分母核查报告

> 分支 `data-evaluation`，2026-10-11（第二轮）。本文只报告**本次实际重算**的结果，
> 并明确区分正式口径与非正式对照。复核入口：
> `src/riskshield/evaluation_prediction_import.py`、`src/riskshield/evaluation_alignment.py`、
> `tests/test_evaluation_alignment.py`，以及主工作区本机证据
> `artifacts/branch-work/data-evaluation/evidence/20261011/v02/`（被 Git 忽略）。

## 1. 本轮结论

| 指标 | 正式结果 | 说明 |
| --- | --- | --- |
| 情感三分类准确率（目标 ≥88%） | **0/0，不可计算** | 19 个候选单位中没有任何单位同时具备被准入的正式预测与其余条件。 |
| 负面识别 precision / recall / binary accuracy（≥90% / ≥85%） | **分母 0，不可计算**（TP/FP/FN/TN 全 0） | 全零混淆矩阵不是 0%。 |
| 走势方向一致率（目标 ≥70%） | **0/0，不可计算** | 合格观察证据 0、独立真人走势标签 0、正式走势预测 0。 |

**正式分母仍为 0，本轮目标中的「非零正式分母」未达成。**
原因不是评分标准缺失：同一套口径与同一份计分器在显式非正式对照下算出了 3/5
（见第 5 节），差别只在于有没有合格预测。

候选单位数、独立事件数、合格样本数与正式可评分数分别统计：

| 统计项 | 数值 |
| --- | --- |
| 候选情感单位 | 19（5 个真人双标冻结标题 + 14 个 v19 待标注标题） |
| 独立候选事件 | 19 |
| 具备两名独立真人标注的单位 | 5 |
| 被准入的正式预测 | **0** |
| 正式可评分单位（情感） | **0** |
| 走势合格事件 | **0** |

## 2. 与上一轮交接的差异（修正）

上一轮 v02 交接把五个单位的缺口概括为「只缺预测」，该概括**不准确**。逐单位复核后：

| unit_id | 缺正式预测 | 缺归档标题哈希 | 缺来源许可证据 |
| --- | --- | --- | --- |
| `sent-fnf-1821s44` | 是 | 否 | 是 |
| `sent-anthem-1h6mj0x` | 是 | 否 | 是 |
| `sent-cigna-159ce8g` | 是 | 否 | 是 |
| `sent-humana-ars-20231213` | 是 | **是** | 是 |
| `sent-aflac-1lg33ey` | 是 | 否 | 是 |

即：4 个单位只缺「正式预测 + 来源许可证据」，`sent-humana-ars-20231213` 还缺独立归档标题哈希。

同时修正两项判定口径：

1. **来源名称、记录 ID 与版本只说明身份，不等于许可证据。** 冻结材料记录了 post id、
   subreddit、归档 API 与不可变标题，但**没有任何逐来源的使用范围或许可记录**。
   本轮把该项单列为 `no_recorded_source_permission_evidence`，**没有**从来源名称推定授权。
2. **正式资格不能只靠「不在非正式模式黑名单」。** 已改为显式白名单加证据校验，见第 3 节。

## 3. 准入规则（本轮实现）

正式预测的准入由 `evaluation_prediction_import.py` 逐条决定，规则要点：

- 执行模式必须在显式白名单（`formal_model_run_v1`、`sealed_model_run_v1`）内；
  未知或缺失模式一律拒绝，不默认成正式；已知工程替身带专门原因拒绝。
- 必须给出方法版本、`run_id`、`predicted_at`。
- 必须绑定冻结输入包：引用可解析、包摘要一致、cutoff 等于该单位 cutoff、
  冻结文本哈希与来源版本一致。
- 必须给出可解析的**运行完整性证据**，且 `run_status` 为 `complete`。
- 必须给出可解析的**冻结记录**，记录绑定预测文档摘要、含 `frozen_at`，
  并声明 `labels_read_before_freeze=false`、`outcomes_read_before_freeze=false`。
- 行本身必须声明 `labels_accessed=false`、`outcomes_accessed=false`。
- 同一「单位 + 任务」只允许一条；重复**整组拒绝**，不按「最符合标签」挑选。
- 所有被拒绝的行带机器可读原因保留在报告里。

本轮用真实材料验证该入口：把上一轮的常量对照预测文档喂给正式导入器，
5 行全部被拒，原因均为 `rejected_engineering_stand_in_mode`。

## 4. 逐单位配对表与全部排除原因

### 4.1 五个真人双标冻结标题

来源版本、可见时间、cutoff、原文哈希与两名未见预测的独立标注均已核验通过：

| unit_id | 事件 | 可见时间 → cutoff | 人工双标 | 准入预测 | 全部缺口 |
| --- | --- | --- | --- | --- | --- |
| `sent-fnf-1821s44` | `fnf_cyberattack_20231123` | 2023-11-23T13:59:55Z → 14:29:55Z | leos + Dick | 无 | `no_admitted_formal_sentiment_prediction`、`no_recorded_source_permission_evidence` |
| `sent-anthem-1h6mj0x` | `anthem_anesthesia_proposal_20241204` | 2024-12-04T17:59:34Z → 18:29:34Z | leos + Dick | 无 | 同上 |
| `sent-cigna-159ce8g` | `cigna_algorithm_denial_lawsuit_20230725` | 2023-07-25T15:53:54Z → 16:23:54Z | leos + Dick | 无 | 同上 |
| `sent-humana-ars-20231213` | `humana_algorithm_care_lawsuit_20231214` | 2023-12-13T21:40:00Z → 22:10:00Z | leos + Dick | 无 | 同上 + `no_archived_title_hash_evidence`、`original_text_hash_missing_or_mismatched` |
| `sent-aflac-1lg33ey` | `aflac_cybersecurity_incident_20250620` | 2025-06-20T12:46:30Z → 13:16:30Z | leos + Dick | 无 | 同 `sent-fnf-1821s44` |

类别分布（每单位 2 名标注者，5/5 一致）：`neutral` 3、`negative` 2、`positive` 0。

### 4.2 v19 的 14 个待标注标题

身份、来源版本、可见时间与 cutoff 齐备，`annotator_slots` 全部为空：

- 每单位统一排除原因 `fewer_than_two_independent_human_labels`；
- 另统一带 `no_recorded_source_permission_evidence`；
- 尚缺 **27** 个独立真人标注槽位。

### 4.3 走势侧

冻结材料仅有 1 个事件带走势证据（`windows_recall_privacy_controversy_20240607`，`holdout_candidate`），
其全部缺口：

- `no_independent_human_direction_label`（`human_direction_label` 为 `null`）
- `no_evidence_backed_observed_direction`
- `domain_scope_not_approved_for_insurance_denominator`（跨域事件）
- `single_source_family_only_no_second_comparable_series`
- `recorded_source_access_failures`（PullPush 截断、Arctic Shift 422、429）

记录到的观察指标（同一来源族、7 天基线窗 vs 7 天观察窗）：story 20→9、
匹配评论 30→1，9 条观察窗 story 中 6 条集中在最后一天。该记录自评
「不满足当前内部协议」，且明确写明「不应自动标为 calming 或 escalation」。
本轮不据此生成方向标签。

## 5. 反向对照（证明准入门槛与计分器确实会计数，非正式结果）

| 对照 | 结果 | 含义 |
| --- | --- | --- |
| 常量离线替身预测 → 正式导入器 | 5/5 被拒（`rejected_engineering_stand_in_mode`） | 白名单准入有效 |
| 同一常量预测直接计分 | 3/5 与真人标签一致 | 门槛与计分器确实计数 |
| 标签替换（首单位一名标注 `neutral`→`positive`） | 分子 3 → 2、分母 5 → 4 | 计分器按标签计分，未恒真 |

对照预测与冻结记录写在证据目录，`qualification` 明确为非正式，
`material_class` 为 `synthetic_fixture_control`，**不进入任何正式分母**。
报告摘要字段只由正式运行计算，对照数字单独存放于 `control_run`。

## 6. 时间冻结与标签隔离

六个时间概念分别记录，互不冒充（逐单位见报告 `timeline_audit`）：

| 概念 | 当前来源 |
| --- | --- |
| 历史材料可见时间 | 冻结材料记录（可解析引用） |
| 本轮拟定回测输入截止时间 | `visible_at + 30 分钟`，**派生回测窗口规则** |
| 输入包实际冻结时间 | **不存在**，记为 `null` |
| 预测生成时间 | `predicted_at`（需产物提供） |
| 预测冻结时间 | `frozen_at`（需冻结记录提供） |
| 评分流程读取标签/结果时间 | 本评测轮记录 |

上一轮的「`visible_at + 30 分钟`」在本文与产物中一律标注为
`derived_backtest_window_rule_not_historical_freeze`，**不冒充历史已有的 cutoff 冻结证明**。
本轮不补造任何时间戳：`input_package_frozen_at` 保持 `null`。

标签侧：已存在的人工标签继续保存，核验重点是**预测执行侧是否接触真值**
（`labels_accessed` / `outcomes_accessed` 必须为 `false`，并由冻结记录二次确认），
而不是机械比较「预测时间早于人工标签创建时间」。人工标签核验含标注者身份、
独立性、是否见过预测、分歧与裁决记录；本轮未替真人补填任何槽位，
也未把 AI 标注改名为人工标签。

## 7. 事件数缺口（10—20 件）

| 项 | 数值 |
| --- | --- |
| 命题目标区间 | 10—20 件独立真实事件 |
| 当前候选事件身份 | 19 |
| 可评分事件（情感） | **0** |
| 可评分事件（走势） | **0** |
| 补齐 10 件下限还差身份数 | 0（身份数已够） |
| 补齐 20 件还差身份数 | 1 |
| 仍需人工标注槽位 | 27 |

**事件身份数不等于可评分数。** 现有 19 个身份中没有任何一个可评分：
5 个缺正式预测与来源许可证据，14 个缺两名独立真人标注。

## 8. 复现命令

```powershell
$env:PYTHONPATH = 'C:\Users\李\.codex\worktrees\riskshield-data-evaluation\多智能体仿真舆情监测与投诉预警\src'
cd C:\Users\李\.codex\worktrees\riskshield-data-evaluation\多智能体仿真舆情监测与投诉预警
python src/riskshield/evaluation_alignment.py `
  --material-root 'D:\test\多智能体仿真舆情监测与投诉预警' `
  --out-dir 'D:\test\多智能体仿真舆情监测与投诉预警\artifacts\branch-work\data-evaluation\evidence\20261011\v02'
```

可选导入已有正式预测产物（本轮无该类产物，故省略）：

```powershell
python src/riskshield/evaluation_alignment.py --material-root '<主工作区>' `
  --out-dir '<证据目录>' --predictions '<材料根内相对路径>' --freeze-record '<材料根内相对路径>'
```

工具只读取冻结材料并计算哈希，不写入冻结目录；标签只进入标签文档，不进入 Agent 输入；
后续走势证据只用于指标计算。

## 9. 边界

- 正式分母为 0 是「不可计算」，不是 0% 或达标。
- AI 标注、常量离线基线、`offline_dynamic_substitute` 与 `dry_run` 都不计作正式结果。
- 来源署名不等于使用许可；本轮未从来源名称推定授权，该项对全部单位单列。
- 五平台持续采集与延迟、真实通知送达、完整内网出口隔离仍未验证，不在本报告范围内。
- 预测产物契约见[提案](prediction-artifact-contract-proposal-2026-10-11.md)（待总 Agent 确认）。
