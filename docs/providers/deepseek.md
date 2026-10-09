# DeepSeek 与可替换模型接口

默认接入目标为 `deepseek-flash`，官方 API 根地址为 `https://api.deepseek.com`，
实际请求发送到 `https://api.deepseek.com/chat/completions`。此标识的版本映射可能变化，
运行记录应记录实际模型标识与参数。测试通过 HTTP MockTransport 验证协议；
单元测试不会读取真实密钥或调用真实模型。

## 统一接口

`ChatModel.complete(messages, tools)` 接收完整历史与工具定义，返回 `ModelReply`：

- `message`：校验后的完整 assistant 消息，包括工具调用和供应商附加字段。
- `finish_reason`：`stop` 或 `tool_calls`；输出达到长度限制时抛出错误。
- `usage`：仅保留供应商 usage 对象的数值字段，不保留字符串或嵌套对象。

业务运行器只依赖 `ChatModel` Protocol。`OpenAICompatibleModel` 通过调用方注入的
`httpx.AsyncClient` 发出非流式请求；调用方负责关闭 client。`base_url` 是 API 根地址，
可以带 `/v1` 等路径，但不要包含 `/chat/completions`。禁止 URL 凭据、查询、片段以及
非 HTTPS 地址；本地开发允许 `localhost`、`127.0.0.1` 和 `::1` 上的 HTTP。

`provider="deepseek"` 时发送 `thinking.type`（`enabled` 或 `disabled`）以及
`reasoning_effort`。默认启用思考、effort 为 `high`、`max_tokens=4096`、超时 60 秒。
这些默认值是初始实现参数，后续应根据真实评估调整。
`provider="openai_compatible"` 不发送 DeepSeek 专有的上述两个参数。
其他厂商如果使用不同协议，应实现另一个 `ChatModel`，不能假设修改 URL 就完全兼容。

## 完整历史与私有字段

根据 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)
（2026-10-09 核验），带 tools 的请求需要回传历史 assistant 消息中的全部
`reasoning_content`，包括跨用户轮次的历史。因此运行器应把 `reply.message` 完整加入内部
历史，不能只重建 `role`、`content`、`tool_calls`。适配器也保留其他供应商附加字段。

`reasoning_content` 仅用于内部协议连续性，不能传给 UI 或作为公开的思考过程展示。
API 层应白名单选择公开字段，而不能直接序列化 `ModelReply.message`。运行日志也不应
输出完整请求、原始供应商响应、密钥或私有推理字段。

工具参数是否为合法 JSON、是否满足工具 Schema，由工具执行器处理；模型层只校验
工具调用记录的结构及调用 ID 的唯一性，允许无正文但具有有效工具调用的 assistant 消息。

## 失败与资源边界

所有可预期模型失败以 `ModelError(code, message, retryable)` 返回安全错误信息，
不转发原始响应体、URL、供应商异常文本或密钥。不自动重试，不自动切换模型。

| code | 含义 | retryable |
| --- | --- | --- |
| `configuration_error` | 参数或服务地址不合法 | false |
| `authentication_error` | 401/403，鉴权或访问被拒绝 | false |
| `rate_limited` | 429，触发服务限流 | true |
| `timeout` | HTTP 超时或整个调用超过时间预算 | true |
| `connection_error` | 网络请求失败 | true |
| `service_unavailable` | 上游返回 5xx | true |
| `request_failed` | 其他 HTTP 拒绝或请求编码错误 | false |
| `invalid_response` | JSON、消息结构或结束原因不合法，或无工具且正文为空 | false |
| `response_too_large` | 响应超出 2 MiB | false |
| `output_truncated` | `finish_reason=length`，答案或工具参数尚未完成 | false |

适配器禁止跟随 HTTP 重定向；先检查响应状态，再限量读取解码后的响应体。
除单次 HTTP 操作超时外，还限制完整请求的总耗时，防止缓慢持续返回的响应无限占用运行。
长度截断不会当成成功答案交付；是否调整参数再次请求由运行器明确决定。
