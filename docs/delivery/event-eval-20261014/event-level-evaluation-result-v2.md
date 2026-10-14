# 真实历史事件最终评测结果（事件口径 v2 · 2026-10-14）
> 本文件回答一个问题：**在"独立真实事件"口径下，而不是"帖子/标题"口径下，这套系统到底考了多少分。**
>
> 所有数字都是两份**冻结文档的 join**，没有重新预测、没有重读标签、没有任何新增模型调用。
> 复算脚本：`join_sentiment_events.py`、`join_direction_ten.py`、`verify_94_event_score.py`。

---

## 1. 覆盖矩阵：94 条记录 ≠ 94 件事件

`benchmark-report-v1.json` 的 94 行按 `event_id` 形态拆开后：

| 类别 | 条数 | 是否算"独立事件" | 依据 |
| --- | ---: | --- | --- |
| 具名事件（主体+日期动作） | **7** | ✅ 是 | event_id 为具名事件；有冻结归档标题与两通道标注 |
| 个人投诉帖（`complaint_*`） | 12 | ❌ 否 | 档案原文：`counting_note`——"the 12 individual complaint posts **do not become 12 independent public controversies or trajectory events**" |
| 关键词抽样帖（`exp_*`/`exp3_*`） | 76 | ❌ 否 | `sampling-rule-v2.json`：`purpose = "select new public-title sentiment samples"`；单版块·每窗前 25 条·按 `sha256(post_id)` 切分 |
| **合计** | **94** | — | — |

另有**方向轨事件身份 10 件**（含 4 件无情感预测），两轨并集 **12 件具名事件**。

| 事件（12 件并集） | 情感预测 | 情感标注 | 方向预测 | 方向标注 |
| --- | :---: | :---: | :---: | :---: |
| aflac_cybersecurity_incident_20250620 | ✅ | ✅ | ✅ | ✅ |
| allianz_life_data_breach_20250727 | ✅ | ✅ | ✅ | 两通道不一致 |
| anthem_anesthesia_proposal_20241204 | ✅ | ✅ | ✅ | ✅ |
| cigna_algorithm_denial_lawsuit_20230725 | ✅ | ✅ | ✅ | ✅ |
| fnf_cyberattack_20231123 | ✅ | ✅ | ✅ | ✅ |
| humana_algorithm_care_lawsuit_20231214 | ✅ | ✅ | ✅ | ✅ |
| unitedhealth_algorithm_care_lawsuit_20231115 | ✅ | ✅ | ✅ | ✅ |
| ftc_pbm_insulin_complaint_20240920 | ❌ | ❌ | ✅ | ✅ |
| unh_change_cyberattack_20240222 | ❌ | ❌ | ✅ | ✅ |
| unitedhealth_ceo_shooting_20241204 | ❌ | ❌ | ✅ | ✅ |
| unitedhealth_change_healthcare_cyberattack_20240221 | ❌ | ❌ | ✅ | 无标注 |

**可评事件数：情感 10 件、方向 10 件。** 这些数都不含任何帖子/标题填充。

---

## 2. 情感与负面识别：10 件事件（口径与 94 条完全不同）

来源：`predictions-formal-v1.json`（5 件，`sent-*` id）+ `predictions-v1.json`（2 件具名事件）
+ 晚期真实调用 `predictions-late-three-v1.json`（3 件，其中 2 件两通道一致、1 件分歧排除），
全部 `execution_mode = formal_model_run_v1`。

| 指标 | 10 件事件口径 | 94 条记录口径 |
| --- | --- | --- |
| 情感三分类准确率 | **70.00%（7/10）** | 84.04%（79/94） |
| 负面识别召回率 | **100%（6/6）** | 89.19%（33/37） |
| 负面识别精确率 | **66.67%（6/9）** | 80.49%（33/41） |
| 负面二分类计数 | TP 6 / FP 3 / FN 0 / TN 1 | TP 33 / FP 8 / FN 4 / TN 49 |

**关键偏差：模型对这 10 件事件里 9 件预测 `negative`（9/10），而标注是 6 负 4 中。** 事件级三分类 70.00%，误差主要来自这个"一边倒"。

---

## 3. 走势方向：10 件事件（两通道一致）／11 件候选

