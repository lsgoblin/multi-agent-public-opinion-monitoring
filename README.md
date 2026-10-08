# 风控盾 · 多智能体保险舆情监测

[当前赛题](docs/sources/2026-09-30-新版命题-多智能体仿真舆情监测.md)要求公开多源监测、情感识别、GraphRAG、独立人设与长期记忆、动态社会仿真、五模块报告和四级预警。[文档索引](docs/README.md)给出设计、计划和证据入口。

用户已授权 Day 4 本机工程和受限 Day 5 离线评测、证据核查与交付文档。Day 1/G1 与 Day 2/G2 已内部受限通过；当前实现包括案例导入与 cutoff 过滤、受限来源采集、证据图检索，以及 Day 3 的独立人设、局部状态、按 run/agent 隔离的持久记忆和同步动态仿真引擎。纯合成北京百炼[500 Agent × 30 轮真实规模尝试](docs/records/41-Day3百炼500x30真实规模尝试.md)已执行，15,000 次请求中 14,994 次有效、6 次无效决策；严格消息动作因果仍未证实，**G3 未通过**。当前决策见[Day 3 工程决策](docs/planning/24-Day3工程决策.md)。

Day 4 已完成[纯合成 Web 闭环](docs/records/44-Day4Web前端本机闭环实测.md)及[真实历史材料到离线替身、报告和预警的本机补验](docs/records/46-Day4任务报告与预警补验.md)，包含四级规则和两渠道 dry_run 预览。任务使用 `offline_dynamic_substitute`，模型和外部网络调用账本均为 0。报告绑定同一案例、图谱和运行；历史材料不能与 Day 3 合成规模账本拼接。G4 尚缺正式真实任务时效、实际通知及完整域内运行证据。

[Docker 本机复验](docs/records/45-Day4Docker本机运行验收.md)已成功构建并启动 API、Web 和入口容器，完成纯合成 10×3 任务及重启持久化检查。API/Web 无默认公网路由，入口容器仍有默认路由，完整容器组出口隔离未通过。

Day 5 [离线评测](docs/records/day5-evaluation.md)和[证据核查](docs/records/day5-delivery-audit.md)已实现，[部署、接口、使用、测试四类文档](docs/delivery/README.md)已交付。当前只有 1 件真实事件和 1 条可计分情感记录，正式指标与 G5 尚未通过。最新主 Agent 全量复跑为 **154 passed、1 warning**，见[文档同步记录](docs/records/47-Day5工程文档同步与复核.md)。

## 本地开发

要求 Python 3.12 与 uv。以下为本地开发命令；运行证据按批次记录，不自动触发收费模型或真实通知。

~~~powershell
uv sync --locked
uv run pytest -q
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000
~~~

工作台另开终端：uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true。停止时在各终端按 Ctrl+C。默认 SQLite 数据库位于被 Git 忽略的 runtime/riskshield.db。模型探测需要显式配置环境与预算，不作为例行启动步骤。

工程文件：src/riskshield/ 为 API、仿真核心、契约、存储、受限采集/证据图与模型网关；ui/ 为工作台；tests/ 含校验与纯合成案例样例；experiments/ 放置未进入正式安装包的百炼合成实验及消息配对审计入口；data/public/ 为脱敏事件材料，data/research/ 为来源核查。历史案例导入不等于五平台在线接入或正式指标达标。

Docker 可按[部署文档](docs/delivery/deployment.md)启动；默认入口端口为 8000/8501，已验收的独立项目 `riskshieldaccept` 使用 18000/18501。API 的 `/day4/historical/jobs` 可启动已有真实历史材料的离线替身任务；`/demos/day4/run` 可准备独立合成演示，`/jobs` 启动并读取本机离线任务，`/reports`、`/alerts/assess` 与 `/alerts/{alert_id}/previews` 处理报告、预警和离线通知预览；这些 Day 4 演示接口不会调用云端模型或发送实际通知。
