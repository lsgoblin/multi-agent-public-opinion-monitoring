# Day 4 Docker 本机运行验收

> 验收编号：`D4-DOCKER-LOCAL-20261007-200641+0800`。首次检查窗口：2026-10-07 20:06–20:14；后续复测：20:43（Asia/Shanghai）。本记录只覆盖本机 Docker 验收尝试，不代表 G4 通过，也不启动 Day 5。

## 结论

- **Docker 本机运行验收：仍未完成，不能判通过。** 首轮检查时 Engine 不可连接；20:43 复测时 Docker Server/Engine 已恢复响应。但实际镜像构建在拉取 `python:3.12-slim` 的 Docker Hub token 请求上超时，镜像和本项目容器仍未创建，健康状态和容器运行 ID 未取得。
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

因此现在可以确认 **Docker 守护进程已恢复**，但本机 Docker 运行验收仍未通过：尚无镜像或容器，健康检查、页面访问、容器间通信、纯合成任务、数据重启持久化和容器公网隔离均未实测。该复测没有生成应用 `run_id`、`job_id`、`report_id` 或 `alert_id`；G4 未通过，G3 仍未通过。
