# 长期偏好设计：官方产品与框架参考

研究日期：2026-10-11（America/Chicago）。本次实际搜索并打开下列官方页面，结论来自
页面正文，不以搜索摘要当作已核实的产品说明。这里比较设计机制，没有产品排名、市场份额
或全平台可用性判断；账号、区域和后续版本可能影响厂商功能。

## 产品控制：看得见、改得了、能停用

ChatGPT Learn 的 [Memories](https://learn.chatgpt.com/docs/customization/memories) 区分
ChatGPT web 的记忆与 local Codex 的独立记忆存储，并说明全局与聊天级的使用/生成控制。
[Personalize ChatGPT](https://learn.chatgpt.com/docs/personalize) 将稳定偏好和跨聊天上下文
作为可由用户管理的个性化能力。我们借鉴控制粒度与偏好可覆盖的思路；没有照搬 Codex 的
后台提取、聊天摘要或本地 memory 文件机制，也不将不同产品的控制面混为同一实现。

Gemini 官方的 [Manage what Gemini remembers about you and customize responses](https://support.google.com/gemini/answer/16598625?co=GENIE.Platform%3DDesktop&hl=en)
说明用户可添加、查看、编辑、删除长期 instructions，并能开关使用。旧“Saved info”入口
在本次访问时已重定向到个性化说明，因此本记录引用当前页面，不假定所有账号界面仍采用
旧名称。借鉴点是独立的用户管理面板及停用控制；并未将厂商的自动保存策略当作本项目授权。

Google 的 [Get personalization with memory of your past Gemini chats](https://support.google.com/gemini/answer/16598469?co=GENIE.Platform%3DDesktop&hl=en)
也区分旧聊天和连接应用的上下文来源。由此得到的本项目设计判断是：只删除偏好表里的当前
条目，不能据此宣传所有历史副本已消失。这个判断不是声称本项目拥有 Gemini 的过去聊天
检索或连接应用能力。

## 框架职责：聊天恢复与跨会话偏好分别存储

LangGraph 的 [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
明确区分 thread 内图状态的 checkpointer 与跨 thread 应用数据的 store。
[Stores](https://docs.langchain.com/oss/python/langgraph/stores) 使用 namespace 和稳定 key
组织数据，示例按用户划分记忆，并说明向量检索需要额外配置，不是所有持久记忆的前提。

采纳其职责划分：图 checkpoint 保持已有执行位置权威，长期偏好放入应用级结构化表，以
服务端 owner 和稳定 ID 定位。当前产品规模下使用既有 SQLAlchemy、PostgreSQL/SQLite 和
Alembic 即可；不为少量可编辑条目新增 embedding 服务或引入 SDK Store 的另一套权威。

## 数据治理：作用域、条目身份和来源元数据

Mem0 的 [Entity-Scoped Memory](https://docs.mem0.ai/platform/features/entity-scoped-memory)
与 [How Mem0 Works](https://docs.mem0.ai/core-concepts/how-it-works) 说明记忆按用户、Agent
或运行等标识限定作用域，并通过 metadata 辅助过滤与管理。
[Update Memory](https://docs.mem0.ai/api-reference/memory/update-memory) 和
[Delete Memory](https://docs.mem0.ai/api-reference/memory/delete-memory) 展示按独立 ID 更新
或删除条目的操作。我们借鉴作用域、条目身份与来源元数据；不接入其云服务，不复制文档
示例代码，不采用其自动提取或向量检索来替代完整会话历史。

## 本项目实际选择

| 参考机制 | 本项目采纳 | 本轮不采纳或不能宣称 |
| --- | --- | --- |
| 用户管理面与开关 | 条目查看/新增/编辑/删除；全局开关和本次使用开关 | 关闭等于删除全部聊天历史或完全失忆 |
| 显式偏好与跨聊天复用 | 批准后的结构化条目跨会话可读；本次要求优先 | 自动把本次安排、天气、票价或网页描述写成长期事实 |
| checkpointer/store 分工 | 偏好表与消息、图检查点分离 | 已接入 LangGraph BaseStore、厂商云记忆或跨聊天检索 |
| 用户作用域与 metadata | 服务端 local-user、稳定 ID、来源、更新时间 | 该 owner 字符串就是生产身份认证或租户隔离 |
| 自然语言记忆请求 | 模型仅提出候选，当前最新原话为来源，用户看卡后批准 | 原话存在就能证明语义解释正确或长期保存已获授权 |
| 可更新与删除 | revision/CAS 拒绝旧覆盖；档案修改使旧待保存卡失效 | 旧偏好删除后仍可用旧卡悄悄重新写入 |

待保存卡的明确批准要求是本项目的产品选择，不是“主流产品一律要求二次批准”的市场
结论。手动编辑页面的保存动作已经是用户明确操作，无需让模型重复确认。

本次用户要求使长期偏好从原先暂不实现的范围变为已授权实现。本轮仍保持单 Agent 自主
ReAct、DeepSeek Flash、只读旅行查询及完整历史策略。档案是规划依据，不是供应商证据。
各项操作的协议、历史保留与验收边界见[实现说明](../user-preferences.md)。

## 研究与实现的区分

上面的官方页面提供设计参考，不证明本项目测试通过。本地作用域、CAS、防旧执行写入、
当前原话限制、提议和效果同事务以及执行期间拒绝设置变更，属于本项目结合已有 durable
runtime 做的工程选择，不能归因于某个外部产品的原样实现。后续结果以代码、迁移与独立
测试报告为准，不在本研究记录中预先填写通过数量。
