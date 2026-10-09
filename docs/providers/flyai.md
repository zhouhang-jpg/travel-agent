# 飞猪 FlyAI 查询适配器

本适配器将官方 FlyAI CLI 的机票、铁路和酒店候选查询映射到项目的报价契约。固定运行包版本为 **`@fly-ai/flyai-cli@1.0.16`**。本文对应 2026-10-09 核验的文档和真实体验查询。

这里的“可调用”表示能够执行候选搜索，不表示取得了生产授权、完整实时报价或可售库存。所有输出始终为 `complete: false`。

## 官方资料与账号开通

- [官方快速开始](https://flyai.open.fliggy.com/docs/quickstart)：安装、正式 API Key 配置入口。
- [官方控制台](https://flyai.open.fliggy.com/console)：登录并获取自己的正式 API Key；具体权限和配额以账号为准。
- [官方 FAQ](https://flyai.open.fliggy.com/docs/faq)：部分商品不返回价格，需要通过返回链接查看最新价格。
- [官方开源参考](https://github.com/alibaba-flyai/flyai-skill)：查询命令及参数。
- [固定版本 npm 元数据](https://registry.npmjs.org/@fly-ai/flyai-cli/1.0.16)：版本、归档及 integrity 信息。

官方公开体验模式无需用户先申请正式 Key，但结果可能受限。推广者签约与返佣是另外的业务功能，本项目不执行入驻、推广签约、支付或预订。

## 固定版本准备

在项目根目录执行：

```powershell
powershell -NoProfile -File .\scripts\setup_flyai.ps1
```

脚本仅下载固定 npm 归档、核验 SHA256、检查归档路径并解压。不会执行 npm 生命周期脚本，不全局安装，不运行 CLI，不请求供应商，不写入 `.env` 或用户主目录。

有已下载归档时可以离线准备：

```powershell
powershell -NoProfile -File .\scripts\setup_flyai.ps1 `
  -ArchivePath 'C:\path\to\flyai-cli-1.0.16.tgz'
```

目标文件：

```text
<project>/.tools/flyai-cli/package/dist/flyai-bundle.cjs
<project>/.tools/flyai-cli/package/package.json
<project>/.tools/flyai-cli/package/README.md
<project>/.tools/flyai-cli/flyai-cli-1.0.16.tgz
<project>/.tools/flyai-cli/provenance.json
```

`.tools/` 已被 Git 忽略；固定版本、来源和哈希保存在本受版本管理的脚本与文档中。运行包不会进入源码仓库。Node 由运行环境单独提供；包声明要求 Node >= 18，此项目在 Windows Node 24.14.0 与 24.19.0 上进行了兼容验证。

固定归档来源：

```text
https://registry.npmjs.org/@fly-ai/flyai-cli/-/flyai-cli-1.0.16.tgz
```

SHA256：

| 文件 | SHA256 |
| --- | --- |
| flyai-cli-1.0.16.tgz | AA133FE4E627DDAB07916EC3AFCA1D51D5A9AC5F8BD71A4219D5E8D8C30FDA40 |
| dist/flyai-bundle.cjs | 194A66EB84094F3D8EE20FAC0A2D6CAE10A405CD59AC100B7E7880EBC97297DA |

也可以使用 `npm pack @fly-ai/flyai-cli@1.0.16 --ignore-scripts` 获取归档，然后交给上述 `-ArchivePath` 路径安装。脚本使用直接下载方式避免依赖全局 npm 包或执行安装钩子。升级时应同时更新版本与哈希，并重新验证响应契约和 guard 兼容性；不能自动跟随 latest。

## 配置与注册接口

适配层需要显式提供三个路径及可选密钥。Node 路径必须是已安装可执行文件，不能只传待由 shell 查找的命令别名。

```python
from travel_tools.providers.flyai import (
    FlyAIClient,
    FlyAIFlightAdapter,
    FlyAITrainAdapter,
    FlyAIHotelAdapter,
)

client = FlyAIClient(
    node_path=r"C:\Program Files\nodejs\node.exe",
    cli_path=r"D:\travel-agent\.tools\flyai-cli\package\dist\flyai-bundle.cjs",
    state_directory=r"D:\travel-agent\private\flyai",
    api_key=None,  # 或从本地受保护配置读取，不把真实值写进代码或日志
    timeout=30.0,
)

flight_handler = FlyAIFlightAdapter(client).search
train_handler = FlyAITrainAdapter(client).search
hotel_handler = FlyAIHotelAdapter(client).search
```

构造器确切签名：

```python
FlyAIClient(
    node_path: str | Path,
    cli_path: str | Path,
    state_directory: str | Path,
    *,
    api_key: str | None = None,
    timeout: float = 30.0,
    executor: CLIExecutor | None = None,
)
```

各 adapter 构造器只接收 `client`；handler 是异步 `.search(request)`，对应 `SearchFlightsInput -> SearchFlightsOutput`、`SearchTrainsInput -> SearchTrainsOutput`、`SearchHotelsInput -> SearchHotelsOutput`。在 registry 中将该 bound method 传入 `ToolSpec.handler`；工具名为复数 `search_flights`、`search_trains`、`search_hotels`。本文件不宣称共享配置模块已经自动完成注册；以当前 bootstrap/settings 实现为准。

`timeout` 范围为 `(0, 120]` 秒。外层 registry 也有截止时间，部署时应使两者协调；取消会终止并回收子进程。没有 Node/CLI 文件时，调用会返回 `provider_not_configured`，不会自动安装。

`private/` 已被 Git 忽略。guard 只将该子进程的 `os.homedir()` 和 `os.tmpdir()` 指向显式状态目录，使官方 CLI 的 `.flyai/device-id`、本地配置等保留在项目私有目录，不触碰真实用户主目录。

## 正式 API Key 的传递依据

官方 1.0.16 bundle 读取 `process.env['FLYAI_API_KEY']`。非空时，该环境变量优先于调试 key、本地 `config.json` 及内置体验模式；官方文档也提供 `flyai config set FLYAI_API_KEY ...` 的写配置方法。

本适配器使用环境变量方式：`FlyAIClient(api_key=...)` 在创建子进程时把值放入其 `FLYAI_API_KEY`，不放入命令参数，不调用 `config set`，不持久化密钥。它不会自动沿用主进程的同名变量；共享 Settings 层必须显式读取并传入构造器。调试 URL、调试 key 和自定义签名环境变量也会从子进程环境移除。

`api_key=None`、隔离状态目录内不存在旧 key 配置时，官方 CLI 使用公开体验模式。如果曾手动在同一隔离目录写过 `.flyai/config.json`，官方 CLI 仍可能采用该配置；不要把包含不同账号 key 的目录共用作体验环境。正式 key 开通后必须重新核验结果完整性、权限与配额，不能仅凭 key 非空认定生产服务已验证。

## 返回信息和能力边界

2026-10-09 的无正式 key 实测：

| 查询 | 观察到的结果 | 保留的限制 |
| --- | --- | --- |
| 上海到北京，2026-10-16 | HTTP 200、业务 status 0，10 个航班候选；实际价格字段为 `ticketPrice` | 没有单独币种、完整税费或库存证据；参考文本不能当作订单总额 |
| 上海到杭州，2026-10-16 | HTTP 200、业务 status 0，10 个铁路候选 | `price` 可能为 `2x`；quantity 为 null |
| 杭州酒店，2026-10-16 至 18 | HTTP 200、业务 status 0，9～10 个酒店候选 | `price` 可能为 `¥3x`；没有明确房型、入住人数、税费或取消政策 |

这些验证只覆盖当时返回的数据，不证明未来可用性或完整供应商覆盖；整个过程没有下单、支付或登录。

映射原则：

- 遮罩价格以及缺少明确 ISO 币种的价格保留在 `conditions.raw_price`，`money` 为 null。`¥` 本身不足以确定币种。
- 只有精确非负数值与明确币种同时存在时，返回 `reference` 类型金额；税费、计价人数、房间数、晚数和有效期继续保留未知。
- 候选出现不等于可售，所有 `inventory` 保持 `unknown`。
- 供应商无时区的时间保留原文，不擅自补时区；明确带 UTC offset 时才映射结构化时间。
- 酒店坐标系未声明，坐标保留为原始证据，不转换成带假定坐标系的字段。
- 多成人、儿童定价、指定币种、坐标/typed ID 查询及多房间查询明确拒绝；不会静默忽略。
- 舱等、直飞、铁路席别和车次类型进行对应筛选；最多 10 个上游候选，筛选后为空不证明市场无此方案。
- 酒店即便使用默认的单成人单房间输入，也没有确认入住容量、房型或可订房价，输出会明确警告。

## Windows guard 与验证

官方 CLI 在本机 Windows Node 24 下完成 fetch 后立即调用 `process.exit(0)`，曾导致 libuv assertion，即 JSON 已输出但进程非零终止。`flyai_cli_guard.cjs` 是项目自己的兼容层，保持原厂 bundle 未修改，也不修改网络请求。

guard 用专用 sentinel 在相同位置结束成功分支，让 Node 自然收尾；非零退出保持失败，其他异常也不会被当作成功。适配器不会忽略非零退出码。新 CLI 或 Node 版本需要重新验证这个兼容措施。

专用测试全部离线；仅其中三项运行本地 Node 假 CLI 来检查成功退出、非零退出和异常。真实响应 fixtures 已移除链接 query 与可能携带标识的路径。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_flyai.py -q
.\.venv\Scripts\ruff.exe check src/travel_tools/providers/flyai.py tests/test_flyai.py
```

正式上线前仍需验证：正式 key 的权限与配额、准确价格和币种、税费口径、缓存/实时语义、库存、人数与房型支持、时区/坐标元数据，以及相应服务条款。当前适配器不会因配置 key 自动提高这些能力声明。
