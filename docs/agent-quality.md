# 候选比较、需求与结论复核及统一评测

本次升级增加 `compare_itinerary_options` 和 `review_itinerary`，并建立统一的中文多轮
评测入口。两项工具由 LLM 按需求自主选择：可以在查询后比较候选、核对候选或草案，也可以
依据反馈继续查询、修正、追问或交付。没有新增固定的“查询 → 比较 → 复核 → 保存”业务流程，
没有要求每轮必须调用这些工具。完整用户原话、模型消息和工具结果继续保留。

## 1. 有限候选的共同口径比较

`compare_itinerary_options` 接收共同的 `basis` 和至少两份已组成的 `options`，复用
`ValidateItineraryInput` 与原行程校验器。工具在 `ToolRegistry` 注册，因此 LangGraph
及 legacy 都可以调用，也可通过既有只读工具 HTTP 接口调用。

共同口径包括时间窗、人数、房间数、预算与费用类别、需要覆盖的住宿夜、固定安排和要保留的
锁定项。共同条件优先于候选自己的声明；不能通过删除某项必要费用或声称自己已完整，规避
共同约束。额外提供的费用也计入总额，不能隐藏在所声明的费用范围之外。

LangGraph 会话调用还会从当前已发布版本注入用户锁定或自报已订的安排；模型漏传这些
保护项不能绕过比较中的保护检查。纯工具 HTTP 或 legacy 调用仍只能检查调用方提供的
保护对象，其他自然语言条件的完整提取也不能由比较器独立证明。

本实现是**已提供完整候选之间的确定性比较与 Pareto 前沿计算**，使用 Python、Decimal 和
已有校验器，没有接入 OR-Tools，也没有自动搜索所有交通、酒店和活动组合。LLM 负责构造
候选与解释取舍；工具只计算输入可支持的指标：

- `total_cost`：最小化费用。只有查询时报价、税费全含、人数及住宿口径明确、必要费用
  完整且为单币种时，才给出 `exact_total_cost`。参考价与未知费用分别保留，未知不当零。
- `travel_minutes`：最小化交通及相邻转场的实查时长；不重复计算交通块中的行驶时间。
  估计时长另放 `reference_travel_minutes`，排定交通块长度不冒充实查行驶时长。
- `activity_minutes`：最大化已排活动区间的分钟数，表示排期长度，不是满意度分数。

必要进出站、换乘、入园或休息等缓冲由原校验器检查。提供转场但 `buffers_complete` 未明确
为 `true`，或任何相关检查出现未知，候选为 `needs_review`，不能进入 Pareto 前沿。
已知冲突为 `conflict`；只有无已知冲突或未知检查、且所选比较指标完整时才有资格参与排序。
不同币种分组比较，不自动换汇；缺失指标保留不可比较关系，不补造分数。

输出保留每项候选的校验、金额口径、未知项、支配关系和取舍。`feasible` 仅表示**传入数据的
一致性检查未出现冲突或未知**，不证明实际可售、预订成功、安全或全市场最优。
`basis`、费用类别和来源字段是调用方声明，这个纯工具不会认证用户原话、供应商身份或报价
真实性；把输入标成 `query_quote` 不等于它来自真实查询。复核时仍需检查会话原始证据。

实现：[比较契约](../src/travel_tools/schemas/plan_comparison.py)、
[比较计算](../src/travel_tools/plan_comparison.py)。

## 2. 会话内的只读需求与结论复核

`review_itinerary` 仅在正式 LangGraph 会话内提供，属于会话内部工具；legacy 不提供它，
既有 `/tools/{name}/call` 也不直接暴露它。工具接收以下两种目标之一：

- `base_version_id`：同一会话当前已发布版本，或当前运行已保存的草案版本。
- `document`：尚未保存的候选行程；可用 `available_source_ids` 导入本会话已查来源。

服务端复用正常保存前的准备边界，校验真实用户消息 ID 和精确原话、锁定保护、来源导入、
查询条件、旧证据适用性及报价口径。版本必须属于当前会话，不能把其他会话或任意旧版本
当作当前复核对象。复核不创建或发布新行程版本，不覆盖当前行程；报告标记 `state=read_only`。
已保存版本保留原文档，重新检查来源适用性；不能用 `available_source_ids` 改写已有版本。
新增来源或方案修改应形成新候选，避免复核时把原本的未知费用包装成已证明结论。

