# 机票来源扩展：分层验证记录

日期：2026-10-10（America/Chicago）。阶段 A 已独立交付（355923e/45c02d1）。
本轮新增 **search_flights(provider=csair)** 南航官网列表适配，原 FlyAI/东航保留。
跨航司携程目标尚未通过项目独立浏览器，不能宣布该目标已完成。

## 三种验证状态

| 来源 | 调研 UI | 项目独立 Chromium | 工具接入 |
| --- | --- | --- | --- |
| 携程 | 当次真实 CAN→上海列表成功，含 9C/AQ/HO 等跨航司候选 | 直接公开结果 URL 被访问保护拒绝，立即停止；未绕过、未使用 UI 登录态/请求凭据 | 未接入 |
| 南航 | CAN→上海成功，包含 SHA/PVG | 真实匿名列表成功，14 条航班，日期/单成人/实际机场证据可见 | 已接入并完成真实工具调用 |
| 国航 | PEK→SHA 的 CA1507 成功；UI 价格为 ¥600/成人、不含税 | 初次等待 25.1 秒无有效航班行；正常确认公开安全公告后仍显示“查询无结果”，不解释为全市场无航班 | 未接入 |
| 春秋 | CAN→上海 6 条真实候选成功 | HTTP 429 / provider_access_blocked，停止 | 未接入 |
| 去哪儿 | 桌面重定向营销首页；移动真实提交后显示空结果 | 本轮未继续独立复现，调研未证明可用来源 | 未接入 |

UI 完成记录为 2026-10-10 10:14:44 UTC（OTA）及同日航司研究文件。
独立南航/国航/春秋首轮 observed_at 均约 10:34:55 UTC；南航工具对照于
10:44:03–10:44:07 UTC 完成。携程失败调用未保存精确 observed_at/HTTP 具体码，
只有本轮日期、URL 和 provider_access_blocked 的执行证据，不补造精确时刻。
这些状态仅代表当次网络、城市、日期和网站环境，UI 成功不替代后端验收。

## 请求与实际观察

统一对照条件：2026-10-17、单程、1 成人、0 儿童/婴儿、经济舱、广州→上海城市范围，
nonstop_only=false、max_results=10、tax_view=included。tax_view 是请求条件，南航/FlyAI
列表不能核实该含税口径，返回保持 unknown，不能声称满足完整含税报价要求。

| 实际来源 | 新查询完整耗时 | 首个有效结果 | 来源调用 | 扫描/返回 | 展示价格数字 | 已核实金额/币种、税费、库存 |
| --- | --- | --- | --- | --- | --- | --- |
| FlyAI | 1.137 秒 | CLI 只交付完整响应，未单独观察首条 | 1 | 10/10 | 10/10 | 均 0/10；未知率 100% |
| 南航 | 3.768 秒 | 2.957 秒（有效可见航班行） | 1 | 14/10 | 10/10 | 均 0/10；未知率 100% |

两次新查询均成功，无查询失败；并不代表 100% 字段已知。南航保留实际 CAN/SHA/PVG、
航站楼、带时区时刻、所选舱等和精确展示数字。CZ3533 实测 07:00–09:20、白云 T2→
虹桥 T2、经济舱 ¥690。单成人页面条件明确，因此 per_person/priced_persons=1；
未把 ¥ 符号单独推定为已核实 ISO CNY，money=null。税费、最终库存和未出现的实际承运、
共享关系、直飞证据保持未知。UI 扩展舱位详情里的 V 舱/20KG 行李未被列表适配冒称已核实。

随后每种来源各一次同条件复用约 0.0006/0.0005 秒，reuse=cache、来源调用 0，保留
原 queried_at、coverage.data_time 和首条/完整取数时间。缓存提速与新查询耗时分开。
不同来源覆盖不同，不以其耗时比值宣称同覆盖的数据速度优势。

## URL 与覆盖范围