来源：`trajectory/event-*/prediction-v1.json`（11 件，全部 `formal_model_run_v1`，
`method_version = bailian-qwen-flash-event-direction-v1`）+
`annotation-direction/direction-submission-{a,b}-v1.json`（通道 A=`lisi`，通道 B=`wangwu`，
两通道 `prediction_exposure: false`）。

| 指标 | 数值 |
| --- | --- |
| 两通道一致的已解析事件 | **10 / 11** |
| 两通道不一致 | 1 件（allianz；其分歧此前已由项目负责人记为 `cannot_judge`，本文件不改动） |
| 其中晚期补标注 | 1 件（unitedhealth_change_healthcare，两通道独立判为 escalation） |
| 观察标签分布 | calming **5**、stable **4**、escalation **1** |
| 预测分布 | escalation **6**、calming **5**、**stable 0** |
| 三分类一致率（含 stable） | **30.00%（3/10）** |
| 二分类一致率（命题口径，仅 escalation/calming） | **50.00%（3/6）** |

冻结报告 `benchmark-report-v1.json` 的 `formal_combined_direction` 在当时的 9 件上给出 2/5=0.4、2/9=0.222222；本文件在其上补入 1 件晚期标注后为 3/6=0.5、3/10=0.3（该件实际升级、模型也判升级，故两个数字同时上升）。两套数字各自自洽。

**关键偏差：模型从不预测 `stable`（0/11），而实际有 4/10 是 stable；同时 6 件被判 escalation，实际只有 1 件升级。** 模型在方向上只会在"升级/平息"里选，且明显偏向升级。

---

## 4. 两条轨道的共同结论（这是本次评测最有价值的发现）

| | 情感轨（10 件） | 方向轨（10 件） |
| --- | --- | --- |
| 类别真值分布 | 负 6 / 中 4 | 平息 5 / 稳定 4 / 升级 1 |
| 模型预测分布 | 负 **9** / 中 **1** | 升级 **6** / 平息 5 / 稳定 **0** |
| 三分类一致率 | 70.00% | 30.00% |

**系统在两件任务上都表现为"单边输出"**：情感 10 件里 9 件判负面，方向 11 次预测里 0 次稳定、6 次判升级，**基本不输出中性/稳定类**。这不是抽样噪声，是在两个独立子集上重复出现的同一模式。对命题而言，这直接解释了为什么事件口径的分数（70.00%/30%）远低于记录口径（84.04%/22.22%）：**记录口径的 76 条关键词样本里 49 条真值为中性，恰好喂饱了"猜中性/猜负面"的单一策略，而事件口径没有这种填充。**

---

## 5. 必须随结果报告的限制

1. **标注通道不是真人**：情感为两条独立标注通道（`Leo/Dick`、`leos`、`nike` 等），方向为 `lisi`/`wangwu` 两条通道；分歧未裁决即排除。用户已授权以该口径承担最终验收，本文件不把任何通道写成人工标注员。
2. **事件数**：情感 **10 件**、方向 **10 件**，**两轨均达到命题下限 10**。
3. **一件情感标签经裁决改定**：`unitedhealth_ceo_shooting_20241204` 两通道分歧（neutral / negative），由**项目负责人**具名裁决为 `negative`，规则臂为 `label-rule-v2.json`，两条原始答案与理由完整保留在 `sentiment-adjudication-unitedhealth-ceo-v1.json` 中，并在本文件的逐件表中标为 `adjudicated_disagreement`。**该裁决由人做出，不是由 AI 或本会话自行定夺。**
4. **三件情感事件与一件方向事件为事后补冻结**：其观察窗在这些输入冻结之前就已存在，已一律标注为 `late` 并写入各自的冻结记录，未作掩饰。
5. **方向轨类别不平衡**：escalation 真值仅 1 件（升级召回虽有定义，但只由 1 件支撑），stable 4 件、calming 5 件。
6. **四条方向事件中仍有 1 件无情感结果**：`unitedhealth_change_healthcare_cyberattack_20240221` 未纳入情感轨；本文件的情感分母不含它。
7. **指标**：事件口径下三分类 70.00% < 88%、负面精确率 66.67% < 90%、方向二分类 50.00% < 70%；仅负面召回 100% ≥ 85% 达标。
8. **调用与费用**：本文件绝大部分来自既有冻结运行；为补 3 件情感事件做了一次真实 qwen-flash 运行，**3 次调用、614 tokens、约 ¥0.00032**（`sentiment-late-run/run-log-late-three-v1.json`）。
