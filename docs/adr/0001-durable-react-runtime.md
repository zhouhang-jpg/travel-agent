# ADR-0001：以低层 StateGraph 验证持久化自主 ReAct

状态：M1 已实施并完成 SQLite / PostgreSQL 恢复验收；默认新会话使用 LangGraph。
实现、测试证据与限制见[持久运行说明](../durable-runtime.md)。
日期：2026-10-10（America/Chicago）。现状基线：`f16de6c`。

## 背景与决定

当前轻量runner、多轮提问、完整历史及校验器已经可用。下一阶段要解决进程恢复、问题/答案
关联、重复请求和已提交结果重放；继续给现有while循环附加独立调度/检查点框架会增加双重
执行权威。选择LangGraph低层StateGraph承担执行位置和interrupt，不以完整LangChain栈
重写模型、工具或消息。不引入多Agent或预设出行场景workflow。

保留React/TS/Vite和FastAPI/Pydantic模块化单体、现有ChatModel/ToolRegistry、DeepSeek
Flash与完整原始消息；LangGraph不是新事实来源。现有runner只在迁移期提供基线和新运行
回退。MCP是之后的工具薄适配，不是恢复纵切的依赖。

## 权威与切换门槛

原始journal负责完整payload；效果/业务结果/公共事件负责已提交事实、幂等、执行归属和预算；
图checkpoint是唯一恢复位置。应用不再造另一个next_node/checkpoint调度器。由于跨连接
事务并非天然原子，图节点通过稳定效果键复用已提交结果，未知完成的只读查询允许标记重试。
checkpoint写入也须有fencing，不能仅给最终结果加run_id条件。

M1只交付最小持久化ReAct和可恢复追问：同会话多次等待、部分回答/你决定、未知目的地、
真实进程退出/恢复、答案幂等、旧owner写入隔离、SSE重放及原始DeepSeek字段续跑全部通过。
PostgreSQL部署目标单独实测；SQLite成功不替代它。默认12次模型请求/300秒主动时间按
原含义保留，等待/停机不计时，同run恢复不重置；无工具次数、上下文或模型输出新上限。

回退开关主要影响新运行。已挂起图线程绑定engine/state版本，通过兼容图继续，或走显式
恢复迁移；不把图checkpoint塞给旧runner，不让两个引擎同时执行同一run。

## 代价与不选方案

- 增加固定版本库和saver依赖；隔离验收后接入应用。采用不可变代际存储与事务保护的canonical checkpoint引用；Windows PostgreSQL saver使用专用Selector I/O循环以兼容Playwright的Proactor主循环。
- 首阶段单进程，换框架/数据库不代表多实例或多人鉴权完成。
- 不选择高层预制Agent/消息转换作为迁移前提，以免丢供应商字段或隐含摘要/裁剪。
- 不选择“先造全套自研checkpoint再接图”，也不选择MCP-first或整套服务拆分。
- 不采用跨连接的非原子check-then-write。旧owner的checkpoint及pending writes只能产生未发布代际，事务围栏阻止其成为canonical图状态。

完整Runtime接缝、标识、迁移批次和验收表见[技术路线](../technical-roadmap.md)。
