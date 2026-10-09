# 多轮自主出行助手

这是 `D:\travel-agent` 的多轮出行助手：React / TypeScript / Vite 前端配合 Python / FastAPI / Pydantic 后端，使用 pnpm、uv、pytest、Ruff。轻量 ReAct 由 LLM 根据当前目标与完整会话历史自主选择提问、查询、规划或调整方案。默认测试模型为 `deepseek-flash`，预留其他模型厂商接口；暂不保存跨会话偏好。

**普通大巴、铁路和机票已完成有限真实样本的后端验证。** 普通大巴使用出行365，铁路优先12306，FlyAI保留为可配置且披露来源的降级或显式候选查询；机票用FlyAI快速候选，并可按需选择东航官网核实含税/税前展示价。网页查询由项目独立浏览器执行，不依赖Codex会话。酒店继续FlyAI；基础地图、天气、搜索和行程校验也已验证。当前票务只验证单成人，酒店限一个房间；列表价和库存快照不承诺可成功购票。机场巴士专线、高铁接驳专线及拼车/商务车暂缓。

用户可以回答助手追问，或在本轮完成后继续修改需求。提示词不限定提问数量、查询顺序或答案模板；模型选择等待回复时单独调用 `ask_user`，普通正文表示本轮交付完成。执行中输入禁用，不支持插话或取消；刷新或关闭页面后后台继续，重新打开可恢复公开对话。
澄清不是一次性的开场阶段：用户回答、工具返回新限制或修改需求后，模型可在同一会话再次
`ask_user`；信息足够或用户授权自行决定时继续处理，不强制每轮提问。用户的新授权更新对应
约束，不扩张局部授权，也不把已被替换的旧首选当成不可变。

助手倾向尽早集中澄清可能导致方案返工的缺失条件，复用已知信息；信息充分时直接查询或规划，
不知道去哪时可以先探索。新增 `get_attraction_opening_hours` 查询常规营业描述和计划日期公告
证据，保留分馆、来源、时间、失败和未知，模型核对官方身份及适用日期；不把缺公告当成开放。

完整用户、模型、工具历史及供应商要求的 `reasoning_content` 在后端持久化。UI 只显示公开答复、问题和工具状态，不展示隐藏推理、原始参数或完整工具结果。操作范围为只读查询与规划。

## 本地运行

需要 Python 3.11+ 和 uv，本次验证环境是 Windows、Python 3.13.9、uv 0.12.24。依赖由 `uv.lock` 锁定。

```powershell
cd D:\travel-agent
uv sync --locked
# 使用公开网页票务来源时，先安装独立浏览器并设置 BROWSER_QUERIES_ENABLED=true。
uv run playwright install chromium
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_examples.py
uv run uvicorn travel_tools.api:app --host 127.0.0.1 --port 8000 --workers 1
```

本次环境没有全局 uv，已在忽略目录 `.tools/bin/uv.exe` 安装本项目可用的 uv。上面的 `uv` 可以替换为 `.\.tools\bin\uv.exe`；未改变全局 PATH、Python 包或 Git 身份。

另一个终端启动前端，打开 `http://127.0.0.1:5173/`：

```powershell
cd D:\travel-agent\frontend
pnpm install --frozen-lockfile
pnpm dev
```

Vite 将 `/api` 代理至后端。当前要求 **单 worker、单应用进程**，不支持多进程共享执行租约。默认数据库为 `sqlite+aiosqlite:///private/travel-agent.db`；可配置 `postgresql+asyncpg://...`，PostgreSQL 尚未真实联调。首次本地启动创建缺失表，后续 schema 升级从仓库根目录运行 `uv run alembic upgrade head`。迁移读取本地 Settings，不把凭据写进迁移文件；迁移测试使用独立临时数据库。

`POST /conversations` 创建会话；`GET /conversations` 及 `GET /conversations/{id}` 恢复公开对话；`POST /conversations/{id}/messages` 接收消息并返回 SSE。执行中同会话的新输入返回 409，关闭 SSE 不取消后台任务。`GET /agent/health` 返回模型配置状态。Agent 导出已配置工具（含普通大巴）并添加 `ask_user`；底层工具目录保留全部契约。

应用层不限制完整上下文字符数、模型输出 token 数、模型响应字节数、追问消息长度或工具调用次数；完整历史不摘要或裁剪，请求不传 `max_tokens` 或 `max_completion_tokens`，输出由供应商默认策略及其自身容量决定。供应商截断或拒绝仍明确报告并保留执行记录。默认每次运行最多 12 次模型决策、300 秒，均可配置；单次模型请求 120 秒。模型默认启用思考、`reasoning_effort=high`。这些运行预算不是业务步骤，失败不自动更换模型或供应商。

HTTP 服务用于本地开发，尚无生产身份认证和租户隔离。默认命令绑定 `127.0.0.1`。`GET /health` 是进程健康检查，不代表外部供应商可用；`GET /tools` 返回全部契约及状态；`GET /tools/model-definitions` 仅返回可调度工具；`POST /tools/{name}/call` 接收 `{"arguments": {...}, "call_id": "可选模型工具调用ID"}`。接口文档见本地 `http://127.0.0.1:8000/docs`。

