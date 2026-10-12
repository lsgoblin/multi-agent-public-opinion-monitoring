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

解压源码后，在项目根目录打开两个 PowerShell 窗口。首次准备环境：

```powershell
uv sync --locked --dev
```

窗口一启动 API：

```powershell
uv run uvicorn riskshield.api:create_app --factory --host 127.0.0.1 --port 8000
```

窗口二启动 Web，并让本机代理绕过 loopback：

```powershell
$env:RISKSHIELD_API_URL = 'http://127.0.0.1:8000'
$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = $env:NO_PROXY
uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

然后打开 `http://127.0.0.1:8501/`。Web 启动时会请求 `/health`、`/readiness`、`/cases` 和 `/channels/status`；API 不可用时页面会提示先启动 API。

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

## 已验证范围

此前本机 Docker 验证包括构建、健康检查、Web 到 API、离线案例任务、五模块报告、预警以及停止后再次启动的数据持久化。入口容器仍有默认路由，完整容器出口隔离、真实通知和目标域内部署均未验证。详细运行证据保留在本机忽略目录及既有 Git 历史中；当前源码提交不包含这些产物。验收结论见[测试与运行报告](test-report.md)。

### 2026-10-12 当前源码复验

在源码提交 `303cc0cbfa7607375c1e90bb9ee975146514d776` 上，使用 Docker Desktop 4.79.0、Engine 29.5.3 和独立 Compose 项目 `riskshield-alerting` 成功构建镜像 `riskshield:day4-local`（镜像 ID `sha256:83d8fa12111d28c554224bb853fb5d5b3a6e4ee4402a26df8fbef0e2e1292616`）。API `/health` 返回成功，Web 首页返回 HTTP 200。通过 API 导入仓库内固定历史案例并执行 10 Agent × 3 轮离线任务，30/30 决策有效；执行模式为 `offline_dynamic_substitute`，应用账本为 0 次模型调用、0 次网络请求。任务生成五模块报告和蓝色暂定预警；企微、邮件均仅生成 `dry_run` 预览。停止再启动 Compose 服务后，任务、报告、预警 JSON 内容的 SHA-256 均保持一致。以上结果只证明本机工程闭环，不代表真实模型、真实通知或正式业务指标达标。

该提交的 Compose 网络观测结果如下：API 与 Web 只连接 `internal: true` 的 default 网络，容器没有默认路由；从二者向 `1.1.1.1:443` 的 TCP 连接不可达，`example.com` 的外部 DNS 查询失败。`local_gateway` 另接未设为 internal 的 `host_ingress`，容器有默认路由；同一 TCP 探测成功，外部 DNS 查询也成功，因此当前配置**允许入口网关出站**。

两个一次性网络试验没有得到可采用的 Compose-only 修复：普通桥接网络设置 `com.docker.network.bridge.enable_ip_masquerade=false` 后，外部 TCP 与 DNS 仍成功；改为仅连接 `internal: true` 网络时，已发布的 loopback 端口没有映射，HTTP 无法访问，因此不能直接用它替换当前入口网络。当前配置保留本机入口功能，同时明确记录网关可出站；本次探测不等于完整抓包或主机防火墙审计，不能据此声称整个容器组数据不出域。
