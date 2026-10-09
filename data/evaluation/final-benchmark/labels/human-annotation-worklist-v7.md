# 真人标注任务清单 v7（交主 Agent 安排回收）

## 现在可发：cutoff 标题情感双人标注

- 任务对象：盲标包 v6 的 5 个冻结标题单位：FNF、Anthem、Cigna、Humana、Aflac。
- 任务量：每个标题由两位真人独立标注 positive / neutral / negative / uncertain，共 **10 个槽位**。
- 当前状态：**0/10 完成，10/10 缺失**；没有代填。
- 每份提交记录标注者 ID、提交时间与时区、标签、理由、所见文本版本、是否看过预测、是否看过其他答案。保留两人原答；分歧交第三位独立裁决。
- 盲态：只展示该冻结标题、来源和发布时间；不展示预测、后续结果或另一位答案。标签仅反映标题表达，不判断事实真伪、严重程度或总体公众情绪。
- 标注包：blind-sentiment-packet-v6.json；单位明细见 human-annotation-worklist-v6.md。

## 暂不发：走势方向标签

v7 为 FNF、Louisiana、Aflac 整理了同一归档源的前后量值，但覆盖稀疏，三件走势证据均不足，方向均 unknown。主 Agent 冻结口径并确认数据满足条件后，再准备双人盲标包。不得让标注者猜方向。

## E 预测与标签隔离

E 只读取 inputs/qualified-inputs-v6.json。预测输出封存后评分侧读取 trajectory-evidence-v7.json；预测执行端不能读取走势或情感标签。
