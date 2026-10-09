# 工具层验证记录

验证日期：2026-10-09。工作目录：`D:\travel-agent`。本记录覆盖基础工具与可选票务供应商接入，使用 Python 3.13.9、uv 0.12.24 和 `uv.lock`。本轮未调用任何大模型。

## 当前工具状态

用户已在本地 `.env` 配置高德、和风和博查。基础目录包含 11 个输入输出契约，当前向模型导出 7 项工具。

| 工具 | 当前状态 | 本轮证据与限制 |
| --- | --- | --- |
| search_places | 已实现、已配置、真实调用通过 | 文本“故宫/北京”和 GCJ-02 周边公园搜索各返回 1 项；结果数按本次请求限制 |
| get_place_details | 已实现、已配置、真实调用通过 | 使用上述真实搜索返回的 POI ID，2 次详情均通过解析 |
| get_routes | 已实现、已配置、真实调用通过 | 步行/驾车/骑行各 1 个方案，公交 5 个方案；均为地图路线估算，不是票务报价 |
| get_weather | 已实现、已配置、真实调用通过 | 北京坐标查询 1 日预报，返回 1 个区间；不代表任意未来日期都能查到 |
| search_web | 已实现、已配置、真实调用通过 | “故宫博物院 官方 开放时间”返回 1 项；确认官网 api.bochaai.com 在本账号样例中可用 |
| fetch_webpage | 已实现、真实网页验证通过 | https://example.com/，HTML 577 字节，标题 Example Domain，正文匹配，无截断 |
| validate_itinerary | 已实现、本地验证通过 | 3 个可执行样例分别为 valid / invalid / unknown |
| search_flights | FlyAI 适配与注册完成，显式体验样例通过 | 完整 Python 注册调度返回 3 条真实候选；金额、完整报价与库存仍未知；当前 .env 未启用 |
| search_trains | 聚合/FlyAI 适配与注册完成 | FlyAI 完整工具实调已返回 3 条车次候选；金额/余票未知，正式 Key 待配置。聚合为备选，缺 key 未实调 |
| search_coaches | 极速适配与注册完成，缺 key | 离线契约测试通过；未真实联调。仅参考班次/票价，无按日查询或库存 |
| search_hotels | FlyAI 适配与注册完成 | FlyAI 完整工具实调已返回 3 条酒店候选；金额/库存未知，正式 Key 待配置 |

真实接口摘要位于被 Git 忽略的 `artifacts/live-probe-all-modes.json`，该轮时间为 **2026-10-09 10:59:40–10:59:46 UTC**。摘要仅记录状态、时间、返回数量和字段名，不记录认证信息或完整原始供应商结果。较早的 `live-probe-providers.json` 在和风 Host 尚未完成配置时记录了不可用；不能用它代替最新结果。

该全模式报告发现骑行返回正常但标准化总耗时为空。随后于 **11:02:40 UTC** 核对真实响应，确认总耗时位于 `paths[].duration`，分段仍为 `steps[].cost.duration`。适配器增加仅限骑行的后备读取，并在 **11:03:47 UTC** 真实复验：1 个方案、总耗时 **1024 秒**，10 个分段均有耗时。保留原问题报告便于追溯；修复后证据也记录在高德说明中。

一次成功仅说明当时账号、查询和响应能通过工具处理，不证明所有地点、日期、参数组合、未来余额或生产可靠性。错误、缺失值与不支持的能力仍会明确返回。

票务接线后于 **11:08:12–11:08:13 UTC** 执行 `scripts/live_probe_quotes.py --tool search_flights --flyai-demo`，通过完整 Settings → registry → Python adapter → 固定 FlyAI CLI 链路查询上海到北京的 2026-10-16 航班。状态 `ok`，按请求限制保留 3 条候选，`complete=false`；可用金额、查询时报价和已知库存均为 **0** 条。不能把这些候选称为准确机票报价。脱敏摘要位于 `artifacts/live-probe-quotes.json`。体验开关只对这次进程生效，未修改用户 `.env`，当前持久配置仍导出 7 个工具。

用户随后确认机票、火车票和酒店先使用飞猪。铁路默认改为显式配置 `TRAIN_SEARCH_PROVIDER=flyai`，不再根据另一家供应商是否有 Key 自动选择聚合；聚合仅在设为 `juhe` 时使用。缺配置或调用失败均不静默回退。用户现有 `.env` 已补入飞猪运行路径、空 Key 字段及铁路选型；保留原有密钥，体验开关仍为 false。正式 Key 尚待用户配置。

于 **11:15:32–11:15:34 UTC** 各执行一次完整工具链体验查询：上海→杭州火车（2026-10-16）、杭州酒店（2026-10-16 至 18）。两项均 `ok`，各保留 3 条候选、`complete=false`，金额/查询时报价/已知库存统计均为 0。铁路与酒店分别返回 4、6 条能力提示。报告为 `artifacts/live-probe-flyai-trains.json` 与 `artifacts/live-probe-flyai-hotels.json`，仅记录脱敏统计。由此验证车次与酒店候选可查询，尚不能确认准确票价、房价及库存。