- [携程实际结果入口](https://flights.ctrip.com/online/list/oneway-can-sha?depdate=2026-10-17&cabin=y&adult=1&child=0&infant=0)
- [南航实际结果入口](https://b2c.csair.com/B2C40/newTrips/static/main/page/booking/index.html?t=S&c1=CAN&c2=SHA&d1=2026-10-17&at=1&ct=0&it=0&b1=CAN&b2=SHA-PVG&orderChannel=JPSS-YDXC)
- [国航实际结果入口](https://www.airchina.com.cn/flight/oneway/pek-sha/2026-10-17)
- [春秋实际结果入口](https://flights.ch.com/CAN-SHA.html?Departure=%E5%B9%BF%E5%B7%9E&Arrival=%E4%B8%8A%E6%B5%B7&FDate=2026-10-17&DepartCityCode=CAN&ArriveCityCode=&IsSearchDepAirport=false&IsSearchArrAirport=false&isOnlyZf=false&ANum=1&CNum=0&INum=0&IfRet=false&SType=01&MType=0&IsNew=1)

南航按实际行的 data-dep/data-arr 与可见机场名核对，上海城市范围包含 SHA/PVG，精确 SHA
请求不返回 PVG。按当前可见航班表读取并等待初始列表稳定，未宣称穷尽延迟滚动或市场；
complete=false，coverage 清楚列扫描、匹配和返回数。没有航班行而只有低价日历不当作成功，
所有必需字段损坏报解析失败，部分缺失/错日期/错机场逐项筛除。票少不当作数字余票，隐藏
模板不当作票价或售罄。请求 nonstop_only=true 时缺少直飞证据的候选不通过筛选。

默认南航并发 1、每秒启动 1 次，复用阶段 A 总工具/浏览器容量、20 秒报价缓存、强制刷新
和完整效果/历史恢复机制。来源能力由工具 provider enum/description 表达；提示词只要求
模型按可用能力自主选择来源，不固定要求东航或南航，也不每次强制查所有来源。

## 复现与后续条件

`python scripts/live_probe_flight_sources.py --live` 对同条件分别执行新查询和复用；
可通过 --origin/--destination/--date/--providers 改为其他样本。会消耗已配置供应商额度，
原始结果和元数据只保存于忽略的 artifacts/flight-sources-live-*。
test_csair.py 使用合成映射数据及本地 HTML 验证日期/机场/舱等、遮罩价、隐藏模板和未知字段，
不拿 fixture 作为真实来源验收。

新增来源单独 9 项测试通过，票务/报价/Agent 相关回归 128 项通过；本阶段文件 Ruff check 与
format 通过。真实 DeepSeek Flash/high 在隔离库自主选 csair 查询指定 CZ3533，两次模型
请求、9.12 秒完成；公开答复保留 ¥690 展示数字，并明确税费、ISO币种、库存和承运未知。
此样本不改产品模型/预算，不侵入原用户会话。

最终扩展回归（票务/报价、legacy/图运行、查询并行/复用）157 项通过。
实现提交 954abfd；本地后端已加载该提交的源码快照
private/releases/954abfd/src（该进程 PYTHONPATH 指向此处），保留工作区其他聊天尚未提交的
改动。以后正常从仓库根目录重新启动服务可读取新的工作区实现，无 .env 永久路径改写。
重启前确认 0 个运行中会话，应用库/检查点库备份于
private/backups/flight-source-20261010T105925Z；当时全部10个会话保留。
正式 HTTP 工具冒烟返回 csair/CZ3533 成功，provider enum 为 flyai/ceair/csair，前端与代理均200。

携程跨航司覆盖仍需有可访问的正式公开渠道或供应商授权接口后另验；本轮未申请账号、
购买服务、提取 cookie/token/签名或绕过访问保护。国航/春秋保留失败事实，当前不暴露为
可用工具来源。有限来源无结果不能推论某路线没有航班/无票，也不提供出票或最终库存保证。
