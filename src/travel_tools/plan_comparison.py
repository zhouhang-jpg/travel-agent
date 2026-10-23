"""Enumerate supplied candidates and derive a conservative, deterministic Pareto frontier."""

from collections import defaultdict
from datetime import UTC
from decimal import Decimal
from itertools import combinations

from travel_tools.itinerary import validate_itinerary
from travel_tools.schemas.itinerary import ValidateItineraryInput, arrangement_view
from travel_tools.schemas.plan_comparison import (
    ComparedOption,
    CompareItineraryOptionsInput,
    CompareItineraryOptionsOutput,
    ComparisonBasis,
    ComparisonCheck,
    ItineraryMetrics,
    ObjectiveDifference,
    OptionTradeoff,
    ParetoGroup,
)
from travel_tools.schemas.quotes import Money


def _minutes(item):
    return (item.end.astimezone(UTC) - item.start.astimezone(UTC)).total_seconds() / 60


def _common_plan(plan, basis, checks):
    """Common constraints prevail; candidate-specific commitments remain additional limits."""
    commitments = {entry.id: entry for entry in plan.fixed_commitments}
    for entry in basis.fixed_commitments:
        if entry.id in commitments and commitments[entry.id] != entry:
            checks.append(
                ComparisonCheck(
                    category="scope",
                    status="fail",
                    ids=[entry.id],
                    message="Candidate rewrites a common fixed commitment.",
                )
            )
        commitments[entry.id] = entry
    data = plan.model_dump()
    data.update(
        planning_window=basis.planning_window.model_dump(),
        fixed_commitments=[entry.model_dump() for entry in commitments.values()],
        fixed_commitments_complete=basis.fixed_commitments_complete,
        required_lodging_nights=(
            None
            if basis.required_lodging_nights is None
            else [entry.model_dump() for entry in basis.required_lodging_nights]
        ),
        required_cost_ids=basis.required_cost_ids,
        budget_scope=basis.budget_scope,
        budget=None if basis.budget is None else basis.budget.model_dump(),
    )
    return ValidateItineraryInput.model_validate(data)


def _basis_checks(plan, basis: ComparisonBasis):
    checks = []
    if not plan.items:
        checks.append(
            ComparisonCheck(
                category="scope",
                status="unknown",
                message="An empty schedule is not a complete candidate itinerary.",
            )
        )
    for transfer in plan.transfers:
        if transfer.buffers_complete is not True:
            checks.append(
                ComparisonCheck(
                    category="travel",
                    status="unknown",
                    ids=[transfer.from_item_id, transfer.to_item_id],
                    message="Applicable transfer buffers are not declared complete; "
                    "missing or explicitly incomplete allowances require review.",
                )
            )
    item_ids = {item.id for item in plan.items}
    lodging = {stay.id: stay for stay in plan.lodging}
    for cost in plan.costs:
        if cost.basis is None or cost.basis.travelers is None:
            status, message = "unknown", "Cost does not establish its entire-party traveler basis."
        elif cost.basis.travelers != basis.travelers:
            status, message = "fail", "Cost traveler count differs from the common party."
        else:
            status, message = "pass", "Cost is declared for the common traveler count."
        checks.append(
            ComparisonCheck(
                category="party_basis",
                status=status,
                message=message,
                ids=[cost.id],
            )
        )
        if set(cost.item_ids) - item_ids or set(cost.lodging_ids) - lodging.keys():
            checks.append(
                ComparisonCheck(
                    category="scope",
                    status="fail",
                    ids=[cost.id],
                    message="Cost refers to absent scheduled items or lodging.",
                )
            )
        if len(set(cost.item_ids)) != len(cost.item_ids) or len(set(cost.lodging_ids)) != len(
            cost.lodging_ids
        ):
            checks.append(
                ComparisonCheck(
                    category="scope",
                    status="fail",
                    ids=[cost.id],
                    message="Cost targets are duplicated; room/night coverage cannot be doubled.",
                )
            )
        if cost.lodging_ids:
            stays = [
                lodging[identifier] for identifier in cost.lodging_ids if identifier in lodging
            ]
            nights = sum((stay.check_out - stay.check_in).days for stay in stays)
            if (
                basis.rooms is None
                or cost.basis is None
                or (cost.basis.rooms is None or cost.basis.nights is None)
            ):
                status, message = "unknown", "Lodging room/night cost basis is not fully known."
            elif cost.basis.rooms != basis.rooms or cost.basis.nights != nights:
                status, message = (
                    "fail",
                    "Lodging cost differs from common rooms or actual stay nights.",
                )
            else:
                status, message = (
                    "pass",
                    "Lodging cost covers the common rooms and actual stay nights.",
                )
            checks.append(
                ComparisonCheck(
                    category="lodging_basis",
                    status=status,
                    message=message,
                    ids=[cost.id],
                )
            )
        elif cost.basis and (cost.basis.rooms is not None or cost.basis.nights is not None):
            checks.append(
                ComparisonCheck(
                    category="lodging_basis",
                    status="unknown",
                    ids=[cost.id],
                    message="A room/night-based cost is not linked to a lodging stay.",
                )
            )
    linked_stays = {identifier for cost in plan.costs for identifier in cost.lodging_ids}
    for stay_id in sorted(lodging.keys() - linked_stays):
        checks.append(
            ComparisonCheck(
                category="lodging_basis",
                status="unknown",
                ids=[stay_id],
                message="Planned lodging has no linked cost; missing expense is not assumed free.",
            )
        )
    for name, protected in (
        ("items", basis.protected_items),
        ("lodging", basis.protected_lodging),
    ):
        entries = {entry.id: entry for entry in getattr(plan, name)}
        for entry in protected:
            following = entries.get(entry.id)
            matches = following is not None and arrangement_view(
                name, following
            ) == arrangement_view(name, entry)
            checks.append(
                ComparisonCheck(
                    category="protection",
                    status="pass" if matches else "fail",
                    ids=[entry.id],
                    message="The protected arrangement is preserved exactly."
                    if matches
                    else "Protected arrangement was removed or changed without release authority.",
                )
            )
    if any(cost.kind == "reference" for cost in plan.costs):
        checks.append(
            ComparisonCheck(
                category="price",
                status="unknown",
                message="Reference prices remain estimates; exact entire-party cost is unknown.",
            )
        )
    return checks


