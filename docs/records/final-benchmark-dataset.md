# 多事件正式评测数据集准备记录

> 当前最终汇总与统计更正：v24。版本化输入、证据、标签及冻结清单才是逐项审计依据；v1—v23 保持原样。

## 当前结论

**正式走势可评分留出事件仍为 0/20。** 另外，已有 **5 个精确原始标题单位完成独立双标，5/5 一致**；这是情感标签准备数，不等于走势合格事件数。V2-Q03 口径尚待冻结，且当前没有与这 5 个标题哈希匹配的 E 情感预测，所以实际情感评分数仍为 0。现有留出身份为 7 件；至少还缺 13 个身份才达到 20 个身份，但正式合格事件缺口仍为 20。

v4 基线审查确认 20 条原始输入记录中只有 8 件可辨识事件身份（含已开发探测的 UNH），留出事件 7 件；12 条个人投诉标题仅作辅助材料，13 条替代线索当时均未接纳。截止前内容和真实投诉帖存在，不等于走势或独立事件资格。

v8 曾向 E 提供 4 件 cutoff-only pilot：FNF、Louisiana BCBS–Elevance、Aflac、State Farm 2023。v11 补交 1 件已有身份的 UnitedHealth/Lokken 诉讼；E 为 v11 返回固定离线工程基线。v12 去掉未固定版本的 STAT 正文细节后重跑，E 使用 `offline_constant_escalation_baseline_v12`，未调用模型或网络。两个批次都不是新增独立事件，走势标签仍为 `unknown`，真人情感标签 0/2。正式可评分事件数仍为 0。

## 统一口径与接纳规则

当前按[最终离线评测内部执行协议 v1](final-scoring-protocol.md)筛查。它是项目内部冻结方法，不是主办方确认口径。命题明确要求真实历史事件、截止隔离和真实走势方向评估；窗口、双来源要求和阈值均标为内部执行口径。

当前协议规定：D0（cutoff 所在 UTC 日）排除；基线 D−7 至 D−1、观察 D+1 至 D+7；固定相同查询、语言/地域、来源范围、去重与采集方式。两个独立可比来源族必须含至少一个公众讨论来源；每窗至少 6/7 个有效日，主讨论指标每窗至少 5 个去重单位。观察/基线比值、零基线、截断、来源冲突和 `unknown` 规则见协议原文。Reddit 帖子和评论属于一个来源族；网页搜索热度、新闻篇数只能作为传播代理，不能写成全平台情绪真值。业务恢复、单次公司回复、结案或无搜索结果都不能单独证明平息。

任何事件只有在身份、cutoff 输入版本、同口径双来源走势证据、独立真人标签与 E 预测封存均可核验后，才能进入正式评分分母。预测只读 cutoff 输入；标签和未来证据只在预测及输入哈希封存后由评分端读取。人类标签未回收时保持缺失，不代填。

## v10 来源筛查

本轮依据身份与证据质量筛查，不查看模型预测。详细查询窗口、来源版本、计数、false-match 检查与排除原因见[独立来源筛查 v10](../../data/evaluation/final-benchmark/manifests/independent-source-screen-v10.json)。

| 候选 | 核查结果 | 处置 |
| --- | --- | --- |
| State Farm 2023 加州新单暂停 | Reddit 同查询 v8 序列为帖子 16→157、评论 82→235；仍只有一个有效来源族。Hacker News 精确查询的基线 8 条评论来自 3 个无关主题，0 条故事；观察窗有 11 条故事、14 条评论，但评论尚未逐条核验。宽查询 `State Farm` 的事前命中也含无关主题。 | 保留 E pilot；第二来源基线不相关，方向 unknown，不进入走势分母。 |
| UnitedHealthcare CEO 2024-12-04 枪击后讨论 | Hacker News 精确查询前窗 0/0、后窗 30 条故事/87 条评论；PullPush 后窗返回 100/100 条帖子/评论，可能受接口上限影响。 | 两源基线均为零；枪击本身不是保险政策/理赔事件，只作辅助线索，不接纳。 |
| Cigna PxDx 诉讼 | Reddit 0/1→0/7；Hacker News 0/0→0/1。 | 既有候选但样本量不足，不接纳走势评测。 |
| FNF 网络事件 | 冻结 Reddit 系列覆盖不足；Hacker News 1/0→0/0。 | 只有一个弱来源族，不接纳走势评测。 |
| Nationwide 北卡不续约 | Reddit 0/0→1/3；Hacker News 0/0→0/0；现有报道只证明日期，未证明分钟级首见时间。 | 不纳入 cutoff 输入或正式走势集合。 |

PullPush 的部分请求返回 HTTPError（本轮未保存状态码），Google Trends 公开时间序列接口返回 HTTP 429；均停止重试。错误响应不等于零讨论。Hacker News 查询摘要与逐日返回情况记录在 v10 筛查文件中。

## 标签与交 E

- [真人情感标注清单 v10](../../data/evaluation/final-benchmark/labels/human-annotation-worklist-v10.md)：5 个冻结原始标题单位，每个由两名盲态真人独立标注，共 10 个槽位；已收到 0/10，缺失 10/10。建议 2026-10-09 09:00（北京时间）前回收，供 10:00 冻结前核验。
- 走势标注任务：0。没有事件通过双来源和最低覆盖门槛，不让标注者猜升级/平息/稳定。
- [E 交接 v10](../../data/evaluation/final-benchmark/manifests/e-handoff-v10.json)允许预测端只读 `qualified-inputs-v8.json`，SHA-256 为 `c49df7870676303950b859729c65def50d5d0042a8ca363fb425bc53a2e2027a`。本轮已向现有 Agent E 任务发送交接，当前等待 E 封存预测、配置/代码哈希和执行模式。标签和后续证据不进入预测上下文。

## v10 统计

| 流转项 | 当前数量 |
| --- | ---: |
| 本轮筛查档案 | 5 |
| v8 E pilot cutoff 输入 / 内容充分候选 | 4 / 4 |
| 本轮新增 cutoff 输入 | 0 |
| 两个合格可比来源族 / 合格走势证据事件 | 0 / 0 |
| 独立真人情感标签 | 0；10 槽待回收 |
| 独立真人走势标签 | 0 |
| 新增正式可评分事件 / 正式可评分留出总数 | **0 / 0** |
| 距中间里程碑 10 件 / 目标 20 件 | **10 / 20** |

## v11 事件证据包

