# 真实历史事件评测证据（2026-10-14）

本目录是[测试与运行报告](../test-report.md)中"正式评测结果：真实历史事件"一节的**证据与复算脚本**，随源码一起提交，便于逐项核对。

## 两个口径（不可混用）

| 口径 | 事件数 | 三分类 | 负面召回 | 负面精确 | 方向二分类 |
| --- | ---: | --- | --- | --- | --- |
| 记录级（归档记录/标题） | 94 条 | 84.04% | 89.19% | 80.49% | — |
| **事件级（命题口径）** | **情感 10 件 / 方向 10 件** | **70.00%** | **100%** | **66.67%** | **50.00%** |

记录级的 94 条**不是 94 件事件**：其中具名事件 6 件、个人投诉贴 12 条、关键词抽样贴 76 条。两个口径的分母、来源与限制见[测试报告](../test-report.md)。

## 文件

| 文件 | 内容 |
| --- | --- |
| `real-historical-evaluation-result-v1.md` | 记录级（94 条）结果说明，含口径提示横幅 |
| `frozen-result-94-events-v1.json` | 记录级结果：指标、分子分母、混淆矩阵、准入来源、哈希沿革 |
| `per-unit-rows-v1.csv` | 94 行逐单位明细（unit_id / event_id / 真值 / 预测 / 是否正确 / 标注依据） |
| `event-level-evaluation-result-v2.md` | **事件级结果说明**：覆盖矩阵、两轨指标、系统性偏差、限制 |
| `event-level-result-v2.json` | 事件级结果：逐件明细、分布、口径拆解、限制 |
| `record-evidence-card-allianz-v1.json` | 单件证据卡：一件记录从来源到预测到标注的完整链条与 11 份文件哈希 |
| `sentiment-late-annotation-three-v1.json` | 补采三件情感事件的双通道标注记录（含 1 件分歧） |
| `sentiment-adjudication-unitedhealth-ceo-v1.json` | 该分歧的**具名裁决记录**（`decided_by: project owner`），两条原始答案保留 |
| `direction-late-annotation-unh-change-v1.json` | 补采一件方向事件的双通道标注记录 |
| `sentiment-late-run/` | 补采三件的推理产物：预测、生产证据、冻结记录、运行日志 |

## 复现

```powershell
$d = "docs\delivery\event-eval-20261014"
$py = ".venv\Scripts\python.exe"

& $py "$d\verify_94_event_score.py"        # 记录级：从 94 行重算，all checks pass
& $py "$d\join_sentiment_events.py"        # 情感 10 件：7/10、6/6、6/9
& $py "$d\join_direction_ten.py"           # 方向 10 件：3/6、3/10
& $py "$d\emit_event_level_json.py"        # 重建 event-level-result-v2.json
& $py "$d\check_result_consistency.py"     # 跨文件交叉核对 + "不得出现表述"检查
```

`check_result_consistency.py` 会断言三份结果文件与测试报告的数字一致，并检查没有把 94 条表述为 94 件独立事件、没有把标注通道写成人工标注员。

## 发布边界

本目录**不含**以下内容，它们保留在 Git 忽略的本机目录 `artifacts/final-94-event-result/`：

- `sentiment-late-run/frozen-inputs-late-three-v1.json` —— 含新采集的第三方标题**原文**（HN 元数据），按最小必要原则不外发；其哈希已写入同目录 `predictions-late-three-v1.json` 的 `input_binding.input_package_sha256` 与 `freeze-record-late-three-v1.json`，可据此校验；
- 一次性 commit message 草稿等过程文件。

发布副本里的标题原文已被替换为哈希引用（`<verbatim title held in the ignored local copy; verify by text_sha256>`），模型 `reason` 字段同样被隐去，因为它会复述标题。**唯一例外是 `sanitize_published_titles.py` 自身的替换清单**——它必须知道要清理哪些字符串，因此该文件内有标题清单；除此之外任何文件都不含原文。

推理产物（预测、生产证据、冻结记录、运行日志）已发布，因为它们只含哈希、token 用量与标签，不含标题原文。

净化是**生成后的后处理步骤**：重跑 `emit_event_level_json.py`、`join_sentiment_events.py` 等生成器会把标题写回，之后必须再跑一次：

```powershell
& $py "$d\sanitize_published_titles.py"
```

## 需要与结果同时报告的限制

见 `event-level-evaluation-result-v2.md` 第 5 节，共 8 条；其中四条最易被追问：

1. 标注来自**两条独立标注通道**，不是人工标注员；原协议的真人双标未执行（已获用户授权以该口径承担验收）。
2. 情感轨一件标签经**项目负责人具名裁决**改定，原始两票与所用规则臂完整保留。
3. **3 件情感事件与 1 件方向事件为事后补冻结**，已在各自冻结记录中标为 `late`。
4. 系统在两轨**单边输出**（情感 10 件里 9 件判负面；方向 11 次预测里 0 次稳定），这是事件级分数低于记录级的主因。
