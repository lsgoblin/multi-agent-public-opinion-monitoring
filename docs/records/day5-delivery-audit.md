# Day 5 交付证据核查（当前快照）

> 日期：2026-10-08（Asia/Shanghai）。这是并发开发期间的当前证据快照，不是最终冻结版本，也不代表 G3、G4 或 G5 通过。Day 5 本机证据核查工具已获用户明确授权；本记录仅核查本地文件和 Git 元数据，不运行模型、采集、通知、部署或 Docker。

## 核查口径

- 当前要求以[新版命题](../sources/2026-09-30-新版命题-多智能体仿真舆情监测.md)及[指标映射表](../design/05-赛题指标映射表.md)为准。G1/G2 的受限通过不代表量化指标达标；G3、G4 仍未通过，G5 由主 Agent 最终复核。
- `已验证`只表示所列证据在该项目标范围内覆盖充分；`部分验证`表示有真实运行、有限样本、替身、静态配置或部分覆盖证据；`未验证`表示没有可用于该项验收的实测证据。状态针对新版命题指标，不等同于内部阶段关口状态。
- `real_model_run`、`offline_dynamic_substitute`、`dry_run`、主机运行、容器运行、应用账本 `network_requests=0` 和操作系统/容器网络隔离分别记录，不互相替代。
- 文档中的“真实历史”只指材料来源模式；Day 4 使用的是 `offline_archived_record_projection` 图投影和离线动态替身，不是本次 GraphRAG 检索或真实模型推演。
- 所列证据路径是本地相对路径。脚本对其生成 SHA256 和存在性结果；哈希标识文件字节，不证明数据内容真实、完整或达到命题指标。

当前机器快照：[snapshot-current.json](../../artifacts/day5-delivery-audit/snapshot-current.json)。脚本为[day5_delivery_audit.py](../../scripts/day5_delivery_audit.py)，其测试用例位于[test_day5_delivery_audit.py](../../tests/test_day5_delivery_audit.py)。

## 新版命题逐项验收

