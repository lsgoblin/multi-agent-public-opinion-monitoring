# 最终部署、启动与打包复核

> 复核时间：2026-10-09（北京时间）。范围为 Agent D 的 Docker 本机运行、固定案例演示、停止/启动持久化与最小提交包准备。没有向外部环境部署、发送真实通知或发起模型调用。

## 结果摘要

- 当前共享工作树在 `codex/day1-foundation`，HEAD 为 `17495e6400d8352c84722a4cb374200d8f12b02f`，仍有多个 Agent 的未提交改动。该 HEAD 不能代表本次镜像所含全部代码；镜像由构建时共享工作树产生。
- Docker 无缓存构建成功。镜像为 `riskshield:day4-local`，ID `sha256:fc534eac7b89f206ce81e045012fa4bb01f9cf781ec65ac91560b91c4890eea8`，大小 181,611,731 字节（173.20 MiB）。容器内 15 个 Python 文件与构建时工作树的 SHA-256 相同。基础镜像固定到 `python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f`。
- 验收 Compose 项目为 `riskshieldagentdfinalutf8`，本机地址为 API `http://127.0.0.1:28002/`、Web `http://127.0.0.1:28503/`。API `/health` 返回 200/`ok`，Web 首页返回 200，浏览器实际打开任务队列、五模块报告和预警中心。
- 固定历史案例在本机 `offline_dynamic_substitute` 模式完成。案例、任务、报告、预警一一关联；通知仅生成离线预览；服务正常停止并启动后四类记录的响应字节哈希一致。
- **没有生成最终提交 ZIP。** D 本轮准备检查时，E 的 `artifacts/final-integration-review/test-results.json` 报告全量 **181 passed、1 warning**；但 `integration-ready.json` 和 `candidate-package-manifest.json` 尚不存在，B 的本轮交接也未出现，因而没有可供 D 核对的同轮冻结清单和成员哈希。按用户指定顺序，先完成 D 准备交接，收到 E 的匹配冻结后再生成包与最终 SHA-256。

## 构建与启动证据

Docker Desktop Linux Engine 为 29.5.3，Compose 为 5.1.4。构建命令使用当前工作树和验收项目专用配置：

```powershell
docker compose -f compose.yaml -f artifacts/final-deployment-review/compose.acceptance-final.yaml -p riskshieldagentdfinalutf8 build --no-cache
docker compose -f compose.yaml -f artifacts/final-deployment-review/compose.acceptance-final.yaml -p riskshieldagentdfinalutf8 up -d
```

构建日志、解析后的 Compose 配置和版本清单分别见[build.log](../../artifacts/final-deployment-review/build.log)、[compose-final.resolved.yaml](../../artifacts/final-deployment-review/compose-final.resolved.yaml)和[version-manifest.json](../../artifacts/final-deployment-review/version-manifest.json)。本轮配置、部署说明、复核记录和新增准备文件的 SHA-256 由[D 准备交接](../../artifacts/final-closure/d-prep-ready.json)固定；先前的 `final-file-sha256.txt` 是上一轮快照，不作为本轮哈希。Docker 构建上下文为 405.79 kB；`.dockerignore` 排除了 `.env`、运行产物、评测集、缓存和虚拟环境。未把密钥复制进镜像。

健康检查、readiness、渠道状态、浏览器请求和容器状态证据见[smoke-summary.json](../../artifacts/final-deployment-review/smoke-summary.json)、[api-health.json](../../artifacts/final-deployment-review/api-health.json)、[api-readiness.json](../../artifacts/final-deployment-review/api-readiness.json)、[channel-status.json](../../artifacts/final-deployment-review/channel-status.json)及[重启后容器状态](../../artifacts/final-deployment-review/compose-ps-after-stop-start.txt)。readiness 中 G3/G4 仍为 false；200 和容器 healthy 只证明服务可访问。

开始时保留了已有 `riskshield`（默认端口）和 `riskshieldaccept`（18000/18501）项目及数据卷；验收使用独立项目名、28002/28503 端口和 `artifacts/final-deployment-review/runtime-final` 目录。API/Web 位于没有默认网关的 Compose 内部网络；`local_gateway` 另接有默认网关的 `host_ingress`，所以完整出口隔离没有通过，详见[路由检查](../../artifacts/final-deployment-review/network-route-summary.json)。

## 固定案例闭环

使用案例 `unh_change_20240222_day2_multisource`，输入 SHA-256 为 `5b81d0e76f5445c978d85d1462780b51ecc12c69c407d0b71d4a9b45ed3291b4`。案例已存在于本次验收库，验收脚本读取 UTF-8 源文件、校验标题后直接执行，没有重复导入（`case_imported=false`）。运行结果：

