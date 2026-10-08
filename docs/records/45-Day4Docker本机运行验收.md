# Day 4 Docker 本机运行验收

> 首次验收编号：`D4-DOCKER-LOCAL-20261007-200641+0800`；首次检查窗口：2026-10-07 20:06–20:14；Engine 复测：20:43。最新运行批次：`D4-DOCKER-LOCAL-20261008-011715+0800`，检查至 01:19（Asia/Shanghai）。下方保留先前失败原文作为历史记录；最新结论以本节及文末“2026-10-08 实机复验”为准。本记录不代表 G4 通过，也不启动 Day 5。

## 最新结论（2026-10-08）

- **Docker 本机运行验收：功能项实测通过，完整出口隔离条件未通过，因此不判完整通过。** 官方 `python:3.12-slim` 拉取与最终镜像构建成功；独立本机 Compose 项目 `riskshieldaccept` 的 API、Web 和本机入口容器均运行，API 健康检查与 Web 页面访问成功，Web→API 通信成功；容器内纯合成 10 Agent × 3 轮离线任务完成，五模块报告和预警自动生成，重启后同一任务与产物内容不变。API/Web 容器内部网络无默认路由，公网 IP 直连返回 `errno=101`；本机入口容器有默认路由，且未做全程抓包，不能声称整个容器组没有任何公网外发。
- **Day 4 Web 离线闭环：纯合成任务与 Web 展示在 Docker 中贯通。** 本批任务由本机 API 发起，Web 真实页面显示完成状态、3/3 轮、零模型调用、零外部网络请求及报告/预警 ID；Web 点击发起任务的证据沿用[44 号主机进程实测](44-Day4Web前端本机闭环实测.md)，不把它混写为本批 Docker 点击证据。
- **G4 未通过；G3 仍为“尚未通过”。** Docker 离线合成任务不替代真实任务、正式时效、通知送达与域内运行验收，G3 的既有缺口不受影响。

## 历史结论（2026-10-07）

- **当时 Docker 本机运行验收未完成。** 首轮检查时 Engine 不可连接；20:43 复测时 Docker Server/Engine 已恢复响应。但实际镜像构建在拉取 `python:3.12-slim` 的 Docker Hub token 请求上超时，当时镜像和本项目容器未创建，健康状态和容器运行 ID 未取得。
- **Day 4 Web 离线闭环：已有主机进程证据。** [44 号记录](44-Day4Web前端本机闭环实测.md)中的 `run_ecee1df4cc854342a618` 等 ID 和截图来自此前直接运行的本机 API/Web，不是本次 Docker 容器运行证据。本次没有生成容器内任务、报告或预警 ID。
- **G4：未通过。G3：仍为“尚未通过”。** 本次未产生真实 Docker 运行证据；G3 的规模运行、消息因果缺口不受本次结果改变。

## 运行与命令证据

| 时间（+08:00） | 检查 | 结果 |
| --- | --- | --- |
| 20:06:41 | 首次 `docker version` | 返回下方 Docker API 命名管道错误。 |
| 20:06:53 | 启动本机 Docker Desktop | Desktop 与 backend 进程出现；服务端始终没有向 Docker CLI 返回可用版本。 |
| 20:07–20:13 | `docker info` / `docker version` 重试 | 等待期间无服务端输出；清理了本轮遗留的阻塞 CLI 进程。 |
| 启动后 | `docker build --progress=plain -t riskshield:day4-local .` | 等待约 45 秒没有构建输出，因 CLI 持续无响应而中断，退出码 1；未取得镜像。 |
| 检查窗口内 | `docker compose config --quiet` | 退出码 0。 |
| 检查窗口内 | Day 4 相关测试 | 28 项通过，1 条既有 Starlette/httpx 弃用警告。 |

首次 Docker 连接的实际错误：

