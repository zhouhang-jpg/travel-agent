# 高德工具适配

实现状态：3 个工具均有可执行适配与离线 HTTP 契约测试。2026-10-09 已阅读下列高德官方文档，并使用用户配置的项目账号完成真实联调：文本/周边查询、2 次详情查询，以及驾车、步行、骑行、公交均收到成功响应。原始全模式报告为 `artifacts/live-probe-all-modes.json`；该轮发现骑行总耗时映射遗漏，随后已按真实响应结构修复并补充回归测试。联调证明当前样例可访问，不代表任意地点、日期、所有账号权限或长期额度保证。缺少 Key 时仍不导出给模型。

## 官方依据与端点

| 工具/模式 | HTTPS GET 端点（主机均为 `restapi.amap.com`） | 官方文档 |
| --- | --- | --- |
| search_places 文本 | `/v5/place/text` | [搜索 POI 2.0](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch) |
| search_places 周边 | `/v5/place/around` | [搜索 POI 2.0](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch) |
| get_place_details | `/v5/place/detail` | [搜索 POI 2.0](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch) |
| get_routes 驾车/步行/骑行 | `/v5/direction/driving`、`/v5/direction/walking`、`/v5/direction/bicycling` | [路径规划 2.0](https://lbs.amap.com/api/webservice/guide/api/newroute) |
| get_routes 公交 | `/v5/direction/transit/integrated` | [路径规划 2.0](https://lbs.amap.com/api/webservice/guide/api/newroute)；分段结构另参照官方指向的 [原版路径规划](https://lbs.amap.com/api/webservice/guide/api/direction) |

认证为 Web 服务 API Key 查询参数；需在高德控制台验证所需服务权限、额度与 IP 等限制。需要数字签名的账号配置目前未实现，不能仅设置 Key 就宣称可用。使用固定官方主机，禁止重定向，单次总超时 15 秒，解压后的 JSON 正文上限 2 MiB，不自动重试。错误映射参照 [官方错误码说明](https://lbs.amap.com/api/webservice/guide/tools/info)，不回显含 Key 的 URL、供应商错误文本或响应正文。

## 契约与边界

- 输入经纬度必须显式声明 `crs: gcj02`。拒绝 WGS84/BD09，不悄悄重标坐标，也不进行近似转换。依据：[高德使用的坐标体系](https://lbs.amap.com/faq/advisory/others/39838)。发给 API 时经度在前，保留 6 位小数。
- `search_places` 接受关键词或分类码、区域和城市限制；带 center 时执行周边搜索。半径仅用于周边。每页 1–25 项，限制分页请求在官方同查询最多 200 项内。不默认声称搜索结果完整。
- V5 `count` 是当前响应的 POI 数量，不是总匹配数。输出 `returned_count` 按实际解析项数计算，`total_count` 为 null、`more_results` 为 unknown。
- `get_place_details` 只查询一个 POI ID。正常空列表表示 found=false；缺少 pois、错误结构或返回了不匹配的 ID 均报错，不能伪装成未找到。
- POI 的今日/每周营业描述、电话、入口坐标等按实际响应保留，缺失、空字符串、供应商空数组归一化为 null。营业描述不证明未来指定日期、节假日或临时闭馆的状态；需要日期对应的证据才能用作确定的开放区间。
- POI 人均消费仅为 `average_spend` 参考金额，不能充当酒店房价、景区门票或日期报价。货币为人民币、计价依据为每人，计价人数/房间数不明、税费口径与库存均 unknown。来源及查询时间继承顶层 sources，供应商数据更新时间未提供则为 null。
- `get_routes` 实现驾车、步行、骑行、公交；请求 `show_fields=cost` 以获得 V5 耗时。距离单位米，耗时单位秒。官方骑行文档将总耗时/分段耗时列入 cost 可选字段组，但 2026-10-09 真实 V5 骑行响应将总耗时放在 `paths[].duration`，分段仍为 `steps[].cost.duration`。因此骑行总耗时在 `cost.duration` 缺失时兼容读取 `path.duration`，有效的 0 值不会被替换；两处均缺失仍保留 null，不能作为 0 秒转场。
- 公交要求起止 `citycode`，不接受城市名冒充编码。只有公交支持带时区的 `departure_time`，转换到中国时间后发送官方日期/时间格式。未指定时不自行添加日期。驾车/步行/骑行不接受未来出发时间，避免让调用方误认为执行了指定时刻的预测。
- 道路收费与出租车费用仅在明确字段存在时输出 `reference` 估算，不能解释为查询时可成交报价，不含库存承诺，不能替代 search_trains/search_coaches 等供应商。公交各段票价口径与 V5 嵌套描述存在需真实响应核对之处，当前不输出合计公交费用；火车分段座席/价格也不输出为票务信息。
- 公交分段的 buslines 是平行候选，输出只选首个；不会把所有候选当作连续乘坐。保留步行说明、首个公交线路及站名、铁路或出租车分段摘要；不保留完整路线几何、所有备选线路或列车席位信息。若调用方需要完整导航，应扩展契约与测试。
- 输出不是导航保证，不验证个人车辆车牌限行，也不支持途经点、避让区、预约/下单或高级导航策略。当前固定策略采用 API 默认，后续可以独立扩展参数。

## 本地验证与真实联调

`tests/test_amap.py` 使用 `httpx.MockTransport` 验证端点/参数、POI/营业字段、GCJ02、合法无结果、null 保留、V5 耗时映射、公交时区转换、估价标签、权限/额度错误、错误响应、正文限制、超时与重定向阻断。这些是人为构造的契约样例，绝非真实 API 数据。

真实联调由用户配置 `AMAP_API_KEY` 后显式运行项目联调脚本，未打印或持久化密钥及完整供应商响应。全模式报告记录文本/周边各返回 1 项、2 次详情成功、驾车和步行各 1 个有耗时方案、公交 5 个有耗时方案。骑行初始 1 个方案的耗时未被旧适配器取出；追加的安全结构检查于 2026-10-09 11:02:40 UTC 确认 `path.duration=1024` 秒，首段 `cost.duration=33` 秒。该差异是输出字段映射问题，已修复，仍保留原报告以说明发现过程。

后续账号/权限变化仍可能使接口失效；若某模式无权限，应收窄配置和模型工具定义或停用 get_routes，不能用离线测试或一次成功请求证明持续可用。

修复后于 2026-10-09 11:03:47 UTC 对同一公开样例执行一次真实骑行查询：返回 1 个方案，1 个方案正确保留总耗时 1024 秒，10 个分段全部有耗时。该次复验只输出数量、耗时与时间等安全元数据；没有再次遍历其他供应商或保存完整响应。`tests/test_amap.py` 当前 53 个离线用例通过，覆盖真实骑行嵌套形状、优先级、零值、缺失与非法耗时。
