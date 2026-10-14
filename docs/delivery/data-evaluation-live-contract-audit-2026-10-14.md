# 真实仿真预测跨分支契约核查（2026-10-14）

> 状态：`data-evaluation` 的离线接口核查，供总 Agent 协调。没有真实模型调用、正式预测准入或验收分母。仿真侧核查提交为 `simulation-scale@d9fd971`（方向导出主体在 `200242d`），评测侧为 `data-evaluation@d0eb64e`。

## 可复现的当前结果

用仿真分支现有真实历史案例和**本地测试替身**生成一份 `prediction_import.v1` 包，再直接送评测分支 `import_formal_predictions`。替身虽向引擎声明 `backend_mode=real_model`，完全不发 API 请求；因此本实验只测两侧结构，不代表真实模型运行。结果为 **1 行、准入 0、拒绝 1**，唯一拒绝码 `simulation_run_ledger_reference_not_an_object`。生产证据文档没有 `run_ledger_ref`、`run_ledger_sha256`、`run_config_sha256` 三项。仿真引擎本身能导出含 `config`、`usage`、`billing` 的原始 `summary()` 和 6 条完整 `trajectory()` 动作，但现有 `write_documents` 没有把两者作为 `riskshield.simulation_scale.run_ledger.v1` 原件写进预测材料根。

本机复现脚本和报告位于主工作区忽略目录 `artifacts/branch-work/data-evaluation/evidence/20261014/`：`cross-branch-contract-smoke.py` SHA-256 `7b9cc3d05e3c8037d8b1a8810d3766a09d345bd6d57e27ac041fefb005b27853`；`cross-branch-smoke-v1/contract-probe-report.json` SHA-256 `a07e260d80f7a6a96210f1536191a031f9b62b4b05bcff270b5d4bb1c619629c`。报告列每个产生的 JSON 哈希。未把测试替身包放入 Git 或正式数据集。

## 真实调用前还必须解决的两处边界

当前 `scripts/run_live_tier.py` 默认加载本机 `runtime/bailian_backend_ref.py`。该本机适配器的 `_fields` 对 `data_mode=real_historical` 在发送请求前抛出 `BailianRequestError: Bailian Day 3 adapter currently permits synthetic input only`；离线直接调用 `_fields` 已复现。该运行时文件未被 Git 跟踪，本机 SHA-256 为 `abd1d42eb9d8eae045e7d4a2dadeb525e7d73465393b52ab659355fab7321b25`，部署或其他机器须重新核对，不能把 `--dry-run` 通过当作真实调用可用。

同一适配器当前把 `netizen`、`customer` 路由到 `qwen-turbo`，其余角色到 `qwen-flash`。本任务获准模型是 **qwen-flash**，所以不能直接以该路由启动收费运行；须由仿真/网关责任方先完成模型路由与真实历史输入的授权边界，并在任何请求前用冻结配置和干运行核对所有角色只会选择获准模型。此处未改适配器、网关或仿真分支，也未调用收费模型。

## 建议的最小交付契约

仿真侧在写预测文档时，同步写出**原引擎** `Day3Simulation.summary(run_id)` 与 `trajectory(run_id)` 的完整 `run_ledger.v1`，将可解析的相对 `run_ledger_ref`、该对象的 canonical JSON SHA-256 和 `summary.config` 的 canonical JSON SHA-256 填入同一 `production_evidence`。评测器会逐轮核对事件/cutoff/来源版本、真实历史与真实后端模式、完整 Agent 动作、Token、模型和费用；不能由评测器根据汇总行事后拼账本。全量动作中的请求审计只留获准本机材料根，不进入源码提交。

还需总 Agent 明确可核验的真实后端标识与请求审计版本，评测侧据此拒绝把声明 `real_model` 的本地替身当成真实供应商运行。当前准入器能核原始账本一致性，却不能仅凭 JSON 中的 `backend_mode` 自证外部 API 确曾执行。即使以上接口补齐，仍须匹配**新留出**的同事件 ID、cutoff、来源版本和主记录；对观察窗覆盖与归属、独立真人真值另行核证。现有新留出正式可评分仍为 **0/20**，四项验收指标均不可计算。
