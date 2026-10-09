# Day 3 仿真性能与动态行为复核

> 最新更新：2026-10-08。第 10 节记录当前固定源码 v4 的真实并发 4/8 小试及唯一一次全规模尝试。两组小试**已验证**；全规模因一次 qwen-flash transport timeout 停止于 partial，不能视作完整成功。第 1–9 节保留先前批次证据，不被新产物覆盖。此前完整 500×30 执行的 81.641 分钟仍超过 60 分钟门槛；当前 v4 全规模配置因未完成而**未验证**。本记录不宣布 G3、G4、G5 或最终验收通过。

## 1. 冻结的 500 Agent × 30 轮运行

复核对象是 2026-10-07 一次纯合成北京百炼运行 `run_50789c8d2dfe4260b682`，输入为虚构 Northstar 案例，不含真实投诉或真实历史来源。原始[脱敏 JSON](../../data/research/day3_synthetic_bailian_500x30_20261007.json)和[SQLite 账本](../../data/research/day3_synthetic_bailian_500x30_20261007.sqlite3)均保留；摘要及逐项对账见[规模复核产物](../../artifacts/final-simulation-review/scale-run-review.json)。

| 项目 | 核验结果 |
| --- | --- |
| 输入 | `fictional_northstar_service_day3` / `fictional_northstar_graph_v1`，`day3-synthetic-v1`，cutoff `2026-01-01T10:30:00+08:00` |
| 配置 | 500 Agent × 30 轮、并发 4、seed 1、15,000 最大调用、¥30 单任务硬上限；路由 qwen-turbo 6,000 次、qwen-flash 9,000 次 |
| 结果 | 15,000 次请求机会、14,994 个有效决策、6 个失败；500 个 Agent 有效参与、30/30 轮结束 |
| 失败 | 6 次均为 schema/response contract 失败，出现在第 14、19、21、24、25、30 轮；失败保留为失败，没有补造引用或自动重试 |
| 用量 | 已知输入 7,552,220 tokens、输出 974,379 tokens；6 次失败 usage 未知 |
| 费用 | 本地估算 ¥2.73385065，含未知 usage 保守预留，不是供应商账单；历史最大预留 ¥29.8368，余量 ¥0.1632 |

JSON 与 SQLite 的运行状态、30 轮统计、15,000 条动作轨迹和消息摘要相符。供应商账单、确切运行源码字节版本没有留存；运行记录仅表明预运行 Git 基点为 `d431a10` 且当时有未提交改动。

### 4,898.446 秒的计时边界

30 个轮级 `elapsed_seconds` 合计 4,898.446 秒，即 81.640766 分钟，按三位小数为 **81.641 分钟**，按一位小数为 **81.6 分钟**；单轮最短 149.595 秒、最长 180.554 秒。

历史计时从进入并发决策池前开始，在决策校验、usage/费用汇总与本轮状态、消息、记忆暂存后停止。它不含上下文构建、预算预检、SQLite 持久化、运行刷新和报告生成。因此这组逐轮时间合计是完整 input-to-report 的下界，已经超过 60 分钟要求；旧账本没有逐请求起止时间，无法把它拆成网络等待、供应商排队、推理和本机处理时间。该运行时效**未达标**。

## 2. 已验证的本机上下文优化

`_context()` 原先会解码所有历史记忆引用，再选出最多 8 条记忆。优化保留引用 JSON 原文，只在记忆实际入选时解析；记忆排序、内容和对外 observation 字段不变。实现位于 [day3.py](../../src/riskshield/simulation.py)。

在同一冻结 SQLite 副本、Python 3.12.4 进程内，丢弃一次预热后配对测量 5 次，每次重建 30×500 个 observation，没有 backend 或网络调用：

| 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 30 轮上下文构建中位数 | 3.703259 秒 | 3.377921 秒 |
| 中位数变化 | — | 快 8.79%，节省约 0.325 秒 |
| JSON 解码次数 | 423,187 | 122,384，减少 71.1% |

配对样本见[配对基准](../../artifacts/final-simulation-review/context-benchmark-paired.json)，输出等价核验见[observation 摘要](../../artifacts/final-simulation-review/observation-equivalence.json)。优化前后 15,000 个 observation 的逐轮 SHA-256 相同，合并摘要为 `57cf5e17e0c4b888721dc5f767862c3e2d30ca6d0be39a49f6e75e881c39b1b0`。这只是相同冻结库上的上下文微基准，节省量约占历史 4,898.446 秒的 0.007%，不能让规模任务满足 50 分钟预算。

## 3. 运行时消息、记忆隔离和引用核验

离线核对结果见[运行时审计产物](../../artifacts/final-simulation-review/runtime-audit-after.json)：

