# 博查网页搜索适配说明

核对日期：2026-10-09。已实现可注入 HTTP 客户端的 Web Search 适配并以虚构响应进行契约测试。本次用户配置本地 Key 后，真实搜索“故宫博物院 官方 开放时间”成功返回 1 条结果（2026-10-09 10:59:45 UTC）；脱敏摘要见 `artifacts/live-probe-all-modes.json`。网页搜索结果不能作为实时票价、房价或库存接口的替代。

## 官方依据与尚待账号确认的差异

- [博查开放平台官网](https://open.bochaai.com/)：公开调用示例为 `POST https://api.bochaai.com/v1/web-search`，Bearer 认证，JSON 参数 `query/freshness/summary/count`；页面给出网页标题、URL、摘要和发布时间等字段。
- [官方 bocha-ai 集成仓库 README](https://github.com/bocha-ai/dsh-web-search-bocha/blob/main/README.md) 与 [适配代码](https://github.com/bocha-ai/dsh-web-search-bocha/blob/main/src/provider.ts)：确认 `data.webPages.value[]` 响应包裹、可选业务 `code`、1–50 条结果和有限时间范围枚举。
- 官网链接的 [Web Search 文档](https://bocha-ai.feishu.cn/wiki/RXEOw02rFiwzGSkd9mUcqoeAnNK) 在此次文档读取中不可访问，未假称已完整读到飞书正文。实现基于上面可读取的官网和官方仓库。

需注意：当前官网示例使用 `api.bochaai.com`，官方集成仓库默认使用 `api.bocha.cn`。本项目采用官网明确展示的前者，禁止自动跟随跳转和静默切换 Host。本次真实账号已验证前者在该查询中可用；没有测试或自动切换到后者。官方仓库支持更多时间格式，但本工具先开放已确认的五个时间范围枚举，不开放未必要的自定义 Host。

## 工具契约

`BochaAdapter(api_key, client).search_web(SearchWebInput)`。

输入：`query` 为 1–1000 字符且不能全空白；`freshness` 可为 `noLimit/oneDay/oneWeek/oneMonth/oneYear`，默认 `noLimit`；`summary` 默认 `true`；`count` 为 1–50，默认 10。1000 字符是本项目资源上限，并非宣称供应商上限。默认值只属于工具契约草案，不属于已确认的 Agent 业务流程。

输出保留 `title/url/snippet/summary/site_name`，缺失可选字段为 `null`。`published_at` 仅在 `datePublished` 可解析且带时区时填入；无时区、不可解析或缺失均保持未知，同时保留 `published_at_raw`。`dateLastCrawled` 仅保留到 `last_crawled_raw`，不推断为发布时间或真实抓取时间，防止供应商历史字段语义问题被静默“修正”。每条结果包含来源 URL 和本次 UTC 查询时间。

只有明确返回 `data.webPages.value=[]` 才是合法空结果；缺失 `data/webPages/value`、错误业务码或错误 URL 不当作搜索无结果。网页文本以 `content_is_untrusted=true` 标记，必须作为外部证据使用，不能当成系统指令。网页来源不保证事实准确性、实时可售状态或预订能力。

## 边界与验证

认证只使用 Bearer 请求头。15 秒总时限、2 MiB 解码后响应体、不跟随重定向、不自动重试。HTTP 失败、业务失败、网络失败与无法解析的响应分别返回安全错误，不透出供应商原始消息或密钥。结果 URL 只允许 HTTP/HTTPS 且不可嵌入用户认证信息；实际抓取仍必须通过 `fetch_webpage` 的目标校验，不因为搜索返回了 URL 就跳过安全检查。

`tests/test_bocha.py` 覆盖端点、请求体、认证、来源、缺失值与日期语义、合法空结果、错误响应、HTTP/业务错误、跳转拒绝和输入上限；共同传输的超时与大小边界另在 `test_qweather.py` 覆盖。

后续接入新环境需本地配置 `BOCHA_API_KEY` 并核实账户权限、余额、所需筛选和无结果形态。本次真实验收只覆盖一个公开查询，不能由此推断剩余额度或所有功能。不要在聊天中粘贴明文密钥。