| 项目 | 值 |
| --- | --- |
| graph / run | `936c73951d2ab46f8e0ee6bb` / `run_c59ee9e85b9b4a048736` |
| job / report / alert | `job_5de8ee73d75c4bd1a65c` / `report_7232e7a4f918c4cd506393f3` / `alert_b004baf85c607f8413a85ded5aa1bcb7` |
| 模式与结果 | `real_historical` 输入 + `offline_dynamic_substitute`；`success`，3/3 轮、30/30 有效动作 |
| 调用与请求 | `model_calls=0`、`network_requests=0` |
| 报告模块 | `emotion_evolution`、`key_nodes`、`propagation`、`recommendations`、`risk` |
| 预警 | 蓝色，`provisional_offline_assessment` |
| 通知预览 | WeCom、邮件均为 `dry_run`，`sent_at=null`，未发送 |

[案例摘要](../../artifacts/final-deployment-review/case-closure-summary.json)记录了进度、模块与关联 ID；[通知预览账目](../../artifacts/final-deployment-review/notification-previews.json)记录零网络请求。浏览器页面实际展示任务状态、报告内容及预警预览。该结果只验证本地离线替身，不代表真实模型质量、在线传播或正式预警准确率。

## 停止、启动与持久化

验收容器完成一次显式 `stop` 后再 `start`。重启后 API health 为 `ok`、Web 首页为 200；案例、任务、报告、预警仍可访问，原始 HTTP JSON 哈希全部相同。复核结果见[stop-start-persistence-check.txt](../../artifacts/final-deployment-review/stop-start-persistence-check.txt)和[持久化摘要](../../artifacts/final-deployment-review/restart-persistence-summary.json)。这验证已完成任务的数据留存，不覆盖卷删除、灾难恢复或运行中任务续跑。

当前验收实例仍在运行。再次访问/停止的最短命令：

```powershell
Start-Process 'http://127.0.0.1:28503/'
docker compose -f compose.yaml -f artifacts/final-deployment-review/compose.acceptance-final.yaml -p riskshieldagentdfinalutf8 stop
```

常规新建实例的启动、访问和停止命令见[部署说明](../delivery/deployment.md)。

两次早期验收尝试因 Windows PowerShell 默认编码与 HTTP 响应结构误用而未能作为有效案例证据；它们各自的独立项目已停止但未删除。最终证据使用 UTF-8 容器内脚本及全新隔离运行目录，不依赖这些尝试。详情见[排查记录](../../artifacts/final-deployment-review/first-attempt-note.txt)。原有 `riskshield`、`riskshieldaccept` 项目和数据卷未修改。

## 网络与正式验收边界

本机 Compose 内部网络设为 `internal: true`；本轮 API/Web 无默认网关。`local_gateway` 连接普通 `host_ingress` 网络并发布 loopback 端口，带默认路由。没有对完整容器组证明出口隔离，也未验证域内部署、域内模型或真实通知；G3、G4、G5 不因本机成功而通过。readiness 与既有[Day 4 Docker 记录](45-Day4Docker本机运行验收.md)保留相应缺口。

## 本轮打包准备与冻结后验证

本轮交接标识为 `closure-20261009-functional-names-v1`。Docker 构建、浏览器案例和持久化实测见上文；本轮只补充文件依赖审计、候选包清单和解压验证步骤，不重写前述运行证据。准备材料位于[本轮 D 目录](../../artifacts/final-deployment-review/closure-20261009-functional-names-v1/README.md)。

打包不得早于 E 的同轮 `integration-ready.json` 和 `candidate-package-manifest.json`：必须核对标识一致、E 记录全量回归通过、冻结成员逐项哈希匹配。D 准备检查时，测试报告仍引用缺失的候选清单，链接因此失效；D 不修改 E 的文档或评分数据。未生成 ZIP。

候选成员按实际依赖审计区分运行时、文档回放和测试依赖。当前 `docs/delivery/test-report.md` 命令引用 `experiments/final_integration_probe.py`；测试当前也导入若干 `experiments/` 和 `scripts/` 模块，因此不能按目录名整体排除。全量测试还引用约 90.3 MiB 的合成性能数据和 C 的受限 v6 标签材料；受限标签不得进入交付包，性能数据是否纳入由 E 的冻结测试子集决定。准确路径与条件见[依赖审计](../../artifacts/final-deployment-review/closure-20261009-functional-names-v1/package-dependency-audit.json)及[候选清单](../../artifacts/final-deployment-review/closure-20261009-functional-names-v1/package-inventory.txt)。

E 冻结后目标包为 `artifacts/final-deployment-review/riskshield-local-demo-20261009.zip`。必须在新的独立目录解压后，使用独立 Compose 项目名和当时空闲的 loopback 端口重新构建；验证 health、Web、固定案例进度、五模块报告、预警 dry_run 和 stop/start 后记录哈希。步骤见[解压验收计划](../../artifacts/final-deployment-review/closure-20261009-functional-names-v1/delivery-verification-plan.json)。打包后的全部证据只写入 `artifacts/final-closure/delivery-verification/` 与 `artifacts/final-closure/d-delivery-ready.json`。

本轮 D 文件哈希由 `artifacts/final-closure/d-prep-ready.json` 固定。该 JSON 写出后，Docker 配置、部署说明、复核记录和本轮准备文件均停止修改；若冻结包存在必要修复，只在 `d-delivery-ready.json` 标记 `needs_revision` 并提供证据，不擅自改包。
