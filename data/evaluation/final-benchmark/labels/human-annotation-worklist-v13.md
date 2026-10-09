# 真人盲态标注清单 v13（待主 Agent 派发）

**目的：** 情感标签按冻结原始标题标注；未来走势方向仅在主 Agent 冻结协议后，使用独立评分证据包另派任务。预测、未来证据和其他标注者答案均不展示。

## 立即可派发的情感单位

| 单位 ID | 冻结文本 | 来源/版本 | 标注范围 |
| --- | --- | --- | --- |
| `sent-fnf-1821s44` | Fidelity National Financial shuts down network in wake of cybersecurity incident | v6 盲包；Arctic Shift 标题快照 | 只标标题表达 |
| `sent-anthem-1h6mj0x` | Anthem Blue Cross Blue Shield Won’t Pay for the Complete Duration of Anesthesia | v6 盲包；Arctic Shift 标题快照 | 只标标题表达 |
| `sent-cigna-159ce8g` | Cigna Sued Over Algorithm Allegedly Used To Deny Coverage To Hundreds Of Thousands Of Patients | v6 盲包；Arctic Shift 标题快照 | 只标标题表达 |
| `sent-humana-ars-20231213` | Humana also using AI tool with 90% error rate to deny care, lawsuit claims | v6 盲包；冻结新闻标题 | 只标标题表达，不判断指称真伪 |
| `sent-aflac-1lg33ey` | Aflac [AFL] hit with a cybersecurity breach | v6 盲包；Arctic Shift 标题快照 | 只标标题表达 |
| `u11-uhc-title-001` | UnitedHealth faces class action lawsuit over algorithmic care denials in Medicare Advantage plans | v11；Arctic Shift 于 2023-11-15T21:22:18Z 的标题快照 | 只标标题表达，不判断诉讼结果 |
| `u13-recall-title-001` | Microsoft changes Recall feature amid criticism | v13；Axios archived snapshot 2024-06-07T19:00:27Z；正文不提供给情感标注者 | 只标原始标题表达 |

## 每个单位两位真人独立标注

- 已有任务：6 个单位 × 2 = 12 槽；v10/v11 状态合计 0 收到、12 缺失。
- v13 新增：1 个单位 × 2 = 2 槽；目前 0 收到、2 缺失。
- **本清单合计：7 个单位、14 个盲态槽；已完成 0、缺失 14。**
- 请按各原批次的允许标签作答：v6 使用其冻结定义；v11/v13 使用 `positive / neutral / negative`。不统一改写旧协议。
- 每位标注者填写：单位 ID、标签、简短理由、标注者代号、提交时间（含 UTC 偏移）、文本版本/哈希、是否看过预测、是否看过其他答案。理由不得含个人身份资料。
- 回收前不要把本清单和答案字段展示给预测执行端。分歧保留原答，交未看预测的独立裁决人；C 不代填。

## 走势标注

当前没有已冻结主 Agent 口径、双来源可比证据与盲态标签包同时齐备的新增走势任务。v13 Windows Recall 的方向保持 `unknown`，不派发猜测题；本事件的走势人工标签数为 0。

## 回收记录（留空供真人填写）

| 单位 ID | 标注者 A 标签/理由/提交时间 | 标注者 B 标签/理由/提交时间 | 是否分歧 | 独立裁决 |
| --- | --- | --- | --- | --- |
| 每个单位各一行，使用原答附件；当前尚未收到提交 |  |  |  |  |