```text
Client:
 Version:           29.5.3
 API version:       1.54
 Go version:        go1.26.4
 Git commit:        d1c06ef
 Built:             Wed Jun  3 18:03:06 2026
 OS/Arch:           windows/amd64
 Context:           desktop-linux
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is correct and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

关键命令输出和最终 Compose 展开片段保存在[CLI 证据](../../artifacts/day4-docker-acceptance/cli-evidence.txt)。本机验收批次编号不是应用 `run_id`；本次没有生成容器 ID、任务 ID、报告 ID 或预警 ID。

## 静态配置与离线逻辑核验

- `docker compose config --quiet` 通过。展开配置显示 API 与 Web 端口只绑定 `127.0.0.1`；Web 依赖 API healthcheck，容器间 URL 为 `http://api:8000`；API 数据库位于 `/app/runtime/riskshield.db`，挂载命名卷 `riskshield_runtime`。
- 原配置使用允许公网出口的默认 Compose 网络。为满足离线运行边界，[compose.yaml](../../compose.yaml) 已将默认网络设为 `internal: true`。展开配置确认该值生效；由于 Docker Engine 无响应，网络隔离尚未以容器路由或抓包实测。
- 运行时任务代码将 `model_calls` 和 `network_requests` 初始化为 0，Day 4 相关测试也断言这些账本值为 0。它们是应用账本与离线逻辑证据，不是本次容器的运行记录，更不是 OS 层网络抓包结果。
- 任务持久化卷路径与重启验证方案已静态确认；由于没有容器，未能创建任务后重启容器，也未验证任务、报告和预警跨重启持久化。

本次执行的相关测试：

```text
uv run pytest -q tests/test_day4_api.py tests/test_day4_tasks.py tests/test_day4_report.py tests/test_day4_alerts.py tests/test_ui.py
28 passed, 1 warning
```

## 访问、截图和验收边界

本次没有 API/Web 容器，因此未请求容器健康检查、未访问 Docker 发布页面、未运行纯合成任务，也未做容器间 HTTP 探测、重启持久化检查或公网抓包。没有可保存的 Docker 页面/容器截图。[44 号记录中的 Web 截图](44-Day4Web前端本机闭环实测.md)仅支持此前主机进程运行，不作为 Docker 截图或容器验收证据。

**后续步骤：**在 Docker Desktop Linux Engine 正常响应后，重新构建镜像并依次验证健康检查、loopback 页面、API/Web 通信、合成任务账本与自动报告/预警；重启 API 容器后核对同一批 ID；最后验证内部网络无公网路由，并保存本轮的容器 ID、应用 ID、命令输出和截图。G3 仍需独立补齐其既有缺口；Day 5 未获授权。

## 后续复测：Engine 恢复，基础镜像拉取受阻

复测批次：`D4-DOCKER-RECOVERY-20261007-2043+0800`。Docker Engine 已恢复：`docker version` 返回 Docker Desktop `4.79.0 (230596)`、Linux Engine `29.5.3`；`docker info` 返回 `Server=29.5.3 OS=Docker Desktop`，退出码 0。`docker ps -a` 可正常读取；既有停止容器 `rcore-tutorial` 未触碰。

随后执行 `docker compose up -d --build`，Compose 开始构建，但无法从 Docker Hub 获取 `python:3.12-slim` 的 token，具体为连接 `auth.docker.io:443` 超时。构建退出码 1；`docker compose ps -a` 无本项目服务，`docker image inspect riskshield:day4-local` 返回 `No such image`。CLI 复测输出和构建错误已追加到[CLI 证据](../../artifacts/day4-docker-acceptance/cli-evidence.txt)。

随后尝试主机侧 `Test-NetConnection` 连通性探测，但约 50 秒没有返回结果而中断；该探测无结论，因此尚未区分 Windows 主机网络与 Docker Desktop/BuildKit 专属网络路径。没有修改代理、DNS 或防火墙设置。

因此该次复测只能确认 **Docker 守护进程已恢复**，当时本机 Docker 运行验收未通过：尚无镜像或容器，健康检查、页面访问、容器间通信、纯合成任务、数据重启持久化和容器公网隔离均未实测。该复测没有生成应用 `run_id`、`job_id`、`report_id` 或 `alert_id`；G4 未通过，G3 仍未通过。

