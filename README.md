# 风控盾 · 多智能体保险舆情监测

[当前赛题](docs/sources/2026-09-30-新版命题-多智能体仿真舆情监测.md)要求公开多源监测、情感识别、GraphRAG、独立人设与长期记忆、动态社会仿真、五模块报告和四级预警。[文档索引](docs/README.md)给出设计、计划和证据入口。

用户已授权启动 Day 2，Day 3—5 尚未授权。已有 FastAPI/SQLite/Streamlit 骨架、案例导入与 cutoff 过滤及一次 DeepSeek 纯合成内容探测。黑猫原投诉缺历史匿名公开可见时间证据，仍是零输入候选；改用有同期独立抓取证据的 UnitedHealth Group 真实事件完成 G1-V2 复核。Day 2 已新增受限 SEC/帖子存档采集、去重、标签录入接口和证据图检索；一次真实运行及未完成的 G2 条件见[初测记录](docs/records/14-Day2采集与图检索初测.md)。这不代表五平台在线接通、人工标签、语义识别或正式指标达标。

## 本地开发

要求 Python 3.12 与 uv。以下命令仅为开发说明，本轮文档清理未运行服务或安装依赖。

~~~powershell
uv sync --locked
uv run pytest -q
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000
~~~

工作台另开终端：uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true。停止时在各终端按 Ctrl+C。默认 SQLite 数据库位于被 Git 忽略的 runtime/riskshield.db。模型探测需要显式配置环境与预算，不作为例行启动步骤。

工程文件：src/riskshield/ 为 API、契约、存储、受限采集/证据图与模型网关；ui/ 为工作台；data/public/ 为脱敏事件材料，data/research/ 为来源核查；tests/ 为校验。历史案例导入不等于五平台在线接入或正式指标达标。
