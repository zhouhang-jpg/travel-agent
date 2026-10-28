# 供应商熔断设计

状态：已实现（2026-10-28），采用下文默认参数；铁路降级链（加入聚合数据）与大巴自动降级未做。
代码：[circuit.py](../src/travel_tools/circuit.py)、[scheduling.py](../src/travel_tools/scheduling.py)、
`ToolRegistry.with_health`（[registry.py](../src/travel_tools/registry.py)），测试见
[test_circuit_breaker.py](../tests/test_circuit_breaker.py)。

## 1. 目标与边界

下游供应商持续不可用时，避免每次调用都等到超时，把一轮 300 秒的运行时限和 12 次决策
耗在同一个故障上；有自动备用来源时直接切换，不先等主来源超时。

- 只做故障隔离和快速失败，不在工具内部自动重试，不增加休眠/等待工具。
- 错误照常以结构化结果交给模型，由模型自主决定换来源、先做别的或告诉用户；不新增固定流程。
- 熔断状态只保存在进程内存。项目当前为单应用进程/单 worker，重启即重置；多进程共享不在范围内。
- 不放宽证据边界：降级或网页结果仍只是对应来源的证据，不冒充官方报价、库存或天气。
- 不新增供应商。酒店、天气、地图的第二来源需另行决定接入。

## 2. 熔断粒度

按**供应商**计数，复用 `supplier_slot` 已有的供应商名：

| 熔断键 | 影响的工具 |
| --- | --- |
| `amap` | `search_places`、`get_place_details`、`get_routes`，开放时间中的地图来源 |
| `qweather` | `get_weather` |
| `bocha` | `search_web`，开放时间中的搜索来源 |
| `flyai` | 机票/酒店/铁路的 FlyAI 来源 |
| `12306`、`ceair`、`csair`、`bus365` | 对应的浏览器来源 |
| `juhe_train`、`jisu_coach` | 对应的 HTTP 来源 |
| `browser_runtime` | 本地 Playwright Chromium 本身（见下） |

- `public_web`（`fetch_webpage` 读取的任意公开网站）**不熔断**：它对应很多不同站点，一个站点故障不代表其他站点不可用。
- `browser_runtime` 单独计数：`provider_browser_error`、浏览器启动失败只计入它，不计入 12306 等站点。
  这样浏览器本身坏了不会误判为某个网站宕机；`browser_runtime` 打开时，四个浏览器来源一起快速失败。

## 3. 状态机

```
closed ──连续失败达阈值 / 立即打开类错误──▶ open ──到 retry_at──▶ half_open
  ▲                                                                   │
  └──────────────────────── 试探成功 ◀────────────────────────────────┤
                                     试探失败：冷却期翻倍（有上限）──▶ open
```

- **closed**：正常放行。按“计入”的失败累计**连续**次数，任何一次成功清零。
- **open**：不发请求，立即返回 `provider_circuit_open`，带绝对时间 `retry_at`。
- **half_open**：到 `retry_at` 后只放行**一个**试探请求；试探期间同一供应商的其他调用仍快速失败。
  试探成功回到 closed；失败重新打开，冷却期翻倍，直到上限。

默认参数（可配置，均为初始值，需按真实故障调整）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `CIRCUIT_BREAKER_ENABLED` | `true` | 关闭后行为与现在一致 |
| `CIRCUIT_FAILURE_THRESHOLD` | 3 | 连续失败次数 |
| `CIRCUIT_OPEN_SECONDS` | 60 | 首次冷却期 |
| `CIRCUIT_MAX_OPEN_SECONDS` | 600 | 翻倍上限 |
| `CIRCUIT_BLOCKED_OPEN_SECONDS` | 600 | 访问拦截、配额耗尽的冷却期 |
| `CIRCUIT_AUTH_OPEN_SECONDS` | 1800 | 凭据被拒的冷却期 |

## 4. 失败分类