```powershell
$body = @{ arguments = @{ url = 'https://example.com/' } } | ConvertTo-Json -Depth 10
Invoke-RestMethod 'http://127.0.0.1:8000/tools/fetch_webpage/call' `
  -Method Post -ContentType 'application/json' -Body $body
```

## 账号配置与真实联调

新环境可从 `.env.example` 准备配置；已有 `.env` 请保留，补入需要的字段。模型使用 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`；数据服务使用高德、和风、博查及 FlyAI 配置。空值视为未配置；体验模式默认关闭。环境变量优先于 `.env`，密钥只存后端，`.env`、虚拟环境、工具缓存、原始结果和私有数据库均已忽略。

```powershell
# 默认只发起一次真实公开网页 HTTPS 请求，不调用供应商或模型。
uv run python scripts/live_probe.py
# 配置供应商后，显式执行少量接口调用，可能消耗供应商额度。
uv run python scripts/live_probe.py --providers
# 额外验证高德周边、驾车、骑行、公交模式。
uv run python scripts/live_probe.py --providers --amap-all-modes
# 导出完整 JSON Schema 与当前可用模型工具定义；不联网。
uv run python scripts/export_schemas.py
# 开发样例：真实多轮模型和工具联调，会消耗已配置服务额度。
uv run python scripts/live_probe_agent.py --live
uv run python scripts/live_probe_clarification.py --live
uv run python scripts/live_probe_opening_hours.py --live
uv run python scripts/live_probe_tickets.py --live
uv run python scripts/live_probe_followup.py --live
pnpm --dir frontend test
pnpm --dir frontend build
```

联调脚本输出到被 Git 忽略的 `artifacts/`，仅记录状态、时间和输出字段等摘要。`--providers` 检查地点文本查询、成功结果的详情、步行路线、每日天气、网页搜索；加上 `--amap-all-modes` 扩展到周边与其他路线模式。真实供应商调用失败时以非零状态退出；缺配置以 `tool_unavailable` 记录为未运行，不当作成功。该脚本不调用票务接口。一次成功是该样例与时间点的证据，不证明所有城市、日期和账号未来额度。

票务/酒店配置见 `.env.example` 和下方供应商文档。保留自己的 `.env`，只补入需要的字段。先准备固定版本 FlyAI CLI，再显式查询一个样例：

```powershell
powershell -NoProfile -File scripts/setup_flyai.ps1
# 只对本次进程启用体验模式，不修改 .env，不调用下单接口。
uv run python scripts/live_probe_quotes.py --tool search_flights --flyai-demo
# 使用已经配置的正式供应商（可能计费），每次仅查询一项。
uv run python scripts/live_probe_quotes.py --tool search_trains
```

`live_probe_quotes.py` 默认查询七天后的单成人样例，支持 `--origin`、`--destination`、`--date`，只输出候选数量及字段完整性统计。它不补全缺失价格、税费或库存。铁路默认 `TRAIN_SEARCH_PROVIDER=12306`；`TRAIN_FALLBACK_PROVIDER=flyai`允许默认来源受阻时明确披露降级，显式指定来源、未开售和空结果不降级；聚合须显式选`juhe`。`live_probe_tickets.py`通过正在运行的HTTP后端少量验证大巴、席别/候补、直达过滤和机票税费展示。`live_probe_followup.py`用真实模型与铁路工具验证再次追问、用户选择后继续和部分回答补问，不修改用户会话。`live_probe_agent.py`使用独立探针数据库；这些都是验收样例，不是固定业务workflow。

## 设计与验证材料

- [工具层设计与共同契约](docs/tool-layer.md)
- [景点开放时间证据与日期边界](docs/opening-hours.md)
- [Agent 行为与完整上下文](docs/agent-behavior.md)、[模型接口与私有字段](docs/providers/deepseek.md)、[前端说明](frontend/README.md)
- [本次验证与待办状态](docs/verification.md)
- [高德接口依据](docs/providers/amap.md)、[和风接口依据](docs/providers/qweather.md)、[博查接口依据](docs/providers/bocha.md)
- [飞猪 FlyAI](docs/providers/flyai.md)、[聚合火车](docs/providers/juhe-train.md)、[极速大巴](docs/providers/jisu-coach.md)、[携程及铁路供应商比较](docs/providers/ticket-suppliers.md)
- [独立浏览器票务来源、配置和事实边界](docs/providers/browser-tickets.md)
- [行程校验规则](docs/itinerary.md)、[票务与酒店契约及供应商准入](docs/quotes.md)
- [正常样例](examples/itinerary-valid.json)、[冲突样例](examples/itinerary-invalid.json)、[证据不足样例](examples/itinerary-unknown.json)

来源、查询时间、数据时间、坐标系、价格性质、人数/房间/晚数、税费和库存语义分别保留。地图费用与网页内容不能替代指定日期的可售报价。校验器只检查传入证据的一致性，缺证据返回 `unknown`。

GitHub 仓库为 [zhouhang-jpg/travel-agent](https://github.com/zhouhang-jpg/travel-agent)，origin 已关联并正常推送 main。上传前检查全部本地提交未包含本地已配置密钥、私有会话、数据库或忽略目录；GitHub Actions 状态以仓库页面为准，不能据本地测试通过宣称 CI 通过。

原讨论聊天只交流需求与方案，本项目聊天负责提示词、代码、调试和验证。
