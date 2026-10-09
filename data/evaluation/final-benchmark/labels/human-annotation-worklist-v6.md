# 真人标注任务单 v6（待主 Agent 派发）

## 立即可发：截止前标题情感

共 5 个冻结标题单位、10 个独立提交槽；现有真人提交 **0/10**。每个单位由两名真实标注者独立完成。标注者只看本单位标题、来源和发布时间，不看预测、未来材料或另一位答案。

标签仅描述标题的原始表达：`positive`、`neutral`、`negative`、`uncertain`。不得据标题判断事件指控是否属实、严重程度或公众总体态度。保留原标题，不以摘要替换；法律指控词句按标题表达标注。

| 单位 | 冻结文本 | 来源 / 时间 | 提交槽 |
| --- | --- | --- | --- |
| `sent-fnf-1821s44` | Fidelity National Financial shuts down network in wake of cybersecurity incident | [Reddit r/technology](https://www.reddit.com/r/technology/comments/1821s44/)；2023-11-23 13:59:40Z；归档 13:59:55Z | `...-A`, `...-B` |
| `sent-anthem-1h6mj0x` | Anthem Blue Cross Blue Shield Won’t Pay for the Complete Duration of Anesthesia | [Reddit r/anesthesiology](https://www.reddit.com/r/anesthesiology/comments/1h6mj0x/)；2024-12-04 17:59:16Z；归档 17:59:34Z | `...-A`, `...-B` |
| `sent-cigna-159ce8g` | Cigna Sued Over Algorithm Allegedly Used To Deny Coverage To Hundreds Of Thousands Of Patients | [Reddit r/technology](https://www.reddit.com/r/technology/comments/159ce8g/)；2023-07-25 15:53:39Z；归档 15:53:54Z | `...-A`, `...-B` |
| `sent-humana-ars-20231213` | Humana also using AI tool with 90% error rate to deny care, lawsuit claims | [Ars Technica](https://arstechnica.com/science/2023/12/humana-also-using-ai-tool-with-90-error-rate-to-deny-care-lawsuit-claims/)；2023-12-13 16:40 EST | `...-A`, `...-B` |
| `sent-aflac-1lg33ey` | Aflac [AFL] hit with a cybersecurity breach | [Reddit r/cybersecurity](https://www.reddit.com/r/cybersecurity/comments/1lg33ey/)；2025-06-20 12:46:13Z；归档 12:46:30Z | `...-A`, `...-B` |

每槽须由标注者填写：`annotator_id`、提交时间（含时区）、标签、简短理由、信心（可选）、所见文本版本、是否看过预测、是否看过另一答案。若分歧，保留两份原答，再交第三位独立裁决；不得覆盖原答。

对应盲标数据：[blind-sentiment-packet-v6.json](blind-sentiment-packet-v6.json)。v5 中的标题和槽位均保留；v6 修复了损坏的标注说明及 Anthem 标题字符。没有收到真人提交前，所有标签保持空值。

## 暂缓派发：走势标签

FNF 和 Louisiana BCBS–Elevance 交易各有 cutoff 输入及后续公开讨论材料，但同口径量化对照仍为 0；方向都保持 `unknown`。先由主 Agent 冻结[走势口径提案 v6](trend-protocol-proposal-v6.md)，再为满足口径的事件制作独立盲标证据包。不得要求标注者在当前证据上猜方向。

