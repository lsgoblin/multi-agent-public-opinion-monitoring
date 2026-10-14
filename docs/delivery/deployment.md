# 部署与运行

## 范围与状态

本文给出当前工程的本机运行方式。Docker 配置提供 API、Web、本机入口容器及 SQLite 持久化卷。2026-10-14 已从提交 `e935522` 完成干净镜像构建并在无外部网络的一次性容器内验证 API/Web 启动；运行中的 `riskshield-alerting` 服务仍使用 2026-10-12 的派生镜像。此前的案例、重启持久化与出口结果属于该旧运行版本：三个应用容器的指定公网探测失败，Docker Desktop WSL2 虚拟机的抽样公网探测成功。新镜像尚未切换到现有服务，完整流量审计及目标域部署仍未验证。

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
- local_gateway 连接 internal default 和 host_ingress；启动时移除默认 IPv4 路由、拒绝活动 IPv6 默认路由，随后清空 Linux capabilities 并降为 UID/GID 10001。它使用 loopback DNS 上游，仍可解析 Compose 内的 `api`/`web` 服务名；本机映射端口保持仅绑定 `127.0.0.1`。
- Dockerfile 使用 `pip install --no-cache-dir -e .`，使 `PROJECT_ROOT` 定位到 `/app`，可读取随镜像复制的渠道状态文件。
- Dockerfile 当前按 `pyproject.toml` 的版本范围安装依赖，构建不读取 `uv.lock`；锁文件仍随源码包提供给主机侧 `uv` 使用。2026-10-09 的镜像构建已成功，但未来依赖解析可能变化。

### Windows/Clash 构建专用代理

2026-10-14 的普通构建在 `auth.docker.io` 匿名令牌请求处超时；显式给构建命令设置 Clash 代理后，提交 `e935522` 的 `--pull --no-cache` 构建成功。主机侧命令使用 `127.0.0.1`，构建容器使用 `host.docker.internal`；本机实测 Clash 端口为 7897。端口变化时须同步修改下列参数。

在干净 Git 工作区执行；镜像标签和 OCI revision 从当前提交生成。该命令只构建镜像，构建阶段不会挂载业务数据库。

```powershell
$revision = (git rev-parse HEAD).Trim()
$image = "riskshield:source-$($revision.Substring(0, 12))"
$oldHttp = $env:HTTP_PROXY
$oldHttps = $env:HTTPS_PROXY
$oldAll = $env:ALL_PROXY
try {
    $env:HTTP_PROXY = 'http://127.0.0.1:7897'
    $env:HTTPS_PROXY = 'http://127.0.0.1:7897'
    $env:ALL_PROXY = 'http://127.0.0.1:7897'
    docker build --pull --no-cache --progress=plain `
        --build-arg HTTP_PROXY=http://host.docker.internal:7897 `
        --build-arg HTTPS_PROXY=http://host.docker.internal:7897 `
        --label "org.opencontainers.image.revision=$revision" -t $image .
    $buildExit = $LASTEXITCODE
} finally {
    $env:HTTP_PROXY = $oldHttp
    $env:HTTPS_PROXY = $oldHttps
    $env:ALL_PROXY = $oldAll
}
if ($buildExit -ne 0) { throw 'Docker 构建失败' }
docker image inspect $image --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

构建参数不作为运行容器的代理配置。实测镜像运行环境没有 HTTP/HTTPS/ALL proxy 变量；后续镜像仍应复核。每次交接记录镜像 digest、源码提交、基础镜像 digest、配置哈希、复制的本机渠道文件哈希及实际依赖版本；源码提交本身不能代替未跟踪数据的版本，也不能保证未来按依赖范围重建得到相同字节。切换运行服务需要以固定 digest 覆盖三个服务的 image，并保留同一 Compose 项目、数据卷和入口端口；单独构建成功不会自动更新现有容器。

### 预警完整性与通知记录

本分支预警保留 `provisional_offline_assessment` 状态，并提供 `run_complete`、`decisions_all_valid`、`alert_is_success_result` 和 `run_action_counts`。只有完成全部配置轮次、对应动作数齐全且全部有效时，`alert_is_success_result` 才为 true；该标志描述模拟运行完整性。部分运行仍可对已完成轮次的有效决策暂定判级，缺少有效决策时拒绝判级。`failed` 单独计为失败；`deadline_exceeded` 与 `not_dispatched` 计为未决请求。未知动作状态不能被视为完整成功。

企微/邮件预览仍为 `dry_run`、`network_requests=0`，`sent_at`、`delivered_at`、`receipt_at` 均为 null，`receipt_status=not_requested`。不完整或缺少完整性字段的旧预警，其预览正文明确标记 `INCOMPLETE OR UNVERIFIED RUN`；缺少字段不推断为成功。本机规则评估至预览生成的耗时不代表真实发送、送达回执或红警 SLA。上述字段描述本分支源码，现有旧镜像不会自动具备新增行为；共享任务、报告和 UI 状态由总 Agent 集成复验。

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

此前本机 Docker 验证包括构建、健康检查、Web 到 API、离线案例任务、五模块报告、预警以及停止后再次启动的数据持久化。2026-10-12 的更新增加了容器内出口限制并通过针对性 TCP/DNS 探测；这不是 Docker Desktop 宿主/虚拟机的全流量审计，也不证明目标域部署、真实通知或正式命题指标。详细运行证据保留在本机忽略目录及既有 Git 历史中；源码提交不包含这些产物。验收结论见[测试与运行报告](test-report.md)。

### 2026-10-12 初始出口观察（提交 `f857ea1`）

