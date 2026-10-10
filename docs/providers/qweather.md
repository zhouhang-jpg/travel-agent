# 和风天气适配说明

核对日期：2026-10-09（America/Chicago）。已实现每日/逐小时预报、当前预警与每日生活指数。
使用现有Host/Key真实取得北京240个逐小时时次、10天日预报、明确零结果的当前预警和3天
12项指数。覆盖由实际返回决定，不承诺所有地区和未来额度。测试使用标识的合成响应，
真实HTTP与模型记录保存在忽略目录 `artifacts/weather-live.json` 和 `weather-model-final.json`。

## 官方依据

- [每日天气预报](https://dev.qweather.com/docs/api/weather/weather-daily-forecast/)：当前页面使用 `GET /weather/v1/daily/{latitude}/{longitude}`，`days=1..10`，两位小数坐标、`localTime` 和 `lang`；响应为 `metadata` 与 `days`。
- [逐小时](https://dev.qweather.com/docs/api/weather/weather-hourly-forecast/)：`/weather/v1/hourly/{latitude}/{longitude}`，`hours=1..240`，默认24；响应`hours[].forecastTime`与数值/单位。
- [当前预警](https://dev.qweather.com/docs/api/warning/weather-alert/)：`/weatheralert/v1/current/{latitude}/{longitude}`，`metadata.zeroResult`和`alerts`；保留更新/取消、取代ID、发布机构和有效期。
- [指数](https://dev.qweather.com/docs/api/indices/indices-forecast/)：此产品仍使用`/v7/indices/1d|3d`，经度/纬度`location`及类型`type`，返回`code/updateTime/daily/refer`。这是该模式的正式契约，不套用预报v1格式。
- [指数类型](https://dev.qweather.com/docs/api/indices/indices-type/)：1运动、3穿衣、5UV为全球类型；6旅游仅中国；0全部须独立使用。
- [API Host](https://dev.qweather.com/docs/configuration/api-host/)：使用账号专属的 `*.qweatherapi.com` 域名，旧共享域名正在退出使用。
- [身份认证](https://dev.qweather.com/docs/configuration/authentication/)：API KEY 可在 `X-QW-Api-Key` 请求头传递；另有 JWT 机制。本适配仅实现 API KEY，不实现 JWT 签发或刷新。
- [坐标词汇表](https://dev.qweather.com/docs/resource/glossary/)：中国大陆使用 GCJ-02，其他地区使用 WGS-84。
- [错误码](https://dev.qweather.com/docs/resource/error-code/)：当前 API 通过 HTTP 状态与 `error` 对象报告错误；例如鉴权、账户额度、服务权限、错误 Host、限流。

旧`/v7/weather/3d`响应不能套用于每日或逐小时预报；指数按其正式v7契约独立解析。

## 工具契约

`QWeatherAdapter(api_host, api_key, client).get_weather(GetWeatherInput)`。

输入 `coordinates` 必须明确坐标系；`region` 默认 `mainland_china`，只接受 GCJ-02；`other` 只接受 WGS-84。不推断区域、不把高德坐标改标签当成 WGS-84。`days` 为 1–10，默认 7；`language` 为 `zh` 或 `en`。地点名称及城市 ID 需由其他地点工具解析后传入坐标。

请求固定 `localTime=true`，回传供应商给出的带时区开始与结束时间，结束时间不包含在区间内。坐标按接口两位小数精度查询，同时保留原坐标与 `queried_coordinates`；发生舍入时返回警告。

输出保留每天及日间/夜间温度、天气现象、降水、湿度与风速的可用信息。数值和单位分别保留，缺失为 `null`；不会把未提供降水概率解释为 0。供应商没有提供预报发布时间，`Source.data_time=null`，`retrieved_at` 仅代表本次查询时间。`metadata.attributions` 会原样保留到结果和来源声明，展示数据时应同时展示声明。

只有实际返回的预报时次/区间得到证据。历史天气、分钟降雨尚未接入；超出范围不外推。
天气预报空列表视为不可解释响应；预警空列表须明确`zeroResult=true`，不当作未来安全。

## 按需模式与时间选择

复用同一`get_weather`，由模型自主选择，不每次强制查询所有模式：

| mode | 参数与数据粒度 | 时间/范围处理 |
| --- | --- | --- |
| daily（默认） | days=1..10，默认7 | forecast_date可选择一天；未显式days时请求10天以匹配该日期 |
| hourly | hours=1..240；target_time或start_time/end_time须带UTC偏移 | 通常省略hours，按目标自动请求必要小时数；只返回匹配时次/时段，保留源总数/覆盖范围 |
| alerts | 当前官方预警，非未来预警预测 | 不接受目标日期/时间；保留message_type、supersedes、有效/到期时刻与防御建议 |
| indices | index_days=1或3，index_types默认[1,3,5,6] | forecast_date可选择日；非中国须显式选择全球类型1..5；未提供的日期/类型不猜 |

范围采用[start_time,end_time)。整点请求匹配源预报时次；目标落在两个相邻时次之间时
返回两条参考，`coverage.status=bracketing`，不插值、不生成目标分钟的假记录。范围可返回
边界邻近时次作参考；缺时次、只覆盖部分或超出范围分别标记`gap/partial/outside_range`。
不同UTC偏移按绝对时刻比较，实际返回仍保留供应商时区。不默默拿最近一天/小时替代。

`coverage.provider_count/returned_count`明确区分供应商返回量与本次按条件选择量，未删除旧
历史；新工具结果完整持久化并使用既有无损紧凑表示。只有选中的mode字段有值：未请求的
days/hours/alerts/indices为null，不能将占位空列表误读成已查询“无预警”。

保留气温/体感、风/阵风/方向、降水量/强度/概率、湿度、能见度等实际字段，数值/单位
分别保留，未知不是0。预报未提供发行时刻时不把retrieved_at当发布时间；指数updateTime
保留为数据更新时间，sources/refer许可与归因保留。指数是每日建议，不是小时天气或安全保证。

示例参数（日期须调整为实际未来范围）：

```json
{"coordinates":{"longitude":116.41,"latitude":39.92,"crs":"gcj02"},
 "mode":"hourly","target_time":"2026-10-13T15:00:00+08:00"}
```

`scripts/live_probe_weather.py --live`通过运行中的HTTP服务验证七个场景；`--model-only --live`
单独做两次真实模型工具链，避免重复HTTP样例。权限不足、超时、异常响应安全失败，不变成天气晴。

## 边界与验证

账号 Host 限定 HTTPS 的 `*.qweatherapi.com`，禁止自定义端口、路径、认证信息及旧域名；密钥只进入请求头。请求限制为 15 秒总时限、2 MiB 解码后响应体，不跟随重定向，不自动重试。错误消息采用固定安全文本，不返回供应商原始错误、请求头或密钥。

`tests/test_qweather.py` 覆盖路径与参数、认证头、缺失值/零值/时区/来源、无效返回结构与数值、HTTP 错误和跳转、网络与超时、总时限、大小上限、Host 与坐标系和预报天数边界。以上是代码契约测试，不能替代真实账号验收。

新环境配置`QWEATHER_API_HOST/QWEATHER_API_KEY`后仍需核实地区、时效、额度和时区。
本轮验证北京每日/240小时/零结果预警/3天指数；积极预警、更新和取消的映射用明确的
合成数据回归测试，不能宣称已实测到所有预警事件。不提交密钥、原始响应和私人会话。