模型可以提供 `requirement_assessments`，逐项说明有效需求及其关联的活动、费用、住宿或来源。
工具检查关联 ID 是否存在、原话引用是否真实、已发布需求是否被遗漏，以及锁定项是否被改动。
模型声称 `satisfied` 只保留为 `claimed`，语义满足仍为 `unknown`；目标存在和原话引用真实
不能证明模型理解正确，也不能证明一条用户消息中的全部要求都已提取。

`claims` 提供可计算的原子结论，检查金额与币种、人数/房间数、安排开始或结束时间及预算。
不同结论依据必须分清：

| 结论 | 当前能核对的内容 | 不能据此推定的内容 |
| --- | --- | --- |
| `cost_amount`，`basis=planned` | 结论金额与文档费用记录是否相等，以及显式传入的计价口径是否一致 | 费用是真实报价、已含税费或现实可售 |
| `cost_amount`，`basis=verified_quote` | 在真实工具历史和来源快照中匹配金额、币种、人数/房间/间夜、税费与适用条件 | 所选航班、车次、酒店或房型与这笔金额的完整身份绑定 |
| `within_budget` | 声明费用范围内的报价记录、必要费用完整性、币种、税费和人数口径 | 用户原始需求的全部必要费用已覆盖、未来价格不变、实际库存或订单已核验 |
| `item_start` / `item_end`、`party_size` | 结论与结构化安排时间、人数和房间数的一致性 | 场馆当天开放、交通可用或整份行程现实可行 |
| `source_statement` | 来源 ID 是否存在及相关来源是否需复核 | 任意一段自然语言已经被来源正文证明；这项语义保持 `unknown` |

费用金额匹配成功仍不认证商品身份。例如来源中有一笔相同金额，不足以证明方案选中的车次
或房型就是这笔报价对应的商品。当前也不自动解析并验证最终任意正文；完整需求覆盖、复杂
语义、安全、预订和上游事实真实性均保留边界，不能把报告里的某个 `pass` 扩大为整体成功。

报告将遗漏、冲突、未知和反馈返回给原 Agent。LLM 自主决定后续处理，不强制自动重试、
二次模型审核或某种交付模板。修改锁定项仍遵守原保存协议，复核工具不能解除保护。

实现：[复核契约与原子检查](../src/travel_agent/plan_grounding.py)、
[会话边界](../src/travel_agent/quality_tools.py)、[行程版本规则](itinerary-versions.md)。

## 3. 统一 LangGraph HTTP/SSE 评测

入口为 [scripts/evaluate_agent.py](../scripts/evaluate_agent.py)，用例为
[evals/agent_cases.json](../evals/agent_cases.json)，格式版本 `schema_version=1.0`。
每个用例启动正式 `create_app`，固定 `agent_engine=langgraph`，通过会话 HTTP API 发送
消息及待答 `question_id`，读取公开状态、版本和 SSE 重放。评测与网页使用同一
`DurableService` / `GraphRuntime`，没有另写一套模型工具循环。

每个用例都使用隔离的临时 SQLite 与 checkpoint 数据库，覆盖配置中的用户数据库路径，
结束后关闭并清理。回放按正式 Schema 验证后的完整参数匹配，不要求固定工具次序。
查询参数不匹配时返回 `evaluation_replay_miss`，没有回放的外部工具禁用；错误不变成空票
或零报价，也不回退到真实供应商。

| 模式 | 模型 | 数据来源 | 能用于什么判断 |
| --- | --- | --- | --- |
| 默认 `offline` | 明确标记的脚本回复 | 显式供应商回放和本地计算 | HTTP/SSE、追问恢复、锁定、patch、比较与复核契约回归；全部 Agent 语义质量状态为 `unknown` |
| `--live` | 当前配置的真实模型和参数 | 回放/禁用外部供应商；演示候选显式标记 | 在给定合成候选或回放证据上的模型调用及结构化断言，不是实际旅行验收 |
| `--live --live-providers` | 当前配置的真实模型和参数 | 实际配置的供应商 | 记录有限真实样本的表现、覆盖与失败；不代表全部日期、来源或任务的成功率 |

不静默切换模型、修改执行预算或增加上下文/输出/工具总次数上限。模型与供应商的真实模式
均需显式选择；`--live-providers` 不能单独使用。真实模式消耗配置的额度。

