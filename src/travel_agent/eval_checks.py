"""Deterministic hidden checks backed by sandbox world facts.

Checks read structured observations only: saved itinerary versions, tool trace,
transcript status, preferences, and world data. Final prose is used only to extract
service numbers and hotel names for grounding; no check scores wording, question
counts or a fixed tool order. Without a structured itinerary, itinerary checks are
`unknown`, never guessed from prose.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from travel_agent.eval_world.model import World, WorldClock, WorldPlace
from travel_tools.common import StrictModel

Status = Literal["passed", "failed", "unknown", "not_applicable"]
Metric = Literal[
    "task_completion", "constraint_preservation", "evidence_support", "clarification_relevance"
]
TRANSPORT_TOOLS = {"search_trains", "search_coaches", "search_flights"}
SERVICE_PATTERN = re.compile(r"(?<![A-Za-z0-9])([GDCZTKYL]\d{1,4})(?!\d)")
UNAVAILABLE = {"无", "候补", "未起售", "售罄", "--"}


class _Check(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    note: str = Field(min_length=1)
    trap: bool = False
    metric: Metric | None = None


class Delivered(_Check):
    kind: Literal["delivered"]
    require_itinerary: bool = False


class NoVisitWhenClosed(_Check):
    kind: Literal["no_visit_when_closed"]
    place_id: str


class WithinOpeningHours(_Check):
    kind: Literal["within_opening_hours"]
    place_ids: list[str] | None = None


class KnownCostWithinBudget(_Check):
    kind: Literal["known_cost_within_budget"]
    amount: Decimal
    currency: str = "CNY"


class NoUnavailableService(_Check):
    kind: Literal["no_unavailable_service"]


class FactsGrounded(_Check):
    kind: Literal["facts_grounded"]


class NoOutdoorDuringRain(_Check):
    kind: Literal["no_outdoor_during_rain"]
    city: str
    day: int


class ToolNotCalled(_Check):
    kind: Literal["tool_not_called"]
    tool: str


class PreferencesUnchanged(_Check):
    kind: Literal["preferences_unchanged"]


class ProtectedItemPreserved(_Check):
    kind: Literal["protected_item_preserved"]
    text: str = Field(min_length=1, description="Text identifying the item, e.g. a train number.")


class UnrelatedDaysPreserved(_Check):
    kind: Literal["unrelated_days_preserved"]
    changed_day: int


class DayChanged(_Check):
    kind: Literal["day_changed"]
    day: int


class ItemsWithinWindow(_Check):
    kind: Literal["items_within_window"]
    start_day: int
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_day: int
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")


class LodgingCovers(_Check):
    kind: Literal["lodging_covers"]
    nights: list[int] = Field(min_length=1)


class NoItinerarySaved(_Check):
    kind: Literal["no_itinerary_saved"]


class IndoorDuring(_Check):
    kind: Literal["indoor_during"]
    day: int
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")


class LodgingNot(_Check):
    kind: Literal["lodging_not"]
    hotel_ids: list[str] = Field(min_length=1)


HiddenCheck = Annotated[
    Delivered
    | NoVisitWhenClosed
    | WithinOpeningHours
    | KnownCostWithinBudget
    | NoUnavailableService
    | FactsGrounded
    | NoOutdoorDuringRain
    | ToolNotCalled
    | PreferencesUnchanged
    | ProtectedItemPreserved
    | UnrelatedDaysPreserved
    | DayChanged
    | ItemsWithinWindow
    | LodgingCovers
    | NoItinerarySaved
    | IndoorDuring
    | LodgingNot,
    Field(discriminator="kind"),
]

DEFAULT_METRIC: dict[str, Metric] = {
    "delivered": "task_completion",
    "no_visit_when_closed": "evidence_support",
    "within_opening_hours": "evidence_support",
    "known_cost_within_budget": "constraint_preservation",
    "no_unavailable_service": "evidence_support",
    "facts_grounded": "evidence_support",
    "no_outdoor_during_rain": "constraint_preservation",
    "tool_not_called": "constraint_preservation",
    "preferences_unchanged": "constraint_preservation",
    "protected_item_preserved": "constraint_preservation",
    "unrelated_days_preserved": "constraint_preservation",
    "day_changed": "task_completion",
    "items_within_window": "constraint_preservation",
    "lodging_covers": "task_completion",
    "no_itinerary_saved": "constraint_preservation",
    "indoor_during": "constraint_preservation",
    "lodging_not": "constraint_preservation",
}


def _norm(text: str | None) -> str:
    return re.sub(r"[\s·（）()【】\[\]，,。.、/-]", "", text or "").lower()


def _moment(value: str, clock: WorldClock) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(clock.zone)


class CheckContext:
    """Observations reduced to the structures the checks read."""

    def __init__(self, world: World, clock: WorldClock, observed: dict):
        self.world, self.clock, self.observed = world, clock, observed
        conversation = observed.get("conversation") or {}
        self.status = conversation.get("status")
        transcript = conversation.get("transcript") or []
        answers = [
            e for e in transcript if e.get("role") == "assistant" and e.get("kind") == "answer"
        ]
        self.final_answer = answers[-1]["content"] if answers else None
        self.ended_with_answer = bool(transcript) and transcript[-1].get("kind") == "answer"
        self.user_text = "\n".join(
            e.get("content") or "" for e in transcript if e.get("role") == "user"
        )
        self.versions = [doc for doc in observed.get("itinerary_documents") or [] if doc]
        current = conversation.get("itinerary") or {}
        self.document = current.get("document") or (self.versions[-1] if self.versions else None)
        self.tools = observed.get("tools") or []
        self.tool_corpus = json.dumps(
            [
                t["result"].get("data")
                for t in self.tools
                if t.get("result", {}).get("status") == "ok"
            ],
            ensure_ascii=False,
        )

    # ---------- itinerary helpers ----------

    @staticmethod
    def item_text(item: dict) -> str:
        return _norm((item.get("title") or "") + (item.get("description") or ""))

    def places_for(self, item: dict) -> list[WorldPlace]:
        text = self.item_text(item)
        ids = {item.get("start_place_id"), item.get("end_place_id")}
        matched = []
        for place in self.world.places:
            if place.id in ids or any(
                len(_norm(name)) >= 2 and _norm(name) in text for name in place.names()
            ):
                matched.append(place)
        return matched

    def item_day(self, item: dict) -> int:
        return self.clock.offset(_moment(item["start"], self.clock).date())

    def open_windows(self, place: WorldPlace, day: int) -> list[tuple[datetime, datetime, str]]:
        """Regular windows minus temporary closures; empty means closed."""
        if place.hours is None:
            return []
        if any(closure.day == day for closure in place.closures):
            return []
        weekday = self.clock.date(day).weekday()
        return [
            (
                self.clock.at(day, window.open),
                self.clock.at(day, window.close),
                window.last_entry,
            )
            for window in place.hours
            if weekday in window.weekdays
        ]


def _result(check, status: Status, reason: str | None = None, **detail) -> dict:
    return {
        "id": check.id,
        "kind": check.kind,
        "metric": check.metric or DEFAULT_METRIC[check.kind],
        "trap": check.trap,
        "note": check.note,
        "oracle_scope": "structured_requirement",
        "status": status,
        **({"reason": reason} if reason else {}),
        **({"detail": detail} if detail else {}),
    }


def _activity_items(context: CheckContext) -> list[dict]:
    return [
        item
        for item in (context.document or {}).get("items", [])
        if item.get("kind", "activity") in ("activity", "meal")
    ]


def check_delivered(check: Delivered, context: CheckContext):
    if context.status != "completed" or not context.ended_with_answer:
        return _result(check, "failed", "no_final_answer", status=context.status)
    if check.require_itinerary and not context.document:
        return _result(check, "failed", "no_saved_itinerary")
    return _result(check, "passed")


def check_closed(check: NoVisitWhenClosed, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    place = context.world.place(check.place_id)
    violations = []
    for item in _activity_items(context):
        if place in context.places_for(item):
            day = context.item_day(item)
            if place.hours is not None and not context.open_windows(place, day):
                violations.append({"item_id": item.get("id"), "day": day})
    return _result(check, "failed" if violations else "passed", violations=violations)


def check_opening(check: WithinOpeningHours, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    violations, checked = [], 0
    for item in _activity_items(context):
        for place in context.places_for(item):
            if place.hours is None or (check.place_ids and place.id not in check.place_ids):
                continue
            checked += 1
            start, end = _moment(item["start"], context.clock), _moment(item["end"], context.clock)
            day = context.item_day(item)
            fits = False
            for opens, closes, last_entry in context.open_windows(place, day):
                latest_start = context.clock.at(day, last_entry) if last_entry else closes
                if opens <= start <= latest_start and end <= closes:
                    fits = True
            if not fits:
                violations.append({"item_id": item.get("id"), "place_id": place.id, "day": day})
    if not checked:
        return _result(check, "not_applicable", "no_items_with_known_hours")
    return _result(check, "failed" if violations else "passed", violations=violations)


def check_budget(check: KnownCostWithinBudget, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    known, unknown, other_currency = Decimal(0), [], []
    for cost in context.document.get("costs", []):
        total = cost.get("total")
        if total is None:
            unknown.append(cost.get("id"))
        elif total.get("currency") != check.currency:
            other_currency.append(cost.get("id"))
        else:
            known += Decimal(str(total["amount"]))
    detail = {
        "known_total": str(known),
        "unknown_cost_ids": unknown,
        "other_currency_cost_ids": other_currency,
    }
    if known > check.amount:
        return _result(check, "failed", "known_costs_exceed_budget", **detail)
    if not context.document.get("costs"):
        return _result(check, "unknown", "no_costs_recorded", **detail)
    return _result(check, "passed", **detail)


def _service_statuses(context: CheckContext, number: str, day: int) -> list[str] | None:
    for train in context.world.trains:
        if train.number == number and (train.days is None or day in train.days):
            return [seat.status for seat in train.overrides.get(day, train.seats)]
    for coach in context.world.coaches:
        if coach.number == number and (coach.days is None or day in coach.days):
            return [coach.overrides.get(day, coach.status)]
    for flight in context.world.flights:
        if flight.number == number and (flight.days is None or day in flight.days):
            return [flight.status]
    return None


def check_unavailable(check: NoUnavailableService, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    violations, checked = [], 0
    known_numbers = {
        s.number for s in [*context.world.trains, *context.world.coaches, *context.world.flights]
    }
    for item in context.document.get("items", []):
        if item.get("kind") != "transport" or item.get("protection"):
            continue  # User-reported bookings are the user's facts.
        # The scheduled service is named in the title; descriptions often mention
        # rejected alternatives ("G7311 only waitlist"), so they count only when they
        # name exactly one service and the title names none.
        numbers = {n for n in known_numbers if n in (item.get("title") or "")}
        if not numbers:
            described = {n for n in known_numbers if n in (item.get("description") or "")}
            numbers = described if len(described) == 1 else set()
        for number in numbers:
            checked += 1
            statuses = _service_statuses(context, number, context.item_day(item))
            if statuses is not None and all(
                status in UNAVAILABLE or status == "0" for status in statuses
            ):
                violations.append({"item_id": item.get("id"), "service": number})
    for stay in context.document.get("lodging", []):
        if stay.get("protection"):
            continue
        label = _norm((stay.get("title") or "") + (stay.get("hotel_id") or ""))
        for hotel in context.world.hotels:
            if _norm(hotel.name) in label or _norm(hotel.id) in label:
                checked += 1
                check_in = context.clock.offset(datetime.fromisoformat(stay["check_in"]).date())
                check_out = context.clock.offset(datetime.fromisoformat(stay["check_out"]).date())
                nights = range(check_in, check_out)
                if all(any(n in room.sold_out_nights for n in nights) for room in hotel.rooms):
                    violations.append({"lodging_id": stay.get("id"), "hotel": hotel.id})
    if not checked:
        return _result(check, "not_applicable", "no_world_services_in_itinerary")
    return _result(check, "failed" if violations else "passed", violations=violations)


def check_grounded(check: FactsGrounded, context: CheckContext):
    texts = [context.final_answer or ""]
    if context.document:
        for item in context.document.get("items", []):
            texts.append((item.get("title") or "") + " " + (item.get("description") or ""))
        for stay in context.document.get("lodging", []):
            texts.append(stay.get("title") or "")
    claimed = " ".join(texts)
    services = set(SERVICE_PATTERN.findall(claimed))
    services |= {c.number for c in context.world.coaches if c.number in claimed}
    hotels = {h.name for h in context.world.hotels if h.name in claimed}
    facts = [("service", s) for s in sorted(services)] + [("hotel", h) for h in sorted(hotels)]
    ungrounded = [
        {"type": kind, "value": value}
        for kind, value in facts
        if value not in context.tool_corpus and value not in context.user_text
    ]
    if not facts:
        return _result(check, "not_applicable", "no_checkable_facts")
    return _result(
        check,
        "failed" if ungrounded else "passed",
        facts=len(facts),
        ungrounded=ungrounded,
    )


def check_rain(check: NoOutdoorDuringRain, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    city = next((c for c in context.world.weather if c.city == check.city), None)
    weather = next((d for d in city.days if d.day == check.day), None) if city else None
    if not weather or not weather.rain_hours:
        return _result(check, "not_applicable", "no_rain_in_world")
    violations = []
    for item in _activity_items(context):
        if context.item_day(item) != check.day:
            continue
        start, end = _moment(item["start"], context.clock), _moment(item["end"], context.clock)
        for first, last in weather.rain_hours:
            rain_start = context.clock.at(check.day, f"{first:02d}:00")
            rain_end = context.clock.at(check.day, f"{last:02d}:00")
            if start < rain_end and end > rain_start:
                outdoor = [p.id for p in context.places_for(item) if p.indoor is False]
                if outdoor:
                    violations.append({"item_id": item.get("id"), "places": outdoor})
    return _result(check, "failed" if violations else "passed", violations=violations)


def check_tool_not_called(check: ToolNotCalled, context: CheckContext):
    calls = [t for t in context.tools if t.get("name") == check.tool]
    return _result(check, "failed" if calls else "passed", calls=len(calls))


def check_preferences(check: PreferencesUnchanged, context: CheckContext):
    before = (context.observed.get("initial_preferences_profile") or {}).get("entries")
    after = (context.observed.get("preferences") or {}).get("entries")
    return _result(check, "passed" if before == after else "failed")


def _find_item(document: dict, text: str) -> dict | None:
    key = _norm(text)
    for item in document.get("items", []):
        if key in CheckContext.item_text(item):
            return item
    return None


def check_protected(check: ProtectedItemPreserved, context: CheckContext):
    if not context.versions:
        return _result(check, "unknown", "no_structured_itinerary")
    first = next((doc for doc in context.versions if _find_item(doc, check.text)), None)
    if first is None:
        return _result(check, "unknown", "protected_item_never_saved")
    before = _find_item(first, check.text)
    after = _find_item(context.versions[-1], check.text)
    if after is None:
        return _result(check, "failed", "protected_item_removed")
    same = (before["start"], before["end"]) == (after["start"], after["end"])
    return _result(
        check,
        "passed" if same else "failed",
        before=[before["start"], before["end"]],
        after=[after["start"], after["end"]],
    )


def _signature(item: dict) -> tuple:
    return (item.get("start"), item.get("end"), item.get("title"))


def check_unrelated(check: UnrelatedDaysPreserved, context: CheckContext):
    if len(context.versions) < 2:
        return _result(check, "unknown", "fewer_than_two_versions", versions=len(context.versions))
    first, last = context.versions[0], context.versions[-1]
    latest = Counter(_signature(item) for item in last.get("items", []))
    changed = []
    for item in first.get("items", []):
        if context.item_day(item) == check.changed_day:
            continue
        signature = _signature(item)
        if latest[signature]:
            latest[signature] -= 1
        else:
            changed.append(item.get("id"))
    total = sum(1 for item in first.get("items", []) if context.item_day(item) != check.changed_day)
    return _result(
        check,
        "failed" if changed else "passed",
        unrelated_items=total,
        changed_item_ids=changed,
    )


def check_day_changed(check: DayChanged, context: CheckContext):
    if len(context.versions) < 2:
        return _result(check, "failed", "no_new_version", versions=len(context.versions))
    first, last = context.versions[0], context.versions[-1]

    def day_items(doc):
        return sorted(
            _signature(item) for item in doc.get("items", []) if context.item_day(item) == check.day
        )

    return _result(check, "passed" if day_items(first) != day_items(last) else "failed")


def check_window(check: ItemsWithinWindow, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    start = context.clock.at(check.start_day, check.start_time)
    end = context.clock.at(check.end_day, check.end_time)
    outside = [
        item.get("id")
        for item in context.document.get("items", [])
        if _moment(item["start"], context.clock) < start
        or _moment(item["end"], context.clock) > end
    ]
    return _result(check, "failed" if outside else "passed", outside_item_ids=outside)


def check_lodging(check: LodgingCovers, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    covered = set()
    for stay in context.document.get("lodging", []):
        check_in = context.clock.offset(datetime.fromisoformat(stay["check_in"]).date())
        check_out = context.clock.offset(datetime.fromisoformat(stay["check_out"]).date())
        covered |= set(range(check_in, check_out))
    missing = [night for night in check.nights if night not in covered]
    return _result(check, "failed" if missing else "passed", missing_nights=missing)


def check_no_itinerary(check: NoItinerarySaved, context: CheckContext):
    return _result(check, "failed" if context.versions or context.document else "passed")


def check_indoor(check: IndoorDuring, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    start = context.clock.at(check.day, check.start_time)
    end = context.clock.at(check.day, check.end_time)
    overlapping = [
        item
        for item in _activity_items(context)
        if _moment(item["start"], context.clock) < end
        and _moment(item["end"], context.clock) > start
    ]
    unmatched, outdoor = [], []
    for item in overlapping:
        places = [p for p in context.places_for(item) if p.indoor is not None]
        if not places:
            unmatched.append(item.get("id"))
        elif any(p.indoor is False for p in places):
            outdoor.append(item.get("id"))
    if outdoor:
        return _result(check, "failed", outdoor_item_ids=outdoor, unmatched_item_ids=unmatched)
    if not overlapping:
        return _result(check, "failed", "no_activity_in_window")
    if unmatched:
        return _result(
            check, "unknown", "items_not_matched_to_world_places", unmatched_item_ids=unmatched
        )
    return _result(check, "passed")


def check_lodging_not(check: LodgingNot, context: CheckContext):
    if not context.document:
        return _result(check, "unknown", "no_structured_itinerary")
    hits = []
    for stay in context.document.get("lodging", []):
        label = _norm((stay.get("title") or "") + (stay.get("hotel_id") or ""))
        for hotel in context.world.hotels:
            if hotel.id in check.hotel_ids and (
                _norm(hotel.name) in label or _norm(hotel.id) in label
            ):
                hits.append(hotel.id)
    return _result(check, "failed" if hits else "passed", hotels=hits)


HANDLERS = {
    "indoor_during": check_indoor,
    "lodging_not": check_lodging_not,
    "delivered": check_delivered,
    "no_visit_when_closed": check_closed,
    "within_opening_hours": check_opening,
    "known_cost_within_budget": check_budget,
    "no_unavailable_service": check_unavailable,
    "facts_grounded": check_grounded,
    "no_outdoor_during_rain": check_rain,
    "tool_not_called": check_tool_not_called,
    "preferences_unchanged": check_preferences,
    "protected_item_preserved": check_protected,
    "unrelated_days_preserved": check_unrelated,
    "day_changed": check_day_changed,
    "items_within_window": check_window,
    "lodging_covers": check_lodging,
    "no_itinerary_saved": check_no_itinerary,
}


def run_hidden_checks(checks: list, world: World, clock: WorldClock, observed: dict) -> list[dict]:
    context = CheckContext(world, clock, observed)
    results = []
    for check in checks:
        try:
            results.append(HANDLERS[check.kind](check, context))
        except (KeyError, ValueError, TypeError, StopIteration) as exc:
            results.append(_result(check, "unknown", "check_error", error=type(exc).__name__))
    return results


def process_metrics(observed: dict) -> dict:
    """Counts from the actual trace; no target values or fixed-order expectations."""
    tools = observed.get("tools") or []
    by_name = Counter(t.get("name") for t in tools)
    errors = Counter(
        (t.get("result") or {}).get("error", {}).get("code")
        for t in tools
        if (t.get("result") or {}).get("status") == "error"
    )
    seen = Counter(
        (t.get("name"), json.dumps(t.get("arguments"), sort_keys=True, ensure_ascii=False))
        for t in tools
        if t.get("name") not in (None, "ask_user")
    )
    transcript = (observed.get("conversation") or {}).get("transcript") or []
    kinds = Counter(entry.get("kind") for entry in transcript if entry.get("role") == "assistant")
    return {
        "tool_calls": len(tools),
        "tool_calls_by_name": dict(sorted(by_name.items(), key=lambda kv: str(kv[0]))),
        "tool_errors_by_code": {str(k): v for k, v in errors.items()},
        "invalid_argument_calls": errors.get("invalid_arguments", 0),
        "duplicate_calls": sum(count - 1 for count in seen.values() if count > 1),
        "questions": kinds.get("question", 0),
        "answers": kinds.get("answer", 0),
        "model_requests": len(observed.get("model_requests") or []),
    }


def summarize(records: list[dict]) -> dict:
    """Aggregate across cases; unknown and not_applicable are reported, never folded in."""
    world_cases = [r for r in records if r.get("hidden_checks") is not None]
    ran = [
        r for r in world_cases if r.get("run_outcome") not in ("harness_error", "skipped_offline")
    ]
    checks = [c for r in ran for c in r["hidden_checks"]]
    decided = [c for c in checks if c["status"] in ("passed", "failed")]
    traps = [c for c in decided if c["trap"]]
    grounding = [c for c in checks if c["kind"] == "facts_grounded" and "detail" in c]

    def rate(items, predicate):
        return round(sum(1 for i in items if predicate(i)) / len(items), 4) if items else None

    elapsed = sorted(r["elapsed_seconds"] for r in ran if "elapsed_seconds" in r)

    def percentile(values, q):
        if not values:
            return None
        return round(values[min(len(values) - 1, int(q * len(values)))], 2)

    tokens = Counter()
    usage_complete = True
    for record in ran:
        totals = (record.get("usage") or {}).get("totals")
        if not totals or record["usage"].get("status") != "reported":
            usage_complete = False
            continue
        for key, value in totals.items():
            if isinstance(value, (int, float)):
                tokens[key] += value
    by_metric = {}
    for metric in sorted({c["metric"] for c in checks}):
        subset = [c for c in decided if c["metric"] == metric]
        by_metric[metric] = {
            "decided": len(subset),
            "pass_rate": rate(subset, lambda c: c["status"] == "passed"),
            "unknown": sum(1 for c in checks if c["metric"] == metric and c["status"] == "unknown"),
        }
    return {
        "world_cases": len(world_cases),
        "ran": len(ran),
        "skipped_offline": sum(1 for r in world_cases if r.get("run_outcome") == "skipped_offline"),
        "harness_errors": sum(1 for r in world_cases if r.get("run_outcome") == "harness_error"),
        "delivered_rate": rate(ran, lambda r: r.get("run_outcome") == "completed"),
        "strict_success_rate": rate(
            ran,
            lambda r: all(c["status"] in ("passed", "not_applicable") for c in r["hidden_checks"]),
        ),
        "no_failed_check_rate": rate(
            ran, lambda r: not any(c["status"] == "failed" for c in r["hidden_checks"])
        ),
        "check_pass_rate": rate(decided, lambda c: c["status"] == "passed"),
        "checks": {
            "total": len(checks),
            **{
                s: sum(1 for c in checks if c["status"] == s)
                for s in ("passed", "failed", "unknown", "not_applicable")
            },
        },
        "trap_pass_rate": rate(traps, lambda c: c["status"] == "passed"),
        "fabrication": {
            "facts": sum(c["detail"].get("facts", 0) for c in grounding),
            "ungrounded": sum(len(c["detail"].get("ungrounded", [])) for c in grounding),
        },
        "by_metric": by_metric,
        "process": {
            key: sum((r.get("process") or {}).get(key, 0) for r in ran)
            for key in (
                "tool_calls",
                "invalid_argument_calls",
                "duplicate_calls",
                "questions",
                "model_requests",
            )
        },
        "elapsed_seconds": {"p50": percentile(elapsed, 0.5), "p90": percentile(elapsed, 0.9)},
        "usage_totals": dict(tokens) if usage_complete and tokens else None,
        "usage_status": "reported" if usage_complete and tokens else "unknown_or_partial",
    }
