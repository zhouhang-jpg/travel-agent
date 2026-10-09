# 极速数据大巴参考信息接入

**本供应商接口不支持指定出行日期，也不返回实时余票。它提供参考时刻和参考票价，不能用于证明某天存在可购买的大巴班次。**

核验日期：2026-10-09。当前状态为适配器已实现、离线契约测试通过；尚未取得 API key，未完成真实联调。

## 官方产品与测试权限

- 官方产品：[长途汽车 API](https://www.jisuapi.com/api/bus/)；[简洁版接口文档](https://m.jisuapi.com/api/bus/)。核查时页面显示接口正常，并列出 20 次免费套餐。
- 注册极速数据账号，申请该长途汽车产品，在账号控制台取得 APPKEY 及对应产品权限。平台支持个人与单位用户，认证与使用规则见[官方服务协议](https://www.jisuapi.com/about/agreement.html)和[接口使用指南](https://m.jisuapi.com/news/detail/580)。
- 免费额度、账号审核及适用资质须以实际申请时的控制台为准。当前未注册、未申请、未购买，不能认为测试权限已取得。

本项目的凭据配置名为 `JISU_COACH_API_KEY`，应通过本地环境或部署密钥配置注入，禁止提交到 Git。构造器不读取环境变量：

```python
JisuCoachAdapter(api_key: str, client: httpx.AsyncClient)
await adapter.search(request: SearchCoachesInput) -> SearchCoachesOutput
```

## 端点和能力限制

使用 `GET https://api.jisuapi.com/bus/city2c`，请求只含：

| 参数 | 当前实现 |
| --- | --- |
| `appkey` | 后端注入的供应商凭据 |
| `start` | 出发城市名称 |
| `end` | 到达城市名称 |

接口没有日期参数。统一工具输入虽然包含 `departure_date`，该适配器**不会将它发送给供应商，也不会据此生成实际发车日期**；工具描述及每次输出 warnings 都说明日期未核验。不要在 UI 或模型提示词中把这项结果展示为“已查询所选日期的车票”。

城市名称不会被替换成猜测的车站；具体起终点使用供应商返回的实际站名。`departure_station` 仅在已返回的结果中做准确名称筛选，不是供应商端的站点搜索。供应商地点 ID、非人民币、多人或儿童专用报价目前不支持，会返回明确错误。

此接口的结果没有班次 ID、日期库存及订票保证。即使存在价格，也不表示有票。它不执行任何预订操作。

## 输出语义

- 正数票价标记为 `reference` 和 `CNY`。乘客类别、人数口径、税费及服务费包含情况没有得到供应商明确说明，因此 `unit=unknown`、`priced_persons=null`，不计算整个人群票价。
- 缺失、空值和零价格保留未知，不把它们改成免费。
- `inventory.status=unknown`，`remaining=null`。
- `departure_at=null`、`arrival_at=null`。合规格式的发车钟点仅放入 `price.conditions`，并注明它是无日期的参考钟点。
- 流水班、时间段及无法解析的时刻不强转成单一时间。
- 不将 `distance` 或可能含“公里”的 `costTime` 解释成运行分钟数。
- **所有结果 `complete=false`**，包括空结果；warnings 保留不能按日期查询、没有实时库存和非完整人群总价的限制。
- 超过 `max_results` 时仅返回部分参考报价，并报告截断。

上游鉴权、限流、维护、业务错误及无效响应使用 `ToolFailure` 返回，不把它们变成成功空结果，也不回显凭据或原始上游错误正文。来源链接不包含 APPKEY。

## 验收与后续选择

离线测试文件：`tests/test_jisu_coach.py`，使用官方公开样例和 `httpx.MockTransport`。已检查不会误绑定日期、价格与库存分离、空值处理、单位与计价口径、异常响应和凭据脱敏。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_jisu_coach.py -q
```

取得测试权限后，先查询项目关心的城市对，验证是否有覆盖、参考站点和价格是否仍有效，并与当前承运渠道核对。即使真实调用成功，此供应商仍不具备按日期实时查票能力。若 MVP 必须给出某天的可售班次和库存，需要另接已获权限的分销或承运商接口，不能升级参考结果的标签来代替。
