# 风控盾 · 多智能体舆情监测与投诉预警

远程仓库：[https://github.com/lsgoblin/multi-agent-public-opinion-monitoring](https://github.com/lsgoblin/multi-agent-public-opinion-monitoring)

[当前命题](docs/sources/2026-09-30-新版命题-多智能体仿真舆情监测.md)要求公开多源监测、情感识别、截止前证据图、动态社会仿真、五模块报告和四级预警。面向提交要求的完整方案见[命题解题思路](docs/solution-approach.md)；操作与接口见[本机交付文档](docs/delivery/README.md)。

## 当前结果

**真实历史事件已有可计算的实测结果，两项口径不可混用。**

- **事件级（命题口径）**：情感 **10 件**事件三分类 7/10 = **70.00%**、负面识别召回 6/6 = **100%**、精确 6/9 = 66.67%；走势方向 **10 件**事件二分类 3/6 = **50.00%**（三分类诊断 3/10）。事件数达到命题下限 10 件。四项目标中**仅负面识别召回达标**。
- **记录级**：94 条真实归档记录三分类 79/94 = 84.04%、负面召回 33/37 = 89.19%、负面精确 33/41 = 80.49%。**这 94 条不是 94 件事件**（具名事件 6 件、个人投诉贴 12 条、关键词抽样贴 76 条），不得当作事件的成绩。
- 结果、逐单位明细、单件证据卡、裁决记录与复算脚本见[真实历史事件评测证据](docs/delivery/event-eval-20261014/README.md)；完整验收清单与未达标项见[测试与运行报告](docs/delivery/test-report.md)。
- 合成事件的真实模型规模批次为 **500 Agent × 30 轮、15,000/15,000 有效决策**（引擎阶段 931.709 秒、估算 ¥2.88427524、逐项对账 270/270）；此前 3,477/15,000 的 `partial` 保留为失败记录。合成事件**不能**充当真实历史事件回测。
- 本机固定案例的 10 Agent × 3 轮端到端复核为离线动态替身，30/30 决策有效，生成五模块报告与蓝色暂定预警；企微/邮件仅为 `dry_run` 预览。
- 系统在两件任务上都偏向单边输出（情感 10 件中 9 件判负面；方向 11 次预测中 0 次判稳定），这是事件级分数低于记录级的主因。
- 仍未验证：五平台在线覆盖、≤15 分钟采集延迟、真实历史事件上的 500×30、事件输入至报告的模型 SLA、真实通知送达与红警时效、域内运行与数据不出域。
- 自然语言追问已实现独立的报告追问接口，报告/来源/cutoff 绑定与离线契约已测试；**真实多轮问答未通过**（分支 3 次真实调用 0 个有效答案、2 次 HTTP 429），该功能默认关闭。

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

## 测试

```powershell
uv run --locked pytest -q -p no:cacheprovider
```

当前 **594 passed, 1 warning**。仓库内不含案例数据库等本机忽略材料，因此 clone 后需要按[用户操作指南](docs/delivery/user-guide.md)导入或准备案例，才能跑完整演示链路。

## 代码入口

| 职责 | 模块 |
| --- | --- |
| 来源采集、截止快照、证据图 | `src/riskshield/evidence.py` |
| 情感与来源探测 | `src/riskshield/sentiment.py`、`source_probe.py` |
| 动态仿真 | `src/riskshield/simulation.py` |
| 报告、追问、预警、任务、离线评测 | `reporting.py`、`questions.py`、`alerts.py`、`tasks.py`、`evaluation.py` |

提交的生产模块均按功能命名。为保持已有本机数据兼容，HTTP 路由、配置键、SQLite 表与评测 JSON schema 未随文件名调整。

## 交付与验证

- [四类交付文档](docs/delivery/README.md)
- [测试与运行报告](docs/delivery/test-report.md)
- [真实历史事件评测证据](docs/delivery/event-eval-20261014/README.md)

最终提交以远程仓库 `main` 分支的源码为准；从 GitHub 下载 ZIP 仅是获取该源码的一种方式。运行证据、案例数据库和评测原始材料保留在本机的忽略目录中，不属于源码提交，也不是应用启动所需文件。

达标结论以[测试与运行报告](docs/delivery/test-report.md)的命题验收清单为准；旧五日计划自设的 G1—G5 内部检查点已不作为验收口径。量化指标中仅负面识别召回率达标，事件数达到命题下限，其余以清单逐项证据为准。