[独立来源筛查 v11](../../data/evaluation/final-benchmark/manifests/independent-source-screen-v11.json)审查 3 个已有候选，不新增事件身份。UnitedHealth/Lokken 诉讼有 2023-11-14 固定 Doc. 1 诉状、STAT 同期报道、2023-11-15 归档标题输入；cutoff 为 `2023-11-15T21:52:18Z`。法院材料中的说法始终标作诉讼指称。

[走势证据 v11](../../data/evaluation/final-benchmark/labels/trajectory-evidence-v11.json)保留同查询、D0 排除窗口的 HN Algolia 与 PullPush 聚合计数及响应哈希。HN 只有基线 1 条相关故事、观察 1 条故事及 1 条相关评论；Reddit 提交为 4→5 条、评论为 0→9 条，观察窗 5 条帖子中仅 2 条通过标题关键词筛查，评论未逐条确认。按当前内部暂定口径，覆盖和事件相关性均不足，方向保持 `unknown`；没有将零结果外推成平台无讨论。

[v11 E 交接](../../data/evaluation/final-benchmark/manifests/e-handoff-v11.json)曾放行 v11 输入，E 确认只生成固定离线工程基线、未调用模型或网络；此输出绑定 v11 原哈希，不用于 v12 或正式评分。v11 输入中的 STAT 当前页面正文细节未具备固定历史版本，因此后续执行以 v12 为准。

[真人盲态任务 v11](../../data/evaluation/final-benchmark/labels/human-annotation-worklist-v11.md)有 1 个原始标题情感单位，由两人独立标注；当前 0/2，缺失 2/2。走势方向任务 0 个，因为证据不足。主 Agent 待核定的窗口、有效日、样本量及 unknown 口径见[走势评分建议 v11](../../data/evaluation/final-benchmark/manifests/trajectory-scoring-proposal-v11.md)。

| v11 流转项 | 数量 |
| --- | ---: |
| 候选档案筛查 | 3 |
| 已有事件身份 | 3 |
| 新增独立事件身份 | 0 |
| 补齐 cutoff 输入并交 E 试点 | 1 |
| 有两个来源族返回部分材料 | 1 |
| 通过当前内部走势门槛 | 0 |
| 本轮真人情感标签 | 0/2 |
| 新增正式可评分事件 / 正式留出总数 | **0 / 0** |
| 距中间 10 件 / 目标 20 件 | **10 / 20** |

## v12 cutoff 输入修订

[v12 输入](../../data/evaluation/final-benchmark/inputs/qualified-inputs-v12.json)只保留归档截止标题和 2023-11-14 固定 Doc. 1 诉状的脱敏摘要；删除 STAT 当前正文细节及任何评分侧证据指针，来源 URL 明确仅作溯源，不允许预测端访问。SHA-256：`d881f93faf83ddcc99381fa036659ed99ca792ab75df6c645aea87ea590d580c`。

[v12 E 交接](../../data/evaluation/final-benchmark/manifests/e-handoff-v12.json)已发送。E 已用隔离 Python `-I` 对 1 件 `holdout_candidate` 完成固定离线工程基线；输入哈希、配置/代码/输出哈希通过，模型调用、网络请求、来源 URL 访问和标签读取均为 0。该结果不是模型预测；[E 的 v12 封存记录](../../artifacts/final-integration-review/v12-pilot/seal-v12.json)保留执行模式与哈希。修订未新增事件，也未改变走势证据或正式分母：本轮新增正式可评分事件 0，总数 0；距 10 件中间里程碑 10 件，距目标 20 件 20 件。

## v13 cutoff 输入与候选事件证据

[v13 cutoff-only 输入](../../data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json)新增 1 件候选：Windows Recall 隐私/安全争议。固定输入锚点为 Axios 2024-06-07 文章的 Internet Archive 捕获 `20240607190027`，捕获时间 `2024-06-07T19:00:27Z`；文章发布元数据为 `2024-06-07T16:47:57.768825Z`，cutoff 按项目 30 分钟规则为 `2024-06-07T19:30:27Z`。捕获 CDX digest 为 `Q5QVW7OJXYZNQHDA5KAQPU2MNC2445KQ`，归档响应 SHA-256 为 `c47df87faeacbb8fa9896736bd1f953cd6927add5011853285abbbbd201b051c`。输入只包含截止时已公开的 opt-in、生物识别验证和截图数据库加密信息；后续走势未进入输入。输入 SHA-256：`01193167bc59ec4e6d59c12922ad7a9e09d9886deb520c255a5e963aaa9b772c`。

[走势证据 v13](../../data/evaluation/final-benchmark/labels/trajectory-evidence-v13.json)固定 HN Algolia 同一 `Windows Recall` 查询、D0 排除窗口：基线 2024-05-31 至 2024-06-06，观察 2024-06-08 至 2024-06-14。逐日接口均成功返回；人工标题相关性筛查后，HN 故事标题为 **20→9**，命中同一事件标题下且评论本身包含查询词的评论为 **30→1**。观察窗 9 条相关故事中有 **6 条集中在 6 月 14 日**，与整体下降方向形成末日反向峰值。帖子数、评论数分别记录，没有合并。

Reddit 仅找到 2 条基线例帖和 3 条观察期例帖，不能代表全量查询；PullPush 先前整窗结果触及 100 条上限，Arctic Shift 多次返回超时，均不用于计数。AP/Axios 的 6 月 14 日报道作为后续传播代理和时间证据，不当作情绪真值。由于目前只有一个可比讨论来源、Reddit 没有完整同口径序列、观察期末日有突增，走势为 `unknown`；未派发猜测式走势标注。

该事件属于跨行业消费科技争议。正式命题面向保险舆情监测，是否可纳入保险项目 10—20 件历史事件分母待主 Agent 确认；因此目前按 `holdout_candidate`，不计正式事件。其他候选复核及排除理由见[候选筛查 v13](../../data/evaluation/final-benchmark/manifests/candidate-screen-v13.json)。本轮没有新增正式可评分事件，10 件中间目标仍差 10，20 件目标仍差 20。

[评分口径建议 v13](../../data/evaluation/final-benchmark/manifests/trajectory-scoring-proposal-v13.md)把命题要求、已确认项目隔离规则和内部建议分开，等待主 Agent 冻结：尤其是 D0/窗口、跨行业样本范围、零基线如何处理、新闻代理是否可作第二来源、有效日/事件单位最低数和 stable/unknown 的分母规则。该建议不是主办方硬门槛。

[真人盲态清单 v13](../../data/evaluation/final-benchmark/labels/human-annotation-worklist-v13.md)合并 v10/v11 尚待真人完成的 6 个情感单位，并新增 1 个 Recall 标题单位：共 **7 个单位、14 个标注槽，已收到 0、缺失 14**。不含走势任务；没有 Agent 标签或预测暴露。

