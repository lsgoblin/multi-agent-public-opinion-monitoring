# 真人情感标注优先任务 v14

**用途：** 由主 Agent 分派真实标注者。此清单是 v13 现有待标单位的优先子集，不新增事件、文本单位或标注计数。

## 标注任务

- **盲态：** 每个冻结标题由两位真人独立标注；不展示预测、后续走势证据或另一位标注者答案。
- **单位：** 仅对下列原始标题文字作情感判断，不判断事件真伪、严重程度或公众整体态度。
- **材料：** [冻结盲标包 v6](blind-sentiment-packet-v6.json)，SHA-256 `e31e1a2962e40bbbe4e2e06dbc7a8cbd2c1198031398b4407a47923b81ec29fc`。
- **回填字段：** 标签、简短理由、信心、标注者代号、提交时间与时区、材料版本、是否看过预测、是否看过另一答案。保留两份原答；分歧交独立裁决。
- **状态：** 5 个单位、10 个真人槽位；已收到 0，缺失 10。它们与 v13 七单位/十四槽清单重合，不作为新增标签样本。

## 优先单位

| 单位 ID | 事件 | 冻结原始标题 | 版本/可见时间 |
|---|---|---|---|
| `sent-fnf-1821s44` | Fidelity National Financial 网络事件 | Fidelity National Financial shuts down network in wake of cybersecurity incident | Arctic Shift 标题快照 v1；2023-11-23 13:59:40Z |
| `sent-anthem-1h6mj0x` | Anthem anesthesia policy 争议 | Anthem Blue Cross Blue Shield Won’t Pay for the Complete Duration of Anesthesia | Arctic Shift 标题快照 v1；2024-12-04 17:59:16Z |
| `sent-cigna-159ce8g` | Cigna PxDx 诉讼 | Cigna Sued Over Algorithm Allegedly Used To Deny Coverage To Hundreds Of Thousands Of Patients | Arctic Shift 标题快照 v1；2023-07-25 15:53:39Z |
| `sent-humana-ars-20231213` | Humana 算法诉讼 | Humana also using AI tool with 90% error rate to deny care, lawsuit claims | Ars Technica 原文标题；2023-12-13 16:40:00−05:00 |
| `sent-aflac-1lg33ey` | Aflac 网络事件 | Aflac [AFL] hit with a cybersecurity breach | Arctic Shift 标题快照 v1；2025-06-20 12:46:13Z |

## 主 Agent 分派前需冻结的标签映射

v6 盲标包的 `allowed_labels` 为 `positive / neutral / negative / uncertain`，但 `label_definitions` 另含 `mixed / unclear` 且未定义 `uncertain`。请先确定允许的最终标签和值映射，再分派；本清单不自行改写旧标签协议。

本轮尚未收到真人提交。走势标签不在此情感清单内，当前没有事件具备已确认的双源走势证据和独立裁决标签。
