# 部署与运行

## 范围与状态

本文给出当前工程的本机运行方式。Docker 配置提供 API、Web、本机入口容器及 SQLite 持久化卷。2026-10-09 实机复验使用当前共享工作树源码完成无缓存构建、启动、固定历史案例离线任务、Web 报告/预警访问及重启持久化核对。入口容器的完整公网出口限制仍未通过。G3、G4、G5 均尚未通过，当前只记录本机工程运行。

实际实现见 [Dockerfile](../../Dockerfile)、[Compose 配置](../../compose.yaml)、[依赖配置](../../pyproject.toml)、[API](../../src/riskshield/api.py) 和 [Web](../../ui/app.py)。

## 环境

- Python：`3.12.x`（[pyproject.toml](../../pyproject.toml) 限定 `>=3.12,<3.13`）。
- 主机开发运行：需要 `uv`，用于安装项目依赖和运行 Uvicorn、Streamlit。开发测试依赖由 `uv sync --dev` 安装。
- Docker 路径：需要 Docker Desktop 的 Linux Engine 和 Docker Compose。首次构建需拉取官方 `python:3.12-slim` 并下载 Python 依赖；2026-10-08 已构建成功。构建联网与应用运行隔离分别验收，未验证全新环境完全离线构建。
- API 和 Web 默认仅发布到 loopback。主机侧页面地址为 `http://127.0.0.1:8501/`，API 地址为 `http://127.0.0.1:8000/`；OpenAPI 页面为 `http://127.0.0.1:8000/docs`。
- Docker 构建上下文排除 `.env`、虚拟环境、评测集和运行产物；镜像只复制源码、Web、公开案例目录及渠道状态文件。根目录 `.env.example` 仅有空凭证字段和配置说明，不含真实密钥。

## 主机启动

在项目根目录打开两个 PowerShell 窗口。首次准备环境：

```powershell
Set-Location 'D:\test\多智能体仿真舆情监测与投诉预警'
uv sync --dev
```

窗口一启动 API：

```powershell
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000
```

窗口二启动 Web，并让本机代理绕过 loopback：

```powershell
Set-Location 'D:\test\多智能体仿真舆情监测与投诉预警'
$env:RISKSHIELD_API_URL = 'http://127.0.0.1:8000'
$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = $env:NO_PROXY
uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

然后打开 `http://127.0.0.1:8501/`。Web 启动时会请求 `/health`、`/readiness`、`/cases` 和 `/channels/status`；API 不可用时页面会提示先启动 API。历史 Web 闭环记录使用上述命令与 loopback 绕过设置，见 [44 号记录](../records/44-Day4Web前端本机闭环实测.md)。

### 主机配置与数据

- API 使用 `RISKSHIELD_DB` 指定 SQLite 文件路径；未设置时使用项目根目录下 `runtime/riskshield.db`。
- `RISKSHIELD_API_URL` 由 Web 读取，默认 `http://127.0.0.1:8000`。
- 当前本机离线替身任务不需要模型密钥。不要为运行本机离线任务调用 `/models/probe`。
- SQLite 文件保存案例、截止快照、图、仿真账本、任务、报告、预警与预览记录。确保该路径所在目录可写。
- 若要显式指定数据库路径，在启动 API 的 PowerShell 窗口先设置 `$env:RISKSHIELD_DB = 'D:\riskshield-data\riskshield.db'`。更改数据库路径会连接到另一份数据库，不会自动迁移原数据。

## Docker Compose 启动

在项目根目录的 PowerShell 窗口设置本机端口，并使用独立项目名启动。项目名会隔离容器、网络和命名 SQLite 卷；选择本机未占用端口：

```powershell
$env:RISKSHIELD_HOST_API_PORT = '28000'
$env:RISKSHIELD_HOST_WEB_PORT = '28501'
docker compose -p riskshield-demo config --quiet
docker compose -p riskshield-demo up -d --build
docker compose -p riskshield-demo ps -a
Invoke-RestMethod http://127.0.0.1:28000/health
Start-Process 'http://127.0.0.1:28501/'
```

成功运行后，Web 地址为 `http://127.0.0.1:28501/`，API 健康检查为 `http://127.0.0.1:28000/health`。在启动 Compose 的同一 PowerShell 窗口停止服务：

```powershell
docker compose -p riskshield-demo stop
```