| 编号 | 目标 | 状态 | 实际证据路径 | 数据 / 执行模式与实测范围 | 当前证据结论与缺口 |
| --- | --- | --- | --- | --- | --- |
| K01 | 接入 ≥5 个主流公开平台 | 未验证 | [23-Day2受限G2关口复核.md](23-Day2受限G2关口复核.md)、[Day 2 多来源运行数据](../../data/research/unh_change_20240222_day2_multisource_probe.json)、[指标映射表](../design/05-赛题指标映射表.md) | 一件真实历史事件；SEC 与 Optum 两个来源完成过采集/检索。它们不等于微博、抖音、小红书、黑猫投诉、新闻门户五个平台在线接入。 | 没有五个平台逐源有效运行批次、合格记录和覆盖范围证据；连接器/来源路线不能计为已接入。 |
| K02 | 热点舆情采集延迟 ≤15 分钟 | 未验证 | [23-Day2受限G2关口复核.md](23-Day2受限G2关口复核.md)、[指标映射表](../design/05-赛题指标映射表.md) | Day 2 是受限真实历史来源探测，不是持续在线采集观测；Day 4 任务使用本机已有材料。 | 缺少按来源记录的发布时间、首次可见/发现时间、采集时间、观测窗口、延迟分布和失败统计。 |
| K03 | 负面舆情识别准确率 ≥90% | 部分验证 | [Day 5 评测](day5-evaluation.md)、[复算结果](../../artifacts/day5-evaluation/local-run-v1.json) | 1 件真实事件，SEC 1 条可计分历史分类记录，独立标签 neutral、模型 negative。 | precision 候选与二分类 accuracy 均为 0/1；正式主公式待定，单样本不能代表总体，目标未验证。 |
| K04 | 负面舆情召回率 ≥85% | 部分验证 | [Day 5 评测](day5-evaluation.md)、[复算结果](../../artifacts/day5-evaluation/local-run-v1.json) | 已实现 TP/FN 计算和零分母处理；当前没有真实负面标签。 | 召回率为 null，分母 0，不能记作 0% 或达标。 |
| K05 | 正/中/负三分类准确率 ≥88% | 部分验证 | [Day 5 评测](day5-evaluation.md)、[复算结果](../../artifacts/day5-evaluation/local-run-v1.json) | SEC 由两位独立标注者一致标为 neutral，预测 negative；Optum 按非独立标签与缺失预测排除。 | 可复算正确数 0/1、混淆矩阵 neutral→negative 为 1；样本不足，正式准确率未验证。 |
| K06 | 单事件支持 ≥500 个独立 Agent | 部分验证 | [41-Day3百炼500x30真实规模尝试.md](41-Day3百炼500x30真实规模尝试.md)、[500×30 运行 JSON](../../data/research/day3_synthetic_bailian_500x30_20261007.json)、[500×30 SQLite](../../data/research/day3_synthetic_bailian_500x30_20261007.sqlite3) | 纯合成数据；百炼真实模型运行记录；500/500 Agent 参与，逐轮决策、消息和记忆留账。 | 规模实测达到 500 Agent，但 15,000 次决策中 14,994 有效、6 次失败；不能据此宣称无失败完成或 G3 通过。 |
| K07 | 单事件支持 ≥30 轮并行仿真 | 部分验证 | [41-Day3百炼500x30真实规模尝试.md](41-Day3百炼500x30真实规模尝试.md)、[500×30 运行 JSON](../../data/research/day3_synthetic_bailian_500x30_20261007.json)、[500×30 SQLite](../../data/research/day3_synthetic_bailian_500x30_20261007.sqlite3) | 纯合成真实模型运行；30/30 轮、15,000/15,000 请求机会；并发配置为 4。记录包含跨轮消息和 Agent 自身记忆输入。 | 有规模与运行时交互记录，但含 6 次 schema 失败；准备发送 payload 哈希不证明 HTTP 实际字节，消息与动作的严格因果仍未证明。 |
| K08 | 多件历史真实事件走势方向一致率 ≥70% | 未验证 | [Day 5 评测](day5-evaluation.md)、[复算结果](../../artifacts/day5-evaluation/local-run-v1.json) | 工具按 event_id 去重；当前只有 1 件真实事件，0 件有成对的合格方向预测与结果标签。 | 一致率为 null，不能计算；仍缺观察窗、独立标签及多事件留出回测。 |
| K09a | 报告含传播路径、情绪曲线、关键节点、风险等级、应对建议五模块 | 部分验证 | [46-Day4任务报告与预警补验.md](46-Day4任务报告与预警补验.md)、[历史任务 JSON](../../artifacts/day4-task-acceptance/real-historical-task.json)、[报告 JSON](../../artifacts/day4-task-acceptance/real-historical-report.json)、[交互索引](../../artifacts/day4-task-acceptance/real-historical-interaction-ledger.json)、[Web 报告截图](../../artifacts/day4-web-closure/02-report-overview.png) | 一件真实历史案例材料进入 `offline_dynamic_substitute`；10 Agent × 3 轮；图模式为 `offline_archived_record_projection`；五模块绑定同一 case/graph/run，并有来源、动作、消息 ID 引用。 | 本机报告结构与有限引用链有实测证据；不是真实模型/GraphRAG 端到端报告，不能作为 G3/G4 全面达标。 |
| K09b | 从事件输入到完整报告 ≤60 分钟 | 部分验证 | [46-Day4任务报告与预警补验.md](46-Day4任务报告与预警补验.md)、[历史任务 JSON](../../artifacts/day4-task-acceptance/real-historical-task.json)、[合成 Web 运行截图](../../artifacts/day4-web-closure/01-task-complete.png) | 本机历史材料 + 离线替身；一次 API 接收到报告生成耗时 0.097597 秒；Web 另有纯合成 10×3 主机闭环。 | 仅证明替身模式一次耗时；未测真实采集、真实模型及端到端事件处理时效。 |
| K10a | 支持红/橙/黄/蓝四级预警及企微/邮件适配 | 部分验证 | [46-Day4任务报告与预警补验.md](46-Day4任务报告与预警补验.md)、[四级合成矩阵](../../artifacts/day4-task-acceptance/synthetic-alert-matrix.json)、[Web 蓝色预警截图](../../artifacts/day4-web-closure/08-alert-blue.png)、[企微预览截图](../../artifacts/day4-web-closure/06-wecom-preview.png)、[邮件预览截图](../../artifacts/day4-web-closure/07-email-preview.png) | 四个合成阈值场景均生成 dry_run 企微/邮件载荷；另有真实历史材料的离线替身蓝色结果。 | 本机规则分支和两种载荷格式有证据；阈值未按真实事件校准，所有消息均为 dry_run。 |
| K10b | 红色预警从发现到实际推送 ≤30 分钟，且有送达状态证据 | 未验证 | [46-Day4任务报告与预警补验.md](46-Day4任务报告与预警补验.md)、[企微离线预览](../../artifacts/day4-web-closure/06-wecom-preview.png)、[邮件离线预览](../../artifacts/day4-web-closure/07-email-preview.png) | 仅有 dry_run 预览，无真实收件人，`sent_at=null`；未发生真实发现到发送/送达的计时。 | 无真实推送 API 返回、渠道受理/送达回执或 30 分钟时限证据。 |
| K11 | 单次推演 Token 可统计、可设上限；标准任务平均成本不高于设定预算 | 部分验证 | [41-Day3百炼500x30真实规模尝试.md](41-Day3百炼500x30真实规模尝试.md)、[500×30 运行 JSON](../../data/research/day3_synthetic_bailian_500x30_20261007.json)、[500×30 SQLite](../../data/research/day3_synthetic_bailian_500x30_20261007.sqlite3)、[项目预算与状态](../planning/00-规划总览与待确认事项.md) | 纯合成真实模型运行；任务上限 ¥30.00；7,552,220 输入、974,379 输出 tokens 有回执；6 个失败请求 usage 未知。账本本地估算 ¥2.73385065，其中包含未知 usage 预留。 | 运行级 usage 和预算闸门有证据；未知 usage 未当作 0。费用是本地估算，不是供应商账单；缺标准任务集合、平均成本和供应商账单复核。价格版本字符串是标签，不是独立价格证明。 |
| K12 | 在不少于 10—20 个历史真实事件上验证量化指标 | 未验证 | [输入](../../data/evaluation/day5/inputs-v1.json)、[独立标签文件](../../data/evaluation/day5/future-labels-v1.json)、[Day 5 评测](day5-evaluation.md) | 已有版本化输入/标签与排除统计，两个来源归属于 1 件真实事件。 | 工具与输入/标签隔离已实现；实际事件数仍不足，不把来源条数或合成事件算入正式事件分母。 |
| F01 | Web 支持任务配置/进度、图谱、看板、报告和自然语言追问 | 部分验证 | [44 号主机 Web](44-Day4Web前端本机闭环实测.md)、[46 号历史补验](46-Day4任务报告与预警补验.md)、[45 号 Docker](45-Day4Docker本机运行验收.md)、[容器 Web 实图](../../artifacts/day4-docker-acceptance/corrected-run/web-task-overview.png) | 主机点击启动与报告展示、容器任务完成页面均有各自证据；容器本批任务从 API 发起。 | 任务与产物展示已实测；没有容器 Web 点击启动的新证据，自然语言追问尚未实现。 |
| F02 | Docker 一键部署、内网化运行 | 部分验证（功能项实测通过） | [45 号 Docker](45-Day4Docker本机运行验收.md)、[容器证据](../../artifacts/day4-docker-acceptance/corrected-run/runtime-cli.txt)、[任务](../../artifacts/day4-docker-acceptance/corrected-run/run-summary.json)、[重启](../../artifacts/day4-docker-acceptance/corrected-run/restart-summary.json) | 最终镜像构建成功；riskshieldaccept API/Web/入口容器运行，合成10×3任务完成，重启后同一产物JSON相等。 | 构建、健康检查、页面、容器通信、任务与持久化功能项通过；完整出口隔离和目标域内运行未通过。 |
| F03 | 最终数据不出域；可证明域内运行及网络边界 | 部分验证 | [45 号 Docker](45-Day4Docker本机运行验收.md)、[路由与连通性](../../artifacts/day4-docker-acceptance/corrected-run/runtime-cli.txt) | API/Web 内部网络无默认路由，直连1.1.1.1:443返回errno=101；应用计数为0。入口容器有默认路由，未做全程抓包。 | 已取得应用容器直接出口受限证据；不能据单个公网IP探测判全容器组无外发，完整出口隔离与域内真实模型未验证。 |
| F04 | 公开来源合规、来源范围/版本/时间可追溯；交付部署/接口/使用/测试文档 | 部分验证 | [交付入口](../delivery/README.md)、[历史报告](../../artifacts/day4-task-acceptance/real-historical-report.json)、[交互索引](../../artifacts/day4-task-acceptance/real-historical-interaction-ledger.json)、[同步记录](47-Day5工程文档同步与复核.md) | 单案例cutoff、版本及交互引用已有本机证据；部署、接口、使用、测试四类文档已按最新实践同步。 | 文档已交付；全量来源许可/脱敏审计、逐结论验证、正式指标与最终版本冻结仍待完成。 |

