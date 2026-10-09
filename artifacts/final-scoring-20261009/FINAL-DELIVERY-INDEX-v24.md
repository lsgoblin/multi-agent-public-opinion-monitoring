# 最终评分材料交付索引（v24）

> 本索引固定本轮评分材料的交付边界与 SHA-256。`ready` 仅表示材料可供 E 选择固定快照；不表示正式分数可计算或 G5 通过。

## 交付结论

冻结链核验结果记录为 v1–v24 成员和父哈希全部通过，v24 冻结 SHA-256 为 `f8e3c3dc812d96827d3ebd2d5ae95c4f4882493f18f07b9a270d16993f7ae246`。正式情感三分类为 **0/0，不可计算**；走势方向为 **0/0，不可计算**；负面识别的 TP/FP/FN/TN 均为 0，precision、recall、binary accuracy 分母为 0，均不可计算。混淆矩阵全零不代表 0% 分数。

正式情感缺少与 5 个冻结人工双标标题配对的 E 标题级预测；v13 情感输出为 `null`，且单位是事件摘要。正式走势合格证据 0、真人走势标签 0。v24 有 7 个留出身份，正式可评分事件为 0/20，缺口 20；另需 13 个不同身份才能单纯补足 20 个身份数，这不替代证据与标签。

## AI 参考标注边界

标注者为 AI（GPT-6 / Codex，`codex-root`），标注时间为 2026-10-09 01:05:06（北京时间）；18 个标题情感标注分布为 negative 7、neutral 8、positive 0、uncertain 3，另有 11 个走势标签均为 `unknown`。标注对封存预测有暴露、未经人工核验，不是盲标或独立人工真值；正式人工真值新增为 0。AI 情感参考一致率 0/0，因没有对齐的 E 标题级情感预测；11 个 `unknown` 不进入升级/平息分母。

## 执行模式及排除

v8 的 4 个单位与 v13 的 1 个候选均为离线常量工程基线：`offline_constant_escalation_baseline_v1` 和 `offline_constant_escalation_baseline_v13`。v13 封存记录为 `formal_score_ready=false`；它们不计为正式模型预测。走势排除含 `holdout_candidate_not_admitted`、观察走势无证据/未决；情感排除含缺失标题级预测、缺失有效标签或单位不是原始标题。

原有 v13 scorer 分支因 `_score_v13_against_v24` 未定义而在计分前失败；未修改该 scorer。本轮适配脚本调用既有 `day5_evaluation.evaluate_documents` 完成 v24/v2 指标计算，仍为 0/0。

## 核心文件

| 文件 | SHA-256 |
| --- | --- |
| [final-score-report-v24.md](final-score-report-v24.md) | `3c812d7655e85d9f31723729cc8b17ac20eef913bcc94fc8b859638386325377` |
| [final-score-report-v24.json](final-score-report-v24.json) | `65edb6e8f218bf2cb96de88d777040c96647539802957541ba49f713cb421c52` |
| [per-unit-results-v24.json](per-unit-results-v24.json) | `2b6093aebfd418f554d58153b47ea2f9208188a884f0f6a54626f22f1699f36f` |
| [ai-annotations-final-v1.json](ai-annotations-final-v1.json) | `cc178d339607d23a65e0e166822461ad4089815d81c5d3c27ee22c15d3ac3fc6` |
| [ai-reference-for-scorer.json](ai-reference-for-scorer.json) | `a42187bd2e5bb96d73e3517338b98e353d3cd8d299b4cf1fa0a1a9981581e731` |
| [v8-scorer-output.json](v8-scorer-output.json) | `66ad919519bc56e35ad02b2fe210148e6f3c3af1492984bd751d51af64301ca9` |
| [v8-ai-reference-scorer-v2.json](v8-ai-reference-scorer-v2.json) | `d369ed7ce8df314ae8bc9ae83990aaf52fb3083c0896b69facba677ede9bcffb` |
| [v13-v24-evaluator-final-v2.json](v13-v24-evaluator-final-v2.json) | `c74d2a7a9b027f63d98380014b087983cc85254223d6895a29a1836ab6a56276` |
| [score_v13_v24.py](score_v13_v24.py) | `05dc09e4c73405325e43bba75e30d412616dbb03335d0dc94eaaece0427f33fd` |

## 必要依赖与封存来源

以下路径与哈希记录在本轮交接 JSON；其中冻结协议、清单、E handoff、seal、预测输出、评分引擎及 AI 来源文件用于追溯和复现。