[v13 E 交接](../../data/evaluation/final-benchmark/manifests/e-handoff-v13.json)已发送至现有 Agent E 任务。E 只允许读取 v13 cutoff 输入；不得读取 labels 或打开来源 URL。当前等待隔离试点及哈希封存。该输入是工程候选，不等于真实模型预测，也不进入正式分母。

| v13 交付项 | SHA-256 |
| --- | --- |
| cutoff 输入 | `01193167bc59ec4e6d59c12922ad7a9e09d9886deb520c255a5e963aaa9b772c` |
| 走势证据 | `d0dd402d2506d1b23d18de7daf6ab2a302e7ef1ca14e64861a8c70534feee536` |
| 评分口径建议 | `1cf1be9a9dbe36fdd670c62547073a8ea284cb5af4c6bddcffa601044d7f62f2` |
| 真人盲态清单 | `94820681ab7745d20118f892e22a9baa1eefe4a34bddf4f11552c29ceed2bf33` |
| E 交接 | `cc5a4d46d088dcb5bf0c46c9ab9bfcfe65bd34386dcc31d769c513c88db977b9` |
| v13 冻结清单（清单不自哈希） | `126f8ca57fddd498efcb4269ae5909972e8e485867afe1bf671dd8eb9982d3db` |

v13 冻结清单逐项固定 7 个新增文件的字节数和 SHA-256；v1—v12 原件不改。来源响应哈希只固定本轮取回字节，不证明平台归档完整性。对 PullPush 的 429 本轮曾在首个限流响应后由逐日循环继续发出请求，但没有得到可用数据；此 endpoint 已停止调用，不能将失败解释为零讨论。

## v14 既有候选复筛与 Erie 替代线索

v14 按主 Agent 核定补充口径复核 v4 的 7 件保险留出事件身份，并补查 1 条 Erie Insurance 替代线索。没有新增正式事件身份或 cutoff-only 预测输入。方法固定 D0 排除、基线 D−7…D−1、观察 D+1…D+7；要求两个独立可比公众讨论来源族，每个来源和窗口至少 6/7 个有效日、至少 5 个去重主单位。主 Agent 核定记录见[走势口径核定补充 v13](../../data/evaluation/final-benchmark/manifests/trajectory-scoring-review-v13.md)。

[来源筛查 v14](../../data/evaluation/final-benchmark/manifests/independent-source-screen-v14.json)对 7 件既有候选用固定 HN Algolia 关键词、7 日 UTC 范围、`tags=story` 和标题/链接去重。该轮只取了整窗结果，没有逐日响应；因此没有把任何候选记为 6/7 有效日。HN 作为单一讨论来源也不满足双来源门槛。

| 既有事件 | HN 相关主题帖：基线→观察 | 复筛结论 |
| --- | ---: | --- |
| FNF 网络事件 | 1→0 | 单条基线、观察无匹配；不能据此判平息 |
| Aflac 网络事件 | 0→3 | 基线为 0，观察不足 5；原查询还匹配到 1 个 FLAC 音乐 false match，标题筛除 |
| Allianz Life 泄露 | 3→0 | 观察无匹配；不能据此判平息 |
| Anthem anesthesia 争议 | 0→7（8 次提交，其中 1 个 AP 链接重复） | 零基线；观察报道与撤回同窗，且有 UHC CEO 遇害混杂 |
| Cigna PxDx 诉讼 | 0→2 | 零基线，观察不足 5 |
| UnitedHealth/Lokken 诉讼 | 1→1 | 单一来源、单位不足 |
| Humana 算法诉讼 | 0→0 | 精确查询返回的命中都与该事件无关；不推断没有公众讨论 |

**Erie 替代线索：** SEC EDGAR 2025-06-11 Form 8-K（accession `0000922621-25-000023`）固定了事件内容和接受时间 `2025-06-11 16:26:09Z`；若将此版本作 cutoff 候选，按项目 +30 分钟得 `2025-06-11T16:56:09Z`。该时间不是该 outage 首次公开报道时间，且本轮未把线索整理成预测输入。HN 在基线/观察窗只有 **0→1** 个去重链接（原始 0→2，两个帖子指向同一篇 Insurance Journal 报道），未逐日采集。Early Retirement 页面是 6 月 14 日开始的一条论坛串，当前页面多时点回复只是单串样本；Reddit 有日期示例但没有完整可比序列；Bogleheads 公开搜索可见 6 月 23 日新串，直接页面返回 HTTP 402 后未重试。该事件保留作替代线索，走势 `unknown`，尚未满足双源和覆盖要求，也未计正式事件。逐项来源和哈希见 v14 筛查 JSON。

结果只能解释为本轮 HN 查询匹配数，不代表平台总体讨论。Bluesky 官方公开检索端点的一次未认证查询返回 HTTP 403；已停止，无重试、无替代入口、无数据计数。既有 Reddit 历史查询的封顶、限流及超时仍按 v13 记录处理。

本轮共审查 8 份事件档案（v4 既有 7 件 + Erie 替代线索 1 件）：已有独立保险留出身份 7 → 既有 cutoff 时间见证 7 → 既有 cutoff 内容足以辨识议题 7；Erie 的固定 SEC 输入版本 1 件但首次公开可见时间资格待核。双源走势合格事件 **0** → 真人方向标签完成 **0** → 新增正式可评分事件 **0**。正式留出总数仍为 **0**，距 10 件中间节点 10 件，距 20 件目标 20 件。已有 E 的 v13 输出是 Windows Recall 候选的离线常量工程基线，非模型预测，也不进入保险正式分母；本轮无新合格 cutoff-only 输入，故没有新建 E 交接。

[真人标注优先清单 v14](../../data/evaluation/final-benchmark/labels/human-annotation-queue-v14.md)从已有 v13 待办中选取 5 个原始标题单位、10 个独立标注槽；0 已收到、10 缺失，不新增样本数。分派前需主 Agent 解决 v6 包允许标签与定义字段不一致：允许值是 `positive / neutral / negative / uncertain`，定义字段另含 `mixed / unclear` 且未定义 `uncertain`。未收到人工答案前不生成标签。

v14 冻结清单固定来源筛查、候选状态和真人任务清单共 3 个文件，未修改 v1—v13 原件。v14 freeze SHA-256：`c6f50253cd0cf6418202dca4bfca0b99d45218c24681e45a15462f5b8e58552b`。它冻结的是本轮来源筛查事实与缺失状态，不是合格走势证据或正式评测集。