## 本次集成范围

2026-10-08 已纳入[Day 5 评测结果](day5-evaluation.md)、[四类交付文档](../delivery/README.md)和45号最新容器证据。主Agent在进度评估批次全量复跑 **154 passed、1 warning**，并重算离线评测结果一致；本次同步的相关测试、链接与文件核查见[47号记录](47-Day5工程文档同步与复核.md)。固定证据清单现包含最新Docker、Day5输入/标签/结果及交付文档，不作为正式冻结版本。

## 执行模式与费用核对

| 项目 | 本次记录 | 解释边界 |
| --- | --- | --- |
| Day 3 规模运行 | 纯合成输入；百炼真实模型运行记录；500/500 Agent、30/30 轮、15,000 次机会、14,994 有效、6 失败。 | 是真实模型模式的本地账本证据，不是新版真实事件走势回测；完整轮次结束不等于无失败，也不证明消息使动作改变。 |
| Token usage | 有 usage 回执的调用合计输入 7,552,220、缓存输入 0、输出 974,379；6 次失败的 usage 缺失。 | 核查脚本从逐条轨迹重新数失败行，检查它们 `usage=null`，核对账本 `unknown_usage_calls=6`，并检查失败行保留了保守预留。不会把缺失 usage 填成 0。 |
| 费用 | 运行账本 `estimated_cost_cny=2.73385065`，与逐条 `cost_cny` 求和核对；未知 usage 有保守预留。 | 本地估算不是供应商最终账单；没有供应商账单或计费 API 证据。价格版本字段只是运行记录标签。 |
| Day 4 任务 | `real_historical` 材料使用 `offline_dynamic_substitute`；五模块报告来自同一运行；模拟运行的模型调用与网络请求应用账本为 0。 | 真实历史输入模式不等于真实模型。图是离线归档记录投影，不是在线 GraphRAG 查询。账本计数不证明 OS 层网络隔离。 |
| Web | 主机进程和 Docker 各有独立运行及截图；容器合成任务10×3从API发起，Web显示完成及产物。 | 主机点击证据与容器显示证据分开；四级分支覆盖来自离线合成矩阵。 |
| 通知 | 企业微信和邮件生成 `dry_run` 预览；无真实收件人，`sent_at=null`。 | 预览不是实际发送，也不是送达回执。红色发现至实际推送时效未验证。 |
| Docker/域内 | 镜像构建、容器功能、合成任务、重启持久化已实测；API/Web无默认路由且公网直连不可达。 | 入口容器有默认路由，完整出口隔离未通过；未取得全程抓包或目标域内部署证据。 |