## 2026-10-08 实机复验：镜像、入口与容器运行

用户确认 Docker Desktop 的镜像拉取模式为 System proxy 后，只做只读代理状态核对，没有修改系统代理、DNS 或防火墙。`docker pull python:3.12-slim` 成功，基础镜像 digest 为 `sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f`。首次 `docker compose build --progress=plain api` 成功，证明先前的 Docker Hub token 超时不再复现。**镜像拉取及 Dockerfile 中的 `pip install` 使用构建联网；下述应用运行的离线计数和网络隔离是另一项检查。**

首次启动时 `internal: true` 网络使 API/Web 只有容器内端口：Compose 声明了 `127.0.0.1:8000/8501`，但 `docker inspect` 的 `HostConfig.PortBindings` 有值、`NetworkSettings.Ports` 为空，`docker port` 无输出，宿主机连接被拒绝；Web→API `/health` 则为 200。定位后在[compose.yaml](../../compose.yaml) 增加本机入口容器，连接内部网络与普通入口网络，且只向宿主机 `127.0.0.1` 发布 8000/8501。API/Web 只连接内部网络。原有容器和数据卷均保留。

浏览器首次打开后出现“后端尚未就绪”。API 日志显示 `/channels/status` 为 500，因普通 `pip install .` 把 `riskshield.api` 放在 `/usr/local/lib/python3.12/site-packages`，其 `PROJECT_ROOT` 推导到 `/usr/local/lib/python3.12`，找不到已复制到 `/app/data/research/` 的文件。修改前运行 Day 4 相关测试 **31 passed, 1 warning**，并在容器中验证从 `/app/src` 导入时 `PROJECT_ROOT=/app` 且文件存在。随后仅将[Dockerfile](../../Dockerfile) 改为 `pip install --no-cache-dir -e .`，重建成功；新容器的 `/channels/status` 返回 200，浏览器展示了真实 Web 工作台。旧项目 `riskshield` 的 3 个容器和数据卷仍保留；修正镜像以新项目 `riskshieldaccept` 启动，宿主机端口为 `127.0.0.1:18000` 与 `127.0.0.1:18501`，没有删除或覆盖旧容器与卷。

最终运行容器镜像 ID：`sha256:3b548822d8b66783b687b8e6f33db3909a0225e82171f4a065b1b46dadd15ab5`。API 容器 ID `84d7dbd174fb6cc65025c0dc217a01ecf3597951bca65fe1f4d2b00cbca04d88`，Web ID `e70b71711477baa7374af3c20fec4251b717e0cb403f234aa9fb76ad2f3fbe29`，本机入口 ID `fb70131b778314b60e79039f3d077f6ede46c8eb44ea93e812b02db0035a65d3`。数据卷 `riskshieldaccept_riskshield_runtime` 创建时间 2026-10-08 01:16:16 +08。容器 ID 与启动时间详见[CLI 运行证据](../../artifacts/day4-docker-acceptance/corrected-run/runtime-cli.txt)。

| 最终项目检查 | 实测结果 |
| --- | --- |
| `docker compose -p riskshieldaccept ps -a` | API `healthy`；Web、入口 `Up`；入口端口只发布到 `127.0.0.1:18000/18501`。 |
| `GET http://127.0.0.1:18000/health` | 200，`status=ok`。 |
| `GET /readiness`、`GET /channels/status` | 均可访问；readiness 仍返回 `g3_v2_passed=false`、`g4_v2_passed=false`。 |
| `GET http://127.0.0.1:18501/` 与浏览器 | HTTP 200；浏览器实际呈现队列、进度和自动生成的产物 ID。 |
| Web 容器内 `GET http://api:8000/health` | 200。 |

## 最终项目：纯合成任务、持久化与出口证据

