# Agent E 本机集成证据索引

本目录保存本轮 E 的集成、评测适配、预检和复核产物。解释每项结果时以对应执行模式、源码哈希、输入哈希和状态为准。A/C 原始运行与冻结文件仍在原目录；C 标签/标题正文不随证据摘要复制。

- [`candidate-package-manifest.json`](candidate-package-manifest.json)：供 D 打包的固定文件列表、文件 SHA-256、演示命令和候选状态；B 最终交接后须再核相关哈希。
- [`version-manifest.json`](version-manifest.json)：本轮代码、文档、输入和证据快照哈希；不包含自身哈希。
- [`test-results.json`](test-results.json)：基线复现、费用保守预留、C v6 映射、Day 4/API/UI 定向回归和最终全量回归。
- [`end-to-end-summary-20261009-final.json`](end-to-end-summary-20261009-final.json)：当前代码版本的历史案例 API→截止图→离线动态替身→五模块报告→预警预览，含绑定 ID、耗时和代码哈希。
- [`a-simulation-review.json`](a-simulation-review.json)：A v4 批次 13 项产物和当前源码哈希核验、c4/c8 小试与最新 500×30 partial 结果。
- [`c-batch-integrity.json`](c-batch-integrity.json)：freeze v1–v24 成员/父链和最新 handoff 选择；仅解析 v24 汇总计数，不打开 C 标签、标题或轨迹正文。
- [`message-pair-reservation-review.json`](message-pair-reservation-review.json)：费用预留测试配置的输入上界、价格公式、单次/四次金额和未知 usage 处理。不是供应商账单。
- [`local-model-preflight.json`](local-model-preflight.json)：v13 cutoff 输入哈希验证；本机模型未配置，0 次模型/网络调用、0 个评分侧文件读取，没有生成 v13 新预测。
- [`v8-pilot/current-evaluator-score-recheck-20261009.json`](v8-pilot/current-evaluator-score-recheck-20261009.json)：既有封存常量工程基线由当前评分器复核，走势/情感均 0/0；不是正式效果结论。
- [`a-simulation-review.json`](a-simulation-review.json) 引用的 A v4 原始批次为 `artifacts/final-simulation-review/live-validation-20261008-v4/`；B 当前截图/复核见 [`final-report-web-review.md`](../../docs/records/final-report-web-review.md)。

最小启动、演示、验收状态和责任方见[最终集成复核](../../docs/records/final-integration-review.md)与[测试报告](../../docs/delivery/test-report.md)。
