# Agent 与工具验证记录

验证日期：2026-10-09。工作目录：`D:\travel-agent`。本记录覆盖多轮 Agent、前端、持久化与工具层，使用 Python 3.13.9、uv 0.12.24 和锁定依赖。当前已完成 `deepseek-flash` 真实模型调用；单元测试仍使用注入式模型和供应商响应。

## 最新多轮 Agent 验收

用户要求给予 LLM 更大自主度，提示词已简化为目标、自主决策说明、运行协议和事实边界，移除固定问题数量、必须先追问/先查工具及答案模板。仅保留 `ask_user` 表示等待用户的接口约定、工具契约、准确性和资源限制。新版系统正文为 771 字符，已重启本地单 worker 后端生效。

- 全量 Python 测试：**496 passed in 17.09s**；Ruff 检查与格式检查通过。
- 前端 SSE 解析测试：**3 passed**；TypeScript 与 Vite 生产构建通过。
- 新增迁移测试：独立临时 SQLite 库完成 upgrade、重复 upgrade、保存及重开恢复、downgrade 和再次 upgrade。默认会话库未参与测试；PostgreSQL 尚未真实联调。
- `tests/test_api.py` 改用内存数据库，避免默认会话库被工具 API 测试初始化或恢复运行租约。

真实模型探针 `artifacts/agent-autonomy-probe.json`：模糊出行需求由模型自主调用 ask_user，提出 5 项补充信息并进入 waiting_user；用户给出上海单人一天需求后调用 get_weather 并 completed；预算修改后复用历史，无新工具调用，completed。三轮均未公开 reasoning_content。这是一个验收样例，不规定所有需求都按该顺序执行。

实际浏览器在 `http://127.0.0.1:5173/` 新建独立“开发验收”会话，完成追问→补充→真实天气/地点/路线查询→预算修改；执行中输入禁用，刷新后恢复 running 并由后台继续，结束后恢复 completed。公开记录 6 条、内部完整历史 15 条，5 条模型消息保留 reasoning_content，所有工具调用 ID 均有匹配结果；预算修改轮没有新查询，`last_error=null`。脱敏摘要为 `artifacts/agent-browser-verification.json`，截图为 `artifacts/agent-browser-verified.jpg`。

正式 FlyAI Key 的火车/酒店验证时间分别为 11:40:26–11:40:28 UTC，各成功返回 3 条候选、complete=false；价格和库存仍不完整。报告为 `artifacts/live-probe-formal-trains.json` 与 `artifacts/live-probe-formal-hotels.json`。当前 Agent 使用 10 项已配置基础工具及 ask_user，排除大巴。

以下保留早期工具层记录以便追溯；其中“正式 Key 待配置”等描述是当时状态，以本节和当前表格为准。

## 当前工具状态

用户已配置高德、和风、博查、FlyAI 与 DeepSeek。工具目录保留 11 项契约，当前 Agent 使用已配置的 10 项工具加 ask_user；大巴排除。

| 工具 | 当前状态 | 本轮证据与限制 |
| --- | --- | --- |
| search_places | 已实现、已配置、真实调用通过 | 文本“故宫/北京”和 GCJ-02 周边公园搜索各返回 1 项；结果数按本次请求限制 |
| get_place_details | 已实现、已配置、真实调用通过 | 使用上述真实搜索返回的 POI ID，2 次详情均通过解析 |
| get_routes | 已实现、已配置、真实调用通过 | 步行/驾车/骑行各 1 个方案，公交 5 个方案；均为地图路线估算，不是票务报价 |
| get_weather | 已实现、已配置、真实调用通过 | 北京坐标查询 1 日预报，返回 1 个区间；不代表任意未来日期都能查到 |
| search_web | 已实现、已配置、真实调用通过 | “故宫博物院 官方 开放时间”返回 1 项；确认官网 api.bochaai.com 在本账号样例中可用 |
| fetch_webpage | 已实现、真实网页验证通过 | https://example.com/，HTML 577 字节，标题 Example Domain，正文匹配，无截断 |
| validate_itinerary | 已实现、本地验证通过 | 3 个可执行样例分别为 valid / invalid / unknown |
| search_flights | FlyAI 已配置，体验候选曾通过 | 完整工具链返回过 3 条候选；正式 Key 航班未另做本轮实调，完整价格/库存仍不保证 |
| search_trains | 默认 FlyAI 已配置，正式 Key 调用通过 | 3 条车次候选；报价/余票仍不完整。聚合为显式备选，未实调 |
| search_coaches | 当前 Agent 排除 | 保留极速参考适配，未真实联调；不做大巴爬虫 |
| search_hotels | FlyAI 已配置，正式 Key 调用通过 | 3 条酒店候选；房型/完整费用/库存仍未完全核实 |

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

GitHub 仓库现已关联并推送到 https://github.com/zhouhang-jpg/travel-agent 。上传前检查全部 5 个本地提交、102 个路径：未发现本地配置中的真实密钥，也没有私有会话、数据库、private/、artifacts/ 或本地工具文件入库。远端 main 与本地提交核对一致；GitHub Actions 结果以实际运行状态为准。

后续重点是 FlyAI 所需服务权限、准确价格、库存、计价人数/房间数及税费的完整性；正式 Key 已通过火车/酒店候选查询，不能据此确认完整报价。大巴本期跳过；PostgreSQL、生产认证、多进程租约尚未完成。当前 FastAPI 按 README 使用本地单 worker。

## 四日游输出截断修复（2026-10-09）

真实北京至上海四日游会话已完成16次工具查询，后续模型请求以 `finish_reason=length` 结束，错误码为 `output_truncated`。原统一错误提示误导为配置问题；现按安全错误码显示长度上限、超时、限流、鉴权等具体原因，不输出供应商原文或推理。

默认输出额度由4096改为16384，模型请求期限由60秒改为120秒；完整上下文额度由200000字符改为500000字符，运行总期限由180秒改为300秒。所有限制仍有限，不裁剪历史，不自动切换模型或重试。

从该会话的完整24条历史在隔离环境恢复，DeepSeek Flash 补充路线与行程校验，约99秒后生成四日方案，状态completed，原历史逐条保持不变。报告仅保存公开回答及验证元数据于忽略目录artifacts。真实报价及指定日期营业状态的准确性仍遵循来源能力边界。

本轮模型、运行器、会话API回归100项通过；全套Python测试500项通过（17.15秒），Ruff检查和格式检查通过。前端未改动，本轮未重复前端构建。后端已重启并确认进程及模型配置健康。

## 取消应用层上下文与模型输出长度限制（2026-10-09）

按用户最新要求，上一节的上下文字符数和输出token额度不再适用。删除AGENT_MAX_CONTEXT_CHARS与LLM_MAX_TOKENS配置及对应代码，不再发送max_tokens/max_completion_tokens，不限制模型响应字节数或ask_user消息长度；本地旧配置已移除，凭据保留。工具检索契约和执行时间/轮数/调用次数预算仍按既有规则执行。供应商自身上下文容量和默认输出策略仍适用；finish_reason=length仍作为真实截断报错，不交付为成功答案。

回归覆盖完整传递超过原500000字符的历史、超过原2MiB的模型响应，以及超过原4000字符的追问。模型、运行器、会话API和工具API相关测试110项通过（12.57秒），Ruff检查及格式检查通过。真实DeepSeek Flash请求在省略输出上限参数后返回stop与“已就绪。”；不据此宣称供应商具有无限容量。前端未修改，未重复构建。