- 账本有 20,622 条运行时消息；前 29 轮产生的 19,470 条消息在紧接的下一轮可见并通过 ID 核对。末轮 1,152 条消息没有第 31 轮，无法核验后续可见性。
- 有 34,464 条记忆记录，0 条跨 Agent 记忆注入；9,523 次请求 observation 含入轮消息，14,500 次含该 Agent 自身此前记忆。
- 589 个有效决策引用可见消息（937 个消息 ID）；2,836 个有效决策引用自身可见记忆（6,696 个记忆 ID）。有效决策引用均通过 observation 可见性校验。

这些证据证明执行器把允许的消息和独立记忆送入输入并校验引用；它们**不证明**真实模型因收到消息而改变动作。离线替身测试也只验证执行器机制，不能替代真实模型的因果对照。

## 4. 可集成接口与停止语义

`Day3Simulation.advance(run_id, backend, *, max_rounds=None, collect_performance=False, stop_event=None, deadline_monotonic=None, stop_on_failure=False)` 保持原必需参数和 `max_rounds`，新增选项均为关键字可选参数。

- 默认 `collect_performance=False`；打开后 summary 增加逐请求性能统计和逐轮阶段计时。单调时钟计时持续时间，ISO 墙钟时间用于关联；请求正文和密钥不进入计时表。
- 请求计时含排队、backend 方法、响应/引用校验、状态/消息/记忆计算、账本与轮次持久化阶段。backend 方法耗时含 adapter、传输及响应解析，不等于供应商推理时间。并发请求耗时可能重叠，不可相加当墙钟。
- `stop_event` 和同一进程内的绝对 `deadline_monotonic` 只停止新派发；已派发的 future 排空并保留结果和费用。它们不取消供应商计费。没有自动重试。
- `valid`、`failed`、`not_dispatched` 分别记录。停止产生的部分轮只保留动作账本，不折叠该轮状态/消息/记忆；恢复时只补派未派发决策，并以 observation hash 拒绝不同输入。已完成成功或失败的决策不会重复派发。
- 为兼容原状态口径，`status="complete"` 代表所有计划轮次和请求机会已终结，不保证每个决策有效；`all_decisions_valid` 只有所有决策有效时才为 true。E 必须同时检查状态与该布尔值，失败或 partial 不能生成成功任务。

E 直接调用可采用如下结构：

```python
summary = engine.advance(
    run_id, backend,
    collect_performance=True,
    stop_event=stop_event,
    deadline_monotonic=time.monotonic() + 50 * 60,
    stop_on_failure=True,
)
if summary["status"] != "complete" or not summary["all_decisions_valid"]:
    mark_task_incomplete_and_keep_ledger(summary)
```

deadline 是本进程的单调时间戳，不可直接跨 API 传递。E 另须从事件进入任务的 T0 开始，用同一单调时钟计到五模块报告完整写盘且可读取的 T1。

### Runner 接口已接通

[run_synthetic_bailian](../../experiments/day3_bailian_synthetic_run.py) 现将性能采集、停止信号、单调时钟截止时间、遇首个失败停止及五模块报告生成作为可选参数接入 `engine.advance()`；默认调用仍只传旧参数，不改变默认模型输入。CLI 受控验证可用 `uv run python -m experiments.day3_bailian_synthetic_run --help` 查看。真实验证保存逐请求计时账本、源/输入哈希、usage/费用与报告；`partial` 和 `all_decisions_valid=false` 保持为非成功状态。取消等待不视作取消供应商计费，执行器排空已派发 future 后再关闭 backend。真实小试和全规模部分运行已使用该接口。

## 5. 性能账本提交方式的本机对比

[离线探针](../../experiments/simulation_performance_probe.py)对同一虚构 Northstar 图、seed 47、64 Agent × 3 轮、每次 backend 方法固定 sleep 20ms 并在第 2 轮让一个 Agent 可控失败，比较并发 4 和 8。每候选有 192 次 backend 方法调用、191 有效、1 个可控失败、无自动重试；观测在途数为 4 和 8。

先说明口径：这**不是**原始 500×30 执行器与新版本的速度比较。原始执行器已经逐轮批量写入决策、消息、记忆和状态。该表比较的是启用性能采集时，新请求计时账本的逐请求提交版本与该计时账本的批次提交版本；两边 `capture_enabled=true`。两版本执行器 SHA-256 分别为 `ec302e223044401cad343b9392f145e8cdbfb26c3e5a4bc49d769aad3e00e2cf` 与 `4a8d64b96045bb89d0d5b5311c46ca07ad039dd6bb850135d2e8f1ca49742b3e`。基线与当前原始产物分别为[逐请求账本版](../../artifacts/final-simulation-review/scheduler-probe-20261008-121904.json)和[批次账本版](../../artifacts/final-simulation-review/scheduler-probe-20261008-123805.json)。

| 并发 | 计时账本逐请求版 `advance()` 墙钟 | 计时账本批次版 `advance()` 墙钟 | 差异 | 计时账本累计写入耗时 |
| ---: | ---: | ---: | ---: | ---: |
| 4 | 1.895530 秒 | 1.354991 秒 | 快 28.5% | 1.139000 → 0.275911 秒 |
| 8 | 1.477058 秒 | 0.713323 秒 | 快 51.7% | 1.169727 → 0.138965 秒 |

