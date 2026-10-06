# 风控盾 · 多智能体保险舆情监测

[当前赛题](docs/sources/2026-09-30-新版命题-多智能体仿真舆情监测.md)要求公开多源监测、情感识别、GraphRAG、独立人设与长期记忆、动态社会仿真、五模块报告和四级预警。[文档索引](docs/README.md)给出设计、计划和证据入口。

用户已授权 Day 3，尚未授权 Day 4—5。Day 1/G1 与 Day 2/G2 已内部受限通过；当前实现包括案例导入与 cutoff 过滤、受限来源采集、证据图检索，以及 Day 3 的独立人设、局部状态、按 run/agent 隔离的持久记忆和同步动态仿真引擎。测试替身已完成 500 Agent × 30 轮工程验证，另有 DeepSeek 纯合成 10 Agent × 3 轮和百炼 `qwen-turbo` 1 Agent × 1 轮受限运行。百炼跨轮输入上界、费用预留和可重复运行入口已完成离线修复；未追加云端请求。真实模型完整规模和行为因果仍未验证，G3 未通过。当前状态见[跨轮上界与验证准备](docs/records/30-Day3跨轮上界与百炼验证准备.md)。

## 本地开发

要求 Python 3.12 与 uv。以下命令仅为开发说明，本轮文档清理未运行服务或安装依赖。

~~~powershell
uv sync --locked
uv run pytest -q
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000
~~~

工作台另开终端：uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true。停止时在各终端按 Ctrl+C。默认 SQLite 数据库位于被 Git 忽略的 runtime/riskshield.db。模型探测需要显式配置环境与预算，不作为例行启动步骤。

工程文件：src/riskshield/ 为 API、契约、存储、受限采集/证据图与模型网关；ui/ 为工作台；data/public/ 为脱敏事件材料，data/research/ 为来源核查；tests/ 为校验。历史案例导入不等于五平台在线接入或正式指标达标。