| 类别 | 错误 | 处理 |
| --- | --- | --- |
| 计入连续失败 | `provider_timeout`、`upstream_timeout`、`provider_network_error`、HTTP 5xx（`provider_http_error` 且可重试）、`provider_unavailable`（运行时故障）、`provider_error`、`provider_invalid_response`、`upstream_invalid_response`、`provider_catalog_unavailable`、`provider_page_unavailable` | 达阈值后打开 |
| 立即打开 | `provider_rate_limited`（429，有 `Retry-After` 时优先使用） | 打开 `CIRCUIT_OPEN_SECONDS` |
| 立即打开（长冷却） | `provider_access_blocked`（验证码/拦截）、高德配额类 infocode | 打开 `CIRCUIT_BLOCKED_OPEN_SECONDS`；继续访问只会加重拦截 |
| 立即打开（长冷却） | `provider_auth` / `provider_authentication`（401/403 凭据） | 打开 `CIRCUIT_AUTH_OPEN_SECONDS`，原因写明需检查凭据 |
| 不计入、也不清零 | 业务结果与请求问题：无票、候补、未开售、`outside_sale_window`、`invalid_arguments`、`unsupported_*`、`provider_location_not_found`、`provider_date_mismatch`、`provider_party_mismatch`、`past_departure_date` 等 | 照常返回 |
| 不计入 | `CancelledError`（运行取消或外层工具时限） | 无法区分原因，不计数；half_open 试探被取消时释放试探名额 |
| 不熔断 | `not_configured` / `provider_not_configured` / Chromium 未安装 | 属于配置问题，工具本来就是 `not_configured` |

## 5. 接入点

### 5.1 `SupplierScheduler.slot`（[scheduling.py](../src/travel_tools/scheduling.py)）

- **排队前检查**：open 或已有试探在进行时直接抛出 `provider_circuit_open`，不占并发名额和频率间隔。
- **退出时记录结果**：正常退出记成功；抛出异常时按第 4 节分类。分类函数同时识别 `ToolFailure`
  错误码和尚未转换的原始异常（`TimeoutError`、`httpx.TimeoutException`、`httpx.HTTPError`），
  因为 [http.py](../src/travel_tools/providers/http.py) 和 [amap.py](../src/travel_tools/providers/amap.py)
  是在 slot 外层才把这些异常转换为 `ToolFailure`。
- `supplier_progress` 增加 `circuit_open` 阶段，公开进度显示“来源暂时不可用”，不再显示排队/运行。

### 5.2 需要配合调整的适配器

- **响应体里的拒绝**：高德 infocode、博查/和风指数/聚合/极速的业务码是在 slot 退出**之后**才判断的。
  实现上不移动这些判断，而是在抛出前调用 `report_supplier_failure`，只对限流、配额、凭据这类
  “立即打开”的错误生效。限制：响应体层面的格式错误不会累计成连续失败（slot 已先记成功）。
  高德 10003（日配额）按配额耗尽处理，使用长冷却。
- **FlyAI**：CLI 超时与外层工具时限相同（均为 20 秒），外层可能先取消，导致超时被当成
  `CancelledError` 而不计数。改为与浏览器来源相同的 `0.8 × 工具时限`。
- **浏览器**：先检查站点熔断，再用不排队的 `supplier_guard` 检查 `browser_runtime`。只有 Chromium
  启动失败（`breaker_scope=browser_runtime`）计入 `browser_runtime`；站点本身的超时、拦截不计入它，
  反而说明浏览器能启动，记为浏览器成功。Chromium 未安装属于配置问题，不熔断。
- **12306 站点目录**：已有独立的 `supplier_slot("12306")`，可直接复用。

### 5.3 错误结构

`ErrorInfo` 增加可选字段 `retry_at`（带时区的绝对时间），只在 `provider_circuit_open` 时填写：

```json
{"code": "provider_circuit_open", "message": "flyai来源暂时不可用（连续超时或连接失败），已暂停访问；可在retry_at之后再试，或使用其他来源。", "retryable": true, "retry_at": "2026-10-28T06:02:00Z"}
```

工具结果中的时间与其他时间戳一样用 UTC；快照里按会话时区显示。使用绝对时间而非“60 秒后”：模型能从运行状态快照拿到 `current_time` 自行比较，历史回放时也不会失真。

### 5.4 缓存

缓存命中在进入 slot 之前判断。熔断期间仍在有效期内的已缓存结果照常复用，不受影响；
失败结果按现有规则不进缓存。

## 6. 自动降级

原则：**备用来源自动切换时，熔断对模型透明**；结果中照常注明实际来源与降级原因，
因为不同来源的余票、席别和价格口径不同。

| 工具 | 行为 |
| --- | --- |
| `search_trains` | `FALLBACK_ERRORS` 增加 `provider_circuit_open`：12306 熔断时立即走 FlyAI，不等 12306 超时。可选：允许 `juhe` 作为降级来源，按配置顺序排成链 |
| `search_coaches` | 可选：新增与铁路相同的降级路由（`COACH_FALLBACK_PROVIDER=jisu`），目前两者只能二选一 |
| `search_flights` | **不自动降级**。东航/南航只覆盖单家航司，与 FlyAI 的候选范围不同，由模型自主选择 `provider` |
| `get_attraction_opening_hours` | 已内部组合多来源；熔断的来源记为该来源尝试失败，其余来源照常查询 |
| 其他单来源工具 | 无备用，直接返回 `provider_circuit_open` |