## v15 事件身份关系与替代线索逐日筛查

[v15身份与来源补筛](../../data/evaluation/final-benchmark/manifests/identity-and-louisiana-screen-v15.json)增加一条路易斯安那 BCBS–Elevance 交易线索的固定查询筛查，并核实 2025 年六月保险公司网络事件之间的归因关系风险。v15没有增加 cutoff 输入、走势真值、人工标签或 E 交接。

路易斯安那交易争议沿用 v5 的事件身份和候选 cutoff `2024-02-06T23:08:11Z`，不拆分听证、帖子、报道或后续暂停。HN Algolia 固定查询 `Louisiana Blue Cross`、`tags=story`，分别对基线 2024-01-30…02-05 和观察 2024-02-07…02-13 进行 14 次单日请求；14/14 返回 HTTP 200，但每一天均为 0 个匹配故事。该结果只说明该索引、精确词项与查询范围没有命中，不能说明没有公众讨论；索引穷尽性也未获证明。Reddit 官方公开搜索端点一次请求返回 HTTP 403，已停止，不重试、不换入口、不将其计作零讨论。v5 已存的医疗协会、地方报道、论坛和后续新闻是有日期的例证，但不是固定查询、穷尽样本或可比计数。故该线索不进入正式走势采集，保持未接纳，不据此标注平息。

