# 阶段 A：查询速度与可靠性

日期：2026-10-10（America/Chicago）。默认 LangGraph 自主 ReAct，单进程、单 worker。
模型仍自主决定工具、顺序和是否追问，没有出行业务 workflow；依赖前次结果的查询由后续
模型轮次产生。只读规划，不接下单、支付、取消、长期偏好或新执行基础设施。

## 执行与保存

ToolSpec.execution 默认 exclusive，未知安全性不并行；现有已审核只读工具显式声明
parallel_read。同一模型批次内连续的安全查询形成并行段，最多启动总工具并发数个 worker，
没有工具总次数上限。状态操作和未知工具是顺序边界，前段完整保存/有序合并后才能执行
下一段。save_itinerary 保持会话事务语义，ask_user 保持独占及混合批次整批报错协议。
legacy runner 仍串行，新图会话使用默认引擎；回退不把已有图线程交给 legacy。

每项完成即独立保存 Effect，不等待较早的慢工具。之后按原 tool_calls 顺序在一个围栏
事务中合并整段 Journal；每个效果键仅追加一次，下一次模型请求前整批历史完整。
公开完成事件按真实完成顺序出现，稳定 call_id 使用 run/model/tool 索引，同名调用互不覆盖。
原始工具字符串、完整模型消息及 DeepSeek reasoning_content/其他附加字段均不裁剪。

结果、事件、预算心跳、历史合并和最终交付均经过 owner/epoch 条件更新，Head 行锁保护
并发 seq 分配；事务不跨网络等待。恢复复用已提交结果，只重试未知/未提交只读效果，
attempt_log 保留 previous_completion=unknown。内部提交幂等不等于外部 exactly-once。
超时修复按原顺序填充真正未知项，已提交成功结果不会改写为失败。
主动执行预算按实际壁钟计时，包括排队和资源收尾；并行工具时长不会逐项相加。

## 有界工具、供应商与浏览器

| 控制项 | 默认并发 | 默认启动频率 |
| --- | --- | --- |
| 全应用工具 dispatch（MAX_CONCURRENT_CALLS） | 4 | 无统一频率限制 |
| 高德 amap | 2 | 5 次/秒 |
| 和风 qweather | 2 | 3 次/秒 |
| 博查 bocha | 1 | 2 次/秒 |
| 公开网页 public_web | 2 | 2 次/秒 |
| 12306、bus365、ceair | 各 1 | 各 1 次/秒 |
| FlyAI、聚合 juhe_train、极速 jisu_coach | 各 1 | 各 1 次/秒 |
| 浏览器运行时 BROWSER_MAX_CONCURRENT_QUERIES | 2 | 按实际来源控制 |

SUPPLIER_LIMITS 是按来源名配置的 JSON，例如
`{"amap":{"concurrency":2,"requests_per_second":5},"bocha":{"concurrency":1,"requests_per_second":2}}`。
未列出的来源保守使用 1 并发、1 次/秒。频率为 0 时仅关闭频率控制；并发限制仍生效。
启动频率与同时在执行的数量独立。浏览器频率计一个查询会话，网页资源加载不伪称为单个
HTTP 请求；12306 站点目录 HTTP 请求也计入该来源。FlyAI 另有共享客户端互斥锁，
即使将供应商并发调高仍保守串行：其 guard 将 home/tmp 指向共享状态，尚未证明独立目录
的设备与会话语义。子进程超时/取消会 kill 并 communicate 回收，不声称已实测 CLI 并行。

供应商 gate 位于实际传输/CLI/浏览器边界，包含开放时间 fanout、天气多请求、网页重定向
和铁路 fallback。父工具只持总工具容量，不持同一个子请求 gate，避免非重入死锁。
当前不新增自动重试，429/访问防护明确返回；配置的铁路降级保持原适用错误范围，候补、
未开售和无票不当作查询失败。工具 deadline 继续包含排队，子请求 deadline 也包含限流等待。

浏览器线程池最多 2 个真实工作；每项查询独立 browser/context/page，线程内在 Windows
使用 Proactor，主服务循环不改。排队取消不会提交线程工作。运行取消向 worker 发送停止
信号并等待 browser/driver 真正关闭；async await 取消不会提前释放容量。运行查询与清理
均有期限，清理可延长取消返回的时间；关闭等待 executor 收尾、清空缓存，并阻止关闭后回填。

## 数据复用与公开进度

缓存是进程内最多 256 项的完整结果 LRU，不是历史摘要或长期偏好。键包含工具、完整
Pydantic 规范化参数以及私有配置指纹（凭据、端点、来源/降级、浏览器和安全设置）；
日期/偏移时区、站点、车型/航班、人数/儿童年龄、房间/间夜、币种/税费口径等均不丢失。
来源选择随完整参数和配置区分；降级结果不缓存，避免主来源恢复后继续使用降级候选。
独立在途请求不合并，各自记录和取消，故不存在一个等待者取消其他调用的问题。