在源码提交 `303cc0cbfa7607375c1e90bb9ee975146514d776` 上，使用 Docker Desktop 4.79.0、Engine 29.5.3 和独立 Compose 项目 `riskshield-alerting` 成功构建镜像 `riskshield:day4-local`（镜像 ID `sha256:83d8fa12111d28c554224bb853fb5d5b3a6e4ee4402a26df8fbef0e2e1292616`）。API `/health` 返回成功，Web 首页返回 HTTP 200。通过 API 导入仓库内固定历史案例并执行 10 Agent × 3 轮离线任务，30/30 决策有效；执行模式为 `offline_dynamic_substitute`，应用账本为 0 次模型调用、0 次网络请求。任务生成五模块报告和蓝色暂定预警；企微、邮件均仅生成 `dry_run` 预览。停止再启动 Compose 服务后，任务、报告、预警 JSON 内容的 SHA-256 均保持一致。以上结果只证明本机工程闭环，不代表真实模型、真实通知或正式业务指标达标。

该基线的 Compose 网络观测结果如下：API 与 Web 只连接 `internal: true` 的 default 网络，容器没有默认路由；从二者向 `1.1.1.1:443` 的 TCP 连接不可达，`example.com` 的外部 DNS 查询失败。`local_gateway` 另接未设为 internal 的 `host_ingress`，容器有默认路由；同一 TCP 探测成功，外部 DNS 查询也成功，因此当时的配置允许入口网关出站。

两个一次性网络试验没有得到仅靠网络模式配置的可用修复：普通桥接网络设置 `com.docker.network.bridge.enable_ip_masquerade=false` 后，外部 TCP 与 DNS 仍成功；仅连接 `internal: true` 网络时，已发布的 loopback 端口不可访问。

### 2026-10-12 网关移除默认路由复验

`local_gateway` 现在通过 [local_gateway.py](../../src/riskshield/local_gateway.py) 在启动时删除容器网络命名空间中的 IPv4 默认路由，若检测到活动 IPv6 默认路由则拒绝启动；随后清空网关进程的 capability bounding/effective/permitted 集并切换到 UID/GID 10001。Compose 给网关配置的 capability 限于 `NET_ADMIN`、`SETUID`、`SETGID`、`SETPCAP`，并启用 `no-new-privileges`。网关 DNS 上游设为 `127.0.0.1`，外部域名查询失败，Compose 内部 `api`/`web` 服务名解析仍成功。API/Web 继续只连接 `internal: true` 网络。入口发布仍仅为 `127.0.0.1:28000→8000` 与 `127.0.0.1:28501→8501`。

在独立 Compose 项目和原 `riskshield-alerting` 项目中均观察到：三个容器对 `1.1.1.1:443` 的 IPv4 TCP 连接不可达；网关 IPv6 TCP 连接不可达；三个容器对 `example.com` 的 DNS 查询失败。API `/health` 成功、Web HTTP 200；独立项目的固定历史案例完成 10 Agent × 3 轮、30/30 有效决策，`offline_dynamic_substitute`、`model_calls=0`、`network_requests=0`。停止再启动后，任务哈希不变。原 `riskshield-alerting` 命名卷保留，既有 job/report/alert 响应哈希与应用后相同。

标准 `docker compose build` 本次因 Docker Hub `auth.docker.io` 基础镜像令牌请求超时而未完成。运行复验镜像由此前本机镜像 `sha256:83d8fa…` 派生，只复制了新网关模块，ID 为 `sha256:6fbc6a…`；旧镜像另保留为 `riskshield:day4-baseline-20261012`。因此，当前源码的干净基础镜像构建仍待网络可用时复核。

容器配置的 capability 仍可由有 Docker daemon 管理权限的操作者用于启动额外进程；该权限属于可信控制面。上述 VM 探测是少量 TCP/DNS 连通性检查，不是全协议抓包或目标域出口审计；不能据此宣称整个主机或目标域已实现“数据不出域”。真实通知、模型调用、联网采集和外部部署均未执行，命题各项的达标状态不变。

### 2026-10-12 Docker Desktop VM/Windows 出口抽样核查

Docker Desktop 使用 WSL2 Linux Engine（内核 `6.6.87.2-microsoft-standard-WSL2`）。在 `docker-desktop` 发行版只读查看到 `eth3` 上存在 IPv4、IPv6 默认路由，DNS 上游为 `10.255.255.254`。VM 对 `1.1.1.1:443` 和 `2606:4700:4700::1111:443` 的零载荷 TCP 握手均成功，解析 `example.com` 成功；从 VM 到本机默认网关 `100.81.255.254:28000` 的 TCP 握手也成功，该端口映射到本机 API。相同 IPv4/IPv6 TCP 及外部 DNS 探测在当前 API、Web、local_gateway 三个容器中均失败（`Network is unreachable` / `Temporary failure in name resolution`）。这说明当前观测到的隔离仅适用于应用容器网络命名空间，Docker Desktop VM 仍具公网出站和访问本机映射服务的路径。

Windows Defender Firewall 的 Domain、Private、Public 配置文件均启用且默认允许出站，允许/阻断日志均关闭；本机可见的启用出站阻断规则数为 0。Docker Desktop 对应 Hyper-V VM Creator 配置也默认允许出站、阻断入站。当前会话不是管理员，`pktmon` 返回“拒绝访问”，因此没有开启或执行主机抓包，也未更改防火墙规则。该检查未覆盖所有连接、协议、代理与流量内容。标准 `docker compose -p riskshield-alerting build --pull --no-cache` 仍在获取 `python:3.12-slim` 元数据时因 Docker Hub token 端点 IPv6 TCP 超时而失败；现有运行容器、镜像标签和数据卷未被替换。
