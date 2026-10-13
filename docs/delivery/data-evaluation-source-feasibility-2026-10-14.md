# 历史事件评测来源可行性核查（2026-10-14）

> `data-evaluation` 分支的来源决策材料，状态为提案。只讨论受限公开保险事件、标题/元数据及截止前证据；不批准新平台采集、转载原文或收费数据调用。20 件正式可评分事件、方向 ≥70%、情感及负面指标均未据此验证。

## 现有来源为什么不能直接形成正式留出集

Hacker News Algolia 能在指定时间窗返回带创建时间的故事与评论；当前采集器只在 `exhaustiveNbHits=true` 且数量、分页和时间均可核时把**这一查询**标为穷尽。五件事前登记的保险事件基线中，四件的入选故事加评论分别仅 2、2、2、3 条；State Farm 2023 事件为 245 条，但它的严格标题规则在基线与观察窗漏掉了明显相关的原始故事，已转为开发诊断。详情及哈希见主工作区 `artifacts/branch-work/data-evaluation/handoff-20261014-v15.md`。查询穷尽不等于事件讨论召回穷尽；这些数字也只代表 HN，不代表公司全渠道舆情或投诉量。不能通过事后放宽标题词、降低待批准的 `B≥20` 草案，或把同一争议拆成多个事件来凑 20 件。

## 备选公开索引的当前边界

