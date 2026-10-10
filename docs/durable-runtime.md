# M1：持久化自主 ReAct 的实现与运行

日期：2026-10-10。默认 `AGENT_ENGINE=langgraph`。应用仍要求单进程、单 worker。
本次仅迁移执行、恢复、问题关联与事件持久化；行程版本/局部修改闭环另见
[下一轮计划](next-iteration.md)，尚未作为产品能力交付。

## 实现与三类权威

`graph_runtime.py` 使用低层 `StateGraph`，节点为 model、tools、wait、final。
模型自主选择提问、工具及交付，图没有天气→交通→酒店等业务步骤。
阶段A已将显式安全的独立只读查询并行、效果逐项独立提交，再按原调用顺序合并历史；
状态操作保持顺序边界，mixed ask_user整批返回原协议错误。细节及新增恢复验收见
[查询运行说明](query-runtime.md)。
`ChatModel` 和 `ToolRegistry` 原样复用，不转换为 LangChain 消息。

| 权威 | 保存内容 |
| --- | --- |
| `agent_journal` | 原始用户/模型消息、原始工具JSON字符串、发送前提交的system运行状态快照；按会话seq追加；旧history一次性导入，Conversation.history仅为兼容投影 |
| run/request/effect/question/event | 已提交模型和工具结果、尝试记录、请求幂等、问题消费、预算、最终交付及公开事件 |
| LangGraph native checkpoint | 执行位置、待运行任务、interrupt与pending writes；应用canonical引用不另存next_node |

固定依赖：LangGraph 1.0.5、checkpoint-sqlite 3.0.1、checkpoint-postgres 3.0.2；
全部传递版本见 `uv.lock`。aiosqlite固定0.21.0，避免0.22移除Thread接口后与本版saver不兼容。
PostgreSQL应用表使用SQLAlchemy/asyncpg，标准异步saver使用psycopg。
Windows的saver在专用Selector循环运行，主应用保留Proactor以支持Playwright子进程。
SDK目前有一条上游弃用提示，不影响已验收行为；升级须重做恢复测试。

## 幂等、问题与恢复

- `conversation_id`稳定；逻辑图thread为`conversation_id/langgraph-v1`。每条新接受的用户
  输入生成一个run；服务恢复沿用原run和累计预算。回答新建run并继续同一图thread。
- 客户端`request_id`是会话内幂等键。同键同正文/问题返回原run公开事件；同键不同输入409。
  单会话运行时拒绝其他输入。旧客户端省略request_id仍可使用，但重试无法去重。
- question_id由产生问题的run/model/tool效果键派生，供应商tool_call_id重复也不冲突。
  ask_user原工具结果只保存一次且不改写。先保存draft，再进入纯interrupt节点；
  只有等待checkpoint已提交后，才绑定checkpoint、发布ready问题并释放执行归属。
- 回答在同一应用事务中保存原始用户消息、请求、单次问题消费和新run；resume传保存的引用。
  已接受但尚未resume可重试；已前进到新问题时不会把旧答案再次投入新interrupt。
  前端发送question_id；旧客户端仅在唯一ready问题且checkpoint一致时允许省略。
- 已提交model/tool效果复用；外部调用结束但未提交时允许重新发起只读查询，attempt_log标明
  previous_completion=unknown。重取保留新查询时间，不承诺外部服务exactly-once。
- 最终业务结果先提交，END尚未提交时恢复只补齐图结束与公开状态，不重新生成或重复发布答案。
- 每个run保存模型参数、预算、提示词版本和工具契约manifest。恢复时配置不一致明确结束，
  不默默使用新参数继续旧请求。新接受的用户消息可以使用当前配置。

所有应用事实和canonical checkpoint引用都通过同一事务中的owner/lease_epoch条件更新。
标准saver写入独立不可变代际，然后在应用事务中发布canonical引用；pending writes也先
复制原native tuple及全部pending writes到新代际。旧owner可以留下不可达SDK记录，但不能
影响canonical checkpoint或业务记录。后续可另做代际清理；M1不自动删除私有恢复证据。

## 预算、公开事件与隐私

