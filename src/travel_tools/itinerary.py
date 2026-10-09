"""Deterministic validation of a proposed itinerary and its supplied evidence."""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from travel_tools.schemas.itinerary import (
    CheckStatus,
    ItineraryCheck,
    OpeningEvidence,
    ScheduledItem,
    ValidateItineraryInput,
    ValidateItineraryOutput,
)
from travel_tools.schemas.quotes import Money


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _opening_result(item: ScheduledItem, evidence: OpeningEvidence) -> CheckStatus:
    if evidence.state == "unknown":
        return "unknown"
    if evidence.state == "closed":
        return "fail"
    # Touching/overlapping opening windows jointly cover continuous visits.
    merged: list[tuple[datetime, datetime]] = []
    for window in sorted(evidence.windows, key=lambda window: _utc(window.start)):
        start, end = _utc(window.start), _utc(window.end)
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    if any(start <= _utc(item.start) and _utc(item.end) <= end for start, end in merged):
        return "pass"
    return "fail"


async def validate_itinerary(request: ValidateItineraryInput) -> ValidateItineraryOutput:
    """Check consistency without fetching facts, planning, or inventing evidence."""
    checks: list[ItineraryCheck] = []

    def add(category: str, status: CheckStatus, message: str, *ids: str) -> None:
        checks.append(
            ItineraryCheck(category=category, status=status, message=message, item_ids=list(ids))
        )

    items = sorted(request.items, key=lambda item: (_utc(item.start), _utc(item.end), item.id))
    if not items:
        add("time_bounds", "unknown", "No scheduled items were supplied.")
        add("overlaps", "unknown", "An empty schedule cannot establish a valid itinerary.")
    else:
        outside = [
            item
            for item in items
            if (
                _utc(item.start) < _utc(request.planning_window.start)
                or _utc(item.end) > _utc(request.planning_window.end)
            )
        ]
        for item in outside:
            add("time_bounds", "fail", "Item falls outside the planning window.", item.id)
        if not outside:
            add("time_bounds", "pass", "All items fall within the supplied planning window.")
        overlaps = False
        for index, left in enumerate(items):
            for right in items[index + 1 :]:
                if _utc(right.start) >= _utc(left.end):
                    break
                overlaps = True
                add("overlaps", "fail", "Scheduled intervals overlap.", left.id, right.id)
        if not overlaps:
            add("overlaps", "pass", "Scheduled intervals do not overlap.")

    transfer_map = defaultdict(list)
    for evidence in request.transfers:
        transfer_map[(evidence.from_item_id, evidence.to_item_id)].append(evidence)
    if len(items) < 2:
        add(
            "transfers",
            "not_applicable" if items else "unknown",
            "There are fewer than two scheduled items to connect.",
        )
    for previous, following in zip(items, items[1:], strict=False):
        ids = (previous.id, following.id)
        evidence = transfer_map.get(ids, [])
        durations = {entry.minimum_minutes for entry in evidence}
        available = (_utc(following.start) - _utc(previous.end)).total_seconds() / 60
        if available < 0:
            add("transfers", "fail", "No transition time exists because items overlap.", *ids)
        elif evidence and (None in durations or len(durations) != 1):
            add(
                "transfers",
                "unknown",
                "Transfer duration evidence is missing or inconsistent.",
                *ids,
            )
        elif evidence:
            required = evidence[0].minimum_minutes
            if required is not None and available >= required:
                add("transfers", "pass", "The gap meets the supplied minimum transfer time.", *ids)
            else:
                add(
                    "transfers", "fail", "The gap is shorter than the supplied transfer time.", *ids
                )
        elif previous.end_place_id is not None and (
            previous.end_place_id == following.start_place_id
        ):
            add("transfers", "pass", "The transition stays at the same explicit place ID.", *ids)
        else:
            add("transfers", "unknown", "No transfer duration evidence connects these items.", *ids)

    opening_map = defaultdict(list)
    for evidence in request.opening_hours:
        opening_map[evidence.item_id].append(evidence)
    if not items:
        add("opening_hours", "unknown", "No scheduled visits were supplied.")
    for item in items:
        evidence = opening_map.get(item.id, [])
        if not evidence:
            if item.opening_hours_required is False:
                add(
                    "opening_hours",
                    "not_applicable",
                    "Opening hours explicitly do not apply.",
                    item.id,
                )
            else:
                add(
                    "opening_hours", "unknown", "Applicable opening windows are not known.", item.id
                )
            continue
        statuses = {_opening_result(item, entry) for entry in evidence}
        if len(statuses) != 1 or "unknown" in statuses:
            add(
                "opening_hours",
                "unknown",
                "Opening evidence is incomplete or contradictory.",
                item.id,
            )
        elif "fail" in statuses:
            add(
                "opening_hours",
                "fail",
                "The full visit is outside the supplied opening windows.",
                item.id,
            )
        else:
            add(
                "opening_hours",
                "pass",
                "The full visit fits the supplied opening windows.",
                item.id,
            )

    if not request.fixed_commitments_complete:
        add("fixed_commitments", "unknown", "The fixed-commitment list is not declared complete.")
    elif not request.fixed_commitments:
        add("fixed_commitments", "not_applicable", "No fixed commitments are required.")
    for commitment in request.fixed_commitments:
        candidates = [item for item in items if commitment.id in item.commitment_ids]
        covering = [
            item
            for item in candidates
            if (
                _utc(item.start) <= _utc(commitment.start)
                and _utc(item.end) >= _utc(commitment.end)
            )
        ]
        if not covering:
            add(
                "fixed_commitments",
                "fail",
                "No linked item covers the entire fixed commitment.",
                commitment.id,
            )
        elif commitment.place_id is None:
            add("fixed_commitments", "pass", "The fixed time is fully reserved.", commitment.id)
        elif any(
            item.start_place_id == item.end_place_id == commitment.place_id for item in covering
        ):
            add(
                "fixed_commitments", "pass", "The fixed time and place are reserved.", commitment.id
            )
        elif any(item.start_place_id is None or item.end_place_id is None for item in covering):
            add(
                "fixed_commitments",
                "unknown",
                "The fixed time is reserved but place is unknown.",
                commitment.id,
            )
        else:
            add(
                "fixed_commitments",
                "fail",
                "The linked item does not stay at the fixed place.",
                commitment.id,
            )

    if request.required_lodging_nights is None:
        add("lodging", "unknown", "Required lodging nights were not specified.")
    elif not request.required_lodging_nights:
        add("lodging", "not_applicable", "The request explicitly requires no lodging nights.")
    else:
        for need in request.required_lodging_nights:
            stays = [
                stay for stay in request.lodging if stay.check_in <= need.night < stay.check_out
            ]
            if not stays:
                add("lodging", "fail", f"No lodging covers the night of {need.night}.")
            elif len(stays) > 1:
                add(
                    "lodging",
                    "fail",
                    f"Multiple planned stays cover {need.night}.",
                    *(stay.id for stay in stays),
                )
            elif need.destination_id is not None and stays[0].destination_id is None:
                add(
                    "lodging",
                    "unknown",
                    "Lodging covers the date but its destination is unknown.",
                    stays[0].id,
                )
            elif need.destination_id is not None and need.destination_id != stays[0].destination_id:
                add(
                    "lodging",
                    "fail",
                    "Lodging is in a different required destination.",
                    stays[0].id,
                )
            else:
                add(
                    "lodging",
                    "pass",
                    f"A planned stay covers the night of {need.night}.",
                    stays[0].id,
                )

    cost_map = {cost.id: cost for cost in request.costs}
    totals: dict[str, Decimal] = defaultdict(Decimal)
    quoted_totals: dict[str, Decimal] = defaultdict(Decimal)
    for cost in request.costs:
        if cost.total is not None:
            totals[cost.total.currency] += cost.total.amount
            if cost.kind == "query_quote":
                quoted_totals[cost.total.currency] += cost.total.amount

    missing_ids = (
        []
        if request.required_cost_ids is None
        else [cost_id for cost_id in request.required_cost_ids if cost_id not in cost_map]
    )
    unknown_costs = [cost.id for cost in request.costs if cost.total is None]
    incomplete_charges = [
        cost.id
        for cost in request.costs
        if (cost.tax_basis != "included" or cost.fee_basis != "included")
    ]
    cost_scope_known = request.required_cost_ids is not None
    if not cost_scope_known or missing_ids or unknown_costs or incomplete_charges:
        add(
            "cost_coverage",
            "unknown",
            "Required-cost coverage, amounts, or included taxes/fees are incomplete.",
            *missing_ids,
            *unknown_costs,
            *incomplete_charges,
        )
    else:
        add("cost_coverage", "pass", "All declared required costs include amounts, taxes and fees.")

    if request.budget_scope == "unlimited":
        add("budget", "not_applicable", "No budget cap is requested.")
    elif request.budget is None:
        add("budget", "unknown", "No budget cap or explicit unlimited budget was supplied.")
    else:
        budget = request.budget
        known_subtotal = totals.get(budget.currency, Decimal(0))
        quote_subtotal = quoted_totals.get(budget.currency, Decimal(0))
        currencies = set(totals)
        references = any(cost.kind == "reference" for cost in request.costs)
        complete = (
            cost_scope_known
            and not missing_ids
            and not unknown_costs
            and not incomplete_charges
            and not references
        )
        if quote_subtotal > budget.amount:
            add(
                "budget",
                "fail",
                "Known quoted costs in the budget currency already exceed the cap.",
            )
        elif currencies - {budget.currency}:
            add(
                "budget",
                "unknown",
                "Costs use other currencies; no currency conversion is assumed.",
            )
        elif not complete:
            message = "Cost coverage, exact quotations, or included charges are not fully known."
            if known_subtotal > budget.amount:
                message += " The available estimate is already above the cap."
            add("budget", "unknown", message)
        else:
            add(
                "budget",
                "pass",
                "Complete supplied quotations fit the budget at their source times.",
            )

    statuses = {check.status for check in checks}
    result_status = (
        "invalid" if "fail" in statuses else "unknown" if "unknown" in statuses else "valid"
    )
    unique_sources = {}
    for evidence in [*request.transfers, *request.opening_hours, *request.costs]:
        for source in evidence.sources:
            unique_sources[source.model_dump_json()] = source
    return ValidateItineraryOutput(
        status=result_status,
        checks=checks,
        known_cost_totals=[
            Money(currency=currency, amount=amount) for currency, amount in sorted(totals.items())
        ],
        sources=list(unique_sources.values()),
        warnings=[
            "Only supplied evidence is checked; freshness and supplier availability "
            "are not verified."
        ],
    )
