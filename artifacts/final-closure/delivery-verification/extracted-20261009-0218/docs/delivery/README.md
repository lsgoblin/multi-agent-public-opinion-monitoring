# 本机工程交付文档

此处说明当前可复核的本机离线原型，不构成 G3、G4 或 G5 通过结论。

## 文档

1. [部署与运行](deployment.md)：主机与 Docker 启动、配置、持久化及网络边界。
2. [API 参考](api.md)：路由、请求约束、任务状态、报告/预警引用和错误语义。
3. [用户操作指南](user-guide.md)：案例准备、任务执行、进度、报告和通知预览。
4. [测试与运行报告](test-report.md)：最终回归、运行证据、验收清单与未达标项。

## 当前结论

- 生产模块按功能命名：`evidence`、`sentiment`、`source_probe`、`simulation`、`reporting`、`alerts`、`tasks`、`evaluation`。旧 import 路径保留极薄兼容入口；映射与哈希见[重命名表](../../artifacts/final-closure/rename-map.json)。
- 本机真实历史材料演示执行 `offline_dynamic_substitute`，来源图为 `offline_archived_record_projection`；这不等于真实模型推演或在线 GraphRAG。
- 企微与邮件仅生成 `dry_run` 预览，不发送通知。Docker 已有本机构建、运行、重启持久化证据；完整容器出口隔离仍待实测。
- C 的[最终评分报告](../../artifacts/final-scoring-20261009/final-score-report-v24.md)记录正式走势与情感指标均不可计算；AI 标注仅为探索性参考。
- [最终集成复核](../records/final-integration-review.md)及[测试报告](test-report.md)列出端到端结果和剩余阻塞。

本轮工作区候选包清单为 `artifacts/final-integration-review/candidate-package-manifest.json`；构建与解压验收由 D 在最终哈希交接后执行。
