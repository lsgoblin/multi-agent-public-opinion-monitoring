# 最终集成、评测与交付复核

**工作标识：** `closure-20261009-functional-names-v1`  
**复核日期：** 2026-10-09  
**范围：** 本机模块命名收尾、API/tasks/report/alert 集成、离线端到端、C 冻结批次核验、交付文档和候选包冻结。保留开始前所有工作区改动；未提交、推送、外发通知、部署外部环境或新增模型调用。

## 当前结论

本机离线闭环、代码回归和 Docker 功能可复核；正式业务指标仍有关键缺项，不能判定 G3/G4/G5 通过。B 的本轮 UI 文件哈希已核验，但其 handoff 为 `partial / ready=false`：当前版截图未保存，完整自然语言问答未实现。此处仅冻结 B 交付的现状和限制，不改写 B 所有文件。

## 实际修改

- 将生产模块按职责移至 `evidence.py`、`sentiment.py`、`source_probe.py`、`simulation.py`、`reporting.py`、`alerts.py`、`tasks.py`、`evaluation.py`，并为旧 `day*` 导入路径保留薄兼容转发。
- 将分配给 E 的日阶段测试文件改为职责名；保留 `tests/test_ui.py`、`tests/test_day5_delivery_audit.py` 及锁定历史运行使用的 `experiments/day3_*` 路径。B 文件因 handoff 为 `ready=false` 未改名或改写。
- 同步 API、模块测试、探针、脚本和文档中的新模块路径；保留 HTTP 路由、schema、配置键、SQLite 字段、任务状态与 ID 语义。
- 保留 C 冻结清单、标签、预测和评分输出原始文件。评分适配回归验证了字符串/对象 `event_id_reference`、事件/单位身份及原文哈希约束，不生成标签。
- 更新根 README、交付索引、测试报告和本记录；生成迁移映射、交接哈希核验和本轮验证证据。细节见[重命名表](../../artifacts/final-closure/rename-map.json)和[交接核验](../../artifacts/final-closure/handoff-verification.json)。

工作区开始时已存在大量未提交的 A/B/C/D 与历史文件变更。迁移表保留初始 HEAD `17495e6400d8352c84722a4cb374200d8f12b02f` 和每个迁移文件的迁移前哈希，未将这些既有改动归属为本轮新建。

## 接口与本机闭环

运行链为归档事件输入 → cutoff 合格来源图 → 仿真任务 → 五模块报告 → 暂定预警及 Web 关联展示。最新固定案例证据为[端到端摘要](../../artifacts/final-closure/end-to-end-20261009-functional-names-v1.json)：

| 对象 | ID / 结果 |
| --- | --- |
| 案例 / 图 / 仿真 | `unh_change_20240222_day2_multisource` / `936c73951d2ab46f8e0ee6bb` / `run_7aff1ceff6934a1da13e` |
| 任务 / 报告 / 预警 | `job_02aa0aed4a594ebe995c` / `report_26db8f02583eb98bde5f7490` / `alert_19fe8d54fd95ea0876eee8b6323fd0c4` |
| 模式 / 终态 | `real_historical` 输入，`offline_archived_record_projection` 图构建，`offline_dynamic_substitute` 执行；任务 `complete`，结果 `success` |
| 进度 / 决策 | 3/3 轮；30/30 有效；0 失败。有效决策与请求机会分别记录 |
| 报告 / 预警 | `propagation`、`emotion_evolution`、`key_nodes`、`risk`、`recommendations` 五模块；蓝色 `provisional_offline_assessment` |
| 来源与记忆 | 两条 cutoff 前归档来源被报告引用；1 条 `not_input` 后续材料被排除；17 个动作引用运行时消息、20 个动作引用自身记忆 |
| 耗时 / 外部动作 | 输入到报告写盘 0.189624 秒，API 可读 0.203963 秒；0 模型调用、0 外部网络请求；企微/邮件均 `dry_run`，无 `sent_at` |

这是本机已有真实历史材料上的离线动态替身工程复核，不是模型预测、实时平台数据、在线 GraphRAG 或现实公众活动测量。消息引用及独立记忆账目证明运行时对象被使用，不证明消息对决策有因果作用。蓝色预警仅是暂定规则结果。

B 的 `b-ready.json` 与其声明的 6 个文件哈希均匹配。B 自报 UI 定向回归 `7 passed, 1 warning`；本轮没有保存当前 UI 截图。D 的只读页面观察记入 B handoff，但没有图像文件路径，不能代替截图。旧版截图不是本轮业务名称版本证据。完整自然语言追问未实现，当前能力仍是词项匹配与引用展示。

## 测试与问题修复

初始 Day 4 API/tasks 基线为 2 failed、9 passed。失败原因是中文报告文本替换后，两个测试仍匹配英文短语。断言更新后保留了离线替身模式、真实模型限制及失败/部分状态语义检查；先前定向回归为 20 passed、1 warning。完整命令与历史批次结果见[测试结果记录](../../artifacts/final-integration-review/test-results.json)。

本轮命名收尾后的最终全量回归为 `uv run pytest -q`：**181 passed, 1 warning，62.46 秒**。唯一警告是既有 Starlette `TestClient`/`httpx` 弃用提示。测试结果已保存至本轮 [test-results JSON](../../artifacts/final-closure/test-results-functional-names-v1.json)。另完成模块与兼容路径导入、`py_compile`、`git diff --check` 及 Markdown 本地链接复核；命令和摘要由本轮证据记录。