用例覆盖未知目的地、部分回答、局部“你决定”的授权范围、已订锁定与局部修改、费用缺失、
来源受阻、候选比较、草案复核及原子结论的 fail/pass/unknown。断言用结构化 JSON 路径核对字段或明确 `unknown`，不按问
次数、固定工具顺序或正文关键词评分。`oracle_scope` 区分 `runtime_contract` 与
`structured_requirement`；完成状态只能证明本轮完成，不能证明任务语义质量。

报告分别给出 `task_completion`、`constraint_preservation`、`evidence_support` 和
`clarification_relevance` 的断言状态与有限范围的质量状态。没有独立可判定依据的最终正文、
推荐质量和追问相关性保持 `unknown`。这些字段不是统一的 Agent 质量分，也不是公开基准
成功率，脚本答案正确不会被算成真实 LLM 推理通过。结果中的唯一 ID 列表另提供
`latest_by_id` 派生索引，按结论/候选身份检查；原始列表保留，不依赖模型的排列顺序。

记录包括用例版本与哈希、实际 model/provider/thinking 参数、prompt/tools/engine/context
版本、执行预算、持久化运行的实际 active_seconds/请求消耗、耗时、实际终态、模型请求和
原始 usage、工具参数/结果轨迹及 SSE 序号。`runtime_marks` 记录既有耐久边界回调的时间，
用于区分模型响应、效果提交和执行收尾；这些诊断不改变业务图、效果内容或公共事件。
缺失 usage 为 `unknown`，不完整汇总为 `partial` 和 `null`，不会用估算填补 token 用量。
隐藏推理和模型密钥不写入报告，但报告含私有输入、来源与工具参数，默认保存到 Git 忽略的
`private/evaluations/agent-report.json`，不作为公共网页内容或公开附件。

## 4. 验证入口与后续范围

从仓库根目录执行：

```sh
uv run pytest tests/test_plan_comparison.py tests/test_plan_grounding.py tests/test_agent_quality_tools.py tests/test_agent_evaluation.py -q
uv run python scripts/evaluate_agent.py
```

只验证真实模型对给定演示候选的比较，可显式执行：

```sh
uv run python scripts/evaluate_agent.py --live --case common_basis_candidate_comparison --output private/evaluations/comparison-live.json
uv run python scripts/evaluate_agent.py --live --case atomic_claim_review --output private/evaluations/review-live.json
```

这条命令不启用实际供应商。需要真实来源的有限样本时，再显式选择 `--live-providers` 和
合适用例。所有断言、终态、来源覆盖与 token 结果以当次私有报告为准。

2026-10-10 本轮验证：后端全量 852 项通过，24 项 PostgreSQL 用例因未配置独立测试库而
跳过；前端 10 项测试及生产构建通过，Ruff 和三个本地校验示例通过。统一 suite v2 的五个
离线案例完成，脚本模式的 Agent 语义质量保持 unknown。

真实模型保持 deepseek-flash、thinking=true、reasoning_effort=high，供应商回放/禁用：
候选比较的三项结构化断言通过（约 8.81 秒、两次模型请求）；原子复核的五项结构化断言
通过，错误人数 fail、文档金额 pass、预算结论 unknown。复核确认运行约 9.80 秒、三次模型
请求，包含一次工具 JSON 语法错误后自主修正；仅复核，不创建版本。任意最终正文语义仍为
unknown，没有将合成数据当真实出行验收。

首轮原子复核观测到约 254.16 秒的总耗时，而两次模型请求合计约 4.88 秒。
答案事件构造与终态更新之间存在约 248 秒的延迟；现有原报告不足以再定位到具体 await，
追加真实运行及触发 heartbeat 的离线诊断均未复现。保留原始私有报告，不归因于模型；
后续报告补充运行消耗与耐久边界计时用于继续观察，不承诺稳定延迟。

提示词/工具 manifest 变化后，旧挂起运行仍按既有 runtime_version_changed 协议停止并保留
历史，需要重新发送需求；不宣称旧执行可以无缝换用新工具。既有行程版本不会因此改写。

后续可在已有观察记录上增加人工标注或独立语义 oracle，扩展商品身份绑定与完整需求覆盖，
再比较不同提示词或工具契约的影响。更大规模的候选组合搜索和约束求解器另行评估；MCP、
来源健康状态、多用户与多进程、地图/时间线/导出也不因本次工具升级自动完成。