| 来源 | 官方资料支持的能力 | 本轮可用性与限制 |
| --- | --- | --- |
| [GDELT DOC 2.0](https://blog.gdeltproject.org/doc-2-0-updates-1-5-year-searching-and-updated-mobile-interface/) | 官方说明其历史搜索自 2017 年起，ArticleList 在指定窗口内可取文章列表；[API 说明](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)给出时间边界、最多 250 条列表及 TimelineVolRaw 计数。 | 2026-10-14 对已登记 Medibank 日期的一次窄窗、单词、仅标题/URL 元数据试探返回 HTTP 429；此前同接口亦曾 429。本轮未取得任何可核的 GDELT 事件结果，停止请求，不能把服务能力当作本项目覆盖证据。若将来可用，仍须按短窗核列表截断、历史可见时间、转载去重、事件归属和来源权利。 |
| [GDELT 原始数据](https://gdeltproject.org/data.html) | 官方提供免费开放的新闻事件及 GKG 元数据文件，2.0 更新约每 15 分钟。 | 官方资料称全年原始数据量巨大；本轮未下载。它提供的是新闻索引/元数据，不自动给出舆情评论或原站全文使用权，不能把文章数直接解释为用户情绪。 |
| [Media Cloud 新闻档案](https://www.mediacloud.org/documentation/search-api-guide) | 官方称历史在线新闻档案可检索。 | [官方示例](https://github.com/mediacloud/api-tutorial-notebooks)要求注册账号和 API key；[使用条款 §2.1、§2.3](https://www.mediacloud.org/legal/media-cloud-terms-of-use)说明平台主要适合学术、非营利和新闻研究，不为商业品牌研究设计，且不授予第三方报道内容的使用权。本轮未注册、未调用，不把它列为已授权正式来源。 |

补充一次**只读可达性探针**：2026-10-14 对 GDELT 2.0 的 `20230526150000.gkg.csv.zip` 发 `HEAD`，本机返回 HTTP 200、`Content-Length=9644013` 字节；没有下载该文件或新闻正文。这仅证明一个 15 分钟历史切片可达，不能证明 14 天完整性、事件归属、标题可用或许可。按同样大小粗算，单件前后各 7 天需 1344 个切片，约 13 GB 压缩数据；它不是已建立的 20 件评测入口。[GKG 2.1 官方字段说明](https://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf)还指出每日 GKG 1.0 兼容流会将计算元数据相同的文章聚类，不能用其较小的每日文件直接代替逐文章数量。该探针也不改变上表的 GDELT DOC API 429 结果。

## 未使用标题线索的首轮身份核查

此前只按标题、未看观察结果选出的 27 条线索中，4 条与本轮五件开发事件同一身份，已从候选池剔除；其余 **23 条只是待核标题线索**。主工作区忽略证据 `artifacts/branch-work/data-evaluation/evidence/20261014/unused-title-lead-reserve-v1.json` 固定了原始标题、ID 和 4 条排除原因，`reserve-anchor-screen-v1.json`、`reserve-anchor-screen-v2.json` 对全部 23 条记录了初步来源与身份状态；**正式准入仍为 0**。该首轮核查只查公开来源和日期，没有获取观察窗或预测；其后的五件基线试探和一件辅助预测单列如下。

几个会改变截止日或事件数的例子： [Farmers 官方公告](https://newsroom.farmers.com/news-releases?item=123225)把裁员公告定在 2023-08-28，晚于公告的论坛标题不能当首次披露；[加州保险厅](https://www.insurance.ca.gov/0400-news/0102-alerts/2026/Major-California-home-insurers-move-to-r.cfm)说明 Allstate 的加州新业务暂停始于 2022 年，不能把 2023 年 6 月的报道再算新停办事件；[密歇根蓝十字原站 PDF](https://providerinfo.bcbsm.com/documents/alerts/2024/202406/alert-20240606-changes-weight-loss-drugs-commercial.pdf)虽标初发 2024-06-06，也明示 2024-07-30 更新，当前版本不能直接当六月 cutoff 输入；[法国 CNIL](https://www.cnil.fr/fr/violation-de-donnees-de-deux-operateurs-de-tiers-payant-la-cnil-ouvre-une-enquete-et-rappelle-aux)所述受攻击主体是两家医疗费用第三方支付管理服务商，并非两家健康险公司。这些是身份和时点核查，不是按模型预测好坏筛事件。保留原标题、后续纠正、未知历史可见时间和不能入样原因，避免用模糊题材凑够 20 件。

## 五件预先固定查询的基线试探

在接触这五件的基线计数前，将事件身份、披露日、`披露日+7 天 00:00 UTC` 的 cutoff 和 HN 故事查询写入主工作区 `reserve-baseline-query-plan-v1.json` 并核 SHA-256。只取截止前七个完整 UTC 日的公开故事标题、URL、发布时间和 ID，再逐故事取同窗评论的 ID/时间；每个请求须通过 `exhaustiveNbHits`、页数和返回条数核对。结果如下，仍只是**各查询的原始命中**，不是事件归属后的正式基线：

| 候选事件 | 原始故事 | 这些故事的同窗评论 | 查询内总量 |
| --- | ---: | ---: | ---: |
| Farmers 2023 裁员公告 | 1 | 230 | 231 |
| Simple Health FTC 判决 | 0 | 0 | 0 |
| BCBS 提供者反垄断和解公告 | 1 | 1 | 2 |
| Allstate / National General 隐私诉讼 | 2 | 1 | 3 |
| Universal Property 佛州和解 | 0 | 0 | 0 |

Farmers 是这五件中唯一超过未批准的 `B≥20` 草案下限的查询。两名互不见答案和预测的 AI 只看基线标题/URL，均将对应裁员故事纳入；更宽的 `Farmers` 查询另返回两条农业话题，两人均排除。由此形成的**查询内 AI 辅助**基线为 1 帖+230 评论。冻结该输入哈希后，一次获授权的 qwen-flash 单模型辅助预测给出 `escalation`；随后才抓同口径观察窗，冻结查询返回 0 帖，旧帖新增 0 评论，更宽查询的三条农业/制造业标题也被两名盲审 AI 一致排除，辅助方向为 `calming`。这是一件 **0/1 的单模型辅助配对**，不进入正式仿真方向分母。模型理由还把 8 月 30 日的基线峰值误称为 9 月 4 日 cutoff 之后，原预测保留，不重试或事后排除。

逐请求覆盖、ID、哈希、AI 归属、冻结预测和费用见主工作区 `reserve-baseline-stories-v1.json`、`reserve-baseline-comments-v1.json`、`farmers-baseline-recall-audit-v1.json`、`farmers-aux-input-freeze-v1.json`、`farmers-aux-prediction-v1.json`、`farmers-aux-observation-v1.json`、`farmers-aux-label-score-v1.json` 及 `model-cost-ledger-v2.json`。扩词检查只能发现已知漏召回，不能证明 HN 对全事件的召回，更不能把新闻讨论量写成全渠道投诉。当前索引不是历史时点快照，Farmers 官网当前页面也未证明历史版本；**正式新增仍为 0/20**。

## 正式来源进入事件池的最低证据

一个新来源必须先确定使用范围及历史版本，再在**新事件**读取观察窗之前冻结：具名披露和事件身份、来源家族、检索式与别名、cutoff、等长窗口、分页或计数饱和证明、所有原始命中 ID/URL/时间及查询日志。两个相互独立且盲于预测的真人要对原始命中做事件归属；模型输入只取截止前已核条目并冻结哈希。后续观察用同一检索和归属口径，另由两名真人独立判断走势；`stable`、`cannot_judge` 和覆盖失败分别列排除。新闻报道量、论坛讨论量和投诉样本是不同总体，不合并成未定义的“全网投诉量”。

目前可继续做的是 HN 规则缺陷诊断、正式预测导入工程和 AI 辅助审核；正式 20 件事件需要一个能在本机合法复核、可证明历史覆盖的公开讨论/新闻档案或已有授权数据集，以及项目契约规定的独立真值。来源变更的权限与费用边界由总 Agent 按项目协作规则协调；本文件不把不可用的外部接口或 AI 标签写成正式达标。
