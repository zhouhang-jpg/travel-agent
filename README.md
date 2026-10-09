# 出行助手工具层

这是 `D:\travel-agent` 的首期工具层：Python / FastAPI / Pydantic，使用 uv、pytest、Ruff。包含 11 个工具的契约、统一调度、按配置导出的模型工具定义，以及独立供应商适配。当前不运行 LLM、聊天前端或完整 ReAct Agent，不保存用户偏好。

**当前基础工具 7 项已可调用。** 2026-10-09 本地配置后，高德地点/详情/路线、和风每日天气、博查网页搜索和公开网页抓取均已取得真实响应；行程校验通过本地正常、冲突和缺证据样例。机票、火车、大巴、酒店 4 项在本轮基础注册表中尚未接入，不导出给模型。同目录中并行补入的票务供应商候选实现需完成独立接入验收，不能仅凭代码存在认定可用。没有凭据的全新环境默认只导出网页抓取和行程校验。

## 本地运行

需要 Python 3.11+ 和 uv，本次验证环境是 Windows、Python 3.13.9、uv 0.12.24。依赖由 `uv.lock` 锁定。

```powershell
cd D:\travel-agent
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_examples.py
uv run uvicorn travel_tools.api:app --host 127.0.0.1 --port 8000
```

本次环境没有全局 uv，已在忽略目录 `.tools/bin/uv.exe` 安装本项目可用的 uv。上面的 `uv` 可以替换为 `.\.tools\bin\uv.exe`；未改变全局 PATH、Python 包或 Git 身份。

HTTP 服务用于本地开发，尚无生产身份认证和租户隔离。默认命令绑定 `127.0.0.1`。`GET /health` 是进程健康检查，不代表外部供应商可用；`GET /tools` 返回全部契约及状态；`GET /tools/model-definitions` 仅返回可调度工具；`POST /tools/{name}/call` 接收 `{"arguments": {...}, "call_id": "可选模型工具调用ID"}`。接口文档见本地 `http://127.0.0.1:8000/docs`。

```powershell
$body = @{ arguments = @{ url = 'https://example.com/' } } | ConvertTo-Json -Depth 10
Invoke-RestMethod 'http://127.0.0.1:8000/tools/fetch_webpage/call' `
  -Method Post -ContentType 'application/json' -Body $body
```

## 账号配置与真实联调

将 `.env.example` 复制为 `.env`，在本机填写 `AMAP_API_KEY`、`QWEATHER_API_HOST`、`QWEATHER_API_KEY`、`BOCHA_API_KEY`。空值视为未配置。不要把明文密钥发到聊天或提交到 Git。环境变量优先于 `.env`；`.env`、虚拟环境、工具缓存、原始结果和私有资料目录已忽略。

```powershell
# 默认只发起一次真实公开网页 HTTPS 请求，不调用供应商或模型。
uv run python scripts/live_probe.py
# 配置供应商后，显式执行少量接口调用，可能消耗供应商额度。
uv run python scripts/live_probe.py --providers
# 额外验证高德周边、驾车、骑行、公交模式。
uv run python scripts/live_probe.py --providers --amap-all-modes
# 导出完整 JSON Schema 与当前可用模型工具定义；不联网。
uv run python scripts/export_schemas.py
```

联调脚本输出到被 Git 忽略的 `artifacts/`，仅记录状态、时间和输出字段等摘要。`--providers` 检查地点文本查询、成功结果的详情、步行路线、每日天气、网页搜索；加上 `--amap-all-modes` 扩展到周边与其他路线模式。真实供应商调用失败时以非零状态退出；缺配置以 `tool_unavailable` 记录为未运行，不当作成功。四项未接入查询不发送请求。一次成功是该样例与时间点的证据，不证明所有城市、日期和账号未来额度。

## 设计与验证材料

- [工具层设计与共同契约](docs/tool-layer.md)
- [本次验证与待办状态](docs/verification.md)
- [高德接口依据](docs/providers/amap.md)、[和风接口依据](docs/providers/qweather.md)、[博查接口依据](docs/providers/bocha.md)
- [行程校验规则](docs/itinerary.md)、[票务与酒店契约及供应商准入](docs/quotes.md)
- [正常样例](examples/itinerary-valid.json)、[冲突样例](examples/itinerary-invalid.json)、[证据不足样例](examples/itinerary-unknown.json)

来源、查询时间、数据时间、坐标系、价格性质、人数/房间/晚数、税费和库存语义分别保留。地图费用与网页内容不能替代指定日期的可售报价。校验器只检查传入证据的一致性，缺证据返回 `unknown`。

Git 已在本目录初始化。GitHub 远程仓库尚未创建或关联；当前工具没有建仓接口，本机没有 `gh`，未读取私有应用配置或借用其他项目凭据。CI 配置已纳入代码，但在创建并推送 GitHub 仓库前尚未在 GitHub 执行。