API、Web、local_gateway 使用同一镜像。Web 通过内部网络服务名 `api` 请求 `http://api:8000`；API/Web 不直接发布主机端口。local_gateway 在内部网络和 host_ingress 网络间转发入站连接，发布端口仅绑定 `127.0.0.1`。默认内部网络为 `internal: true`，host_ingress 为普通网络。

配置使用方式：

- API 容器设置 `RISKSHIELD_DB=/app/runtime/riskshield.db`。
- 命名卷 `riskshield_runtime` 挂载到 `/app/runtime`，用于保留 SQLite 数据。
- Web 容器设置 `RISKSHIELD_API_URL=http://api:8000` 和 `STREAMLIT_BROWSER_GATHER_USAGE_STATS=false`。
- 容器内服务监听 `0.0.0.0` 以供 Compose 网络使用；主机发布仍限制在 `127.0.0.1`。
- `RISKSHIELD_HOST_API_PORT` / `RISKSHIELD_HOST_WEB_PORT` 控制入口主机端口，默认 8000/8501。
- local_gateway 连接内部网络和 host_ingress；当前实测 API/Web 无默认路由，但入口有默认路由，不能承诺全容器无外发。
- Dockerfile 使用 `pip install --no-cache-dir -e .`，使 `PROJECT_ROOT` 定位到 `/app`，可读取随镜像复制的渠道状态文件。
- Dockerfile 当前按 `pyproject.toml` 的版本范围安装依赖，构建不读取 `uv.lock`；锁文件仍随源码包提供给主机侧 `uv` 使用。2026-10-09 的镜像构建已成功，但未来依赖解析可能变化。

## 停止、重启和持久化

Docker 停止服务：

```powershell
docker compose -p riskshield-demo stop
```

再次启动已创建的服务：

```powershell
docker compose -p riskshield-demo start
```

重建/重启服务：

```powershell
docker compose -p riskshield-demo up -d --build
```

移除容器和网络但保留命名卷：

```powershell
docker compose -p riskshield-demo down
```

**不要运行 `docker compose down -v`，该选项会删除命名卷及其中数据。** 主机运行可在 API、Web 各自终端按 `Ctrl+C` 停止，再按启动步骤重开。

实机复验的命名卷为 `riskshieldaccept_riskshield_runtime`。重启 API、Web、local_gateway 后，同一任务、报告和预警 JSON 内容相等，健康检查和 Web HTTP 均成功；该证据覆盖已完成任务跨服务重启，未测删除卷、灾难恢复或运行中任务续跑。任务执行中若 API 进程重启，代码会将仍处于 `queued`/`running` 的任务记录标记为 `failed`，失败类别 `interrupted_on_restart`；不能假定任务会自动续跑。

## 故障排查

| 现象 | 检查与处理 |
| --- | --- |
| Web 显示后端尚未就绪 | 确认 API 已启动；访问 `/health`；确认 `RISKSHIELD_API_URL` 指向正确地址。若本机代理拦截 loopback，按主机启动步骤为 Web 进程设置 `NO_PROXY`/`no_proxy`。 |
| API 无法写入数据库 | 检查 `RISKSHIELD_DB` 指向的目录是否存在且当前用户有写权限；确认没有把 Web 的 `RISKSHIELD_API_URL` 当成数据库配置。 |
| `docker compose config --quiet` 失败 | 检查 Docker Compose 版本和当前目录是否为包含 `compose.yaml` 的项目根目录。 |
| Docker CLI 无法连接 Engine | 在 Docker Desktop 启动 Linux Engine，再检查 `docker version` 的 Server 部分和 `docker info`；客户端版本输出不足以证明 Engine 已就绪。 |
| Engine 可用但构建失败 | 检查 Docker Hub 对 `auth.docker.io:443` 的访问与基础镜像拉取错误。2026-10-07 的复测正是在获取 `python:3.12-slim` token 时超时；未调整代理、DNS 或防火墙。 |
| 页面可开但任务失败 | 在“处置队列”刷新进度，查看受限错误类别；核对案例、图 ID、数据模式、cutoff、来源版本和可见时间。任务失败不会提供异常堆栈给页面。 |

## 已实测项目与网络边界