- [experiments/final_benchmark_score.py](../../experiments/final_benchmark_score.py) — `df245674a868967068ffc8044c338e58aae022278102d3174b2de8c5dc91cd33`
- [src/riskshield/day5_evaluation.py](../../src/riskshield/day5_evaluation.py) — `e0c73f0f126894a465e49dcc37f9c408e61cc23ad8d573452adaf5e9178047bc`
- [experiments/verify_final_benchmark_freezes.py](../../experiments/verify_final_benchmark_freezes.py) — `e316d25bdc94f7718bd275b54df12eb5e2acc9409c702cba691a6b257f502d5b`
- [docs/records/final-scoring-protocol-v2.md](../../docs/records/final-scoring-protocol-v2.md) — `cb39c32c8ec2b4823a22fab8ee6e2c36bda047cea3f605a9a7a2fa0dec5e336a`
- [data/evaluation/final-benchmark/manifests/freeze-v24.json](../../data/evaluation/final-benchmark/manifests/freeze-v24.json) — `f8e3c3dc812d96827d3ebd2d5ae95c4f4882493f18f07b9a270d16993f7ae246`
- [data/evaluation/final-benchmark/manifests/final-reconciliation-v24.json](../../data/evaluation/final-benchmark/manifests/final-reconciliation-v24.json) — `d8c69cb69300f49657ae01c862202974d050f04038b6537d37a0172c0158364d`
- [data/evaluation/final-benchmark/manifests/freeze-v8.json](../../data/evaluation/final-benchmark/manifests/freeze-v8.json) — `9110a1c397ed94c790a493af6796b9df692615ec022eda67fd577ce717356aa7`
- [data/evaluation/final-benchmark/manifests/e-handoff-v8.json](../../data/evaluation/final-benchmark/manifests/e-handoff-v8.json) — `d554d5b1e415ea6ca51c51b6db7b1ee9bc14b3aeec34459c0dc3421916246477`
- [data/evaluation/final-benchmark/manifests/e-handoff-v13.json](../../data/evaluation/final-benchmark/manifests/e-handoff-v13.json) — `cc5a4d46d088dcb5bf0c46c9ab9bfcfe65bd34386dcc31d769c513c88db977b9`
- [artifacts/final-integration-review/v8-pilot/pilot-seal-e-handoff-v8.json](../final-integration-review/v8-pilot/pilot-seal-e-handoff-v8.json) — `0748b57d03acfd73120b77bf4ae8b1e8776394482272552656c11c37221a164f`
- [artifacts/final-integration-review/v8-pilot/pilot-predictions-e-handoff-v8.json](../final-integration-review/v8-pilot/pilot-predictions-e-handoff-v8.json) — `a1cfc23a9f1b16e7f4b8a32a3494181de4afac5619c8f93ca11936e3bac4d4d5`
- [artifacts/final-integration-review/v13-pilot/seal-v13.json](../final-integration-review/v13-pilot/seal-v13.json) — `cef4a4252430afc190d5c83f3b4340a37263e50aa53e00a14c993997e5a2feb4`
- [artifacts/final-integration-review/v13-pilot/predictions-v13.json](../final-integration-review/v13-pilot/predictions-v13.json) — `39957cf8c485dfe6720ef2ab7f29598af6e553bc568c2b57f4955ca95e55bb3f`
- [data/evaluation/final-benchmark/inputs/qualified-inputs-v8.json](../../data/evaluation/final-benchmark/inputs/qualified-inputs-v8.json) — `c49df7870676303950b859729c65def50d5d0042a8ca363fb425bc53a2e2027a`
- [data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json](../../data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json) — `01193167bc59ec4e6d59c12922ad7a9e09d9886deb520c255a5e963aaa9b772c`
- [data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json](../../data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json) — `e31e1a2962e40bbe4e2e06dbc7a8cbdd2c1198031398b4407a47923b81ec29fc`
- [data/evaluation/final-benchmark/labels/trajectory-evidence-v8.json](../../data/evaluation/final-benchmark/labels/trajectory-evidence-v8.json) — `47a3b4191c826c53cc1005ed8960c9372a1934a3f4b87582f10118fe4457d058`
- [data/evaluation/final-benchmark/labels/trajectory-evidence-v13.json](../../data/evaluation/final-benchmark/labels/trajectory-evidence-v13.json) — `d0dd402d2506d1b23d18de7daf6ab2a302e7ef1ca14e64861a8c70534feee536`
- [data/evaluation/final-benchmark/labels/blind-sentiment-reannotation-packet-v19.json](../../data/evaluation/final-benchmark/labels/blind-sentiment-reannotation-packet-v19.json) — `0f92c86092187ba936a48881eb98d8592e38a7bdcadb39a547c1f1d85697f565`
- [data/evaluation/final-benchmark/labels/verified-original-titles-v19.json](../../data/evaluation/final-benchmark/labels/verified-original-titles-v19.json) — `54a08c3543f29bd808e74b1b1998286771a26626caf9c4f68dd7f416fe0235e2`
- [data/evaluation/final-benchmark/labels/trajectory-evidence-v4.json](../../data/evaluation/final-benchmark/labels/trajectory-evidence-v4.json) — `dee1d41bb77211ad828b58be3ef5311e5d48d3a9df360095600db2864cb41d5b`
- [data/evaluation/final-benchmark/labels/trajectory-evidence-v11.json](../../data/evaluation/final-benchmark/labels/trajectory-evidence-v11.json) — `783d7310e98840ecd0a0810ec1995b8c6d9909b381c805fbb4cbd7d573fad9ed`

## 本轮复核

- JSON 可解析；报告内嵌哈希、E seal 对应预测哈希、AI 标注来源文件哈希均已复核。
- 冻结链复核结果沿用本轮评分报告所记录的 v1–v24 全部成员和父哈希通过；本整理没有改写冻结材料。
- 本整理只新增索引与交接 JSON；未运行测试。此前 181 项通过属于整理前基线，不能作为本轮整理后测试结果。
- 本交接状态 `ready` 仅供 E 纳入固定快照；评分仍不可计算，G5 正式达标未证明。
