"""Explicit supplier selection and a disclosed, configurable railway fallback."""

from travel_tools.common import ToolFailure

FALLBACK_ERRORS = {
    "provider_unavailable",
    "provider_access_blocked",
    "provider_browser_error",
    "provider_timeout",
    "provider_catalog_unavailable",
    "provider_http_error",
}


class TrainRouter:
    def __init__(self, adapters: dict, primary: str, fallback: str = "none"):
        self.adapters, self.primary, self.fallback = adapters, primary, fallback
        self.description = (
            f"查询铁路车票。默认来源{primary}；可用来源{','.join(adapters)}。"
            "provider可显式选12306/flyai/juhe；station_scope=exact核对准确站名，只有城市时可选city。"
            "direct_only按直达要求筛选，train_numbers可指定车次；12306返回各席别价格/余票，有不是数字，候补不是有票。"
            f"仅configured来源受阻时可降级到{fallback}，结果明确标注降级原因；显式来源不自动切换。"
            "FlyAI最多10项候选、无分页，币种/库存缺失保持未知；展示金额不等于可买到的完整报价。"
        )

    async def search(self, request):
        selected = self.primary if request.provider == "configured" else request.provider
        try:
            adapter = self.adapters.get(selected)
            if adapter is None:
                raise ToolFailure(
                    "provider_unavailable", "Selected railway source is not configured."
                )
            return await adapter.search(request)
        except ToolFailure as error:
            fallback = self.adapters.get(self.fallback)
            if (
                request.provider != "configured"
                or selected == self.fallback
                or fallback is None
                or error.code not in FALLBACK_ERRORS
            ):
                raise
            result = await fallback.search(request)
            result = result.model_copy(deep=True)
            result.warnings.append(
                f"Railway source {selected} failed ({error.code}); "
                f"explicitly configured fallback {self.fallback} used. "
                "These candidates have not been officially verified; inventory may be unknown."
            )
            if result.coverage:
                result.coverage.fallback_from = selected
                result.coverage.fallback_reason = error.code
            return result


class FlightRouter:
    def __init__(self, adapters: dict):
        self.adapters = adapters
        self.description = (
            f"查询机票，可用来源{','.join(adapters)}。provider=flyai快速查候选，单成人，最多10项无分页；"
            "provider=ceair可选核实东航官网列表，支持城市或provider_location_id中的IATA集合如SHA,PVG。"
            "flight_numbers按具体航班筛选；tax_view选included/excluded。"
            "官网非全市场覆盖，只核实经济/超级经济舱的展示价，不将税前/含税价直接比较。"
            "provider=csair查南航国内单成人列表，默认经济舱；可见舱等匹配，城市机场集合保留。"
            "南航列表税费/ISO币种和直飞证据可能未知，不能声称已满足含税价或直飞核实。"
            "按需求自主选已配置来源，不强制查所有网站；有限来源无结果不等于全市场无航班。"
            "营销航司不等于实际承运人，行李/退改/最终库存未知时保留；不预订。"
        )

    async def search(self, request):
        adapter = self.adapters.get(request.provider)
        if adapter is None:
            raise ToolFailure("provider_unavailable", "Selected flight source is not configured.")
        return await adapter.search(request)
