# Day 2 nike 对 Optum 来源的人工初标

> 2026-10-04 收到用户提交的真人判断。本记录只代表一条人工初标，不是独立真值或正式准确率样本。当前 G2 状态见[一页状态](23-Day2受限G2关口复核.md)。

## 标注对象

标注输入：`case_id=unh_change_20240222_day2_multisource`，`record_id=optum-status-20240221-cyber-update`，公司 UnitedHealth Group；截止前标题“Change Healthcare cybersecurity network interruption”；人工摘要“Optum 状态页当时报告 Change Healthcare 因网络安全问题发生网络中断，已断开受影响系统，预计中断至少持续到当天。”对应 Optum [事件状态页](https://status.changehealthcare.com/incidents/hqpjz25fn3n7)的更新 `9j1b644m46kx`。原件现含后续进展，核对时只看指定更新。判断对象是文字**对 UnitedHealth Group 的表达态度**，不是网络事件本身的风险程度；可见时间限制见[跨来源记录](19-Day2跨来源聚合与在线探测.md)。

## 用户提交的原始判断

- 标注者代号：`nike`。
- 标签：`neutral`（中性）。
- 原始理由：“标题和摘要陈述了网络中断、断开受影响系统及预计持续时间，没有表达对 UnitedHealth Group 的赞扬或批评。事件本身有负面影响，但这里判断的是文字对公司的态度。”
- 作答前所见结果：用户说明看过 Leo、Li 或模型对**另一条 SEC 材料**的结果；未看到这条 Optum 更新的标注结果。因此本条对自身结果盲态，但不是对同一事件先前标签完全盲态，不能当作独立评测真值。
- 实际阅读材料：理由明确涉及截止前标题和人工摘要；是否核对 Optum 原件指定更新，用户未说明。
- 用户报告时间：`2026-10-04 15:00 +08:00`。用户只给出 `15:00`，日期按本轮会话日期记录；不自动等同实际阅读完成时刻。
- 本地标签接口写入时间：`2026-10-04T07:06:03.361287+00:00`，即北京时间 `15:06:03.361287`。此时间与用户报告时间分开保存。
- 不确定或有争议之处：用户未另行提供。

[结构化初标记录](../../data/research/unh_change_20240222_optum_human_label_nike.json)保存原话、已知阅读条件和时间来源。原始判断未由助手代填；没有向模型发送 Optum 材料。

## 录入核验与后续

已将[多来源事件包](../../data/public/unh_change_20240222_day2_multisource_event.json)导入本地 `runtime/riskshield.db`，再通过现有 Day 2 标签接口录入 `optum-status-20240221-cyber-update / nike / neutral`。读回时中文理由与结构化文件中的原话完全一致，接口返回 `submitted_label_identity_unverified_not_formal_truth`。首次命令的终端显示编码曾出现乱码，数据库实际内容经 UTF-8 字符串相等检查确认正确，未改写其他标签。

此条是新增来源的**人工初标**。若要形成正式准确率真值，仍需冻结口径、安排未看过同事件标签的独立复核，并扩充事件与类别样本；黑猫有效详情、在线延迟和真实跨事件聚合证据仍缺。