2026-10-08 01:17:15–01:17:17 +08，向新项目 API 提交 `POST /demos/day4/run`，参数 `agent_count=10, rounds=3, concurrency=4`，再 `POST /jobs`。模式为 `data_mode=synthetic`、`execution_mode=offline_dynamic_substitute`；未调用收费模型、未使用真实事件、未发送企微或邮件。

| 标识/结果 | 实测值 |
| --- | --- |
| `run_id` | `run_f7df9bc466904c0c8461` |
| `job_id` | `job_deee04f704c64fa7977f` |
| `report_id` | `report_222f182315709db12836fb3b` |
| `alert_id` | `alert_804cfcc0cf0a0d84da29e5adb1c876d5` |
| 任务 | `complete`，3/3 轮，`model_calls=0`，`network_requests=0` |
| 报告 | `emotion_evolution`、`key_nodes`、`propagation`、`risk`、`recommendations` 五模块自动生成，并绑定同一运行 ID |
| 预警 | `blue`、`provisional_offline_assessment`，绑定同一运行 ID；仅是合成离线判断 |

随后 `docker compose -p riskshieldaccept restart api web local_gateway`，没有删除容器或卷。01:18:27 +08 再次读取**同一** `job_id`、`report_id`、`alert_id`：任务、报告和预警 JSON 内容逐项相等，计数仍为 0；API 健康检查与 Web 页面仍为 200，浏览器重新加载后仍显示该任务。重启前后 JSON、脚本和时间见[最终运行证据目录](../../artifacts/day4-docker-acceptance/corrected-run/)内的 `run-summary.json`、`restart-summary.json`、`completed-job.json`、`report.json`、`alert.json` 及对应 `post-restart-*.json`。

隔离检查区分三层：

1. **应用账本：**本次任务 `model_calls=0`、`network_requests=0`，只说明应用所记录的调用为零。
2. **API/Web 直接公网出口：**`riskshieldaccept_default` 的 `Internal=true`；API/Web 的 `/proc/net/route` 均无默认路由。两容器各以 TCP `connect_ex(('1.1.1.1', 443))` 探测，均返回 Linux `errno=101`（Network is unreachable），同时 Web→API 为 200。这是运行时无法直连该公网 IP 的实测，比应用计数更强，但不是全程流量抓包。
3. **本机入口容器：**`riskshieldaccept_host_ingress` 的 `Internal=false`，入口容器有默认路由；它的代码只把本机入站 TCP 分别转发给 `api:8000` 与 `web:8501`，但无法据此证明整个容器组没有任何公网外发。未实施抓包或出口防火墙规则，**全容器出口隔离尚未通过**。

构建输出摘录见[最终构建日志](../../artifacts/day4-docker-acceptance/corrected-run/build-final.log)；该日志是构建成功后的缓存复跑，首次完整安装下载发生在先前的成功构建。容器日志见[日志](../../artifacts/day4-docker-acceptance/corrected-run/container-logs.txt)。浏览器实图见[任务概览](../../artifacts/day4-docker-acceptance/corrected-run/web-task-overview.png)、[零调用计数](../../artifacts/day4-docker-acceptance/corrected-run/web-task-metrics.png)、[产物 ID](../../artifacts/day4-docker-acceptance/corrected-run/web-task-ids.png)、[重启后队列](../../artifacts/day4-docker-acceptance/corrected-run/web-after-restart.png)。首次错误页保存在[修复前截图](../../artifacts/day4-docker-acceptance/web-before-image-fix.png)。

**下一步：**若完整 Docker 验收要求“所有容器均无公网出口”，须在允许的本机网络机制下为入口容器补充可验证的出口限制，或由主 Agent 明确以 API/Web 应用容器隔离为验收口径；并在合适的网络观测条件下补全全程外发证据。G4 另需真实任务、时效、送达和域内运行证据；G3 仍尚未通过。用户随后已明确授权受限 Day 5 离线评测与交付准备，见[47 号同步记录](47-Day5工程文档同步与复核.md)；该授权不改变本次 Docker 与关口结论。
