# Day 4 Web 前端本机离线闭环实测

> 2026-10-07。记录一次获准的本机纯合成 UI 运行。该证据只覆盖离线动态测试替身，不改变 G3 未通过或 G4 待核验结论；没有调用云模型、发送真实通知、部署或提交代码。

## 服务与运行

- API：`uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000`，监听 `127.0.0.1:8000`，进程 PID `54288`，启动于 `2026-10-07 19:06:54 +08:00`。
- Web（PowerShell）：先设置 `$env:RISKSHIELD_API_URL='http://127.0.0.1:8000'`、`$env:NO_PROXY='127.0.0.1,localhost'`、`$env:no_proxy=$env:NO_PROXY`，再运行 `uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true`。监听 `127.0.0.1:8501`，进程 PID `19112`，服务于 `2026-10-07 19:13:22 +08:00` 开始响应。为避免本机会话代理将 loopback 请求转发并返回 502，只对该 Web 进程设置 loopback 绕过项；未改系统代理或项目代码。
- 页面：`http://127.0.0.1:8501/`。点击“准备离线演示任务”，选择新生成运行，再点击“启动本机离线任务”和“刷新任务进度”。

| 字段 | 实测值 |
| --- | --- |
| run_id | `run_ecee1df4cc854342a618` |
| job_id | `job_d1004afbc48946369621` |
| report_id | `report_e7ada35b962f32f785554082` |
| alert_id | `alert_58b5efe79b8843cd1c15237c8698b450` |
| 案例 / 图谱 | `day4_demo_fictional_synthetic_v1` / `day4_demo_fictional_synthetic_graph_v1` |
| 数据模式 / 执行模式 | `synthetic` / `offline_dynamic_substitute` |
| 配置 | 10 Agent × 3 轮，并发 4；本地费用 ¥0 |
| 排队 / 开始 / 完成 | `19:14:33.645259` / `19:14:33.651853` / `19:14:33.741756`（均为 2026-10-07，Asia/Shanghai，`+08:00`） |
| 结果 | 完成 `3/3` 轮；每轮 10/10 个决策有效；`model_calls=0`、`network_requests=0`；没有失败 |

任务用本机动态测试替身执行；Agent 按自身状态、记忆和运行时消息作出后续决策。以上 `network_requests=0` 是任务账本指标，不是操作系统层抓包结论。

## Web 输出核验

| 模块 | 页面实测 |
| --- | --- |
| 传播路径图谱 | 显示 60 条运行时模拟消息、30/30 条模拟关系；界面明确提示消息数量不证明因果影响。 |
| 情绪演化 | 显示 3 轮曲线；由仿真状态转移重建并与最终 Agent 状态核对，不是观测到的公众情绪，也不是持久化的逐轮原始快照。 |
| 关键活跃节点 | 展示 Agent、接收消息数和消息 ID；标注为模拟活动，不表示因果影响。 |
| 风险等级 | 本次合成运行输出蓝色，状态 `provisional_offline_assessment`，规则 `day4-simulated-complaint-share-v1-provisional`；红/橙/黄门槛分别为 40%/20%/5%，尚未按真实事件校准。 |
| 应对建议 | 展示来源、动作/消息引用和审核建议；模拟消息不是平台传播，也不决定 G3/G4。 |

企业微信预览为 `preview_52a2b19a902a426d8c60`，通道 `wecom`，载荷类型 `text`；邮件预览为 `preview_29048c103cfc4a53b056`，主题为 `[OFFLINE PREVIEW] BLUE simulated alert`。两者均为 `delivery_status=dry_run`、`network_requests=0`、无真实收件人、`sent_at=null`；预览正文明确说明未发送。发现时间和真实送达时间未记录，因此没有红色预警 30 分钟送达证据。

本次仅触发蓝色分支；红、橙、黄等级的不同输入分支没有在这次 UI 运行中逐一触发。

## 截图

- 任务完成与运行指标：[01-task-complete.png](../../artifacts/day4-web-closure/01-task-complete.png)
- 报告摘要与 ID：[02-report-overview.png](../../artifacts/day4-web-closure/02-report-overview.png)
- 情绪演化：[03-report-emotion.png](../../artifacts/day4-web-closure/03-report-emotion.png)
- 关键节点与风险等级：[04-report-risk-nodes.png](../../artifacts/day4-web-closure/04-report-risk-nodes.png)
- 应对建议及限制：[05-report-recommendations.png](../../artifacts/day4-web-closure/05-report-recommendations.png)
- 企业微信离线预览：[06-wecom-preview.png](../../artifacts/day4-web-closure/06-wecom-preview.png)
- 邮件离线预览：[07-email-preview.png](../../artifacts/day4-web-closure/07-email-preview.png)
- 四级预警结果及判级依据：[08-alert-blue.png](../../artifacts/day4-web-closure/08-alert-blue.png)

## G4 仍缺证

1. 本次是纯合成替身任务，不是来自真实公开平台或真实历史事件的端到端运行；五平台在线覆盖、采集延迟、真实来源图谱与真实结果回测仍未验证。
2. 红色预警从发现到实际推送 ≤30 分钟、企业微信/邮件真实送达和回执均无证据；本次发现与发送时间为空，通知没有发送。
3. 从真实事件输入到完整报告 ≤60 分钟尚未以真实任务计时证明；本次运行很快完成只说明离线替身，不代表真实任务时效。
4. Docker 目前只有配置校验记录，镜像构建、容器运行及域内无外发仍未实测。
5. G3 仍未通过；本次小规模 Web 任务不证明 500×30 严格消息因果，也不替代多件真实事件的正式指标回测。Day 5 仍未授权。
