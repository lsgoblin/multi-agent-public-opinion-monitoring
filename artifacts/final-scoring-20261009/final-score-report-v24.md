# 最终离线评分报告（v24 / 2026-10-09）

## 结果

本轮**没有可报告的正式模型分数**。最新 v13 E 输出是 `offline_constant_escalation_baseline_v13`，`formal_score_ready=false`；v8 的四条输出同样是离线常量基线。常量替身不计作正式模型预测。v24 冻结记录为 7 个留出事件身份、合格走势事件 0、独立真人走势标签 0、已配对 E 情感预测 0。

| 指标 | 正式结果 | AI 参考结果 |
|---|---:|---:|
| 情感三分类准确率 | 不可计算（0/0） | 不可计算（0/0） |
| 负面识别 precision / recall / binary accuracy | 不可计算（0/0；TP/FP/FN/TN 均 0） | 不适用 |
| 走势方向一致率 | 不可计算（0/0） | 不可计算（0/0） |

情感 3×3、负面 2×2 和走势 2×2 混淆矩阵均为全零，因为没有满足配对条件的样本；这不表示准确率为 0%。

## 本轮标注

- AI 标注 18 个冻结标题单位：positive 0、neutral 8、negative 7、uncertain 3；每条包括原文哈希、来源版本、时间、理由和未校准信心。
- 对 11 个有冻结走势材料的事件身份给出 `unknown`；未根据新闻结果、业务动作或单日波动猜测升级/平息。
- 标注者类型为 AI；已看过封存 E 预测和工作区既有 AI 建议，故不是盲标，未人工核验，正式人工真值新增为 0。
- 14 个 v19 标题的 27 个独立真人槽位仍未完成。AI 标签不填补这些槽位。

## 评分执行

- v8 评分器成功运行 4 个工程单位：走势分母 0，情感分母 0。另传入本轮 AI 参考文件后，AI 参考分母仍为 0；这些单位没有标题级情感预测。
- v13 的原有 `final_benchmark_score.py` 分支因 `_score_v13_against_v24` 未定义而失败，源文件未改动。随后通过新增的本轮适配脚本调用现有 `day5_evaluation.evaluate_documents`，按 v24/v2 计算并生成逐项排除记录；仍是 0/0，不把常量基线或 AI 标签计为正式模型/真人真值。命令：`$env:PYTHONPATH='src'; python artifacts/final-scoring-20261009/score_v13_v24.py`。
- v24 冻结链校验：v1–v24 成员和父哈希全部通过。

## 剩余量

- 正式可评分留出事件：0/20，缺口 20；若只计达到 20 个事件身份，还需 13 个不同身份。
- v19 独立真人情感标注槽：27。
- 5 个已双标原始标题单位均无配对 E 情感预测。
- 正式趋势方向标签和合格趋势证据均为 0。

完整逐单位明细：`artifacts/final-scoring-20261009/per-unit-results-v24.json`。AI 标注及来源哈希：`artifacts/final-scoring-20261009/ai-annotations-final-v1.json`。评分器/指标输出：`artifacts/final-scoring-20261009/v8-scorer-output.json`、`artifacts/final-scoring-20261009/v8-ai-reference-scorer-v2.json`、`artifacts/final-scoring-20261009/v13-v24-evaluator-final-v2.json`。
