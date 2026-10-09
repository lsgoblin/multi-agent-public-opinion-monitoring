# 风控盾 · 多智能体舆情监测与投诉预警

远程仓库：[https://github.com/lsgoblin/multi-agent-public-opinion-monitoring](https://github.com/lsgoblin/multi-agent-public-opinion-monitoring)

[当前命题](docs/sources/2026-09-30-新版命题-多智能体仿真舆情监测.md)要求公开多源监测、情感识别、截止前证据图、动态社会仿真、五模块报告和四级预警。面向提交要求的完整方案见[命题解题思路](docs/solution-approach.md)；操作与接口见[本机交付文档](docs/delivery/README.md)。

## 当前结果

- A 的最新纯合成 500 Agent × 30 轮批次为 `partial`：3,477/15,000 次有效决策，1 次 timeout 后停止；输入到 partial 报告为 591.206 秒。完整规模及 ≤60 分钟链路尚未达标，G3 未通过。
- C 的 v24 最终评分快照显示正式走势与情感分母均为 0。5 个一致的人工双标标题没有配对 E 情感预测；AI 标注只作探索参考。指标不可计算，G5 未通过。
- 本机固定案例的 10 Agent × 3 轮端到端复核为离线动态替身，30/30 决策有效，生成五模块报告和蓝色预警；企微/邮件只是 dry-run 预览。它不证明真实模型或实时舆情。
- Docker 与 Web 证据按各自记录范围复核；完整容器出口隔离、真实通知、域内运行、平台覆盖和自然语言问答仍未验证。G4 未通过。

## 本机启动

需要 Python 3.12、uv。API 与 Web 分别在两个 PowerShell 终端运行：

```powershell
uv sync --locked
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000
```

第二个终端运行：

```powershell
$env:RISKSHIELD_API_URL = 'http://127.0.0.1:8000'
$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = $env:NO_PROXY
uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

打开 `http://127.0.0.1:8501/`。离线任务不会调用云模型或发送通知。Docker 启动、网络边界及持久化命令见[部署与运行](docs/delivery/deployment.md)。

## 代码入口

| 职责 | 模块 |
| --- | --- |
| 来源采集、截止快照、证据图 | `src/riskshield/evidence.py` |
| 情感与来源探测 | `src/riskshield/sentiment.py`、`source_probe.py` |
| 动态仿真 | `src/riskshield/simulation.py` |
| 报告、预警、任务、离线评测 | `reporting.py`、`alerts.py`、`tasks.py`、`evaluation.py` |

提交的生产模块均按功能命名。为保持已有本机数据兼容，HTTP 路由、配置键、SQLite 表与评测 JSON schema 未随文件名调整。

## 交付与验证

- [四类交付文档](docs/delivery/README.md)
- [测试与运行报告](docs/delivery/test-report.md)

最终提交以远程仓库 `main` 分支的源码为准；从 GitHub 下载 ZIP 仅是获取该源码的一种方式。实验脚本、运行证据和评测材料保留在本机的忽略目录中，不属于源码提交，也不是应用启动所需文件。

G3、G4、G5 均尚未通过；本地演示能力不构成正式业务指标达标。