def _travel_metrics(plan):
    if not plan.items:
        return None, None
    total = 0.0
    complete, estimated = True, False
    for item in plan.items:
        if item.kind != "transport" and item.travel_duration is None:
            continue
        duration = item.travel_duration
        if duration is None or duration.minutes is None or duration.basis == "unknown":
            complete = False
        else:
            total += duration.minutes
            estimated |= duration.basis != "queried"
    transfers = defaultdict(list)
    for transfer in plan.transfers:
        transfers[(transfer.from_item_id, transfer.to_item_id)].append(transfer)
    items = sorted(
        plan.items,
        key=lambda item: (
            item.start.astimezone(UTC),
            item.end.astimezone(UTC),
            item.id,
        ),
    )
    for previous, following in zip(items, items[1:], strict=False):
        evidence = transfers[(previous.id, following.id)]
        if not evidence:
            if previous.end_place_id is None or previous.end_place_id != following.start_place_id:
                complete = False
            continue
        values = {entry.minimum_minutes for entry in evidence}
        if (
            None in values
            or len(values) != 1
            or any(entry.duration_basis == "unknown" for entry in evidence)
        ):
            complete = False
        else:
            total += evidence[0].minimum_minutes
            estimated |= any(entry.duration_basis != "queried" for entry in evidence)
    return (
        total if complete and not estimated else None,
        total if complete and estimated else None,
    )


def _metrics(plan, basis, validation, checks):
    totals = {kind: defaultdict(Decimal) for kind in ("query_quote", "reference")}
    for cost in plan.costs:
        if cost.total is not None:
            totals[cost.kind][cost.total.currency] += cost.total.amount
    unknown_ids = [cost.id for cost in plan.costs if cost.total is None]
    charges = [
        cost.id
        for cost in plan.costs
        if (cost.tax_basis != "included" or cost.fee_basis != "included")
    ]
    complete = not any(
        check.status in {"fail", "unknown"}
        and check.category
        in {
            "cost_coverage",
            "party_basis",
            "lodging_basis",
            "scope",
            "price",
        }
        for check in [*validation.checks, *checks]
    )
    exact_total = None
    if complete and len(totals["query_quote"]) == 1:
        currency, amount = next(iter(totals["query_quote"].items()))
        exact_total = Money(currency=currency, amount=amount)
    elif complete and not plan.costs and basis.budget is not None:
        exact_total = Money(currency=basis.budget.currency, amount=0)
    travel, reference_travel = _travel_metrics(plan)
    return ItineraryMetrics(
        quoted_cost_totals=[
            Money(currency=currency, amount=amount)
            for currency, amount in sorted(totals["query_quote"].items())
        ],
        reference_cost_totals=[
            Money(currency=currency, amount=amount)
            for currency, amount in sorted(totals["reference"].items())
        ],
        unknown_cost_ids=unknown_ids,
        incomplete_charge_ids=charges,
        cost_scope_complete=complete,
        exact_total_cost=exact_total,
        travel_minutes=travel,
        reference_travel_minutes=reference_travel,
        scheduled_transport_minutes=sum(
            _minutes(item) for item in plan.items if item.kind == "transport"
        ),
        activity_minutes=sum(_minutes(item) for item in plan.items if item.kind == "activity"),
    )


def _values(option, objectives):
    values = {}
    for objective in objectives:
        if objective == "total_cost":
            cost = option.metrics.exact_total_cost
            if cost is None:
                return None
            values[objective] = (cost.amount, cost.currency)
        else:
            value = getattr(option.metrics, objective)
            if value is None:
                return None
            values[objective] = (Decimal(str(value)), "minutes")
    return values


