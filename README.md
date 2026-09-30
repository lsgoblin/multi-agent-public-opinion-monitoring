# 风控盾 · Day 1

> 2026-09-30：主办方内部数据暂不可用，当前数据路线调整为公开投诉与舆情自采，详见 [获取方案与统计口径](./09-公开投诉自采方案与口径变更.md)。本次仅修订文档；未新增采集器或投诉数据，现有运行能力不变。

当前交付是本地工程骨架、历史资料契约与就绪检查，**尚未实现 NLP、多智能体仿真、投诉预测或预警**。用户只授权执行 Day 1；完成本轮后停止，不继续 Day 2。

## 当前功能

- FastAPI 接口、SQLite 持久化和 Streamlit 资料工作台。
- 公开案例包校验与幂等导入，同 ID 不同内容拒绝覆盖。
- 按历史 cutoff 过滤输入，后续资料仅在评测接口展示；未知可知时间不进入输入。
- 数据模式、历史快照完整性、采集方式和来源分别保留。
- 日投诉、Agent 动作的结构化契约；缺失投诉不填零。
- 可选的兼容 Chat Completions 接入探测；无模型时明确阻塞，没有模拟成功响应。

首例为“2024 年 3 月众安保险营销骚扰相关报道”，目前 3 条人工整理的公开报道摘要，属于 **news 来源**，不是指定五渠道已采集完成。没有真实逐日投诉，也没有完整历史真值；不能计算 KR2/KR3。详见 [Day 1 验收](./06-Day1执行与验收记录.md)、[数据资料](./07-Day1数据来源与案例审计.md)和[模型方案](./08-Day1模型接入方案.md)。

## 本地启动（PowerShell）

环境：Python 3.12、uv。依赖锁定在 `uv.lock`，仅安装到项目 `.venv`。

```powershell
uv sync --locked
uv run python -m riskshield.cli import-case data/public/za_marketing_2024.json
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000
```

另开一个终端：

```powershell
uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
```

- 工作台：[http://127.0.0.1:8501](http://127.0.0.1:8501)
- 接口文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- 两个终端分别按 Ctrl+C 停止。应用默认仅监听本机，不是生产共享部署。
- SQLite 位于被 Git 忽略的 `runtime/riskshield.db`。删除或覆盖数据不是日常启动步骤。

也可先启动空库，再在“历史案例”页点击“导入随附公开案例”。再次导入不会重复入库。其他案例按 `GET /contracts` 的 schema 准备，再通过 CLI 或 `POST /cases` 导入。公开样本不含消费者姓名、电话、账号或完整投诉原文；未经确认的内部数据不得放入可提交目录。

## 验证

```powershell
uv run pytest -q
uv run python -m riskshield.cli model-status
uv run python -m riskshield.cli probe-model
```

当前没有模型配置，最后一条应返回 `blocked`，退出码 2。它不是成功的模型试验。单元测试中的响应替身只检查契约与错误处理，不计真实模型或动态仿真证据。

## 工程边界

| 位置 | 用途 |
| --- | --- |
| `src/riskshield/schemas.py` | 时间、来源、案例、日投诉和动作契约 |
| `src/riskshield/store.py` | SQLite 导入、版本冲突与截止前快照 |
| `src/riskshield/api.py` | 本地 API、来源/资源就绪情况 |
| `src/riskshield/model_gateway.py` | 仅含合成内容的显式模型探测 |
| `ui/app.py` | 可操作的 Day 1 资料工作台 |
| `data/public/` | 公开来源的短摘要与溯源信息 |
| `data/research/` | 人工来源核查，不冒充自动采集日志 |
| `tests/` | 时间泄漏、幂等、契约、模型失败等测试 |

独立 worker、自动采集、NLP、动态 Agent 循环、预测训练与告警属于后续天数，当前不建立返回固定结果的替代实现。
