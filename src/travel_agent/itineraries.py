"""Versioned planning facts, with user provenance and read-only evidence checks."""

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from sqlalchemy import JSON, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from travel_agent.itinerary_schema import ItineraryDocument, SaveItineraryInput
from travel_agent.storage import Base
from travel_agent.tool_encoding import decode_tool_result
from travel_tools.common import Source, utc_now
from travel_tools.itinerary import validate_itinerary
from travel_tools.schemas.itinerary import LodgingStay, ScheduledItem
from travel_tools.schemas.quotes import PriceEvidence


class ItineraryHead(Base):
    __tablename__ = "itinerary_heads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    current_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class ItineraryVersion(Base):
    __tablename__ = "itinerary_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    effect_id: Mapped[str] = mapped_column(String(200), unique=True)
    base_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    journal_seq: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[str] = mapped_column(String(40))
    document: Mapped[dict] = mapped_column(JSON)
    review: Mapped[dict] = mapped_column(JSON)
    diff: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String)


class PlanConflict(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or []


def public_version(row, *, internal=False):
    result = {
        "id": row.id,
        "base_version_id": row.base_id,
        "revision": row.revision,
        "created_at": row.created_at,
        "document": deepcopy(row.document),
        "review": deepcopy(row.review),
        "diff": deepcopy(row.diff),
        "change_reason": row.reason,
    }
    if internal:
        return result

    def readable(value):
        if isinstance(value, dict):
            return {k: readable(v) for k, v in value.items() if k != "query_conditions"}
        if isinstance(value, list):
            return [readable(v) for v in value]
        return value

    return readable(result)


def canonical_document(parsed, original):
    """Retain compact source_ids after the legacy validator resolves them once."""
    data = parsed.model_dump(mode="json")
    for section in ("transfers", "opening_hours", "costs"):
        for item, source in zip(data[section], original.get(section, []), strict=True):
            if source.get("source_ids"):
                item["source_ids"] = list(source["source_ids"])
                item["sources"] = [
                    Source.model_validate(s).model_dump(mode="json")
                    for s in source.get("sources", [])
                ]
            if section == "transfers":
                for buffer, raw in zip(item["buffers"], source.get("buffers", []), strict=True):
                    if raw.get("source_ids"):
                        buffer["source_ids"] = list(raw["source_ids"])
                        buffer["sources"] = [
                            Source.model_validate(s).model_dump(mode="json")
                            for s in raw.get("sources", [])
                        ]
    for item, raw in zip(data["items"], original.get("items", []), strict=True):
        duration = raw.get("travel_duration")
        if duration and duration.get("source_ids"):
            item["travel_duration"]["source_ids"] = list(duration["source_ids"])
            item["travel_duration"]["sources"] = [
                Source.model_validate(s).model_dump(mode="json")
                for s in duration.get("sources", [])
            ]
    return data


def apply_patch(document, patch):
    result = deepcopy(document)

    def merge(before, after):
        if isinstance(before, dict) and isinstance(after, dict):
            return {**deepcopy(before), **{k: merge(before.get(k), v) for k, v in after.items()}}
        return deepcopy(after)

    for section in ("items", "lodging", "costs", "requirements"):
        removed = set(getattr(patch, "remove_" + section))
        rows = {row["id"]: row for row in result[section] if row["id"] not in removed}
        edits = getattr(patch, section)
        if any(not isinstance(row.get("id"), str) for row in edits):
            raise PlanConflict("invalid_itinerary_patch", "局部修改条目必须使用稳定id。")
        if len({row["id"] for row in edits}) != len(edits):
            raise PlanConflict("invalid_itinerary_patch", "局部修改不能重复同一条目id。")
        for row in edits:
            rows[row["id"]] = merge(rows.get(row["id"], {}), row)
        result[section] = list(rows.values())
    allowed = {
        "title",
        "planning_window",
        "party",
        "budget",
        "budget_scope",
        "source_catalog",
        "evidence",
        "transfers",
        "opening_hours",
        "fixed_commitments",
        "fixed_commitments_complete",
        "required_lodging_nights",
        "required_cost_ids",
    }
    if set(patch.set_fields) - allowed:
        raise PlanConflict("invalid_itinerary_patch", "set_fields包含不支持的字段；条目按id修改。")
    for field, value in patch.set_fields.items():
        result[field] = merge(result.get(field), value)
    return result


def source_key(source):
    parsed = Source.model_validate(source)
    return (
        parsed.provider,
        parsed.url,
        parsed.retrieved_at.astimezone(UTC).isoformat(),
        parsed.data_time.astimezone(UTC).isoformat() if parsed.data_time else None,
    )


def available_sources(bank):
    catalog = {}
    for key, records in bank.items():
        identifier = "source_" + sha256(json.dumps(key).encode()).hexdigest()[:16]
        catalog[identifier] = {
            "source": deepcopy(records[0]["source"]),
            "query_conditions": records[0]["arguments"],
        }
    return catalog


def source_bank(history):
    """Find sources in actual decoded tool data and retain their actual query arguments."""
    bank, calls = {}, {}

    def prices(value):
        found = []
        if isinstance(value, dict):
            if "money" in value and "unit" in value and "kind" in value:
                try:
                    found.append(PriceEvidence.model_validate(value).model_dump(mode="json"))
                except ValidationError:
                    pass
            for child in value.values():
                found.extend(prices(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(prices(child))
        return found

    def walk(value, record):
        if isinstance(value, dict):
            if isinstance(value.get("provider"), str) and value.get("retrieved_at"):
                try:
                    fields = {k: v for k, v in value.items() if k in Source.model_fields}
                    bank.setdefault(source_key(fields), []).append({**record, "source": fields})
                except (ValidationError, ValueError, TypeError):
                    pass
            for child in value.values():
                walk(child, record)
        elif isinstance(value, list):
            for child in value:
                walk(child, record)

    for message in history:
        if message.get("role") == "assistant":
            calls = {c["id"]: c["function"] for c in message.get("tool_calls") or []}
        if message.get("role") == "tool":
            try:
                function = calls.get(message.get("tool_call_id"), {})
                if function.get("name") in {"save_itinerary", "ask_user", "validate_itinerary"}:
                    continue  # A model-authored plan is not new supplier evidence.
                result = decode_tool_result(message["content"])
                if result.get("status") == "ok":
                    walk(
                        result.get("data"),
                        {
                            "arguments": json.loads(function.get("arguments", "{}")),
                            "prices": prices(result.get("data")),
                            "route_minutes": [
                                r["duration_s"] / 60
                                for r in (result.get("data") or {}).get("routes", [])
                                if isinstance(r, dict) and r.get("duration_s") is not None
                            ],
                        },
                    )
            except (ValueError, KeyError, TypeError):
                continue
    return bank


def verify_user(reference, users, *, after=None):
    user = users.get(reference.message_id)
    if (
        not user
        or reference.quote not in user["content"]
        or not reference.quote.strip()
        or (after is not None and user["seq"] <= after)
    ):
        raise PlanConflict(
            "invalid_user_reference",
            "用户条件或锁定依据必须引用真实且适用的用户原话。",
            [
                {
                    "message_id": reference.message_id,
                    "message_exists": bool(user),
                    "quote_is_exact": bool(user and reference.quote in user["content"]),
                    "newer_reference_required": after is not None,
                    "reference_is_newer": bool(user and (after is None or user["seq"] > after)),
                }
            ],
        )


def protect_previous(document, previous, request, users):
    for requirement in document.requirements:
        if requirement.user_reference:
            verify_user(requirement.user_reference, users)
    for section in ("items", "lodging"):
        for item in getattr(document, section):
            if item.protection:
                verify_user(item.protection.user_reference, users)
    if not previous:
        return
    old = previous.document
    releases = {(r.section, r.id): r for r in request.release_protections}
    for release in releases.values():
        verify_user(release.user_reference, users, after=previous.journal_seq)
    conflicts = []
    for section in ("items", "lodging"):
        candidates = {i["id"]: i for i in document.model_dump(mode="json")[section]}
        for item in old[section]:
            if not item.get("protection"):
                continue
            following = candidates.get(item["id"])
            normalized = (
                (ScheduledItem if section == "items" else LodgingStay)
                .model_validate(item)
                .model_dump(mode="json")
            )
            if following != normalized and (section, item["id"]) not in releases:
                conflicts.append({"section": section, "id": item["id"], "title": item.get("title")})
    if conflicts:
        raise PlanConflict(
            "locked_item_conflict",
            "修改涉及已锁定或用户自报已订安排；请保留或向用户澄清。",
            conflicts,
        )
    conditions = {r.id: r for r in document.requirements}
    for requirement in old["requirements"]:
        if requirement["origin"] != "user" or requirement["state"] != "active":
            continue
        candidate = conditions.get(requirement["id"])
        if candidate and candidate.model_dump(mode="json") == requirement:
            continue
        revisions = [
            r
            for r in document.requirements
            if r.origin == "user"
            and (r.id == requirement["id"] or requirement["id"] in r.supersedes)
        ]
        if not revisions:
            raise PlanConflict(
                "constraint_loss",
                "修改遗漏既有用户条件；保留该条件或引用用户的明确修订。",
                [{"id": requirement["id"], "statement": requirement["statement"]}],
            )
        for revision in revisions:
            verify_user(revision.user_reference, users, after=previous.journal_seq)


def evidence_review(document, bank, users, previous):
    reports = {}
    dump = document.model_dump(mode="json")
    item_map = {i.id: i for i in document.items}
    lodging_map = {i.id: i for i in document.lodging}
    for source_id, source in document.source_catalog.items():
        use = document.evidence.get(source_id)
        reasons = []
        state = "needs_review"
        if use is None:
            reasons.append("来源适用条件尚未声明")
        elif use.origin == "user":
            if not use.user_reference:
                raise PlanConflict("invalid_user_reference", "用户提供的证据需要原话引用。")
            verify_user(use.user_reference, users)
            state = "user_reported"
            reasons.append("用户提供，供应商未核验")
        elif use.origin == "assumption":
            state = "assumption"
            reasons.append("模型估计或假设，不能当查询时报价")
        else:
            matches = bank.get(source_key(source), [])
            if not matches:
                reasons.append("无法对应本会话已查工具来源")
            elif not use.query_conditions or not any(
                all(record["arguments"].get(k) == v for k, v in use.query_conditions.items())
                for record in matches
            ):
                reasons.append("查询条件缺失或与原始工具参数不符")
            else:
                state = "observed"
            dates = {item_map[i].start.date() for i in use.item_ids}
            for cost_id in use.cost_ids:
                cost = next(c for c in document.costs if c.id == cost_id)
                dates.update(item_map[i].start.date() for i in cost.item_ids)
            for identifier in use.lodging_ids:
                stay = lodging_map[identifier]
                dates.update([stay.check_in, stay.check_out])
            if use.travel_dates and dates - set(use.travel_dates):
                reasons.append("安排日期已超出来源声明的适用范围")
            if use.kind in {"quote", "weather", "opening"}:
                if not use.travel_dates:
                    reasons.append("动态证据缺少适用日期")
                query_dates = set()
                for record in matches:
                    args = record["arguments"]
                    for field, value in args.items():
                        if field in {
                            "date",
                            "departure_date",
                            "travel_date",
                            "forecast_date",
                            "target_time",
                            "start_time",
                            "end_time",
                            "check_in",
                            "check_out",
                        }:
                            try:
                                query_dates.add(date.fromisoformat(str(value)[:10]))
                            except ValueError:
                                pass
                if query_dates and set(use.travel_dates) - query_dates:
                    reasons.append("声明日期与原查询日期不一致")
            if use.travelers is not None and document.party.travelers != use.travelers:
                reasons.append("出行人数与证据不一致")
            if use.rooms is not None and document.party.rooms != use.rooms:
                reasons.append("房间数与证据不一致")
            if use.valid_until is not None and use.valid_until <= utc_now():
                reasons.append("来源声明有效期已过，需重新查询")
            if source.retrieved_at > utc_now():
                reasons.append("来源查询时间在未来")
            # Same source/time reused for a changed target is explicitly stale even
            # if the model rewrites its declared applicability to fit the new plan.
            if previous and source_id in previous.document["source_catalog"]:
                old_source = previous.document["source_catalog"][source_id]
                if source_key(source) == source_key(old_source):
                    prior = previous.document["evidence"].get(source_id, {})
                    current_use = dump["evidence"].get(source_id, {})
                    prior_scope = {k: v for k, v in prior.items() if k != "query_conditions"}
                    current_scope = {
                        k: v for k, v in current_use.items() if k != "query_conditions"
                    }
                    if prior_scope != current_scope:
                        reasons.append("旧来源的适用条件被改写，需复核")
                    for section, ids in (
                        ("items", use.item_ids),
                        ("costs", use.cost_ids),
                        ("lodging", use.lodging_ids),
                    ):
                        before = {i["id"]: i for i in previous.document[section]}
                        after = {i["id"]: i for i in dump[section]}
                        relevant = (
                            {"start", "end", "start_place_id", "end_place_id", "title"}
                            if section == "items"
                            else {"check_in", "check_out", "hotel_id", "destination_id"}
                            if section == "lodging"
                            else {"total", "basis"}
                        )
                        if any(
                            identifier in before
                            and any(
                                before[identifier].get(k) != after[identifier].get(k)
                                for k in relevant
                            )
                            for identifier in ids
                        ):
                            reasons.append("关联安排或费用变化，旧证据待复核")
            if reasons:
                state = "needs_review"
        reports[source_id] = {
            "state": state,
            "reasons": reasons,
            "retrieved_at": source.retrieved_at.isoformat(),
            "data_time": source.data_time.isoformat() if source.data_time else None,
            "snapshot_only": bool(use and use.kind in {"quote", "weather", "opening"}),
        }
    return reports


def version_diff(before, after, previous_review, review):
    changes = []
    for section in ("items", "lodging", "costs", "requirements"):
        old = {i["id"]: i for i in (before or {}).get(section, [])}
        new = {i["id"]: i for i in after[section]}
        for identifier in dict.fromkeys([*old, *new]):
            left, right = old.get(identifier), new.get(identifier)
            if left == right:
                continue
            fields = [
                k
                for k in dict.fromkeys([*(left or {}), *(right or {})])
                if (left or {}).get(k) != (right or {}).get(k)
            ]
            changes.append(
                {
                    "section": section,
                    "id": identifier,
                    "operation": "added"
                    if left is None
                    else "removed"
                    if right is None
                    else "changed",
                    "before": left,
                    "after": right,
                    "fields": fields,
                }
            )
    for field in (
        "planning_window",
        "party",
        "transfers",
        "opening_hours",
        "budget",
        "budget_scope",
        "required_lodging_nights",
        "required_cost_ids",
        "evidence",
    ):
        if before and before.get(field) != after.get(field):
            changes.append(
                {
                    "section": field,
                    "id": field,
                    "operation": "changed",
                    "before": before.get(field),
                    "after": after.get(field),
                    "fields": [field],
                }
            )
    old_totals = {
        t["currency"]: t["amount"] for t in (previous_review or {}).get("known_cost_totals", [])
    }
    new_totals = {t["currency"]: t["amount"] for t in review["known_cost_totals"]}
    deltas = []
    for currency in dict.fromkeys([*old_totals, *new_totals]):
        left, right = old_totals.get(currency), new_totals.get(currency)
        deltas.append(
            {
                "currency": currency,
                "before": left,
                "after": right,
                "delta": str(Decimal(right) - Decimal(left))
                if left is not None and right is not None
                else None,
                "scope": "known_subtotal_not_full_budget",
            }
        )
    return {"changes": changes, "cost_deltas": deltas}


def matching_quote(cost, records):
    if cost.total is None or cost.basis is None:
        return None
    basis = cost.basis
    for record in records:
        queried_party = record["arguments"].get("travelers")
        if isinstance(queried_party, dict):
            persons = queried_party.get("adults", 0) + len(queried_party.get("children_ages", []))
            if persons != basis.travelers:
                continue
        if cost.lodging_ids and record["arguments"].get("rooms") != basis.rooms:
            continue
        for price in record["prices"]:
            money = price["money"]
            if (
                price["kind"] != "query_quote"
                or money is None
                or money["currency"] != cost.total.currency
            ):
                continue
            unit = price["unit"]
            multiplier = None
            if unit == "whole_party" and price["priced_persons"] == basis.travelers:
                multiplier = 1
            elif unit == "whole_stay" and (
                price["priced_rooms"] == basis.rooms and price["priced_nights"] == basis.nights
            ):
                multiplier = 1
            elif unit == "per_person" and basis.travelers:
                multiplier = basis.travelers
            elif unit == "per_room_per_night" and basis.rooms and basis.nights:
                multiplier = basis.rooms * basis.nights
            elif unit == "per_room" and basis.rooms and price["priced_nights"] == basis.nights:
                multiplier = basis.rooms
            if multiplier and Decimal(money["amount"]) * multiplier == cost.total.amount:
                return price
    return None


class ItineraryService:
    def __init__(self, facts):
        self.facts = facts
        self.store = facts.store

    async def current(self, conversation_id):
        async with self.store.sessions() as session:
            head = await session.get(ItineraryHead, conversation_id)
            return (
                await session.get(ItineraryVersion, head.current_id)
                if head and head.current_id
                else None
            )

    async def context(self, conversation_id):
        from travel_agent.durable_storage import Journal

        current = await self.current(conversation_id)
        async with self.store.sessions() as session:
            users = (
                await session.scalars(
                    select(Journal)
                    .where(Journal.conversation_id == conversation_id)
                    .order_by(Journal.seq)
                )
            ).all()
            pending = (
                await session.scalars(
                    select(ItineraryVersion)
                    .where(
                        ItineraryVersion.conversation_id == conversation_id,
                        ItineraryVersion.status == "draft",
                    )
                    .order_by(ItineraryVersion.journal_seq.desc())
                )
            ).first()
        return {
            "current_itinerary": public_version(current, internal=True) if current else None,
            "pending_itinerary": public_version(pending, internal=True) if pending else None,
            "user_message_references": [
                {"message_id": u.id, "user_message_number": index}
                for index, u in enumerate((u for u in users if u.payload.get("role") == "user"), 1)
            ],
            "available_sources": available_sources(source_bank([u.payload for u in users])),
        }

    async def prepare(self, lease, key, arguments):
        from travel_agent.durable_storage import Journal

        previous = await self.current(lease["conversation_id"])
        if arguments.get("base_version_id") != (previous.id if previous else None):
            raise PlanConflict(
                "stale_itinerary_version", "修改基于旧版本，请使用当前版本并重新检查影响。"
            )
        async with self.store.sessions() as session:
            records = (
                await session.scalars(
                    select(Journal)
                    .where(Journal.conversation_id == lease["conversation_id"])
                    .order_by(Journal.seq)
                )
            ).all()
        users = {
            r.id: {"content": r.payload["content"], "seq": r.seq}
            for r in records
            if r.payload.get("role") == "user"
        }
        prepared = deepcopy(arguments)
        bank = source_bank([r.payload for r in records])
        catalog = available_sources(bank)
        imports = prepared.get("available_source_ids", [])
        target = (
            prepared.get("document")
            if "document" in prepared
            else prepared.get("patch", {}).setdefault("set_fields", {})
        )
        if imports and target is not None:
            for identifier in imports:
                if identifier not in catalog:
                    raise PlanConflict(
                        "unknown_available_source", "请使用运行环境中列出的真实来源ID。"
                    )
                target.setdefault("source_catalog", {})[identifier] = catalog[identifier]["source"]
                use = target.setdefault("evidence", {}).setdefault(identifier, {})
                use.setdefault("origin", "tool")
                use["query_conditions"] = deepcopy(catalog[identifier]["query_conditions"])
        request = SaveItineraryInput.model_validate(prepared)
        original = (
            apply_patch(previous.document, request.patch)
            if request.patch is not None
            else prepared["document"]
        )
        # Server-issued IDs are references to immutable actual query records.
        # Pin them after recursive patch merging, which otherwise retains old
        # narrative keys or lets the model paraphrase the original arguments.
        for identifier in set(original.get("source_catalog", {})) & catalog.keys():
            original["source_catalog"][identifier] = deepcopy(catalog[identifier]["source"])
            use = original.setdefault("evidence", {}).setdefault(identifier, {})
            use["origin"] = "tool"
            use["query_conditions"] = deepcopy(catalog[identifier]["query_conditions"])
        document = ItineraryDocument.model_validate(original)
        protect_previous(document, previous, request, users)
        reports = evidence_review(document, bank, users, previous)
        pending = []
        # Effective checks cannot treat unsupported/stale quotes or openings as
        # current facts. The stored version retains the original source and time.
        effective = document.model_copy(deep=True)
        for source_id, report in reports.items():
            if report["state"] != "observed":
                pending.append(
                    {
                        "category": "evidence",
                        "id": source_id,
                        "message": "；".join(report["reasons"]),
                    }
                )
        for cost in effective.costs:
            source_ids = [
                sid
                for sid, src in document.source_catalog.items()
                if any(source_key(src) == source_key(s) for s in cost.sources)
            ]
            if cost.kind == "query_quote" and (
                not source_ids or any(reports[sid]["state"] != "observed" for sid in source_ids)
            ):
                pending.append(
                    {
                        "category": "cost",
                        "id": cost.id,
                        "message": "报价来源或适用条件未核实，金额仅作参考",
                    }
                )
                cost.kind = "reference"
            if cost.kind == "query_quote":
                proof = matching_quote(
                    cost,
                    [
                        record
                        for source in cost.sources
                        for record in bank.get(source_key(source), [])
                    ],
                )
                if proof is None:
                    pending.append(
                        {
                            "category": "cost",
                            "id": cost.id,
                            "message": "金额或人数/房间/晚数无法对应原始报价，作为参考保留",
                        }
                    )
                    cost.kind = "reference"
                else:
                    if (
                        proof.get("valid_until")
                        and datetime.fromisoformat(proof["valid_until"].replace("Z", "+00:00"))
                        <= utc_now()
                    ):
                        cost.kind = "reference"
                        pending.append(
                            {
                                "category": "cost",
                                "id": cost.id,
                                "message": "原始报价已过有效期，需重新查询",
                            }
                        )
                    if cost.tax_basis != proof["tax_basis"] or cost.fee_basis != proof["fee_basis"]:
                        pending.append(
                            {
                                "category": "cost",
                                "id": cost.id,
                                "message": "税费口径按原始报价保留，不能视为已全部包含",
                            }
                        )
                    cost.tax_basis, cost.fee_basis = proof["tax_basis"], proof["fee_basis"]
            if cost.total is None or cost.basis is None or cost.basis.travelers is None:
                pending.append(
                    {"category": "cost", "id": cost.id, "message": "金额或计价人数口径待确认"}
                )
                if cost.kind == "query_quote":
                    cost.kind = "reference"
            elif cost.basis.travelers != document.party.travelers:
                pending.append(
                    {"category": "cost", "id": cost.id, "message": "费用人数与当前出行人数不一致"}
                )
                if cost.kind == "query_quote":
                    cost.kind = "reference"
            if cost.lodging_ids:
                stays = [s for s in document.lodging if s.id in cost.lodging_ids]
                expected_nights = sum((s.check_out - s.check_in).days for s in stays)
                if (
                    cost.basis is None
                    or cost.basis.rooms != document.party.rooms
                    or cost.basis.nights != expected_nights
                ):
                    pending.append(
                        {
                            "category": "cost",
                            "id": cost.id,
                            "message": "住宿费用房间数或间夜口径待确认",
                        }
                    )
                    if cost.kind == "query_quote":
                        cost.kind = "reference"
        # Use one model validation after updating dependent opening fields.
        check_data = effective.model_dump(mode="json")
        for item in check_data["items"]:
            duration = item.get("travel_duration")
            if duration and duration["basis"] == "queried":
                related = [
                    sid
                    for sid, src in document.source_catalog.items()
                    if any(source_key(src) == source_key(s) for s in duration["sources"])
                ]
                if not related or any(reports[s]["state"] != "observed" for s in related):
                    duration.update(minutes=None, basis="unknown")
                else:
                    minima = [
                        minutes
                        for source in duration["sources"]
                        for record in bank.get(source_key(source), [])
                        for minutes in record["route_minutes"]
                    ]
                    if not minima:
                        duration.update(minutes=None, basis="unknown")
                    elif duration["minutes"] is not None and duration["minutes"] < min(minima):
                        duration["minutes"] = min(minima)
            elif item["kind"] == "transport" and duration is None:
                pending.append(
                    {
                        "category": "transport",
                        "id": item["id"],
                        "message": "交通时间块的实际行驶时长尚未核实",
                    }
                )
        for transfer in check_data["transfers"]:
            related = [
                sid
                for sid, src in document.source_catalog.items()
                if any(source_key(src) == source_key(s) for s in transfer["sources"])
            ]
            if (
                transfer["duration_basis"] == "queried"
                and transfer["minimum_minutes"] is not None
                and (not related or any(reports[s]["state"] != "observed" for s in related))
            ):
                transfer.update(minimum_minutes=None, duration_basis="unknown")
        for opening in check_data["opening_hours"]:
            related = [
                sid
                for sid, src in document.source_catalog.items()
                if any(source_key(src) == source_key(s) for s in opening["sources"])
            ]
            if not related or any(reports[s]["state"] != "observed" for s in related):
                opening.update(state="unknown", windows=[])
        checked = await validate_itinerary(ItineraryDocument.model_validate(check_data))
        if checked.status == "invalid":
            raise PlanConflict(
                "itinerary_conflict",
                "安排存在已知冲突，请修正或向用户澄清后再保存。",
                [c.model_dump(mode="json") for c in checked.checks if c.status == "fail"],
            )
        for transfer in document.transfers:
            if transfer.buffers_complete is None:
                pending.append(
                    {
                        "category": "transfer",
                        "id": transfer.to_item_id,
                        "message": "进出站、换乘或入园等必要缓冲范围待核实",
                    }
                )
        review = {
            "status": "unknown" if pending or checked.status == "unknown" else "valid",
            "checks": [c.model_dump(mode="json") for c in checked.checks],
            "known_cost_totals": [m.model_dump(mode="json") for m in checked.known_cost_totals],
            "pending": pending,
            "evidence": reports,
            "unknown_cost_ids": [c.id for c in document.costs if c.total is None],
        }
        canonical = canonical_document(document, original)
        for saved, checked_item in zip(canonical["items"], check_data["items"], strict=True):
            if saved.get("travel_duration") and checked_item.get("travel_duration"):
                saved["travel_duration"].update(
                    minutes=checked_item["travel_duration"]["minutes"],
                    basis=checked_item["travel_duration"]["basis"],
                )
        for saved, normalized in zip(canonical["costs"], effective.costs, strict=True):
            saved.update(
                kind=normalized.kind, tax_basis=normalized.tax_basis, fee_basis=normalized.fee_basis
            )
        diff = version_diff(
            previous.document if previous else None,
            canonical,
            previous.review if previous else None,
            review,
        )
        return {
            "id": str(uuid5(NAMESPACE_URL, key + "/itinerary")),
            "base_id": request.base_version_id,
            "document": canonical,
            "review": review,
            "diff": diff,
            "reason": request.change_reason,
        }

    async def list(self, conversation_id):
        async with self.store.sessions() as session:
            rows = (
                await session.scalars(
                    select(ItineraryVersion)
                    .where(
                        ItineraryVersion.conversation_id == conversation_id,
                        ItineraryVersion.status == "published",
                    )
                    .order_by(ItineraryVersion.revision.desc())
                )
            ).all()
            head = await session.get(ItineraryHead, conversation_id)
        return {
            "current_id": head.current_id if head else None,
            "items": [
                {
                    k: v
                    for k, v in public_version(r).items()
                    if k in {"id", "revision", "created_at", "change_reason", "base_version_id"}
                }
                for r in rows
            ],
        }

    async def get(self, conversation_id, version_id):
        async with self.store.sessions() as session:
            row = await session.get(ItineraryVersion, version_id)
            if row and row.conversation_id == conversation_id and row.status == "published":
                return public_version(row)
        return None


async def stage_version(session, conversation_id, run_id, key, seq, candidate):
    head = await session.get(ItineraryHead, conversation_id)
    if head is None:
        head = ItineraryHead(id=conversation_id, revision=0)
        session.add(head)
    if head.current_id != candidate["base_id"]:
        raise PlanConflict("stale_itinerary_version", "当前方案版本已变化，未保存旧修改。")
    session.add(
        ItineraryVersion(
            id=candidate["id"],
            conversation_id=conversation_id,
            run_id=run_id,
            effect_id=key,
            base_id=candidate["base_id"],
            journal_seq=seq,
            status="draft",
            created_at=utc_now().isoformat(),
            document=candidate["document"],
            review=candidate["review"],
            diff=candidate["diff"],
            reason=candidate["reason"],
        )
    )


async def publish_version(session, conversation_id, run_id, completed):
    drafts = (
        await session.scalars(
            select(ItineraryVersion)
            .where(
                ItineraryVersion.conversation_id == conversation_id,
                ItineraryVersion.status == "draft",
            )
            .order_by(ItineraryVersion.journal_seq.desc())
        )
    ).all()
    current = next((r for r in drafts if r.run_id == run_id), None)
    if not current:
        return None
    if not completed:
        for row in drafts:
            if row.run_id == run_id:
                row.status = "abandoned"
        return None
    head = await session.get(ItineraryHead, conversation_id)
    if head.current_id != current.base_id:
        raise PlanConflict("stale_itinerary_version", "交付时版本变化，未覆盖当前方案。")
    head.revision += 1
    head.current_id = current.id
    current.status, current.revision = "published", head.revision
    for row in drafts:
        if row.id != current.id:
            row.status = "superseded"
    return current


async def abandon_drafts(session, conversation_id, run_id):
    rows = (
        await session.scalars(
            select(ItineraryVersion).where(
                ItineraryVersion.conversation_id == conversation_id,
                ItineraryVersion.run_id == run_id,
                ItineraryVersion.status == "draft",
            )
        )
    ).all()
    for row in rows:
        row.status = "abandoned"
