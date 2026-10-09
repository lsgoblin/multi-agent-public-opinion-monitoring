# D 部署与打包准备 — closure-20261009-functional-names-v1

本目录是本轮 D 的新交接证据，不覆盖旧运行记录或历史哈希。

- [依赖引用审计](package-dependency-audit.json)：按实际 Docker 配置、文档命令和测试导入列出依赖，不按 `experiments/` 目录名整体排除。
- [候选打包清单](package-inventory.txt)：候选范围、隐私/体积边界及等待 E 冻结的项目。
- [冻结后验收步骤](delivery-verification-plan.json)：独立解压、Compose 构建、案例、页面、dry-run 与停止/启动持久化验证。
- 当前轮 D owned files 的哈希由 `artifacts/final-closure/d-prep-ready.json` 固定；该文件生成后停止修改部署文件和本目录。

最终 ZIP 仅在 `integration-ready.json` 与 `candidate-package-manifest.json` 均带本轮标识、E 已记录全量测试通过且成员哈希逐项匹配后构建。目标文件名为 `riskshield-local-demo-20261009.zip`。
