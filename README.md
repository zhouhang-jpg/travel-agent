# 多轮自主出行助手

这是 `D:\travel-agent` 的多轮出行助手：React / TypeScript / Vite 前端配合 Python / FastAPI / Pydantic 后端，使用 pnpm、uv、pytest、Ruff。轻量 ReAct 由 LLM 根据当前目标与完整会话历史自主选择提问、查询、规划或调整方案。默认测试模型为 `deepseek-flash`，预留其他模型厂商接口；暂不保存跨会话偏好。

**基础工具已验证；机票、火车与酒店统一先使用 FlyAI 候选查询。** 高德地点/详情/路线、和风每日天气、博查搜索与公开网页读取均已取得真实响应；行程校验通过正常、冲突和缺证样例。FlyAI 正式 Key 已返回火车和酒店候选，但价格、税费、房型与库存仍可能不完整；目前查询限一个成人，酒店限一个房间。聚合铁路为显式备选；本期 Agent 排除大巴票，不做大巴爬虫，保留旧适配代码供后续使用。

用户可以回答助手追问，或在本轮完成后继续修改需求。提示词不限定提问数量、查询顺序或答案模板；模型选择等待回复时单独调用 `ask_user`，普通正文表示本轮交付完成。执行中输入禁用，不支持插话或取消；刷新或关闭页面后后台继续，重新打开可恢复公开对话。

完整用户、模型、工具历史及供应商要求的 `reasoning_content` 在后端持久化。UI 只显示公开答复、问题和工具状态，不展示隐藏推理、原始参数或完整工具结果。操作范围为只读查询与规划。

## 本地运行

需要 Python 3.11+ 和 uv，本次验证环境是 Windows、Python 3.13.9、uv 0.12.24。依赖由 `uv.lock` 锁定。

```powershell
cd D:\travel-agent
uv sync --locked
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

`POST /conversations` 创建会话；`GET /conversations` 及 `GET /conversations/{id}` 恢复公开对话；`POST /conversations/{id}/messages` 接收消息并返回 SSE。执行中同会话的新输入返回 409，关闭 SSE 不取消后台任务。`GET /agent/health` 返回模型配置状态。Agent 在可用工具上添加 `ask_user` 并排除 `search_coaches`；底层工具目录仍保留全部契约。

默认每次运行最多 12 次模型决策、24 次工具调用、500000 个上下文字符、300 秒，均可配置。完整历史不摘要或裁剪；超限明确报告并保留历史，用户可继续。模型默认启用思考、`reasoning_effort=high`、16384 个输出 Token、单次 120 秒超时；这些是运行参数，不是业务步骤。失败不自动更换模型或供应商。

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

`live_probe_quotes.py` 默认查询七天后的单成人样例，支持 `--origin`、`--destination`、`--date`，只输出候选数量及字段完整性统计。它不补全缺失价格、税费或库存。铁路默认 `TRAIN_SEARCH_PROVIDER=flyai`，如需聚合必须显式设为 `juhe`；缺配置或上游失败均不自动切换供应商。`live_probe_agent.py` 使用开发样例和独立探针数据库演示追问、补充、查询、修改；这是一条验收路径，不是固定 Agent workflow。

## 设计与验证材料

- [工具层设计与共同契约](docs/tool-layer.md)
- [Agent 行为与完整上下文](docs/agent-behavior.md)、[模型接口与私有字段](docs/providers/deepseek.md)、[前端说明](frontend/README.md)
- [本次验证与待办状态](docs/verification.md)
- [高德接口依据](docs/providers/amap.md)、[和风接口依据](docs/providers/qweather.md)、[博查接口依据](docs/providers/bocha.md)
- [飞猪 FlyAI](docs/providers/flyai.md)、[聚合火车](docs/providers/juhe-train.md)、[极速大巴](docs/providers/jisu-coach.md)、[携程及铁路供应商比较](docs/providers/ticket-suppliers.md)
- [行程校验规则](docs/itinerary.md)、[票务与酒店契约及供应商准入](docs/quotes.md)
- [正常样例](examples/itinerary-valid.json)、[冲突样例](examples/itinerary-invalid.json)、[证据不足样例](examples/itinerary-unknown.json)

来源、查询时间、数据时间、坐标系、价格性质、人数/房间/晚数、税费和库存语义分别保留。地图费用与网页内容不能替代指定日期的可售报价。校验器只检查传入证据的一致性，缺证据返回 `unknown`。

Git 已在本目录初始化。GitHub 远程仓库尚未创建或关联；当前工具没有建仓接口，本机没有 `gh`，未读取私有应用配置或借用其他项目凭据。CI 配置已纳入代码，但在创建并推送 GitHub 仓库前尚未在 GitHub 执行。

原讨论聊天只交流需求与方案，本项目聊天负责提示词、代码、调试和验证。
