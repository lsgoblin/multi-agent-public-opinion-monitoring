# 风控盾 · 多智能体保险舆情监测

[当前赛题](docs/sources/2026-09-30-新版命题-多智能体仿真舆情监测.md)要求公开多源监测、情感识别、GraphRAG、独立人设与长期记忆、动态社会仿真、五模块报告和四级预警。[文档索引](docs/README.md)给出设计、计划和证据入口。

用户已授权 Day 4 本机代码工程，尚未授权 Day 5。Day 1/G1 与 Day 2/G2 已内部受限通过；当前实现包括案例导入与 cutoff 过滤、受限来源采集、证据图检索，以及 Day 3 的独立人设、局部状态、按 run/agent 隔离的持久记忆和同步动态仿真引擎。纯合成北京百炼[500 Agent × 30 轮真实规模尝试](docs/records/41-Day3百炼500x30真实规模尝试.md)已执行，15,000 次请求中 14,994 次有效、6 次无效决策；严格消息动作因果仍未证实，**G3 未通过**。当前决策见[Day 3 工程决策](docs/planning/24-Day3工程决策.md)。

Day 4 已增加本机[动态离线任务、五模块报告、暂定四级预警、企微／邮件离线预览、Web 查看和 Docker 配置](docs/records/43-Day4本机工程与离线验证.md)。Web 可一键准备纯合成案例并启动离线动态替身，逐轮查看进度与失败，完成后自动生成报告和预警；该路径的云模型调用与外部网络请求均为 0。离线预览不发送通知。G4 仍待真实任务、计时、送达和域内边界证据复核。Day 2 真实历史图谱与 Day 3 纯合成 500×30 账本属于不同案例，不拼接为同一事件报告。

## 本地开发

要求 Python 3.12 与 uv。以下为本地开发命令；本轮已运行离线测试，未启动对外服务或调用模型。

~~~powershell
uv sync --locked
uv run pytest -q
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000
~~~

工作台另开终端：uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true。停止时在各终端按 Ctrl+C。默认 SQLite 数据库位于被 Git 忽略的 runtime/riskshield.db。模型探测需要显式配置环境与预算，不作为例行启动步骤。

工程文件：src/riskshield/ 为 API、仿真核心、契约、存储、受限采集/证据图与模型网关；ui/ 为工作台；tests/ 含校验与纯合成案例样例；experiments/ 放置未进入正式安装包的百炼合成实验及消息配对审计入口；data/public/ 为脱敏事件材料，data/research/ 为来源核查。历史案例导入不等于五平台在线接入或正式指标达标。

Docker 配置可用 `docker compose config --quiet` 静态校验；本轮未验证镜像构建或容器启动。API 的 `/demos/day4/run` 可准备独立合成演示，`/jobs` 启动并读取本机离线任务，`/reports`、`/alerts/assess` 与 `/alerts/{alert_id}/previews` 处理报告、预警和离线通知预览；这些 Day 4 演示接口不会调用云端模型或发送实际通知。
