"""Read-only review of explicit requirement links and atomic plan claims.

The model supplies semantic assessments. They are never upgraded to deterministic
proof; atomic checks describe the structured plan and the observed source snapshot.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from travel_agent.itinerary_schema import ItineraryDocument
from travel_tools.common import StrictModel, utc_now
from travel_tools.schemas.itinerary import Identifier, UserReference
from travel_tools.schemas.quotes import Money

ReviewStatus = Literal["pass", "fail", "unknown"]


class RequirementAssessment(StrictModel):
    requirement_id: Identifier
    assessment: Literal["satisfied", "unsatisfied", "unknown"]
    explanation: str = Field(min_length=1)
    item_ids: list[Identifier] = Field(default_factory=list)
    cost_ids: list[Identifier] = Field(default_factory=list)
    lodging_ids: list[Identifier] = Field(default_factory=list)
    source_ids: list[Identifier] = Field(default_factory=list)


class AtomicClaim(StrictModel):
    id: Identifier


class CostAmountClaim(AtomicClaim):
    kind: Literal["cost_amount"]
    cost_id: Identifier
    amount: Money
    basis: Literal["planned", "verified_quote"] = "planned"
    travelers: int | None = Field(default=None, ge=1)
    rooms: int | None = Field(default=None, ge=1)
    nights: int | None = Field(default=None, ge=1)
    taxes_fees: Literal["as_recorded", "included"] = "as_recorded"


class WithinBudgetClaim(AtomicClaim):
    kind: Literal["within_budget"]
    budget: Money


class ItemTimeClaim(AtomicClaim):
    kind: Literal["item_start", "item_end"]
    item_id: Identifier
    time: AwareDatetime


class PartySizeClaim(AtomicClaim):
    kind: Literal["party_size"]
    travelers: int = Field(ge=1)
    rooms: int | None = Field(default=None, ge=1)


class SourceStatementClaim(AtomicClaim):
    kind: Literal["source_statement"]
    statement: str = Field(min_length=1)
    source_ids: list[Identifier] = Field(min_length=1)


PlanClaim = Annotated[
    CostAmountClaim | WithinBudgetClaim | ItemTimeClaim | PartySizeClaim | SourceStatementClaim,
    Field(discriminator="kind"),
]


class ReviewItineraryInput(StrictModel):
    base_version_id: str | None = Field(default=None, min_length=1)
    document: ItineraryDocument | None = None
    available_source_ids: list[Identifier] = Field(default_factory=list)
    requirement_assessments: list[RequirementAssessment] = Field(default_factory=list)
    claims: list[PlanClaim] = Field(default_factory=list)

    @model_validator(mode="after")
    def unambiguous_review(self):
        if (self.base_version_id is None) == (self.document is None):
            raise ValueError("supply exactly one of base_version_id or document")
        for entries, field in (
            (self.requirement_assessments, "requirement_id"),
            (self.claims, "id"),
        ):
            ids = [getattr(entry, field) for entry in entries]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {field} in review")
        return self


class ReviewFinding(StrictModel):
    category: Literal["requirement", "user_reference", "protection", "claim"]
    id: str
    status: ReviewStatus
    code: str
    feedback: list[str]
    assessment_state: Literal["claimed", "review_needed", "unaddressed"] | None = None


class GroundingReview(StrictModel):
    status: ReviewStatus
    requirements: list[ReviewFinding]
    claims: list[ReviewFinding]
    references: list[ReviewFinding]
    unassessed_requirement_ids: list[str]
    raw_user_message_ids_without_requirement_reference: list[str]
    scope: str = (
        "Atomic checks concern the supplied structured plan and source snapshots only. "
        "Requirement satisfaction remains a model assessment; exact user quotes prove "
        "attribution, not interpretation or complete coverage of all user messages. "
        "Quotation checks match amount and pricing scope to a queried price record; "
        "they do not establish the selected train/flight/hotel or room-type identity. "
        "Arbitrary final prose, real-world availability, booking and safety are not verified."
    )


def _reference_matches(reference: UserReference, users: dict) -> bool:
    user = users.get(reference.message_id)
    return bool(user and reference.quote.strip() and reference.quote in user["content"])


def _finding(category, identifier, status, code, feedback, *, state=None):
    return ReviewFinding(
        category=category,
        id=identifier,
        status=status,
        code=code,
        feedback=feedback,
        assessment_state=state,
    )


def review_itinerary(
    document: ItineraryDocument,
    requirement_assessments: list[RequirementAssessment],
    claims: list[PlanClaim],
    *,
    users: dict,
    source_reports: dict,
    bank: dict | None = None,
    previous_document: ItineraryDocument | None = None,
) -> GroundingReview:
    """Pure review; callers resolve the actual version, raw users and source bank.

    ``bank`` is the existing source_bank result from durable tool history. Without
    it verified quotations and a positive budget claim cannot pass. No writes,
    semantic regexes, extra model call or fixed business workflow are introduced.
    """
    from travel_agent.itineraries import matching_quote, source_key

    maps = {
        "item_ids": {i.id: i for i in document.items},
        "cost_ids": {c.id: c for c in document.costs},
        "lodging_ids": {s.id: s for s in document.lodging},
        "source_ids": document.source_catalog,
    }
    active = {r.id: r for r in document.requirements if r.state == "active"}
    assessments = {a.requirement_id: a for a in requirement_assessments}
    requirements, references, claim_results = [], [], []
    cited_users = set()

    def check_reference(identifier, reference):
        valid = _reference_matches(reference, users)
        references.append(
            _finding(
                "user_reference",
                identifier,
                "pass" if valid else "fail",
                "exact_raw_quote" if valid else "raw_user_quote_mismatch",
                [
                    "原话引用真实；不证明模型对原话的解释。"
                    if valid
                    else "message_id或quote不能对应真实用户原话，请重新引用。"
                ],
            )
        )
        return valid

    for requirement in document.requirements:
        if requirement.user_reference:
            if check_reference(requirement.id, requirement.user_reference):
                cited_users.add(requirement.user_reference.message_id)
    for section in ("items", "lodging"):
        entries = {i.id: i for i in getattr(document, section)}
        for item in entries.values():
            if item.protection:
                check_reference(f"{section}:{item.id}", item.protection.user_reference)
        if previous_document:
            for old in getattr(previous_document, section):
                if old.protection and entries.get(old.id) != old:
                    references.append(
                        _finding(
                            "protection",
                            f"{section}:{old.id}",
                            "fail",
                            "protected_item_changed",
                            ["已锁定或用户自报已订安排被改变/删除；保存前需沿用正式保护解除协议。"],
                        )
                    )
    if previous_document:
        for old in previous_document.requirements:
            if old.origin == "user" and old.state == "active" and old.id not in active:
                if not any(old.id in r.supersedes for r in active.values()):
                    requirements.append(
                        _finding(
                            "requirement",
                            old.id,
                            "fail",
                            "previous_requirement_missing",
                            ["候选行程遗漏已发布方案中的有效用户要求。"],
                            state="unaddressed",
                        )
                    )

    for identifier in active:
        assessment = assessments.get(identifier)
        if assessment is None:
            requirements.append(
                _finding(
                    "requirement",
                    identifier,
                    "fail",
                    "requirement_unassessed",
                    ["该有效要求没有需求覆盖说明；请关联安排/费用/住宿/来源或明确待确认。"],
                    state="unaddressed",
                )
            )
            continue
        invalid = [
            f"{field}:{target}"
            for field, entries in maps.items()
            for target in getattr(assessment, field)
            if target not in entries
        ]
        if invalid:
            status, code, feedback = (
                "fail",
                "invalid_requirement_targets",
                ["引用不存在的目标：" + "、".join(invalid)],
            )
        elif assessment.assessment == "unsatisfied":
            status, code, feedback = "fail", "requirement_unsatisfied", [assessment.explanation]
        else:
            status, code, feedback = (
                "unknown",
                "semantic_satisfaction_unverified",
                [
                    "满足与否由模型解释，目标存在不等于该要求已被确定性证明。",
                    assessment.explanation,
                ],
            )
            stale = [
                sid
                for sid in assessment.source_ids
                if source_reports.get(sid, {}).get("state") != "observed"
            ]
            if stale:
                feedback.append("关联来源待复核：" + "、".join(stale))
            if not any(getattr(assessment, field) for field in maps):
                feedback.append("未关联安排、费用、住宿或来源，缺少可检查的覆盖依据。")
        requirements.append(
            _finding(
                "requirement",
                identifier,
                status,
                code,
                feedback,
                state="claimed" if assessment.assessment == "satisfied" else "review_needed",
            )
        )
    for identifier in assessments.keys() - active.keys():
        requirements.append(
            _finding(
                "requirement",
                identifier,
                "fail",
                "unknown_or_inactive_requirement",
                ["覆盖说明引用了不存在或已被替代的要求。"],
                state="review_needed",
            )
        )

    def quote_issues(cost):
        issues = []
        if cost.kind != "query_quote" or cost.total is None:
            issues.append("费用不是已核实的查询时报价。")
        basis = cost.basis
        if basis is None or basis.travelers is None or basis.travelers != document.party.travelers:
            issues.append("费用计价人数缺失或与当前出行人数不一致。")
        if cost.lodging_ids:
            nights = sum(
                (maps["lodging_ids"][i].check_out - maps["lodging_ids"][i].check_in).days
                for i in cost.lodging_ids
            )
            if basis is None or basis.rooms != document.party.rooms or basis.nights != nights:
                issues.append("费用房间数或间夜口径不符。")
        related = [
            sid
            for sid, source in document.source_catalog.items()
            if any(source_key(source) == source_key(s) for s in cost.sources)
        ]
        if not related or any(
            source_reports.get(sid, {}).get("state") != "observed" for sid in related
        ):
            issues.append("费用来源缺失、过期或适用条件待复核。")
            for sid in related:
                issues.extend(source_reports.get(sid, {}).get("reasons", []))
        proof = matching_quote(
            cost,
            [
                record
                for source in cost.sources
                for record in (bank or {}).get(source_key(source), [])
            ],
        )
        if proof is None:
            issues.append("金额/人数/房间/晚数无法对应会话中的真实工具报价。")
        else:
            if (
                proof.get("valid_until")
                and datetime.fromisoformat(proof["valid_until"].replace("Z", "+00:00")) <= utc_now()
            ):
                issues.append("原始报价已过有效期，需重查。")
            if cost.tax_basis != proof["tax_basis"] or cost.fee_basis != proof["fee_basis"]:
                issues.append("税费口径与原始报价不一致。")
        return issues

    for claim in claims:
        status, code, feedback = (
            "pass",
            "matches_structured_plan",
            [
                "与当前结构化方案一致；报价匹配仅核对金额及计价口径，"
                "不证明所选班次/酒店/房型身份绑定、原话解释、实际可售或已预订。"
            ],
        )
        if isinstance(claim, CostAmountClaim):
            cost = maps["cost_ids"].get(claim.cost_id)
            if cost is None:
                status, code, feedback = "fail", "invalid_claim_target", ["费用ID不存在。"]
            elif cost.total is None:
                status, code, feedback = "unknown", "cost_unknown", ["费用金额未知。"]
            elif claim.amount != cost.total:
                status, code, feedback = (
                    "fail",
                    "cost_amount_mismatch",
                    ["结论金额或币种与费用记录不一致。"],
                )
            else:
                conflicts, unknown = [], []
                for field in ("travelers", "rooms", "nights"):
                    value = getattr(claim, field)
                    if value is None:
                        continue
                    stored = getattr(cost.basis, field) if cost.basis else None
                    party_value = getattr(document.party, field, value)
                    if stored is None or party_value is None:
                        unknown.append(f"{field}计价口径未知。")
                    elif value != stored or value != party_value:
                        conflicts.append(f"{field}与费用计价/出行口径不一致。")
                if claim.taxes_fees == "included":
                    if cost.tax_basis in {"excluded", "partial"} or cost.fee_basis in {
                        "excluded",
                        "partial",
                    }:
                        conflicts.append("费用记录明确未含全部税费，不能声称全部包含。")
                    elif cost.tax_basis == "unknown" or cost.fee_basis == "unknown":
                        unknown.append("税费未确认全部包含，不能声称含全部税费。")
                if claim.basis == "verified_quote":
                    unknown.extend(quote_issues(cost))
                if conflicts:
                    status, code, feedback = "fail", "cost_basis_mismatch", conflicts
                elif unknown:
                    status, code, feedback = "unknown", "quote_or_scope_unverified", unknown
        elif isinstance(claim, WithinBudgetClaim):
            if document.budget is None:
                status, code, feedback = "unknown", "budget_unknown", ["方案预算上限未知。"]
            elif claim.budget != document.budget:
                status, code, feedback = "fail", "budget_mismatch", ["预算金额或币种不一致。"]
            else:
                costs = document.costs
                subtotal = sum(
                    (
                        c.total.amount
                        for c in costs
                        if c.total
                        and c.total.currency == claim.budget.currency
                        and c.kind == "query_quote"
                    ),
                    Decimal(0),
                )
                problems = []
                if document.required_cost_ids is None:
                    problems.append("必要费用范围未知。")
                elif set(document.required_cost_ids) - maps["cost_ids"].keys():
                    problems.append("必要费用条目缺失。")
                if not costs:
                    problems.append("没有可核对的费用条目，空清单不能证明全程零费用。")
                for cost in costs:
                    problems.extend(quote_issues(cost))
                    if cost.total and cost.total.currency != claim.budget.currency:
                        problems.append("存在其他币种，不能自动换算。")
                    if cost.tax_basis != "included" or cost.fee_basis != "included":
                        problems.append("税费没有确认全部包含。")
                if subtotal > claim.budget.amount:
                    status, code, feedback = (
                        "fail",
                        "known_quotes_exceed_budget",
                        ["已知同币种查询时报价小计已经超出预算。"],
                    )
                elif problems:
                    status, code, feedback = (
                        "unknown",
                        "budget_coverage_unverified",
                        list(dict.fromkeys(problems)),
                    )
                else:
                    code, feedback = (
                        "within_declared_budget_snapshot",
                        [
                            "已声明费用范围内的金额和计价口径可对应来源时刻的查询报价记录，"
                            "符合结构化预算；不保证费用范围覆盖全部原始需求、所选班次/"
                            "酒店/房型与报价的身份绑定或未来价格。"
                        ],
                    )
        elif isinstance(claim, ItemTimeClaim):
            item = maps["item_ids"].get(claim.item_id)
            if item is None:
                status, code, feedback = "fail", "invalid_claim_target", ["安排ID不存在。"]
            elif getattr(item, claim.kind.removeprefix("item_")).astimezone(UTC) != (
                claim.time.astimezone(UTC)
            ):
                status, code, feedback = "fail", "item_time_mismatch", ["结论时间与安排不一致。"]
        elif isinstance(claim, PartySizeClaim):
            pairs = [(claim.travelers, document.party.travelers)]
            if claim.rooms is not None:
                pairs.append((claim.rooms, document.party.rooms))
            if any(stored is not None and actual != stored for actual, stored in pairs):
                status, code, feedback = "fail", "party_size_mismatch", ["人数或房间数不一致。"]
            elif any(stored is None for _, stored in pairs):
                status, code, feedback = "unknown", "party_size_unknown", ["人数或房间数未知。"]
        elif isinstance(claim, SourceStatementClaim):
            missing = set(claim.source_ids) - document.source_catalog.keys()
            if missing:
                status, code, feedback = (
                    "fail",
                    "invalid_source_reference",
                    ["来源ID不存在：" + "、".join(sorted(missing))],
                )
            else:
                status, code, feedback = (
                    "unknown",
                    "source_semantics_unverified",
                    ["来源存在和查询成功不能证明这段文字真实；需核对原始证据内容。"],
                )
                for sid in claim.source_ids:
                    report = source_reports.get(sid, {})
                    if report.get("state") != "observed":
                        feedback.extend(report.get("reasons") or [f"来源{sid}待复核。"])
        claim_results.append(_finding("claim", claim.id, status, code, feedback))

    findings = [*requirements, *references, *claim_results]
    statuses = {f.status for f in findings}
    status = (
        "fail"
        if "fail" in statuses
        else "unknown"
        if "unknown" in statuses or not active or not claims
        else "pass"
    )
    return GroundingReview(
        status=status,
        requirements=requirements,
        claims=claim_results,
        references=references,
        unassessed_requirement_ids=sorted(active.keys() - assessments.keys()),
        raw_user_message_ids_without_requirement_reference=sorted(users.keys() - cited_users),
    )


REVIEW_ITINERARY_TOOL = {
    "type": "function",
    "function": {
        "name": "review_itinerary",
        "description": "只读核对当前已发布或候选行程的需求覆盖关联与原子结论。"
        "base_version_id可为同会话当前已发布或本轮草案ID；与document二选一。"
        "document只读检查，可用available_source_ids导入已查来源。"
        "requirements里的有效要求逐项说明并关联真实ID。"
        "模型声称满足仅是claimed，不会当成语义证明。"
        "claims可核对费用金额/币种/人数/时间/预算；unknown费用或税费缺失不能宣称预算内。"
        "报价核对只证明金额与计价口径可对应查询报价记录，不证明所选班次/酒店/房型绑定。"
        "source_statement即使来源存在也不等于文字被证明。工具不保证所有原话已提取、"
        "最终任意文字正确或现实可售；根据反馈自主决定补查、修改或追问，无需每轮强制调用。",
        "parameters": ReviewItineraryInput.model_json_schema(),
    },
}