最终批次账本版的吞吐统计以 `advance()` 内计时为分母：并发 4 为 142.624 完成请求/秒、141.881 有效决策/秒；并发 8 为 271.798 完成请求/秒、270.382 有效决策/秒。请求 p50/p95/p99 为 20.600/21.049/21.352ms（并发 4）及 20.574/20.995/21.172ms（并发 8）。backend 方法 p50/p95/p99 分别为 20.552/21.007/21.321ms 与 20.548/20.954/21.141ms。排队 p50/p95/p99 分别为 0.146/0.276/0.442ms 与 0.129/0.267/0.398ms。测试模型只有一个受控替身，单次可控失败类别为 RuntimeError，usage 对失败未知；没有真实模型费用。

这些差异只反映计时账本写入粒度与对应版本墙钟，不能称为相对原始轮级持久化执行器的提速，也不能外推到真实模型或 500×30。没有另测性能采集开关的 CPU 开销。

## 6. 版本冻结、授权与价格核验

真实运行批次在 [manifest.json](../../artifacts/final-simulation-review/live-validation-20261008/manifest.json) 固定了 Git HEAD `17495e6400d8352c84722a4cb374200d8f12b02f`、代码/依赖/fixture 哈希、Python 3.12.4、输入/图谱/初始状态哈希、seed 1、角色与模型路由、并发变量、调用上限及计时边界。冻结运行源码 SHA-256：runner `9632c387af65914affd9779a0659ab08f906277606d96aa5f02d54ad3ee91b96`，Day 3 `4a8d64b96045bb89d0d5b5311c46ca07ad039dd6bb850135d2e8f1ca49742b3e`，adapter v2 `bd1622f0528ad8849f1cad8cf3b632adb361848329c06cc2ee066e1bca64645b`，提示词 `de3bca9b162a8106771e95c0f107ef40cd38e5ff26d8cf2533d6f1a6b5915e8c`。失败后修复版本的哈希见 [post-run-contract-fix.json](../../artifacts/final-simulation-review/live-validation-20261008/post-run-contract-fix.json)；运行期间未改源码，修复后未再发真实调用。manifest 内嵌 SHA-256 是去掉 `manifest_sha256` 字段后对 UTF-8、排序键、无空格 JSON 的 SHA-256，复算一致。

