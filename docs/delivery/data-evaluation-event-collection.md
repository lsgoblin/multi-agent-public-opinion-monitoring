# 事件级公开讨论采集约束

`riskshield.evaluation_hn_collection.collect_hits` 用于采集 Hacker News 公开帖子标题和同一帖子的评论身份。它只保留故事 ID、发表时间、标题、URL，以及评论 ID、发表时间、所属故事 ID；不保留评论正文或作者。

采集者先在事件计划中写定事件身份、独立于讨论序列的公开披露来源与日期、cutoff、等长基线/观察窗、故事搜索词和标题归属规则。基线阶段只查询截止前故事及其评论；预测和输入冻结后，观察阶段用**同一规则**查询新故事，并统计截止前已选故事及观察窗新故事下的观察窗评论。两个阶段都保留请求参数、条目投影、响应摘要及覆盖状态。若事件与已有开发事件重合，或标题规则无法判定归属，记录排除，不按预测对错改规则。

`collect_hits` 仅在 API 明确返回 `exhaustiveNbHits=true`、`nbHits` 与返回条目数相等、页数不超过一页且所有条目时间与身份有效时给出 `coverage=exhaustive`。否则按时间二分重查；无法分割或请求次数超限就报错。失败、近似计数和缺失窗口均不能记作零。关闭拼写容错可帮助获得可穷尽的查询，但**不是**覆盖证明；最终仍逐次检查 API 返回字段。Algolia [官方说明](https://support.algolia.com/hc/en-us/articles/15632498096529-How-the-facets-parameter-affects-hit-count-accuracy-in-Algolia)明确指出，`exhaustiveNbHits=false` 时 `nbHits` 可为估计值；[分页说明](https://support.algolia.com/hc/en-us/articles/4919156676625-Why-doesn-t-pagination-nbPages-hitsPerPage-match-nbHits)也指出可取页数可能小于命中总数。

完整查询覆盖只证明**当前索引对已声明查询的返回完整**，不能证明标题确实属于该事件，也不能凭当前索引证明历史时点的索引版本。后两项须由逐条事件归属记录和截止前来源证据另行解决。旧公司名称宽泛查询及 `nbHits` 不能自动转换成事件级真值；开发期使用过的事件和 AI 标签不能进入新留出集正式分母。方向的最低基线量、升降阈值与 `stable` 处理尚属待冻结提案，采集程序不内置它们。