## 本地测试与样例

基础层测试命令：

```powershell
.\.tools\bin\uv.exe run pytest tests/test_amap.py tests/test_api.py tests/test_bocha.py tests/test_common.py tests/test_itinerary.py tests/test_quotes.py tests/test_qweather.py tests/test_registry.py tests/test_webpage.py -q
.\.tools\bin\uv.exe run python scripts/check_examples.py
```

基础测试覆盖：供应商路径/参数/认证与返回映射、正常空结果和错误区别、缺失值/单位/时区/坐标、报价/库存/税费契约、HTTP 限流/鉴权/畸形响应、超时/大小/重定向、SSRF 与 DNS 固定、注册/Schema/可用性过滤、结果上限、API 请求体上限与期限、行程正常/矛盾/缺证据边界。离线供应商响应明确为合成契约夹具，不用它们证明真实接口已开通。

最终基础层测试：**270 passed**；本轮纳入 Git 的 Python/文档代码块 Ruff 检查和格式检查通过。测试构成：高德 53、和风与博查 67、行程与报价契约 90、API 9、共同类型 4、调度 9、网页抓取 38。GitHub CI 尚未运行，不能据本地通过宣称 CI 通过。

样例脚本通过真实注册与调度调用，**3/3 符合预期**：

- 正常样例：7 项 pass、2 项 not_applicable，总体 valid。
- 冲突样例：重叠与转场共 2 项 fail、5 项 pass、2 项 not_applicable，总体 invalid。
- 缺证样例：5 项 unknown、2 项 pass、1 项 not_applicable，总体 unknown。

票务集成后完整运行 `python -m pytest -q`：**396 passed**，包括上述 270 项基础测试、39 项 FlyAI 测试、72 项聚合/极速测试、15 项供应商选择与注册调度测试。FlyAI 测试包含真实本地 Node 子进程退出/异常隔离测试，其余供应商协议测试使用脱敏或合成夹具。测试通过不等于有正式报价权限。全项目 Ruff 检查与格式检查也通过。

FlyAI 固定包为 `@fly-ai/flyai-cli@1.0.16`，安装脚本检查归档及 bundle SHA256，不执行 npm 生命周期脚本或自动查询。包保存在忽略目录 `.tools/flyai-cli`；配置、设备状态保存在项目私有目录，密钥只通过子进程环境传递。正式 key 尚未验证。

飞猪选型变更后，针对供应商选择及 HTTP 注册集成运行 `pytest tests/test_quote_registry.py tests/test_api.py -q`：**27 passed**。新增回归确保选定飞猪时不会因聚合 Key 存在而切换，且选定但缺配置的供应商不会被另一家替代；Ruff 检查与格式检查通过。本次没有重复运行其他未修改模块的完整测试。

## 复核中发现并处理的问题

- 空白地点 ID 会让转场误判同地：所有行程 ID/地点/引用统一去首尾空白并拒绝空值，null 保持未知，补充 17 个回归用例。
- 同步 HTML 解析不能响应异步期限：改为带取消检查点的增量解析，增加复杂度上限；标题单独限制 512 字符并标注截断。
- HTTP 慢上传发生在工具期限外：请求体接收增加独立期限，超时返回 408，不把不完整正文传给工具。
- 过大工具结果可能占满未来上下文：统一输出增加 2 MiB 边界，超限明确失败，不返回静默截断历史。
- Swagger 原先没有展示手动读取的请求体：补入调用包的 OpenAPI Schema，交互文档可填写 arguments。
- 真实高德骑行响应总耗时层级与原映射不同：按实际返回与官方耗时语义修正，并补充 8 个回归用例及一次真实复验。

## Git、安全配置与后续事项

本目录已初始化 Git，保留原始 AGENTS.md，沿用现有 Git 身份，没有修改全局配置。`.env`、`.venv`、`.tools`、`artifacts`、私有目录与密钥文件均被忽略。真实配置和原始响应不纳入提交。

GitHub 仓库尚未创建或关联：当前 GitHub 工具没有创建仓库能力，本机未提供 `gh`，本任务没有借用其他项目或私有应用配置。已添加 CI 文件，但未在 GitHub 实际运行。

后续需要申请并验证正式票务酒店账号，重点是准确价格、库存、计价人数或房间数与税费。聚合和极速尚缺 key；FlyAI 正式 key 和实际报价完整性仍需联调。携程官方 MCP 可作为企业合作候选，12306 暂未查到普通开发者自助公开 API 的官方接入证据，详见供应商比较。公开部署还需要身份认证与运行环境方案；本轮 FastAPI 服务按 README 绑定本机地址使用。