## 当前缺口与复核清单

- [ ] 按五个目标平台逐源取得许可/使用范围、有效记录、运行批次和覆盖证明；补连续采集时间戳与 ≤15 分钟延迟分布。
- [ ] 冻结标注规范、独立标注和争议处理；建立事件独立留出集，分别复算负面准确率、召回率与三分类准确率及混淆矩阵。
- [ ] 补齐 500×30 失败原因与运行质量复核；通过受控对照证明消息影响后续动作，并确认失败、重试、实际 dispatch 及完整计费留证。G3 仍未通过。
- [ ] 在至少 10—20 件独立真实历史事件上冻结 cutoff、走势窗口和真值，完成逐事件方向回测；不得把合成事件或投诉样本外推为全渠道结果。
- [ ] 以真实来源与正式执行模式测量从输入到完整五模块报告的 ≤60 分钟时效，并验证结论逐项回溯到本次 Agent 交互和原始来源。
- [ ] 按真实事件校准四级预警规则；使用已授权的实际接收渠道验证红色发现到推送 ≤30 分钟及送达回执，区分渠道发送和送达状态。
- [ ] 用供应商账单/回执与逐调用 usage 对账；保留未知 usage 并作上界预算，明确价格版本、失败/重试费用及标准任务平均成本对预算的比较。
- [ ] 完成 Web 所有目标操作和自然语言追问证据；分别保存主机与容器运行 ID，不能跨模式借证。
- [x] Docker 构建、启动、健康检查、合成任务闭环、容器通信和重启持久化已取得独立证据。
- [ ] 入口容器仍需可验证出口限制和完整网络观测；之后在目标域内复核数据路径与模型运行边界。
- [x] 四类交付文档及Day5评测结果已同步，最新主Agent全量测试批次为154 passed、1 warning。
- [ ] 完成全量来源合规与脱敏审计、足量正式指标评测，并冻结最终交付版本。
- [ ] 主 Agent 复核 G3、G4 及上述证据后再判定 G5；本次仅生成工作区当前快照，不宣告冻结或达标。

## 核查脚本边界

[核查脚本](../../scripts/day5_delivery_audit.py)只读取固定证据清单、当前入口、规划、交付与验收文档的本地链接和 Git HEAD/工作区状态；只向 `artifacts/day5-delivery-audit/snapshot-current.json` 写入快照。快照包含证据文件大小与 SHA256、缺失路径、Markdown 本地链接结果、Git 状态清单和 usage/成本核对摘要。脚本不会读取密钥，不复制证据正文，不连接模型/平台/通知/供应商计费服务，不运行应用、测试、采集或 Docker。测试用例源文件：[test_day5_delivery_audit.py](../../tests/test_day5_delivery_audit.py)。