async def compare_itinerary_options(
    request: CompareItineraryOptionsInput,
) -> CompareItineraryOptionsOutput:
    """Validate every option, compare exact derived metrics, and preserve unknowns."""
    options = []
    sources = {}
    for option in request.options:
        checks = []
        plan = _common_plan(option.itinerary, request.basis, checks)
        validation = await validate_itinerary(plan)
        checks.extend(_basis_checks(plan, request.basis))
        metrics = _metrics(plan, request.basis, validation, checks)
        if metrics.travel_minutes is None:
            checks.append(
                ComparisonCheck(
                    category="travel",
                    status="unknown",
                    message="Actual travel time is missing, contradictory, or estimated.",
                )
            )
        if "total_cost" in request.objectives and metrics.exact_total_cost is None:
            checks.append(
                ComparisonCheck(
                    category="price",
                    status="unknown",
                    message="A complete quoted total in one currency is unavailable for ranking.",
                )
            )
        statuses = {entry.status for entry in [*validation.checks, *checks]}
        status = (
            "conflict"
            if "fail" in statuses
            else ("needs_review" if "unknown" in statuses else "feasible")
        )
        result = ComparedOption(
            id=option.id,
            label=option.label,
            status=status,
            validation=validation,
            checks=checks,
            metrics=metrics,
        )
        result.pareto_eligible = (
            status == "feasible" and _values(result, request.objectives) is not None
        )
        options.append(result)
        evidence = [
            *validation.sources,
            *(
                source
                for item in plan.items
                if item.travel_duration
                for source in item.travel_duration.sources
            ),
        ]
        for source in evidence:
            sources[source.model_dump_json()] = source
    tradeoffs = []
    for left, right in combinations(options, 2):
        left_values, right_values = (
            _values(left, request.objectives),
            _values(right, request.objectives),
        )
        differences = []
        if "conflict" in {left.status, right.status}:
            relation, reason = "excluded", "A candidate has known hard conflicts."
        elif not left.pareto_eligible or not right.pareto_eligible:
            relation, reason = "incomparable", "Unknown evidence or incomplete selected metrics."
        elif any(left_values[key][1] != right_values[key][1] for key in request.objectives):
            relation, reason = "incomparable", "Metric currencies differ; no conversion is assumed."
        else:
            for objective in request.objectives:
                left_value, unit = left_values[objective]
                right_value, _ = right_values[objective]
                preferred = (
                    "equal"
                    if left_value == right_value
                    else (
                        "left"
                        if (
                            left_value > right_value
                            if objective == "activity_minutes"
                            else left_value < right_value
                        )
                        else "right"
                    )
                )
                differences.append(
                    ObjectiveDifference(
                        objective=objective,
                        left_value=left_value,
                        right_value=right_value,
                        unit=unit,
                        preferred=preferred,
                    )
                )
            preferences = {entry.preferred for entry in differences} - {"equal"}
            if preferences == {"left"}:
                relation, reason = (
                    "dominates",
                    "Left is no worse in every selected objective and better in one.",
                )
                right.dominated_by.append(left.id)
            elif preferences == {"right"}:
                relation, reason = (
                    "dominated",
                    "Right is no worse in every selected objective and better in one.",
                )
                left.dominated_by.append(right.id)
            elif not preferences:
                relation, reason = (
                    "equivalent",
                    "All selected metrics are equal; preserve both options.",
                )
            else:
                relation, reason = (
                    "tradeoff",
                    "Each candidate improves a different selected objective.",
                )
        tradeoffs.append(
            OptionTradeoff(
                left_id=left.id,
                right_id=right.id,
                relation=relation,
                differences=differences,
                reason=reason,
            )
        )
    grouped = defaultdict(list)
    for option in options:
        if option.pareto_eligible:
            values = _values(option, request.objectives)
            key = tuple((objective, values[objective][1]) for objective in request.objectives)
            grouped[key].append(option)
    pareto_groups = [
        ParetoGroup(
            metric_units=dict(key),
            option_ids=[option.id for option in group],
            pareto_option_ids=[option.id for option in group if not option.dominated_by],
        )
        for key, group in grouped.items()
    ]
    return CompareItineraryOptionsOutput(
        options=options,
        objectives=request.objectives,
        pareto_option_ids=[
            option.id for option in options if option.pareto_eligible and not option.dominated_by
        ],
        pareto_groups=pareto_groups,
        tradeoffs=tradeoffs,
        sources=list(sources.values()),
        warnings=[
            "No exchange rates, missing prices, hidden fees, supplier inventory, or global market "
            "coverage are assumed. Supplied source authenticity/freshness are not verified.",
            "Activity minutes are scheduled time, not subjective quality. Separate currency "
            "groups cannot establish dominance over each other.",
        ],
    )
