# 和风天气适配说明

核对日期：2026-10-09。已实现可注入 HTTP 客户端的每日天气适配，契约测试使用明确标识的虚构响应。本次用户补全本地 API Host/API KEY 后，真实查询北京坐标的一日预报成功，返回 1 个日区间（2026-10-09 10:59:44 UTC）；脱敏摘要见 `artifacts/live-probe-all-modes.json`。这不代表所有地区/天数/未来额度均已验证。

## 官方依据

- [每日天气预报](https://dev.qweather.com/docs/api/weather/weather-daily-forecast/)：当前页面使用 `GET /weather/v1/daily/{latitude}/{longitude}`，`days=1..10`，两位小数坐标、`localTime` 和 `lang`；响应为 `metadata` 与 `days`。
- [API Host](https://dev.qweather.com/docs/configuration/api-host/)：使用账号专属的 `*.qweatherapi.com` 域名，旧共享域名正在退出使用。
- [身份认证](https://dev.qweather.com/docs/configuration/authentication/)：API KEY 可在 `X-QW-Api-Key` 请求头传递；另有 JWT 机制。本适配仅实现 API KEY，不实现 JWT 签发或刷新。
- [坐标词汇表](https://dev.qweather.com/docs/resource/glossary/)：中国大陆使用 GCJ-02，其他地区使用 WGS-84。
- [错误码](https://dev.qweather.com/docs/resource/error-code/)：当前 API 通过 HTTP 状态与 `error` 对象报告错误；例如鉴权、账户额度、服务权限、错误 Host、限流。

旧版本教程中的 `/v7/weather/3d`、`code/updateTime/daily` 响应不可直接套用到此实现。代码明确拒绝旧版响应，避免错把契约变化当成成功空结果。

## 工具契约

`QWeatherAdapter(api_host, api_key, client).get_weather(GetWeatherInput)`。

输入 `coordinates` 必须明确坐标系；`region` 默认 `mainland_china`，只接受 GCJ-02；`other` 只接受 WGS-84。不推断区域、不把高德坐标改标签当成 WGS-84。`days` 为 1–10，默认 7；`language` 为 `zh` 或 `en`。地点名称及城市 ID 需由其他地点工具解析后传入坐标。

请求固定 `localTime=true`，回传供应商给出的带时区开始与结束时间，结束时间不包含在区间内。坐标按接口两位小数精度查询，同时保留原坐标与 `queried_coordinates`；发生舍入时返回警告。

输出保留每天及日间/夜间温度、天气现象、降水、湿度与风速的可用信息。数值和单位分别保留，缺失为 `null`；不会把未提供降水概率解释为 0。供应商没有提供预报发布时间，`Source.data_time=null`，`retrieved_at` 仅代表本次查询时间。`metadata.attributions` 会原样保留到结果和来源声明，展示数据时应同时展示声明。

只有实际返回的预报区间得到天气证据；区间以外日期仍未知。当前工具只支持每日预报，尚不提供历史天气、逐小时、分钟降雨或预警。日数短于请求会明确警告；完全缺少 `days` 或空 `days` 视为不可解释响应。

## 边界与验证

账号 Host 限定 HTTPS 的 `*.qweatherapi.com`，禁止自定义端口、路径、认证信息及旧域名；密钥只进入请求头。请求限制为 15 秒总时限、2 MiB 解码后响应体，不跟随重定向，不自动重试。错误消息采用固定安全文本，不返回供应商原始错误、请求头或密钥。

`tests/test_qweather.py` 覆盖路径与参数、认证头、缺失值/零值/时区/来源、无效返回结构与数值、HTTP 错误和跳转、网络与超时、总时限、大小上限、Host 与坐标系和预报天数边界。以上是代码契约测试，不能替代真实账号验收。

后续接入新环境需在本地安全配置 `QWEATHER_API_HOST`、`QWEATHER_API_KEY`，核实所需地区、天数、额度和时区字段。当前仅对上述一日预报样例做了真实验收。不要将密钥或完整原始响应提交到 Git。