[45 号记录](../records/45-Day4Docker本机运行验收.md)保留 2026-10-07 的 Engine 与基础镜像拉取失败；2026-10-08 已成功拉取、构建并修复安装路径。独立本机项目 `riskshieldaccept` 的镜像 ID 为 `sha256:3b548822d8b66783b687b8e6f33db3909a0225e82171f4a065b1b46dadd15ab5`，API healthy，Web 与入口容器运行。原有项目容器和卷保留。

该批采用独立端口，复现命令为：

```powershell
$env:RISKSHIELD_HOST_API_PORT = '18000'
$env:RISKSHIELD_HOST_WEB_PORT = '18501'
docker compose -p riskshieldaccept up -d --build
docker compose -p riskshieldaccept ps -a
```

此命令说明复验配置，并非建议覆盖正在运行的验收实例。该项目 API 为 `http://127.0.0.1:18000/`，Web 为 `http://127.0.0.1:18501/`；主机进程和默认 Compose 项目的 8000/8501 不应混记为该批证据。

| 验证项 | 当前结果 |
| --- | --- |
| 构建、健康检查、Web 页面、Web→API | 实测通过；`/channels/status` 亦返回 200 |
| 纯合成 10 Agent × 3 轮 | 完成；run_id 为 `run_f7df9bc466904c0c8461`，模式 `offline_dynamic_substitute`，调用账本为 0 |
| 自动报告与预警 | 五模块报告和蓝色预警生成 |
| 服务重启后的持久化 | 同一任务、报告、预警 JSON 相等，API/Web 仍可访问 |
| API/Web 直接公网出口 | 无默认路由；直连 `1.1.1.1:443` 返回 `errno=101` |
| 完整容器组出口隔离 | 未通过；local_gateway 有默认路由，未取得全程抓包/完整出口限制证据 |
| 目标域内部署、域内真实模型、真实通知 | 未验证 |

主要证据：[容器与路由](../../artifacts/day4-docker-acceptance/corrected-run/runtime-cli.txt)、[任务摘要](../../artifacts/day4-docker-acceptance/corrected-run/run-summary.json)、[重启核对](../../artifacts/day4-docker-acceptance/corrected-run/restart-summary.json)、[Web 实图](../../artifacts/day4-docker-acceptance/corrected-run/web-task-overview.png)。完整 Docker 验收仍需入口容器出口限制及网络观测；G4 不因功能项完成而通过。

## 2026-10-09 当前源码验收

无缓存构建镜像 `riskshield:day4-local`，ID 为 `sha256:fc534eac7b89f206ce81e045012fa4bb01f9cf781ec65ac91560b91c4890eea8`，大小 173.20 MiB；容器中的 15 个 Python 源文件与构建时工作树哈希一致。验收项目 `riskshieldagentdfinalutf8` 使用 `127.0.0.1:28002` / `127.0.0.1:28503` 和独立临时目录 `artifacts/final-deployment-review/runtime-final`。该目录绑定只用于本轮验收，普通启动使用项目名隔离的 Compose 命名卷。

固定案例 `unh_change_20240222_day2_multisource` 的源文件以 UTF-8 读取并校验 SHA-256；验收库中已存在该案例，脚本核对标题后直接创建任务，没有重复导入。任务为真实历史材料加 `offline_dynamic_substitute`，10 Agent × 3 轮、30/30 有效决策，`model_calls=0`、`network_requests=0`。报告五模块、蓝色暂定预警均绑定同一 case/graph/run；企微与邮件状态为 `dry_run`、`sent_at=null`。Web 实际打开任务队列、五模块报告和预警中心；先正常停止、再启动 API、Web、入口容器后，同一案例、任务、报告、预警的 HTTP 原始 JSON SHA-256 均保持一致，健康检查和 Web 首页为 200。

本轮最新镜像、代码哈希、任务和重启摘要见[版本清单](../../artifacts/final-deployment-review/version-manifest.json)、[案例闭环](../../artifacts/final-deployment-review/case-closure-summary.json)、[持久化复核](../../artifacts/final-deployment-review/restart-persistence-summary.json)及[构建日志](../../artifacts/final-deployment-review/build.log)。正式真实模型、域内模型、真实通知和全容器出口隔离仍未验证；离线替身成功不代表 G3/G4/G5 通过。源码尚未由 E 最终冻结，因此本轮不生成最终提交 ZIP；打包清单和冻结后命令见[部署复核记录](../records/final-deployment-review.md)。
