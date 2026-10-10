# 独立浏览器票务查询

项目使用 Playwright 的独立 Chromium 读取公开页面，后端自行启动和关闭浏览器，不依赖
Codex 标签页、桌面自动化工具、个人浏览器档案或已保存的调研数据。只查询，不登录、下单或支付。
不读取 cookie、token 或签名，不调用受阻的私有查询接口，不处理或绕过验证码和访问防护。

安装与配置：

```powershell
.\.tools\bin\uv.exe sync
.\.tools\bin\uv.exe run playwright install chromium
```

在本地 `.env` 中设置 `BROWSER_QUERIES_ENABLED=true`；`TRAIN_SEARCH_PROVIDER=12306`，
`COACH_SEARCH_PROVIDER=bus365`。`TRAIN_FALLBACK_PROVIDER=flyai` 可在默认铁路来源受阻时
降级，返回 `coverage.fallback_from/fallback_reason` 和警告。显式 `provider=12306` 不自动
降级；未开售、日期错误、参数错误和无结果也不自动降级。原有 `juhe`、`jisu` 保留显式配置。

浏览器使用有界独立线程池（默认最多2项），来源另有限流；Windows 专用 Proactor 事件循环支持子进程，避免 Uvicorn
Selector 循环限制。每次使用全新上下文，保留页面自身资源行为，退出、超时或异常均关闭浏览器
和驱动。应用关闭时停止接受新查询并回收线程。`BROWSER_QUERY_TIMEOUT_SECONDS=18`，实际
预算不超过外层工具时限的80%，为关闭/降级留时间；这里限制时间，不限制工具调用次数。

`BROWSER_QUERY_CACHE_SECONDS=0` 默认不缓存，允许配置至300秒。缓存只保留成功且有结果的
页面数据，原 `queried_at`、来源时间和 `coverage.data_time` 不改成当前时间，命中标注
`cache_hit=true`。访问受阻、验证码、异常和空结果不缓存，不把受阻转成无票。

## 查询与事实边界

| 工具/来源 | 参数与行为 | 已保留的边界 |
| --- | --- | --- |
| `search_coaches` / 出行365 | 通过可见城市选项、日历和查询表单；`page` 明确查询页；`departure_station` 可筛真实发站 | 实际站名独立保留，其他目的地不覆盖成查询目的地；车型分类看产品证据，未知保留；只读当前页；税费/最终票价未知 |
| `search_trains` / 12306 | 公共站名目录解析站名/电报码，浏览器公开查询页；`station_scope=exact/city`、`seat_class`、`train_numbers`、`direct_only` | 准确站点逐项筛选，同城范围不冒充精确端点；保留所有展示席别；“有”无数字，“候补”不可当有票；当前15日预售窗口；此来源不搜索换乘 |
| `search_trains` / FlyAI | 显式 `provider=flyai` 或配置的降级；按直达/车次/真实站点与城市证据过滤 | CLI最多10项，无分页；每段站点与原始时间保留，币种/库存缺失不能补造 |
| `search_flights` / FlyAI | 默认快速候选，`nonstop_only`、`cabin`、`flight_numbers` | 保留每段机场代码、航站楼、原始时间、营销航司；无偏移时间不强加时区；营销不等于承运 |
| `search_flights` / 东航 | `provider=ceair`，`tax_view=included/excluded`，按具体航班核实；城市或 `provider_location_id` 的IATA集合 | 当前核实经济/超级经济舱，官网不覆盖全市场；机场必须逐项对齐；本地时刻保留，境外时区未知不猜；共享标签缺失不证明无共享；行李/退改/最终库存未知 |
| `search_flights` / 南航 | `provider=csair`，国内单成人单程，默认经济舱；按可见舱等和实际机场筛选 | 初始可见列表，不穷尽市场/滚动；日期和计价人数验证；税费/ISO币种/余票/缺失直飞关系未知。独立浏览器与真实工具验证见[来源记录](flight-source-validation.md) |

东航提供一组国内城市/机场常用名别名，名称未覆盖时明确报错，也可传可核实的IATA代码。
未知的实际机场别名不能按查询城市硬填机场代码。官网支持范围随页面结果而定，不承诺所有
航空公司或全部机场。每次请求只验证一个成人，查询人数不等于已经取得全体计价口径。

`price.display` 区分精确数字与遮罩数字，保存原文和原币种符号。只有ISO币种有依据时生成
`price.money`；东航 `/zh/cny` 配置与展示符号可作为CNY依据，单独“￥”“元”不伪装成上游
ISO字段。展示数字已知而规范化金额因币种未知为空，不应说成没有任何价格信息。
所有列表价仍为展示/参考，不是已确认的结算总额或库存保证。税前和含税价不能直接比价。

`inventory.status` 区分 `available/unavailable/waitlist/not_offered/not_on_sale/sales_suspended/unknown`；
数字只有实际展示时才进入 `remaining`。“无”可确认不可售，但不编造数字；“有”不编造座位数。
访问受阻/查询失败通过工具错误返回。未知库存的报价候选不能说成可买。

`coverage` 记录实际扫描、匹配和返回条数，页码/后续页、缓存和数据时间；`max_results` 是明确
请求条件，超过时仍报告匹配总数，不伪称查完全部市场。已返回结果使用原有无损紧凑表示，
完整历史不裁剪、不摘要。示例与回归测试中的模拟数据不能用于实际查询或称为实时库存。

真实复核可用 `scripts/live_probe_tickets.py --live`；测试限定日期与路线，仅代表当次样本。