每条新接受输入默认12次模型请求、300秒主动执行，模型单请求120秒；不加上下文、输出或
工具总次数上限，不传max_tokens。图recursion_limit只作极高内部保护，不先于业务预算结束；
40个工具的单批次已有验收。完整历史和供应商附加字段继续回传，不摘要或裁剪。
首条行为策略和单运行工具定义保持稳定；动态时钟、预算和行程索引在闭合工具批次后追加
到历史，发送前受owner/epoch围栏提交。已完成模型效果的恢复不重复追加或记账；未知尝试
保留旧快照并为新尝试追加准确状态。协议、观测与真实DeepSeek验收见[上下文缓存](context-cache.md)。

模型请求前提交预算预留；进程在预留与真正发送之间退出时，该次也保守占用预算，记录
未知完成的尝试；无法声称供应商一定收到。真实重发再次计数。主动时间每0.5秒提交并在
正常暂停/结束时补齐尾部；硬退出可能少记一个心跳间隔及当次数据库提交延迟。等待用户、
停机时间不计入主动执行。数据库持续不可用时不保证精确尾部，恢复不重置已经提交的预算。

公开事件持久保存连续event_seq、稳定message_id。POST消息继续返回SSE；
GET `/conversations/{id}/events?after=N`支持重放，也接受Last-Event-ID。
队列仅跟随持久事件，不是事实来源。刷新取得公开对话与同一快照的event_seq，再重连进度；
轮询作为服务重启时的补偿。浏览器sessionStorage保存尚未确认完成的请求标识以便安全重试。
公开输出只有用户正文、助手正文、问题及工具状态，不暴露隐藏推理、原始工具参数或私有图state。

## 数据库部署与回退

应用表使用可逆Alembic revision 0002；第三方saver schema由SDK `setup()`独立管理，
不声称归应用Alembic管理。SQLite原会话库和同名`.graph`检查点库都要备份；PostgreSQL
需要包含应用与SDK表的完整一致备份。数据库有旧conversations表但无alembic_version时，
先核对0001 schema完全一致并备份，再stamp 0001，随后upgrade head；不要盲目stamp。

生产数据降级前必须停服务、备份并检查挂起图线程。0002 downgrade仅验证在隔离库可逆，
不表示删除图表后旧程序还能恢复图会话。`AGENT_ENGINE=legacy`仅影响未绑定的新会话；
已有langgraph-v1线程继续使用兼容图。未知engine版本需要显式迁移，不直接交旧runner。
单进程startup会接管原活动run并递增epoch；不得同时启动第二个应用实例或多个worker。

## 验收证据

- `tests/test_graph_runtime.py`：连续问题、部分回答/你决定/未知目的地、同供应商call_id多次
  使用、原始字段/工具字符串、答案幂等和冲突、事件顺序/隐私、回退引擎绑定、时间预算及40工具批次。
- `tests/test_graph_restarts.py`：真实子进程`os._exit(73)`后重启；SQLite和真实PostgreSQL17
  各验证draft、waiting checkpoint、ready、answer accepted、resume advancement、新问题、
  模型调用中/保存后、工具返回未保存/保存后、final保存未END、累计请求预算；原生checkpoint、
  pending writes及结果旧owner写入拒绝；Alembic升降级。不是仅关闭连接或重建对象。
- `frontend/src/api.test.ts`：未知投递时保留request/question ID、不完整SSE保留请求、冲突提示。
- 完整Python回归653项通过；追加双后端checkpoint围栏2项也通过。前端6项通过，TS/Vite构建通过。
- 新默认引擎切换后，API/迁移/图回归37项通过；最新双后端进程恢复与围栏32项通过。
  本地原会话库已备份至`private/backups/m1-20261010T065617Z`，迁移至0002且保留7个会话；
  重启后`/agent/health`确认langgraph/deepseek-flash，5173网页及其API代理均可访问。
- `scripts/live_probe_durable.py --live`：隔离库真实DeepSeek Flash、thinking enabled/high；
  追问后自主调用天气、地点、路线和开放时间，8次模型请求、约61秒主动执行、最终completed；
  原始reasoning_content保留且公开事件不暴露。结果在忽略的artifacts目录，不写用户会话。
  这是有限样例，不能保证所有供应商、城市和日期均可用。

复现PostgreSQL测试时，设置`TRAVEL_TEST_POSTGRES_URL`为**独立测试服务器**，账号须能
创建/删除临时数据库。每个用例创建m1随机库并在结束后删除，绝不可指向生产测试权限不明的服务。
未设置该变量时PostgreSQL用例明确skip；SQLite通过不替代PostgreSQL验收。

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
pnpm --dir frontend test
pnpm --dir frontend build
uv run python scripts/live_probe_durable.py --live
```