| 数据 | 默认有效期 | 配置 |
| --- | --- | --- |
| 地点/详情 | 600 秒 | CACHE_PLACES_SECONDS |
| 天气 | 120 秒 | CACHE_WEATHER_SECONDS |
| 路线 | 60 秒 | CACHE_ROUTES_SECONDS |
| 搜索/网页/开放时间 | 120 秒 | CACHE_WEB_SECONDS |
| 票务/酒店 | 20 秒 | CACHE_QUOTES_SECONDS |

设为 0 关闭相应缓存。TTL 表示允许复用快照的时间，不保证未来价格/库存或开放事实。
ToolResult.reuse=cache 附 original_started_at/original_finished_at；data 中原 queried_at、
retrieved_at、data_time、来源、未知字段和 coverage 不改写。当前 dispatch 的时间只表示
本次操作，不能称为新报价。真空结果、未知、未开售、候补保留区别；错误、未配置、受阻、
内部来源失败和 fallback 不缓存。浏览器原有可选原始页缓存默认仍为 0，保留原取数时间。

模型可对外部只读工具传 force_refresh=true，HTTP envelope 也支持 force_refresh；
它绕过工具及浏览器缓存，不绕过限流、访问防护或预算。参数完整保存在调用历史。

公共事件区分工具排队、执行、复用、成功与失败；supplier_progress 还保留真实子请求的
来源、request_id 和排队/执行/结束。前端按稳定调用 ID 显示同名查询，支持旧引擎事件；
GET 对话快照从同一 event_seq 以内的公开事件重建 tool_progress，刷新可恢复当前状态，
SSE 保持重放和去重。不输出百分比、工具参数、密钥、隐藏推理或完整私有图状态。

## 验收与实际边界

测试入口 test_query_scheduling.py、test_browser_runtime.py、test_graph_restarts.py 和前端
progress.test.ts。覆盖并发重叠/上限、未知工具及状态边界、局部失败、排队超时、取消清理、
内部 fanout、缓存参数/时效/原时间/失败语义、120 项工具批次和下一请求完整原始历史。
既有追问、部分回答、限定“你决定”、未知目的地及行程版本保护继续回归。

SQLite 与真实 PostgreSQL 的独立 OS 进程 os._exit(73)/重启验证三个新增窗口：
后一个快工具已保存而第一个仍运行；所有 effects 已保存但 Journal 未合并；Journal 已合并
但 graph checkpoint 未写。已保存快结果不再查询，未知慢结果允许重取；顺序/完整性在
下一模型请求中断言，重复请求不新增模型调用。原 owner/checkpoint/事件围栏和迁移验收保留。

`python scripts/benchmark_queries.py` 用同样 8 项、每项 0.15 秒的确定性查询走完整图执行：
本轮串行 1.964 秒、并行 0.796 秒，约 2.47 倍；实际峰值 1/4，区间无重叠/有重叠，
均 8 次查询、8 成功、0 失败/未知，返回合同相同，串行/并行主动时间约 1.828/0.743 秒。
这包含数据库、图和事件开销，非只测 gather。

`--live` 另测故宫地点（北京、page_size=1）、北京每日天气（GCJ02 116.41/39.92、days=1）
及官方开放时间网页搜索（count=1）。本次串行新查询 0.924 秒、并行新查询 0.410 秒，
各 3 次真实来源请求、0 失败；随后缓存约 0.0004 秒、0 来源请求。样本很少、网络有波动，
不能把该比值当稳定 SLA。网页搜索成功不等于指定日期开放确认；时次/证据缺口仍保持。

真实 DeepSeek Flash/thinking enabled/high：隔离库、2 次模型请求、14.42 秒壁钟，
约 14.30 秒主动时间。自主同批调用地点、天气、开放时间，8 项供应商请求；三项工具成功，
完整 reasoning_content 保留且公开不可见。内部网页取数成功不证明任意日期事实全部核实。
查询未侵入用户会话；原始证据在忽略的 artifacts/query-*，不提交私人库或凭据。

真实 Chromium 两项查询 0.870 秒，独立 context、执行重叠；实际取消清理约 0.096 秒，
运行超时报 provider_timeout，关闭后新增 Chromium PID、工作线程、缓存项均为 0。
有限样例不替代所有网站、操作系统和供应商账号的持续验收。

本轮完整 Python 回归 712 项通过（含真实 PostgreSQL）；最后追加缓存状态与进度刷新断言后，
查询专项 22 项通过，前端 9 项及 TS/Vite 构建通过，Ruff check/format 与 3 项示例通过。
并行恢复和追问复查 8 项通过。完整回归与后来追加的专项分别记录，不混称一次运行。
实现提交 355923e。本地后端已更新，无数据库 schema 迁移；重启前确认 0 运行中会话，
通过 SQLite backup API 备份应用库及检查点库至
private/backups/queries-20261010T103241Z，重启保留当时全部 9 个会话。