显式指定 `provider` 的调用（例如 `provider=12306`）沿用现有规则：不自动切换。

## 7. 告诉模型

### 7.1 什么时候进快照

只有**模型需要据此改变决策**时才写进运行状态快照：

| 情况 | 快照 |
| --- | --- |
| 有自动备用，且备用可用 | 不变，仍为 `ready` |
| 所有路线都不可用（如酒店只有 FlyAI 且熔断；铁路主备都熔断） | `availability: "temporarily_unavailable"`，带 `reason` 和 `retry_at` |
| 备用需要模型自己选（机票：FlyAI 熔断，东航/南航可用） | 仍为 `ready`，`reason` 写明哪个来源暂不可用、可改用哪些 `provider` |
| 组合工具部分来源不可用（开放时间） | 不变；结果里已注明缺失来源 |

到达 `retry_at` 进入 half_open 后，快照恢复为 `ready`，下一次模型调用就会被放行作为试探。
试探失败时，失败结果带新的 `retry_at`，下一份快照同步更新。

### 7.2 实现方式

- `ToolRegistry` 增加 `health_overlay(now)`：根据“工具 → 供应商路线”映射和当前熔断状态，
  只返回与静态目录不同的工具条目。
- 在 [graph_runtime.py](../src/travel_agent/graph_runtime.py) 调用 `append_runtime_context` 前，把
  覆盖层合并到 `manifest["catalog"]` 的**副本**上；legacy 的 [runner.py](../src/travel_agent/runner.py) 同样处理。
- **不修改 manifest**：manifest 带签名，并与已保存运行的 `runtime_spec` 比对；把动态状态写进去会被当成
  运行时配置变化。
- **不修改工具定义**：`model_definitions()` 保持不变，暂时不可用的工具仍在工具列表中，
  避免工具定义变化导致上下文前缀缓存失效。
- 快照本来就只追加变化字段，熔断开始和恢复各追加一次，不影响已有前缀缓存。
- 快照按模型尝试写入 Journal，恢复执行时复用当时的快照，回放结果与原执行一致。
- 系统提示词的运行状态说明补一句 `temporarily_unavailable` 与 `retry_at` 的含义（只改一次，会使缓存失效一次）。

### 7.3 例子

```
14:00:05  search_hotels → FlyAI 超时（第 3 次连续失败）→ flyai 打开，retry_at 14:01:05
14:00:12  快照：search_hotels temporarily_unavailable，预计 14:01:05 后可再试
          模型改查天气、景点，或先交付方案并标注酒店待查
14:01:20  快照：search_hotels 恢复 ready
14:01:25  search_hotels → 作为试探请求放行 → 成功，flyai 关闭
```

## 8. 可观测性

- 状态切换写日志：供应商、旧/新状态、触发错误码、`retry_at`，不含请求参数和响应内容。
- 可选：在现有本地 API 增加只读的供应商健康状态接口，供调试查看。

## 9. 测试

- 状态机：阈值打开、成功清零、业务错误不计数、立即打开类错误、half_open 只放行一个试探、
  试探失败冷却期翻倍且不超过上限、`CancelledError` 释放试探名额。
- 快速失败不占并发名额、不消耗频率间隔。
- 原始异常与 `ToolFailure` 错误码分类一致；高德配额 infocode 计入熔断。
- 铁路：12306 熔断时立即降级 FlyAI，结果带 `fallback_from` 和降级原因，结果不进缓存。
- 浏览器：`browser_runtime` 打开时四个浏览器来源均快速失败，站点熔断计数不受影响。
- 快照：有备用时不出现；主备都熔断时出现；恢复时追加 `ready`；manifest 签名和工具定义不变。
- 熔断关闭开关：行为与现有完全一致。
- 评测 world 模式不经过真实供应商，不受影响；后续可在世界故障注入中增加熔断类故障。

## 10. 已定与后续

- 采用默认参数（连续 3 次、60 秒起、上限 600 秒）；凭据错误走熔断（30 分钟）。参数通过
  `.env` 的 `CIRCUIT_*` 配置，需按真实故障再调整。
- 铁路降级链（加入聚合数据）和大巴自动降级（出行365 → 极速数据）未做，可单独排期。
- 前端目前不展示供应商级进度事件；`circuit_open` 只扩展了事件类型，工具本身仍显示为失败。
