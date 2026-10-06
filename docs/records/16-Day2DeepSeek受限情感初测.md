# Day 2 DeepSeek 受限情感三分类初测

> 2026-10-03 的一次合成协议测试和一次 cutoff 合格 SEC 摘要初测；不构成正式准确率。[当前 G2 状态](23-Day2受限G2关口复核.md)另见复核记录。

## 授权与输入

按[运行边界决定](../planning/15-Day2语义模型运行边界决定.md)，固定使用 DeepSeek `deepseek-flash`、非思考模式和 JSON 输出。本地从[事件包](../../data/public/unh_change_20240222_event_v2.json)导入临时 SQLite 后调用 `Store.snapshot`；快照只有 `unh-sec-20240222-initial` 一条合格输入，后续更新被排除。真实请求的用户消息仅含 `company`、`title`、`summary`：公司为 UnitedHealth Group，标题及人工摘要来自该条快照。记录 ID 仅留本地。请求不含 SEC 网页字节、URL、帖子、后续更新或其他来源；系统消息只含三分类指令与 JSON 示例。实现见 [day2_sentiment.py](../../src/riskshield/day2_sentiment.py)。

## 实际运行

先发送虚构保险公司服务通知，返回合法 JSON：`neutral`，理由“仅客观陈述服务不可用的事实。”；耗时 2.116 秒，输入 130、输出 16、合计 146 tokens。随后发送唯一获准的真实摘要，返回合法 JSON：**`negative`**，理由“描述网络攻击事件，语气偏负面。”；耗时 1.902 秒，输入 155、输出 17、合计 172 tokens。两次请求均为 `thinking=disabled`、`max_tokens=160`、无重试；响应报告模型 ID 为 `deepseek-flash`。按[DeepSeek 官方模型页](https://api-docs.deepseek.com/quick_start/pricing/)，该 ID 当前对应 DeepSeek-V4.1-Flash；响应没有单独给出底层版本号。

两次共用输入 285、输出 33、合计 318 tokens。按官方 **峰值**缓存未命中输入 $0.30/百万、输出 $1.20/百万，并以 **8 元/美元**保守折算，费用上界估算为 **¥0.0010008**，低于执行时的单次完整任务 ¥2.36 上限。这是根据 token 回执的估算，非供应商账单或实际扣费。调用前为每次请求预留 100,000 输入 tokens 与 160 输出 tokens 的峰值费用额度；本轮只有上述两次请求。用户后来将后续单次完整任务上限提高至 ¥5.00；该运行数据文件保留执行时写入的旧预算字段，不回写历史结果。

[机器可读运行结果](../../data/research/unh_change_20240222_deepseek_sentiment_probe.json)记录请求模型、提示词版本、字段白名单、实际响应 ID、结果、理由、耗时、token 用量、估算费用、摘要哈希及运行时刻，不含密钥和真实输入正文。

## 与人工初标对照

[Leo 的用户初标](15-Day2人工情感初标表.md)为 `neutral`，依据是申报客观陈述负面事件，但未明显赞扬或批评公司。本次模型标为 `negative`，理由聚焦网络攻击事件的负面语气。**两者不一致**；模型可能把事件负面性带入对公司表达态度的判断，但单次输出不能确定分歧原因，也不能据此判定哪方正确。应由另一位真人在不看模型答案的条件下独立复核，再检查标签口径；本轮不计算准确率。

## 验证与限制

合成请求先验证 JSON 结构、标签枚举、理由、正常结束、模型 ID 和 token 用量；失败即不发真实请求。本地测试检查外发字段白名单、非思考模式、记录 ID/URL/后续更新不外发，以及合成失败时停止。`.venv\Scripts\python.exe -m pytest -q`：**34 passed**，另有 1 条现有 Starlette/httpx 弃用警告；文档相对链接与 `git diff --check` 通过。此次云端调用只验证单模型单条摘要，不证明域内运行、五平台在线、实时延迟或事件聚合；未进入 Day 3。