身份复核发现，公开行业报道把 Erie、Philadelphia 与 Aflac 的六月 2025 网络事件描述为可能同属保险业攻击活动：[Insurance Journal 6月23日](https://www.insurancejournal.com/news/national/2025/06/23/828749.htm)；[Insurance Journal 6月24日](https://www.insurancejournal.com/news/east/2025/06/24/828919.htm)。报道转述的 Scattered Spider 归因是概率性威胁情报，并非公司确认或同一事件的证明。C 暂时保留各受害公司事件身份，但在独立性上标记待主 Agent 核定；在主 Agent 冻结“按受害事件”还是“按攻击活动簇”计独立样本之前，不把 Erie 与 Aflac 作为两个独立正式事件计数。Allianz Life 的 2025-07-16 第三方云系统事件依据 [AP 7月26日报道](https://apnews.com/article/12b991a141c24d3a060642c0d173e0be)单独保留；已核查来源没有建立其与六月攻击活动的关系，也不推断攻击者身份。

| v15 流转项 | 数量/状态 |
| --- | ---: |
| 本轮新筛替代线索 | 1 |
| 新接纳独立事件身份 / 合格 cutoff 输入 | 0 / 0 |
| 新增双源合格走势事件 / 走势真人标签 | 0 / 0 |
| 新增正式可评分事件 / 正式留出总数 | **0 / 0** |
| 距今晚 10 件节点 / 20 件目标 | **10 / 20** |
| 优先情感标注任务 | 5 个标题单位；10 槽待回收 |

v15沿用主 Agent 已核定的 v13 内部口径：D0 排除，D−7…D−1 对 D+1…D+7；两个独立可比公众讨论来源族；每来源每窗至少 6/7 有效日及至少 5 个去重事件相关主单位；零基线、缺失、冲突及混杂均为 `unknown`，新闻只作补充。该口径不是主办方已确认要求。尚需主 Agent 明确六月保险网络事件的独立事件计数单位及最终验收数量口径。

没有新合格 cutoff-only 输入，所以本轮没有向 E 发起新预测交接；最新 E 输出仍为 v13 Windows Recall 候选的离线常量工程基线，不是模型预测。v14 真人盲标任务清单保留 5 个优先标题单位、10 个缺失槽；v6 标签映射中的 `uncertain` 未定义，而 `mixed / unclear` 未列入允许标签。v15 向主 Agent 提议使用已定义的 `positive / negative / neutral / mixed / unclear` 五类，保留 mixed、unclear 并删除未定义的 uncertain；该映射仍待主 Agent 冻结，未分派、未代填。任务单位 ID 列在 v15 状态清单中。

v15冻结清单固定本轮身份/来源筛查与状态文件，保留 v1—v14 所有原件。v15 freeze SHA-256：`12fed14c1680f2b3308f3ae17f1dccbe48e4de362028ee0d60b7f7aee086b9f7`。本清单固定筛查证据与缺失状态，不代表新增正式事件或评测达标。

## v16 真人情感标注首轮回收

用户以标注者代号 `leos` 提交了 v14 优先清单 5 个冻结标题的第一轮判断，提交时间均为北京时间 `2026-10-08 22:09:36`。逐项原文、理由、信心、来源版本及预测/他人答案暴露声明保存在[真人标注提交 v16](../../data/evaluation/final-benchmark/labels/human-sentiment-submissions-v16.json)。提交标签只有 `neutral` 与 `negative`，都属于 v6 原 `allowed_labels`；未改写、未合并或推断标签。该批 SHA-256 为 `5a083a6f545bfc6c75749e97db5b17a1362a8c32f22ee4a5825180bf1ac79060`。

当前 5 个单位各收到 1/2 份真人答案；**第二标注者仍缺 5 槽，双人完成单位 0，裁决 0**。第二标注者必须独立盲标，不得查看此 v16 答案。`mixed / unclear / uncertain` 的标签映射问题仍需主 Agent在后续涉及这些标签前冻结。本次单人情感标注不产生走势真值，不改变正式可评分事件数：新增正式可评分事件 0，总数 0；距 10 件中间节点 10，距 20 件目标 20。情感标签须与 E 预测上下文隔离，预测与输入哈希封存前不交评分端。

[状态清单 v16](../../data/evaluation/final-benchmark/manifests/candidate-status-v16.json)和[v16 冻结清单](../../data/evaluation/final-benchmark/manifests/freeze-v16.json)记录新增回收状态。v16 freeze SHA-256：`e1ed3254d4682bef8e45de0234b945128b94b1f663144d017b2c79269d82f791`。v16 只追加真人原答与状态；v1—v15 原件均未改。

## v17 原始 20 条输入记录标注补交

用户追加提交剩余 15 个 v4 记录 ID 的单人标签。原答案、理由、置信度、时间、暴露声明和用户报告的材料版本保存在[真人标注提交 v17](../../data/evaluation/final-benchmark/labels/human-sentiment-submissions-v17.json)。与 v16 合并后，`leos` 对 v4 的 20 条记录均提交了一次标签；这不是 20 件独立事件的人工真值。

逐项文本范围核验为：5 条 v6 精确冻结原始标题各有 1 份标注，另一位盲态标注者还缺 5 槽；12 条个人投诉使用脱敏标题摘要，不能替代原帖情感；另外两条留出事件使用“可见标题摘要”，当前未核实其与精确归档标题相同。另有 1 条 UNH 开发集记录，属于此前已暴露开发项，不进入留出标签计数。15 条补交中除开发项外的 14 条留出记录先保留为摘要评价/待核文本，不能冒充原始标题真值。所有原答均保留，不做覆盖或推断；后续须先核实冻结文本，决定补充原文后重标或仅作辅助标签。

当前全体输入记录的人类提交数为 20（1 位标注者、每记录 1 次）；其中满足 v6 精确标题单位的提交为 5，双人完成单位仍为 0，裁决为 0。情感摘要标签不补充走势证据，也不改变正式可评分事件数：新增 0，总数 0；距 10 件中间节点 10，距 20 件目标 20。不得将 v17 文件暴露给 E 或第二标注者；第二位标注者只能在冻结文本核验后独立作答。

[状态清单 v17](../../data/evaluation/final-benchmark/manifests/candidate-status-v17.json)和[v17 冻结清单](../../data/evaluation/final-benchmark/manifests/freeze-v17.json)固定本轮 15 条补交及范围判断。v17 标签文件 SHA-256：`d283723f075f0a362176b1aa1e993902474e8c6b6257b71f68cbde74061859e7`；v17 freeze SHA-256：`bb21a00dab0f380ec7b2edea819f5078d6d823d5f1aab067fba80f1ad690fd70`。v17 追加文件，v1—v16 原件未改。

## v18 五个标题单位第二轮盲标回收

用户提交标注者 `Dick` 对 v6 的五个冻结原始标题单位完成第二轮标注，提交时间均为北京时间 `2026-10-08 22:29:45`。逐项答案、理由、信心和暴露声明保存在[真人标注提交 v18](../../data/evaluation/final-benchmark/labels/human-sentiment-submissions-v18.json)。提交者声明未看预测、未看第一位答案；该盲态信息是自报，未独立审计。来源版本从对应 v6 冻结标注包继承，因为本次消息未重复填写来源版本，并在 v18 文件逐项注明。

五个单位与 v16 第一轮标签全部一致：FNF、Cigna、Humana 为 `neutral`；Anthem、Aflac 为 `negative`。因此 5/5 个情感单位现各有两份标注，标签一致 5、分歧 0、待裁决 0。这里的“双人完成”仅指五个冻结标题情感单位，不代表五件走势事件已可评分。v6 标签定义仍有 `uncertain` 未定义、`mixed/unclear` 不在允许列表的问题；本批标签均为 v6 允许值中的 `neutral/negative`。

原有 14 条摘要范围未核记录仍待原文核验；UNH 开发记录仍排除于留出集。走势证据和走势真人标签仍为 0，新增正式可评分事件 0、正式留出总数 0。v18不更改输入、未来证据或 E 预测交接，也不向 E 暴露情感标签。

[v18 状态清单](../../data/evaluation/final-benchmark/manifests/candidate-status-v18.json)和[v18 冻结清单](../../data/evaluation/final-benchmark/manifests/freeze-v18.json)追加固定本轮材料；v18 标签文件 SHA-256：`5f96fb51c90a007698ae4e6424987078d5075fddc8c7cb6d610f8c225739dd20`，冻结清单 SHA-256：`8a37151856f8e160e51bb0097db8ed5c0730879a1c37221cd2b33bf46703e9cf`。v1—v17 原件未改。

## v19 原始标题哈希核验与盲态复标包

用户提供 v4 留出记录中的 14 个 Reddit 原始标题及帖子链接，来源版本均报为 `arctic-shift-title-snapshot-v1`。C 对每条用户提供的 UTF-8 标题逐字计算 SHA-256，并与本地[归档证据 v1](../../data/evaluation/final-benchmark/manifests/archive-post-evidence-v1.json)的 `title_sha256` 对照：**14/14 匹配，0 条不匹配**。`complaint_auto_claim_denial_20241116` 标题字段末尾的 LF 纳入哈希后匹配；不能去掉换行再视作同一字节串。公开时间、归档可见时间和 cutoff 从对应归档记录及 v4 输入取得，逐条见[原始标题材料 v19](../../data/evaluation/final-benchmark/labels/verified-original-titles-v19.json)。

与旧 v4 标注文本逐条比较，只有 UnitedHealth 算法诉讼标题与归档原题完全一致；其旧 neutral 提交按一份首标保留，记录中没有该次提交单独的文本哈希。其余 **13 条**（Allianz 一条及个人投诉标题 12 条）与旧摘要文本不同，相关旧答案保留为摘要辅助标注，不能算作原始标题标签。12 条个人投诉仍然只是帖子级材料，不增加独立事件数。

[原题盲态复标包 v19](../../data/evaluation/final-benchmark/labels/blind-sentiment-reannotation-packet-v19.json)包含 14 个确切标题，不含旧标签、理由或模型预测：13 个摘要不匹配单位需两份新独立标注，UnitedHealth 需一份独立第二标注，共 **27 个待填槽位**。只分配给未看过相应旧摘要答案和预测的真人标注者。包内沿用 v6 的标签列表和定义；`uncertain` 未定义、`mixed/unclear` 不在允许列表的冲突仍需主 Agent 核定，因此该包是已备妥的隔离材料，尚非正式评分批准。

本轮没有新增截止前模型输入、后续走势证据或走势标签；正式可评分事件仍为 0。原始标题标签材料不交给 E。v19 状态见[状态清单](../../data/evaluation/final-benchmark/manifests/candidate-status-v19.json)，冻结清单见[v19 冻结清单](../../data/evaluation/final-benchmark/manifests/freeze-v19.json)。原始标题材料 SHA-256：`54a08c3543f29bd808e74b1b1998286771a26626caf9c4f68dd7f416fe0235e2`；盲态复标包 SHA-256：`0f92c86092187ba936a48881eb98d8592e38a7bdcadb39a547c1f1d85697f565`；状态清单 SHA-256：`065e12814f20028d5aeb6c06c5280873da32fa489f5f125934ce84744ec929bd`；v19 冻结 SHA-256：`7eeab73a8b72abccb54a6f10b7ddd76d529c6a5f66aa900a1477e9b9a5d29854`。v1—v18 原件未改。

## v20 截止前复核与独立 Agent 建议

两位未获得历史对话、既有标签或预测内容的子 Agent，分别只依据 v19 中 14 条已核验哈希的原始标题给出情感建议。建议材料按 unit ID 关联标题哈希，保存在[Agent 情感建议 v20](../../data/evaluation/final-benchmark/labels/agent-sentiment-suggestions-v20.json)：两份建议有 11 条相同、3 条不同，分歧为 `complaint_home_nonrenewal_20241229`、`complaint_home_nonrenewal_20231018`、`complaint_home_nonrenewal_20231010`。这只是两个 AI Agent 的结果，不是人工双标、人工裁决或 gold truth，不增加人工标签数；不得交给 E。14 条中 UHC 仍只有 1 份可暂留的人类首标、0 条双标，另 13 条无已核验的精确原题人工答案，仍缺 27 个独立人工标注槽位。此前已双标的 FNF、Anthem、Cigna、Humana、Aflac 是另外 5 个标题单位，不属于这 14 条。

本轮只读审查了既有候选、替代线索、来源筛查、走势证据和 v2 走势口径，没有发现可新增正式评分的事件。v4 的 20 条记录对应 8 个事件身份（1 个开发、7 个留出）；12 条个人投诉是辅助帖子。正式合格走势证据、独立真人走势标签、正式可评分留出事件仍分别为 **0、0、0**；距 20 件目标差 20。State Farm 加州新业务暂停是现有保险线索中最值得继续核查的一件，但 Reddit 16→157 讨论量骨架之外，HN 基线无相关单元、观察窗未逐条判相关且缺逐日覆盖；尚缺两来源、两窗口可比序列和真人走势标签。Windows Recall 的 HN 序列较完整，但跨行业且缺第二个可比来源；Anthem 零基线并有观察期混杂；Erie 和 Louisiana 线索也未过证据要求。相应依据见 `candidate-events-v4.json`、`replacement-leads-v4/v5.json`、`candidate-screen-v13.json`、`trajectory-evidence-v13.json`、`independent-source-screen-v14.json`、`identity-and-louisiana-screen-v15.json` 和 `final-scoring-protocol-v2.md`。方向缺证、零基线、来源冲突或窗口覆盖不足继续记 `unknown`，不推断为平息。

v20 状态见[状态清单](../../data/evaluation/final-benchmark/manifests/candidate-status-v20.json)，冻结清单见[v20 冻结清单](../../data/evaluation/final-benchmark/manifests/freeze-v20.json)。新增建议材料 SHA-256：`137f31c620ecf462285fe41f8de2abbb6fd8d0ca98761312572239ca3b380c81`；v20 状态清单 SHA-256：`aedbbc4c073ece890fe9bff6e0a27516fab73da4cf5c01ab94cb22a40ad30551`；v20 冻结 SHA-256：`f9da39749de815f192eb98a25aab898314bb22a7858ed76000d02fcc8b1dd155`。v1—v19 文件均保留。v19 状态和规划材料记为 10 月 9 日 12:00 截止；用户本轮明确要求北京时间 10 月 9 日 00:00 前完成，主 Agent 需核对项目总览等文件中的时限不一致。本轮没有新增合格 cutoff-only 输入，因此没有交接 E，也没有新增正式预测。

## v21 State Farm 补充讨论源

公开核查发现[Insurance Forums 讨论串第 2 页](https://www.insurance-forums.com/community/threads/state-farm-halts-home-insurance-in-california-wsj.110855/page-2)与[第 3 页](https://www.insurance-forums.com/community/threads/state-farm-halts-home-insurance-in-california-wsj.110855/page-3)是 Reddit、HN 之外的一条后续讨论来源。该串显示始于 2023-05-26；已读页面的回复 #11—#30 共 20 条，显示日期为 5 月 29 日至 6 月 2 日。页面内容含 State Farm 暂停新业务讨论，也有一般承保、保费及加州保险市场内容，因此 20 条只表示显示的论坛回复数，**未**按事件相关性编码，不能作为 20 个相关主单位或趋势指标。该论坛面向保险代理人与经纪人，不能代表普通公众。仅页面摘要和日期被脱敏记录；不保留发言用户名或个人资料。

按材料整理用的 D0 排除七日窗口草案，若以 5 月 26 日为 D0，观测窗为 5 月 27 日至 6 月 2 日；已访问片段只覆盖其中 5 个日期，缺 5 月 27—28 日。对保险论坛做了三条定向公开搜索，未找到可固定的同口径 5 月 19—25 日基线序列；搜索未命中不证明讨论为零。讨论串开始日也不能证明基线无讨论。论坛显示日期的时区未核实。页面 1、4、5 返回 HTTP 403 后停止访问，未登录或绕过限制。[走势证据 v9](../../data/evaluation/final-benchmark/labels/trajectory-evidence-v9.json)保存了脱敏页码、日期计数、访问结果和排除理由。结论是 **unknown**：新增一个部分后续来源，但它不能补足可比基线、6/7 日覆盖、逐条事件相关性编码或双盲真人走势标签。State Farm 不进入正式分母；本轮新增合格走势事件仍为 0，正式可评分留出事件仍为 0/20。

[状态清单 v21](../../data/evaluation/final-benchmark/manifests/candidate-status-v21.json)与[v21 冻结清单](../../data/evaluation/final-benchmark/manifests/freeze-v21.json)追加保存上述证据。走势证据文件 SHA-256：`1f137256c600aec7ac87689089cafb3ca5d21fdd781626206d1c7ba55ef2a5f3`；v21 状态清单 SHA-256：`f7747623b8021875059196e9a2d43388d33f362a850e8b9b2ea3e89e462a5f5b`；v21 冻结 SHA-256：`d12dfa1ac18f4d745394a641cebc189eb4fc42d0b49261718d2a7d4f6579dd7a`。v1—v20 原件未覆盖。

## v22 Leo 用户裁决与评分口径确认

用户以 Leo 名义确认 14 条原始标题的 AI 情感建议均通过复核，并对三条建议不一致的单位明确选择 `neutral`：`complaint_home_nonrenewal_20241229`、`complaint_home_nonrenewal_20231018`、`complaint_home_nonrenewal_20231010`。逐单位登记见[人工复核 AI 建议 v22](../../data/evaluation/final-benchmark/labels/human-review-ai-suggestions-v22.json)。Leo 已看过两份 AI 建议，因此 v22 记录为**人类复核/分歧裁决**，不冒充两份独立盲态人工标签；三条没有提供理由或信心值。原始 AI 建议 v20 保持不变，仍显示各 Agent 原始答案。

用户同意保留 State Farm 论坛摘录为辅助证据、走势 `unknown` 且不进入正式评分，并确认继续使用冻结的[正式离线评测协议 v2](final-scoring-protocol-v2.md)，不放宽正式评分条件。因此本轮新增正式可评分事件 **0**；合格走势事件仍 **0**；合格独立真人情感双标槽位仍缺 **27**；走势真人标签仍 **0**；正式可评分留出事件仍 **0/20**。现存 7 件留出事件身份和 12 条辅助投诉帖的统计不变。

状态见[候选状态 v22](../../data/evaluation/final-benchmark/manifests/candidate-status-v22.json)，冻结清单见[v22 冻结清单](../../data/evaluation/final-benchmark/manifests/freeze-v22.json)。Leo 复核记录 SHA-256：`83263959ecd45e92efe4654561f168ae673d6dcaae157399f33072180b0e83fc`；v22 状态清单 SHA-256：`2736c5b5496c90abc29d0913b2f1a5979941c37056c46b755dd6c81cd8b8f3ba`；v22 冻结清单 SHA-256：`f850031d0d59a561216cc0b08149c4fc7bf6ae4b383b15416a91ce3340871bd0`。v1—v21 原件未覆盖。

## v24 最终减法核验与口径更正

本次只汇总现有冻结材料，没有新增候选。逐项核对 v6 原始标题盲标包、v16 首标、v18 次标和归档标题证据后，确认 `sent-fnf-1821s44`、`sent-anthem-1h6mj0x`、`sent-cigna-159ce8g`、`sent-humana-ars-20231213`、`sent-aflac-1lg33ey` 共 5 个标题单位均满足：同一冻结文本、同一 SHA-256、两位不同标注者、标签一致、各自申报未看预测或另一标注者答案；对应归档标题哈希 **5/5 匹配**。标签为 neutral 3、negative 2。盲态是标注者申报，未能独立审计其实际信息接触情况。

因此，情感线应计 **5 个已完成人工双标单位**，不是 0。v19 的 14 条原题复标包仍有 **27 个待标槽位**；Leo 对 v20 AI 建议的 v22 复核不属于盲态独立双标。但 v6 仍标注 V2-Q03 分类/统计口径待主 Agent 冻结，所以这 5 个单位暂记为“人工双标已完成”，不提前写成正式情感评分合格分母。情感标签和走势标签分别统计。

这 5 个情感单位目前还不能形成模型情感评分：可见 E 试点输出是 `offline_constant_escalation_baseline_v1`，其中情感预测均为 `null`；对应试点评分报告情感样本量为 0。因此已完成人工情感标签数为 **5**，已计算情感分数单位数为 **0**。

走势线继续按用户确认的 v2 协议：合格后续走势证据事件 **0**，独立人工走势标签 **0**，正式可评分留出事件 **0/20**。现有 7 个留出事件身份中没有一个越过正式走势证据门槛；12 条个人投诉帖仍是辅助材料。身份数量上至少还差 13 个独立身份，正式合格事件数量仍差 20，二者不得混为一谈。State Farm 2023 论坛材料仅作辅助、方向 unknown；不放宽规则。

本次证据与状态冻结：审查表 [最终核对 v24](../../data/evaluation/final-benchmark/manifests/final-reconciliation-v24.json)，状态 [v24](../../data/evaluation/final-benchmark/manifests/candidate-status-v24.json)，冻结 [v24](../../data/evaluation/final-benchmark/manifests/freeze-v24.json)。最终核对 SHA-256：`d8c69cb69300f49657ae01c862202974d050f04038b6537d37a0172c0158364d`；状态 SHA-256：`d601952832bcabc0be6211d4b3d4acbea0e1f74a384ca3ca1250b33bc7d7867c`；冻结清单 SHA-256：`f8e3c3dc812d96827d3ebd2d5ae95c4f4882493f18f07b9a270d16993f7ae246`。v23 冻结 SHA-256：`f9eab15a9cd76887bc5156f4ea2ee6549ca5710baadea6946645e1dbb73be213`。

## 版本与复核

v1—v9 原件与冻结哈希保留。v10 只冻结筛查和交接补充，不改 cutoff 输入。各清单 SHA-256（清单外）：

| 冻结清单 | SHA-256 |
| --- | --- |
| v1 | `f452859c41acee69d1a299c46b41715229fc2433f6993b9a0a5622e4f9d039c3` |
| v2 | `803c12e7acf8578cfafbb0f67eba325723e0a5fb704051f43d9a7fb5d1b6a9aa` |
| v3 | `50a365f063853ad0a2dbc399f939b867286e23c522ce0ddb84a4ef58b27442c6` |
| v4 | `cbc898c10081d9524d9c62bd1e278dc3fc72243fe9339c6a0a34bcda011a5081` |
| v5 | `75b02d7f2d9970c20dadd8cc5214869bd601ba3eecb4c9a4a90b13c2b1094e83` |
| v6 | `5656e8e172e8a844e35877c148b3f6b19b9c570a8c414c982c1226d01b5a2811` |
| v7 | `d533bd1041d0f3ee2f59ebd21434a601e5f9497bed437e9e5859bc34f7069dc6` |
| v8 | `9110a1c397ed94c790a493af6796b9df692615ec022eda67fd577ce717356aa7` |
| v9 | `30d4fa8cdb3806a32130253bf7140e86d2ca5d8978c43486b1152120cbcb3067` |
| v10 | `543d1c1eb43f1edff019212b1ab7a38999c88bcf3401151ac66c0636b0b96397` |
| v11 | `2125d4dbcff67dfcee9888c6ebc2a84a90d2b165642f394f7f76d35182af0694` |
| v12 | `92fc628160297b9bdb3afa847a15ccb80afca62badb4669de8d21e914b82314c` |
| v13 | `126f8ca57fddd498efcb4269ae5909972e8e485867afe1bf671dd8eb9982d3db` |
| v14 | `c6f50253cd0cf6418202dca4bfca0b99d45218c24681e45a15462f5b8e58552b` |
| v15 | `12fed14c1680f2b3308f3ae17f1dccbe48e4de362028ee0d60b7f7aee086b9f7` |
| v16 | `e1ed3254d4682bef8e45de0234b945128b94b1f663144d017b2c79269d82f791` |
| v17 | `bb21a00dab0f380ec7b2edea819f5078d6d823d5f1aab067fba80f1ad690fd70` |
| v18 | `8a37151856f8e160e51bb0097db8ed5c0730879a1c37221cd2b33bf46703e9cf` |
| v19 | `7eeab73a8b72abccb54a6f10b7ddd76d529c6a5f66aa900a1477e9b9a5d29854` |
| v20 | `f9da39749de815f192eb98a25aab898314bb22a7858ed76000d02fcc8b1dd155` |
| v21 | `d12dfa1ac18f4d745394a641cebc189eb4fc42d0b49261718d2a7d4f6579dd7a` |
| v22 | `f850031d0d59a561216cc0b08149c4fc7bf6ae4b383b15416a91ce3340871bd0` |
| v23 | `f9eab15a9cd76887bc5156f4ea2ee6549ca5710baadea6946645e1dbb73be213` |
| v24 | `f8e3c3dc812d96827d3ebd2d5ae95c4f4882493f18f07b9a270d16993f7ae246` |

v10 冻结清单覆盖 4 个补充文件；v11 覆盖 7 个新增输入、来源证据、筛查、交接和标注/口径文件；v12 覆盖 3 个输入修订与 E 交接文件；v13 覆盖 7 个新增输入、来源证据、筛查、交接和口径/标注文件；v14 覆盖本轮 3 个来源筛查、状态和标注任务文件；v15 覆盖 2 个新增身份与来源筛查、状态文件；v16 覆盖 2 个人工标注回收与状态文件；v17 覆盖 2 个追加标注与状态文件；v18 覆盖第二标注提交与状态文件；v19 覆盖原题 hash 核验、隔离复标包与状态清单；v20 覆盖只供复核的 AI 情感建议与截止状态审查；v21 覆盖一条脱敏的部分后续论坛证据及状态清单；v22 覆盖 Leo 对 AI 建议的复核记录及更新状态。各版成员哈希和字节数逐项校验，均不包含本说明文档本身。v1—v21 原件保持不变；哈希只固定本地文件，不能证明来源真实性、完整性或正式达标。

仓库根目录 PowerShell 最短核验（v22）：

```powershell
@'
from pathlib import Path
import hashlib, json
r = Path('.')
b = r / 'data/evaluation/final-benchmark'
freezes = [json.loads((b / f'manifests/freeze-v{v}.json').read_text(encoding='utf-8')) for v in range(1, 23)]
assert all((r / f['path']).stat().st_size == f['bytes'] and hashlib.sha256((r / f['path']).read_bytes()).hexdigest() == f['sha256'] for z in freezes for f in z['files'])
s = json.loads((b / 'manifests/candidate-status-v22.json').read_text(encoding='utf-8'))
a = json.loads((b / 'labels/agent-sentiment-suggestions-v20.json').read_text(encoding='utf-8'))
m = json.loads((b / 'labels/verified-original-titles-v19.json').read_text(encoding='utf-8'))
p = json.loads((b / 'labels/blind-sentiment-reannotation-packet-v19.json').read_text(encoding='utf-8'))
e = json.loads((b / 'labels/trajectory-evidence-v9.json').read_text(encoding='utf-8'))
h = json.loads((b / 'labels/human-review-ai-suggestions-v22.json').read_text(encoding='utf-8'))
assert hashlib.sha256((b / 'manifests/freeze-v21.json').read_bytes()).hexdigest() == s['base_freeze_v21_sha256'] == h['base_freeze_v21_sha256']
assert all(hashlib.sha256(x['original_title'].encode('utf-8')).hexdigest() == x['original_title_utf8_sha256'] == x['archive_title_sha256'] for x in m['records'])
assert len(a['records']) == 14 and a['agreement_summary']['same_suggestion'] == 11 and a['agreement_summary']['different_suggestion'] == 3
assert p['new_independent_annotation_slots_required'] == 27 and s['human_review_decisions_v22']['human_adjudications_of_disagreement_units'] == 3
assert all(x['final_human_review_label'] == 'neutral' for x in h['units'] if x['unit_id'] in s['human_review_decisions_v22']['three_final_labels'])
assert s['trajectory_and_formal_scoring']['formal_score_ready_holdout_total'] == 0
assert e['records'][0]['eligibility']['formal_score_ready_event'] is False and e['records'][0]['provisional_window_assessment']['direction'] == 'unknown'
print('freeze members v1-v22=OK; source titles=14/14; Leo reviewed AI suggestions=14; three disputes=neutral; blind slots still required=27; forum evidence=partial/unknown; formal events=0; freeze v22 SHA-256=', hashlib.sha256((b / 'manifests/freeze-v22.json').read_bytes()).hexdigest())
'@ | python -
```

v19 历史核验命令（仅复核 v19）：

```powershell
python -c "import json,pathlib,hashlib; r=pathlib.Path('.'); b=r/'data/evaluation/final-benchmark'; fs=[json.loads((b/'manifests'/f'freeze-v{v}.json').read_text(encoding='utf-8')) for v in range(1,20)]; ok=[all((r/x['path']).is_file() and (r/x['path']).stat().st_size==x['bytes'] and hashlib.sha256((r/x['path']).read_bytes()).hexdigest()==x['sha256'] for x in f['files']) for f in fs]; s=json.loads((b/'manifests/candidate-status-v19.json').read_text(encoding='utf-8')); base_ok=hashlib.sha256((b/'manifests/freeze-v18.json').read_bytes()).hexdigest()==s['base_freeze_v18_sha256']; m=json.loads((b/'labels/verified-original-titles-v19.json').read_text(encoding='utf-8')); p=json.loads((b/'labels/blind-sentiment-reannotation-packet-v19.json').read_text(encoding='utf-8')); print('freeze_v1-v19_members_ok=',all(ok),'v19_base_ok=',base_ok,'title_hash_matches=',m['source_hash_verification']['matches'],'title_hash_mismatches=',m['source_hash_verification']['mismatches'],'reannotation_slots=',p['new_independent_annotation_slots_required'],'trajectory_labels=',s['trajectory_and_formal_scoring']['independent_human_trajectory_labels_received'],'formal_total=',s['trajectory_and_formal_scoring']['formal_score_ready_holdout_total'],'v19_freeze_sha256=',hashlib.sha256((b/'manifests/freeze-v19.json').read_bytes()).hexdigest()); assert all(ok) and base_ok and m['source_hash_verification']['matches']==14 and p['new_independent_annotation_slots_required']==27"
```

该核验只检查本机冻结成员、输入哈希和状态登记，不证明真人提交、E 预测封存、主 Agent 批准或正式评测达标。`freeze-v10.json` 曾含字面量 `\\n` 导致 JSON 解析失败；已修成合法 JSON 并重算其清单外哈希，v1—v9 文件未改。



v23 冻结仅覆盖新增候选筛查和状态；v24 冻结仅覆盖最终核对与状态更正，不增加事件候选。v24 明细固定五组人类双标、源标题哈希、v19 尚缺槽位及 E 情感预测缺失事实。
