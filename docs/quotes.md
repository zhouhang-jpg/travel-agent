# 票务与酒店只读查询契约

以下四个工具已实现严格契约、独立适配接口与按配置注册。**缺少对应配置时不向模型导出**；不可用调用返回明确错误，不能返回伪造的空成功结果。注册只确认实现及本地配置，真实服务权限与字段质量由独立联调证明。

铁路默认`TRAIN_SEARCH_PROVIDER=12306`，优先官方公开页面；[FlyAI](providers/flyai.md)保留显式选择或配置且披露的降级，[聚合](providers/juhe-train.md)保留显式来源。普通大巴已恢复，默认出行365，旧[极速](providers/jisu-coach.md)只在显式配置时使用。机票默认FlyAI，可用`provider=ceair`按需核实东航官网。独立浏览器的安装、关闭、缓存、来源选择和真实边界见[浏览器票务](providers/browser-tickets.md)。酒店继续FlyAI，候选价格、房型及库存仍可能不完整。此轮只验证有限样本，未承诺全国或全市场覆盖。

| 工具 | 输入/输出 Pydantic 类型 | 供应商接口 |
| --- | --- | --- |
| `search_flights` | `SearchFlightsInput` / `SearchFlightsOutput` | `FlightSearchAdapter` |
| `search_trains` | `SearchTrainsInput` / `SearchTrainsOutput` | `TrainSearchAdapter` |
| `search_coaches` | `SearchCoachesInput` / `SearchCoachesOutput` | `CoachSearchAdapter` |
| `search_hotels` | `SearchHotelsInput` / `SearchHotelsOutput` | `HotelSearchAdapter` |

所有接口均为 `async search(request) -> output` 的独立只读 `Protocol`。协议位于 `src/travel_tools/providers/quotes.py`，契约位于 `src/travel_tools/schemas/quotes.py`。协议声明本身不是可执行供应商；注册可用工具时必须另有真正的适配器和经过校验的配置。

## 查询范围

交通查询必须指定起点、终点、出发日期以及 `travelers`。地点包括人类可读的 `query`，可选供应商地点 ID 和带坐标系的坐标。坐标不是机场、车站或城市编码的自动替代。成人数量必须为正整数，儿童年龄列为 0–17 岁（含婴儿）；具体年龄政策和成人陪同约束由接入的供应商实现验证。没有指定的票种、席别或币种保持空值。首期交通契约是单程查询；组合往返需分别查询且不能假定票价可直接组合。

酒店查询必须给出目的地、入住日、退房日、入住人数和房间数；退房必须晚于入住。房间数和人数是查询条件，不代表酒店允许入住，具体房间分配和儿童政策应由供应商返回并验证。当前契约尚不描述逐房间入住分配；需要该信息的供应商接入时必须扩展契约，不能擅自均分。

```python
from travel_tools.schemas.quotes import SearchHotelsInput

request = SearchHotelsInput.model_validate(
    {
        "destination": {"query": "杭州西湖附近"},
        "check_in": "2026-10-20",
        "check_out": "2026-10-22",
        "travelers": {"adults": 2, "children_ages": [6]},
        "rooms": 1,
        "preferred_currency": "CNY",
    }
)
# 这里只验证通用契约。当前 FlyAI 适配器不支持此多旅客/儿童/指定币种请求，
# 会明确返回 unsupported_parameters，不能忽略条件后伪装为匹配结果。
```

## 报价与库存不能混用

每条结果独立携带供应商、可选 offer ID、查询时间 `queried_at`、至少一个 `sources`、`price` 和 `inventory`。来源保留 provider、可选 URL、抓取时间、可选数据时间和归因。时间必须带时区。

`price.kind` 严格区分：

- `query_quote`：供应商在该次查询返回的报价，不保证未来有效或可订。
- `reference`：参考金额，不能宣称为指定日期的可购买报价。
- `unknown`：总价缺失，`money` 必须为空；不能填零。

已知金额必须有大写三字母币种和非负十进制数；合法零金额保留。金额口径 `unit` 区分整组、每人、整个住宿、每房、每间夜或未知。`priced_persons`、`priced_rooms`、`priced_nights` 记录供应商明确返回或已验证的计价数量，缺失保持空值，不从查询条件推断成供应商已确认的口径。`tax_basis` 和 `fee_basis` 各自区分包含、不含、部分、未知；已知税/附加费金额单列，币种须与主报价一致。可选有效期和条件原样保留。

`price.display`单独保存原展示文字、精确数字/遮罩状态和币种符号，`currency_basis`说明规范化币种依据。数字已知但ISO币种未核实时，`money=null`并不代表没有展示价格；不能填零或猜币种。原始时间保存在`departure_text/arrival_text`，无偏移不强加时区；网页明确当地时刻且国内机场/车站可核实时才转为带偏移时间。`segments`保留每段机场/站点、航站楼、营销与实际承运、共享及原时刻，营销名称不填成实际运营商。

`inventory.status`另区分`waitlist`（候补）、`not_offered`、`not_on_sale`和`sales_suspended`。报价不能推导可售库存；“有”只确认列表状态，不造数字，“候补”不是有票。剩余数量只有实际数字证据时填写；未知或询价状态无确切余量。`raw_status`保留原文。车次、时间、实际承运人、席别、酒店房型、取消条款等未核实就保持空值。

各输出要求查询时间、类型化 `offers` 以及供应商响应完整性 `complete`。只有实际成功执行查询、供应商确实没有返回匹配结果时才可以返回空列表；认证失败、权限缺失、配额超限、无供应商、接口未实现及上游故障必须显式失败。`complete` 指查询响应是否完整，不能冒充整个市场已搜索完，也不能代替未知字段。
`coverage`记录来源、扫描/匹配/返回数量、页码/后续页、原数据时间、缓存命中、测得耗时和降级原因。
`train_numbers/flight_numbers`可核实特定候选，`direct_only/nonstop_only`按实际段数和路由证据筛选；
候选条数限制和分页范围均明确披露，不能把当前页或最多10项CLI结果说成全市场最低价。

## 供应商接入前置核验

1. 阅读供应商当前官方文档，确认是**客户主动查询报价/库存接口**。给商户写入库存、接收订单或供给回调的接口不满足本查询需求。
2. 确认账号、产品授权、API 端点、签名/认证、地区覆盖、限流和使用条款。凭据只能通过本地环境安全配置，不能放进代码、聊天或测试夹具。
3. 根据供应商真实字段补足日期、席别/房型、计价范围、税费、来源与查询时间，不使用地图估算或网页参考价冒充实时供应商报价。
4. 使用隔离适配器实现成功、空匹配、权限/认证失败、限流、超时和响应缺字段处理。契约测试使用明确标注的合成夹具；真实联调单独记录环境、时间、脱敏证据和限制。
5. 只有实现与配置都存在才导出模型工具定义；真实联调结论仍需独立记录。保持只读，不加入下单、付款、取消或修改订单能力。

`tests/test_quotes.py` 只验证 Pydantic 契约（人数/日期、报价口径、未知值、税费币种、库存矛盾、来源及时区）。供应商映射由各适配测试验证，配置选择与注册由 `tests/test_quote_registry.py` 验证；离线测试不证明账号已开通。当前待办是正式账号产品权限、实际报价完整性及聚合/极速的真实联调；[供应商比较](providers/ticket-suppliers.md) 记录携程和 12306 的公开接入证据。