此次用户明确授权纯合成 Northstar 输入、北京百炼 qwen-turbo/qwen-flash、先两臂各最多 50 次，再依据小试决定是否执行一次 500×30，任务总费用硬上限 ¥30、无自动重试。运行前官方北京按量价核验为 qwen-turbo 输入/缓存输入/输出 ¥0.30/¥0.06/¥0.60 每百万 tokens，qwen-flash ¥0.15/¥0.03/¥1.50；配置采用未缓存输入价，历史未知 usage 按预留计费。官方来源：[qwen-turbo](https://help.aliyun.com/zh/model-studio/qwen-turbo)、[qwen-flash](https://help.aliyun.com/zh/model-studio/qwen-flash)、[模型计费说明](https://help.aliyun.com/zh/model-studio/model-pricing)，控制台促销价不在核验范围内。

运行清单有两个保留的元数据问题：`official_price_check.region` 字符串出现编码损坏；嵌入的本地 preflight 对象仍显示 `live_authorized=false` 和“需手工比较价格”。这两个字段不是本轮授权或价格核验的权威结论。原清单未改动，纠正说明、授权来源、价格核验及哈希见 [metadata-correction.json](../../artifacts/final-simulation-review/live-validation-20261008/metadata-correction.json)。独立检查依据为清单中的后端路由 `region=cn-beijing`、当时用户本轮明确授权，以及上述官方文档的北京标准按量价。

小试前每臂保守预留 ¥0.099456（50 次上限、turbo 20 次、flash 30 次），低于授权的每臂 ¥0.10。两臂实际估算费用合计 ¥0.01235517。完整规模预留 ¥29.8368，扣除小试实际估算后本任务剩余预算 ¥29.98764483，预留后余量 ¥0.15084483。该费用为本地 usage 估算与未知 usage 预留，不是供应商账单。

## 7. 真实并发 4/8 小试结果

两候选均用 `day3-synthetic-v1`、25 Agent × 2 轮、seed 1、同一初始状态/图谱/角色/提示词和固定模型路由。输入版本、代码与配置哈希详见 manifest；独立 run、SQLite、JSON 和五模块报告均在 `live-validation-20261008/`，逐请求与消息正文不在最终文档中展开。

| 指标 | 并发 4 | 并发 8 |
| --- | ---: | ---: |
| 调用机会 / 已派发 / 有效 / 失败 / 未派发 | 50 / 50 / 50 / 0 / 0 | 50 / 50 / 50 / 0 / 0 |
| 有效输出率 | 100% | 100% |
| 仿真耗时 | 17.282 秒 | 29.418 秒 |
| 输入至五模块报告产物 | 17.341 秒 | 29.500 秒 |
| 完成请求及有效决策吞吐 | 2.895 / 秒 | 1.701 / 秒 |
| backend 方法耗时 p50/p95/p99 | 1.109 / 2.324 / 3.299 秒 | 1.295 / 6.027 / 15.311 秒 |
| 排队耗时 p50/p95/p99 | 0.096 / 0.306 / 0.393 毫秒 | 0.101 / 0.353 / 0.947 毫秒 |
| 实际最大在途 | 4 | 8 |
| usage 未知 / 估算费用 | 0 / ¥0.00621450 | 0 / ¥0.00614067 |

原始比较及按模型统计见 [pilot-comparison.json](../../artifacts/final-simulation-review/live-validation-20261008/pilot-comparison.json)，run 账本分别为 [pilot-c4.json](../../artifacts/final-simulation-review/live-validation-20261008/pilot-c4.json) 与 [pilot-c8.json](../../artifacts/final-simulation-review/live-validation-20261008/pilot-c8.json)。此单批样本中并发 8 的仿真耗时比并发 4 高 70.2%，有效吞吐低；p95 backend 耗时约为 2.59 倍，p99 约为 4.64 倍。因此在此次固定路由/输入下选并发 4 进入全规模尝试。小试规模不足以证明一般化优劣，也不能外推 500×30 时效。

两臂都产生了五模块报告（propagation、emotion_evolution、key_nodes、risk、recommendations），但动作全部是 `observe`，生成 0 条消息；各有 25 次请求携带自身先前记忆。故消息后续轮可见性在小试中未被触发，记忆隔离仅有输入/引用边界证据，不能据此做消息因果推断。

## 8. 全规模真实运行：partial，按首错停止

满足小试配置门槛后，以并发 4、500 Agent × 30 轮、最多 15,000 次派发、任务预算剩余 ¥29.98764483、stop-on-first-failure、不重试启动一次真实纯合成验证，并连接 Day 4 的五模块报告生成。运行期间没有修改源码。详细原始结果与审计见 [full-c4.json](../../artifacts/final-simulation-review/live-validation-20261008/full-c4.json)、[SQLite ledger](../../artifacts/final-simulation-review/live-validation-20261008/full-c4.sqlite3)、[outcome review](../../artifacts/final-simulation-review/live-validation-20261008/full-c4-outcome.json) 和 [partial five-module report](../../artifacts/final-simulation-review/live-validation-20261008/full-c4.five-module-report.json)。

- 在第 11 轮 `agent_0397` 的 qwen-flash 响应出现 `evidence_ids` 为空，触发 schema `too_short`。该请求 usage 未知，按预留 ¥0.0016128 计入；没有补造引用或自动重试。按停止策略，当前已派发请求排空后停止新增派发。
- 完成 10 个整轮，并完成第 11 轮的 400 个有效决策；1 个派发失败、99 个该轮机会未派发，未来 19 轮的 9,500 个机会未启动。共派发 5,401 次，5,400 个有效、1 个失败；有效输出占全计划 36%。任务与仿真均为 `partial`，`all_decisions_valid=false`。
- 仿真段 1,840.618 秒（30.677 分钟）；事件输入到 partial 五模块报告文件写盘 1,841.098 秒（30.685 分钟）。报告仅表示 partial 输入的报告生成成功，不是完整 500×30 报告。
- 观测有效吞吐 2.934 决策/秒，最大实际在途 4；请求/backend 方法 p50/p95/p99 为 1.252/2.030/2.799 秒，排队 p50/p95/p99 为 0.081/0.197/0.398 毫秒。backend 计时含 adapter、传输和响应解析，不能解释为供应商推理耗时。累计 backend 方法耗时 6,770.934 秒与请求并发重叠，不能求和当墙钟。
- 阶段累计计时中，调度及排空墙钟为 1,701.583 秒；观察构建 0.215 秒；请求账本持久化累计 10.060 秒；SQLite 轮次持久化 0.189 秒；状态读取 0.057 秒、图谱读取 0.006 秒、消息读取 0.009 秒、记忆读取 0.076 秒、预算预检 0.002 秒。计时分项有并行和嵌套关系，勿直接相加。现有证据表明这段运行墙钟主要花在并发调度/等待 backend 返回；backend 内部供应商排队、传输与推理各自占比无法区分。
- 使用/费用：prompt 2,028,391 tokens、completion 365,848 tokens、缓存输入 256 tokens，1 次 usage 未知。全规模 partial 估算 ¥0.84157041，含未知 usage 预留；加小试为 ¥0.85392558。没有供应商账单核验。
- 动态证据：已持久化 1,827 条运行时消息、6,416 条记忆记录；输入边界审计到 1,827 个消息引用，0 个未来/不可见消息引用错误；26,714 个自身记忆引用，0 个跨 Agent 或未来记忆错误。它验证执行器的可见性/隔离引用校验，不构成模型反事实或动作因果证据。

失败是有效输入契约与提示词描述不够明确：v2 提示词要求引用允许 ID，却没有明确要求至少一个证据 ID，schema 则要求非空。停止后最小修复把该要求明确加入提示词并将 adapter/engine 审计版本同步到 v3，未填充任何 citation，未续跑混合版本任务。修复说明见 [post-run-contract-fix.json](../../artifacts/final-simulation-review/live-validation-20261008/post-run-contract-fix.json)；v3 提示词 SHA-256 `4a140a50b3036a7c82e978d6d4c030ab18d374159b457568c733de08591694f2`，Day 3 SHA-256 `232126062559b62a73d9d4c918940c9cbe5063ef30936f3fd27dd97aa047bd42`，adapter SHA-256 `0e3eb5aa5c351cd4318c7c164c3630a3f6ec2c87230d9a38131c8e989eb9b424`。修复后的当前代码测试通过，但没有用 v3 重发真实请求，也不据此推断失败已消除。原 partial 与失败账本保持原样。

## 9. 验证、结论与集成

实际验证命令与结果：

```powershell
uv run python -m py_compile src/riskshield/day3.py experiments/day3_bailian_synthetic_backend.py experiments/day3_bailian_synthetic_run.py tests/test_day3_bailian_synthetic_run.py tests/test_day3_bailian_synthetic_backend.py
uv run pytest tests/test_day3_bailian_synthetic_run.py tests/test_day3_bailian_synthetic_backend.py tests/test_day3_simulation.py tests/test_simulation_performance.py tests/test_simulation_performance_probe.py tests/test_day4_report.py -q
uv run pytest tests/test_day4_api.py tests/test_day4_tasks.py -q
```

结果分别为编译成功、`78 passed, 1 warning`、`12 passed, 1 warning`。全局 `git diff --check` 无差异错误；Git 给多个已有变更文件提示 LF 将转为 CRLF，这是行尾提示。其他 Agent 的报告/API/UI/评分改动未由本职责修改。

**结论：**计时、停止与逐请求账本接口、执行器轮次/引用/记忆边界离线机制、两组真实小试以及 partial 的真实计时/费用/报告产物均**已验证**，证据限于列出的源码版本和运行账本；实际消息输入边界的运行时引用审计**已验证**，消息对动作的因果影响**未验证**。完整 500×30 运行**部分验证**，全计划任务有效完成率仅 36%，且在 v2 提示词下出现 1 个 schema 失败而按规则停止；完整规模性能**未验证**。既有完整运行仿真计时 81.641 分钟，已高于内部 50 分钟预算和 60 分钟端到端上限；本轮 partial 的 30.685 分钟不能代表完整规模。因此 ≤60 分钟目标**未达标**，不是关口通过结论。

本轮授权总上限按冻结清单为 15,100 次调用（两臂 100 + 一次完整运行 15,000）；已派发 5,501 次，剩余 9,599 次调用机会，不足以启动一个新的 15,000 次完整 run。当前累计估算费用 ¥0.85392558，硬上限内余额 ¥29.14607442；按现有每次最大预留配置，另一个完整 run 需 ¥29.8368，超过余额 ¥0.69072558。旧 partial 不能改用 v3 提示词续跑，避免混合提示词版本；不得绕过预算闸门或复用已完成决策。因此本轮停止真实调用，不再尝试新全量 run。

剩余集成/复核依赖：主 Agent/E 需决定是否可在不改模型输入/输出约束的前提下，基于真实可证明的请求上界重新核算预算预留；如仍不能在 ¥30 硬上限和调用上限内覆盖一个干净的 15,000 次 run，则全规模验证仍无法完成。若可行，采用当前 v3 提示词/adapter/Day3 哈希冻结新 run，并先为超出本轮 15,100 次调用范围的部分取得明确授权；核验 task 状态映射保留 `partial` 和 `all_decisions_valid=false`。E 从真实事件输入开始计时，直至完整五模块报告可读取，并记录总时长。没有执行端到端完整报告链路计时，不能声称正式时效达标。

### 主 Agent 可重复的最短核验

```powershell
uv run pytest tests/test_day3_bailian_synthetic_run.py tests/test_day3_bailian_synthetic_backend.py tests/test_day3_simulation.py tests/test_simulation_performance.py tests/test_simulation_performance_probe.py tests/test_day4_report.py -q
uv run pytest tests/test_day4_api.py tests/test_day4_tasks.py -q
uv run python -c "import json,pathlib; p=pathlib.Path('artifacts/final-simulation-review/live-validation-20261008'); d=json.loads((p/'full-c4-outcome.json').read_text(encoding='utf-8')); print(d['task_status'],d['completion'],d['time']['event_input_to_report_artifact_elapsed_seconds'],d['usage_and_cost']['combined_estimated_cost_cny'])"
```

## 10. 最新固定源码 v4：小试与全规模 partial

本节对应新目录 [live-validation-20261008-v4](../../artifacts/final-simulation-review/live-validation-20261008-v4/)。旧目录及冻结的既有运行没有修改。当前用户授权本批次只使用虚构 Northstar 合成输入、北京百炼 `qwen-turbo`/`qwen-flash` 固定路由，先各执行一次 25 Agent × 2 轮并发 4/8 小试；条件满足后最多启动一次干净的 500 × 30 验证，累计费用不超过 ¥30，不自动重试。本批次实际派发 100 次小试请求和 3,478 次全规模请求；全规模授权已用一次，剩余费用空间不构成追加调用授权。

### 10.1 价格、预留与冻结版本

运行前复核官方北京目录价：`qwen-turbo` 输入/缓存输入/输出为 ¥0.30/¥0.06/¥0.60 每百万 tokens；`qwen-flash` 在输入 ≤128k 档为 ¥0.15/¥0.03/¥1.50。控制台活动价未计入。[qwen-turbo 官方价格](https://help.aliyun.com/zh/model-studio/qwen-turbo)、[qwen-flash 官方价格](https://help.aliyun.com/zh/model-studio/qwen-flash)、[百炼定价说明](https://help.aliyun.com/zh/model-studio/model-pricing)。本地配置使用未缓存输入价，与网页一致；单请求输出上限保持 turbo 160、flash 256 tokens。

预检用固定 Northstar v1 图谱证据，加上输入适配器对消息/记忆的字段上限、最坏 JSON 转义和 128-token 协议预留，按真实路由逐模型计算：每个 50-call 小试臂预留 ¥0.089748，低于每臂 ¥0.10 上限；500 × 30 预留 ¥26.924400，低于该次运行预算 ¥28.94607442。费用上限按保守预算核算：既有本项目估算 ¥0.85392558 + 两臂预算上限 ¥0.20 + 全规模运行上限 ¥28.94607442 = ¥30.00。该预留是本地上界估算，不是供应商账单。

冻结清单 [execution-lock.json](../../artifacts/final-simulation-review/live-validation-20261008-v4/execution-lock.json) 的规范化哈希为 `1c71aaa4b8799063d6859470f5590731f314afdca67544e679baed8f91bdb545`；脱敏后端配置 SHA-256 为 `d549d117fff33ded69dcaca9f065be35b766716442e4c85a38617766381c56a6`。真实运行记录的关键文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `experiments/day3_bailian_synthetic_run.py` | `51944d344990e83c6921ea04a21e39531a342f90ad8a959ebb5b4194db854506` |
| `experiments/day3_bailian_synthetic_backend.py` | `fc0faada6a4f1bd5cf5f93ff371698110481f8627a0404823d73d5bdeb797f3a` |
| `src/riskshield/day3.py` | `c78404e242bb18d3d179da5865cbae1227fc1ed3f13717fe249d8e11849d17c7` |
| 系统提示词 | `4a140a50b3036a7c82e978d6d4c030ab18d374159b457568c733de08591694f2` |
| `src/riskshield/day4_report.py` | `4bfc52367fbc81b77dbcd65d0a3d175f4dc04a177c146ea0ad510677307e1b3d` |
| `tests/fixtures/day3_synthetic_case.py` | `8845dab1e7590f8404b3a04ddb484a4c5f01dc45c0138c63be942e22aee1fc9b` |
| `uv.lock` | `0e6e5d30e5c81e80fc0f104b870490c1e34575180b565f3ec3c661552a42996c` |

运行环境为 Python 3.12.4。其余依赖与源码哈希见冻结清单。运行前后核对显示清单内源码哈希未变化；后端运行期间未更改。提示词 v4 明确要求 `evidence_ids` 至少含一个允许引用，输入、记忆规则和输出限制未变。新一轮性能开关默认关闭；计时、停止参数均为旧调用可省略的关键字选项。

### 10.2 并发 4/8 同输入真实小试

输入/后端哈希在两臂完全相同：case `day3-synthetic-v1`、图谱 SHA-256 `ce423df6f23383735d2f21bc49eb9877de55b054724a266947107fe4dad79752`、初始 Agent 状态 SHA-256 `41ecbdb478ff87e51f115b3d8f66cbdaf7231f647cf6c18f114ac430a8811ebf`、后端配置哈希 `d549d117fff33ded69dcaca9f065be35b766716442e4c85a38617766381c56a6`；seed 1、role offset 0、提示词和角色模型路由均固定。每臂使用新的 SQLite 与输出文件，两个小试臂之间只改变并发值。

| 指标 | 并发 4 | 并发 8 |
| --- | ---: | ---: |
| 请求机会 / 已派发 / 有效 / 失败 / 未派发 | 50 / 50 / 50 / 0 / 0 | 50 / 50 / 50 / 0 / 0 |
| 有效率 / usage 未知 | 100% / 0 | 100% / 0 |
| 仿真墙钟 | 18.295 秒 | 14.143 秒 |
| 合成输入至五模块报告写盘 | 18.358 秒 | 14.206 秒 |
| 完成请求 / 有效决策吞吐 | 2.735 / 2.735 每秒 | 3.539 / 3.539 每秒 |
| backend 方法 p50/p95/p99 | 1.108 / 2.490 / 4.763 秒 | 1.120 / 4.329 / 6.315 秒 |
| 排队 p50/p95/p99 | 0.098 / 0.402 / 0.478 毫秒 | 0.120 / 0.498 / 0.788 毫秒 |
| 实测最大在途 | 4 | 8 |
| 本地估算费用 | ¥0.00623805 | ¥0.00619086 |

单批样本中 c8 仿真墙钟快 22.70%，有效吞吐高 29.39%，费用低 0.76%；其 backend p95/p99 分别高 73.83%/32.60%，显示尾延迟变差。两臂均通过 50/50 有效、零失败、零未派发、零未知 usage、完整五模块报告、未超预算等预先设定门槛。该结果只支持“这次小试 c8 更快”，不构成稳定分布或全规模保证。详细对照与逐模型统计见 [pilot-comparison.json](../../artifacts/final-simulation-review/live-validation-20261008-v4/pilot-comparison.json)；原始记录见 `pilot-c4.json`、`pilot-c4.sqlite3`、`pilot-c8.json`、`pilot-c8.sqlite3` 与对应 `five-module-report.json`。

两臂实际都有运行时消息：c4 生成 15 条、c8 生成 12 条；分别有 6 和 3 个后续轮消息引用，均指向前序消息且收件 Agent 匹配。每臂 25 个自身记忆引用全部属于该 Agent 且早于当前轮。引用边界审计 0 个错误。这验证该执行器把符合可见范围的运行时消息/独立记忆引用交给请求适配器，不证明模型因消息而改变动作。

### 10.3 唯一一次全规模尝试：partial，transport timeout 后停止

两臂均通过后，按本批次实测墙钟与吞吐选择 c8 做一次新的 500 Agent × 30 轮运行；没有复用旧 partial 决策。执行器调用上限 15,000、运行预算 ¥28.94607442、并发上限 8、单调截止时间为启动后 6 小时、性能计时打开、首个决策失败即停止、无重试。报告包含五个模块（emotion_evolution、key_nodes、propagation、recommendations、risk），但输入轨迹不完整，因此它是 partial 报告。

- 实际完成 6 个整轮，第 7 轮遇到首个失败即停止。已派发 3,478 次：3,477 有效、1 失败；第 7 轮另有 22 次标记 `not_dispatched`，之后 23 轮共 11,500 次机会未启动。任务和执行器状态均为 `partial`，`all_decisions_valid=false`。总计划有效决策完成率为 23.18%。
- 唯一失败位于第 7 轮 `agent_0324` 的 `qwen-flash` 请求，类别 `BailianTimeoutError:transport`。该请求有 `prepared_for_dispatch` 记录但无 usage 回执，保守预留 ¥0.0014742 进入估算；已发请求排空并记账后停止新派发，没有把超时解释为供应商未计费。
- 该错误**不是权限拒绝的证据**：账本分类为客户端 transport timeout，不是 HTTP 401/403。实际超时的网络/服务端根因不能从本次脱敏错误分类进一步确认。由于只有一次失败，没有证据支持提高 timeout、修改模型路由或重试；因此本轮没有擅自修改冻结源码，也没有补跑。
- 仿真耗时 590.872 秒；从合成 case/graph 准备之前到 partial 五模块报告文件写盘共 591.206 秒。它不是完整 500×30 报告时长，不与完整任务的 60 分钟门槛比较。backend p50/p95/p99 为 1.178/2.193/3.934 秒，队列为 0.081/0.195/0.480 毫秒，实测峰值在途 8。完成请求吞吐 5.887/s，有效决策吞吐 5.885/s。backend 时间含适配器、网络传输和响应解析，不能称为供应商推理时间。
- 模型分组：turbo 1,392 有效、0 失败、8 未派发；flash 2,085 有效、1 失败、14 未派发。Usage 输入共 1,422,830、输出 204,600、缓存输入 0；1 次 usage 未知。估算费用 ¥0.53111850（含该失败的预留），非供应商账单。
- 当前账本包含 2,340 条消息、4,548 条记忆；本次记录的 3,500 条决策 observation（有效、失败和未派发）含 2,340 个消息引用，均指向更早轮次且收件方为当前 Agent；12,647 个记忆引用均为当前 Agent 的先前记忆；未来/错收件消息引用与跨 Agent/未来记忆引用均为 0。此证据仅覆盖执行器输入引用、持久化边界及本次 partial 轨迹，不证明严格的动作因果效应。

运行 JSON、SQLite、五模块 partial 报告与审计摘要分别为 [full-c8.json](../../artifacts/final-simulation-review/live-validation-20261008-v4/full-c8.json)、[full-c8.sqlite3](../../artifacts/final-simulation-review/live-validation-20261008-v4/full-c8.sqlite3)、[full-c8.five-module-report.json](../../artifacts/final-simulation-review/live-validation-20261008-v4/full-c8.five-module-report.json)、[full-c8-outcome.json](../../artifacts/final-simulation-review/live-validation-20261008-v4/full-c8-outcome.json)。汇总文件 SHA-256 为 `45e68d951e793377240732c3c7bd93bdc935ea1e1f6464a30929b1b17aa5ef72`；完整产物哈希和字节数见 [artifact-index.json](../../artifacts/final-simulation-review/live-validation-20261008-v4/artifact-index.json)，索引自哈希为 `8628876725407cb37023f546198651c1599ad8219d0843c6e69ca81efaba83ed`。

本批次合计估算支出为 ¥1.39747299（含此前本项目估算 ¥0.85392558、两臂实际 ¥0.01242891 和本次 partial 全规模 ¥0.53111850）；距 ¥30 硬上限尚有 ¥28.60252701。该余额不授权再次进行模型调用。本次授权的一次全规模尝试已消耗；后续任何全量复测需要新的明确授权。

### 10.4 状态、推荐与集成

- 计时/停止接口、路由/预算预检、有效/失败/未派发区分、partial 状态及引用隔离在本批次测试和运行中**已验证**；本机相关测试为 93 passed、1 warning。当前两组真实小试**已验证**；c8 单批较快但尾延迟更高。
- 全规模 500×30 **部分验证**，当前版本的全规模成功率/时效**未验证**。唯一失败是 transport timeout；不是权限拒绝证据。未经新授权，不再运行或重试。
- 历史完整 500×30 逐轮时间 81.641 分钟，超过 60 分钟，因此项目目标仍**未达标**。本批次 591.206 秒只对应 6 个整轮和第 7 轮 partial 报告，不能据此改变结论。对完整真实事件输入到完整五模块报告仍须由 E 在产品任务链路内从 T0 实测到 T1；当前 runner 的 T0 是固定合成 case/graph 读取之前，不包括真实采集、事件整理或 E 的其他模块耗时。
- 本批次一次验证选用 c8，但不足以推荐其作为已证明的长期全规模默认值：小试只有每臂 50 次，c8 尾延迟更差，全规模出现一次 timeout。没有等价当前源码的 c4 全规模对照。主 Agent 可将 c8 视为本次选定验证配置，把 c4 保留作未来获得授权后的保守对照候选，不应声称两者全规模已比较。

Runner 保持旧调用兼容，新增关键字均有默认值：`collect_performance=False`、`stop_event=None`、`deadline_monotonic=None`、`stop_on_failure=False`、`generate_report=False`、`report_output_path=None`。单调时间计时，ISO 墙钟用于关联；停止信号/截止时间只阻止新派发，等待已派请求结束并持久化，不能取消供应商费用。E 集成时须同时看 `task_status` 和 `all_decisions_valid`：只有 `task_status == "complete"` 且 `all_decisions_valid is True` 才能标成功；partial 必须让任务保持未完成。E 仍须测量产品流程级事件输入 T0 至完整五模块报告可读取 T1。

```python
summary = run_synthetic_bailian(
    # 真实调用仅限另行获批；下例说明兼容接口形状
    ..., enable_live=True, collect_performance=True,
    stop_event=stop_event,
    deadline_monotonic=time.monotonic() + 50 * 60,
    stop_on_failure=True, generate_report=True,
    report_output_path=report_path,
)
if summary["task_status"] != "complete" or summary["all_decisions_valid"] is not True:
    mark_task_incomplete(summary)
```

主 Agent 可复跑的本机核验：

```powershell
uv run pytest tests/test_day3_bailian_synthetic_run.py tests/test_day3_bailian_synthetic_backend.py tests/test_day3_simulation.py tests/test_simulation_performance.py tests/test_simulation_performance_probe.py tests/test_day4_report.py tests/test_day4_api.py tests/test_day4_tasks.py -q
uv run python -c "import json,pathlib; p=pathlib.Path('artifacts/final-simulation-review/live-validation-20261008-v4'); r=json.loads((p/'full-c8-outcome.json').read_text(encoding='utf-8')); print(r['task_status'],r['completed_rounds'],r['valid_decisions'],r['failed_decisions'],r['not_dispatched_current_round'],r['timing']['simulation_elapsed_seconds'],r['timing']['event_input_to_report_artifact_elapsed_seconds'],r['estimated_cost_cny'])"
```