## C 评测交接和隔离

C 的 `c-ready.json` 属于本工作标识，报告、索引及声明的冻结/输出哈希已逐项核验。文件内容未改写。[C v24 评分报告](../../artifacts/final-scoring-20261009/final-score-report-v24.md)显示正式 score-ready 事件为 0/20：情感三分类、负面 precision/recall/accuracy 和走势方向均不可计算（0/0），全零矩阵不代表 0% 准确率。5 个一致的双标标题单位没有配对 E 预测；27 个真人标注槽仍缺。AI 标签只供探索参考，不是独立人工真值。

交接版本按完整 handoff、其白名单、cutoff 输入哈希和冻结链判定，没有取最大文件版本号替代交接。v6 handoff 明确仍白名单指向 `qualified-inputs-v5.json`，不得改读 v6/v19 标签或另一版本输入；最新完整可用 handoff 是 v13，仅含 1 件 `holdout_candidate`，输入哈希 `01193167bc59ec4e6d59c12922ad7a9e09d9886deb520c255a5e963aaa9b772c`，仍 `formal_score_ready=false`。

本轮不具备隔离真实模型 endpoint/name 配置，也没有本批新收费调用授权，因此未执行新模型预测。现有 v8/v13 常量基线及 C 已封存评分输出只作工程/流程证据，不作正式效果。v24 C 报告由其独立适配脚本调用现有指标引擎生成，哈希已验证。E 的 v6 字符串引用适配只映射 5 个空槽单位，未造标。正式流程仍要求仅从 handoff allowlist 产生预测，封存输入/配置/执行模式/输出后才由评分端读取标签。

为保留逐事件结论，本轮生成了[逐事件评分状态摘要](../../artifacts/final-integration-review/per-event-score-status-v24.json)。摘要仅列 7 个事件标识、5 个未配对人工标题单位和逐项排除原因，不复制标题、标签、原始输入或预测；C 的冻结逐单位结果仍是权威来源。

## A、C、D 交接核对

- **A：** 500×30 纯合成真实模型批次 `run_c532286a08384534bc08` 的 13 项运行产物哈希匹配。其 execution lock 锁定旧路径 `src/riskshield/day3.py` 的哈希 `c78404e2…`；迁移表记录该迁移前版本，当前功能模块为 `simulation.py`，本轮兼容编辑后的哈希也不同。因此该运行证明锁定的 A 版本结果，不冒充本轮改名后代码的新实测。A 结果为 3,477/15,000 有效决策、6/30 轮、一次 qwen-flash timeout 后 `partial`，输入至 partial 报告 591.206 秒；G3 与完整 60 分钟 SLA 均未通过/验证。见[A 运行复核](../../artifacts/final-integration-review/a-simulation-review.json)。
- **C：** v24 handoff 与输出哈希匹配；评分结果按 C 最终报告原样引用。评测数据树和盲标签不进入本机 demo ZIP。
- **D：** `d-prep-ready.json` 标识一致，D 声明的 16 个文件/证据哈希匹配且其文件保持冻结。已存在 Docker 构建、服务健康、Web→API、离线案例、报告及停止/启动持久化证据；入口容器有默认路由，完整出口隔离、全程流量审计和域内运行未通过/未验证。见[D 部署复核](final-deployment-review.md)。

## 交付冻结与复现

全量测试复现命令：`uv run pytest -q`。本机闭环复现命令：

```powershell
uv run python experiments/final_integration_probe.py --out artifacts/final-closure/end-to-end-replay.json
```

该复现使用项目既有归档材料及临时数据库，固定为 `offline_dynamic_substitute`，不会触发云模型、外部通知或数据外发。运行脚本本身已写入工作区闭环证据。

候选 ZIP 清单位于 `artifacts/final-integration-review/candidate-package-manifest.json`，本轮源码/依赖/文档摘要位于 `artifacts/final-closure/version-manifest-closure-20261009-functional-names-v1.json`，冻结状态与哈希位于 `artifacts/final-closure/integration-ready.json`。D 的 ZIP 及独立解压复验属于后续 D handoff；候选版本带有上述 UI 截图缺口，并不等于正式验收通过。

## 剩余问题与责任

| 项目 | 状态 | 责任方 / 补充证据 |
| --- | --- | --- |
| 当前版 Web 截图 | 未验证 | B handoff 未就绪；需在支持中文的受控浏览器保存当前版本图像。 |
| 完整自然语言追问 | 未实现 | 产品/API 实现方；词项检索不能计作问答。 |
| G3 500×30 与完整时效 | 未达标/未验证 | A 后续按新授权运行完整任务；当前 partial 不计通过。 |
| 五平台覆盖、采集延迟及正式情感/走势指标 | 未验证/不可计算 | C/评测方补齐 cutoff 数据、独立人工标签及预测配对。 |
| 通知送达和红警 SLA | 未验证 | 仅有 dry-run，须有经授权的真实送达回执。 |
| Docker 出口边界及域内运行 | 未验证 | D/部署验收方补全入口容器流量证据与域内环境证明。 |
| 最终关口 | G3/G4/G5 未通过 | 由原评估对话独立核验；本记录和候选包不自动通过关口。 |
