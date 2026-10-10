"""Minimal version envelope around the existing itinerary/validator contract."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from travel_tools.common import StrictModel
from travel_tools.schemas.itinerary import Identifier, UserReference, ValidateItineraryInput


class TravelParty(StrictModel):
    travelers: int | None = Field(default=None, ge=1)
    rooms: int | None = Field(default=None, ge=1)


class Requirement(StrictModel):
    id: Identifier
    statement: str = Field(min_length=1)
    origin: Literal["user", "assumption", "evidence"]
    state: Literal["active", "superseded"] = "active"
    supersedes: list[Identifier] = Field(default_factory=list)
    user_reference: UserReference | None = None
    source_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def attributed(self):
        if self.origin == "user" and self.user_reference is None:
            raise ValueError("user conditions require a raw user reference")
        if self.origin == "evidence" and not self.source_ids:
            raise ValueError("evidence conditions require source_ids")
        return self


class EvidenceUse(StrictModel):
    origin: Literal["tool", "user", "assumption"] = "tool"
    kind: Literal["quote", "weather", "opening", "route", "other"] = "other"
    user_reference: UserReference | None = None
    item_ids: list[Identifier] = Field(default_factory=list)
    cost_ids: list[Identifier] = Field(default_factory=list)
    lodging_ids: list[Identifier] = Field(default_factory=list)
    travel_dates: list[date] = Field(default_factory=list)
    travelers: int | None = Field(default=None, ge=1)
    rooms: int | None = Field(default=None, ge=1)
    query_conditions: dict = Field(
        default_factory=dict, description="Actual supplier query arguments."
    )
    valid_until: AwareDatetime | None = Field(default=None, description="Source expiry, if known.")


class ItineraryDocument(ValidateItineraryInput):
    title: str = Field(min_length=1)
    party: TravelParty = Field(default_factory=TravelParty)
    requirements: list[Requirement] = Field(default_factory=list)
    evidence: dict[Identifier, EvidenceUse] = Field(
        default_factory=dict,
        description="Keys are exactly source_catalog IDs, not separate evidence IDs. "
        "Values describe that source's use; do not add source_ids inside values.",
    )

    @model_validator(mode="after")
    def version_references(self):
        if not self.items:
            raise ValueError(
                "a saved itinerary needs scheduled items; ordinary queries need no save"
            )
        ids = [r.id for r in self.requirements]
        if len(set(ids)) != len(ids):
            raise ValueError("requirement IDs must be unique")
        for requirement in self.requirements:
            if set(requirement.source_ids) - self.source_catalog.keys():
                raise ValueError("requirement sources must exist in source_catalog")
        if set(self.evidence) - self.source_catalog.keys():
            raise ValueError("evidence keys must exist in source_catalog")
        for use in self.evidence.values():
            for name, references in [
                ("items", use.item_ids),
                ("costs", use.cost_ids),
                ("lodging", use.lodging_ids),
            ]:
                if set(references) - {entry.id for entry in getattr(self, name)}:
                    raise ValueError("evidence targets must exist")
        for cost in self.costs:
            if set(cost.item_ids) - {i.id for i in self.items} or set(cost.lodging_ids) - {
                i.id for i in self.lodging
            }:
                raise ValueError("cost targets must exist")
        return self


class ReleaseProtection(StrictModel):
    section: Literal["items", "lodging"]
    id: Identifier
    user_reference: UserReference


class ItineraryPatch(StrictModel):
    items: list[dict] = Field(
        default_factory=list,
        description="Partial item updates keyed by existing id, or full new items.",
    )
    lodging: list[dict] = Field(
        default_factory=list, description="Partial stay updates keyed by id."
    )
    costs: list[dict] = Field(
        default_factory=list, description="Partial cost updates keyed by id; null means unknown."
    )
    requirements: list[dict] = Field(default_factory=list)
    remove_items: list[Identifier] = Field(default_factory=list)
    remove_lodging: list[Identifier] = Field(default_factory=list)
    remove_costs: list[Identifier] = Field(default_factory=list)
    remove_requirements: list[Identifier] = Field(default_factory=list)
    set_fields: dict = Field(
        default_factory=dict,
        description=(
            "Only changed document fields: title, planning_window, party, budget, budget_scope, "
            "source_catalog, evidence, transfers, opening_hours, fixed_commitments, "
            "fixed_commitments_complete, required_lodging_nights, required_cost_ids. "
            "Dictionaries merge recursively; arrays replace. Unspecified facts remain unchanged."
        ),
    )


class ArgumentEdit(StrictModel):
    start: int = Field(ge=0, description="Zero-based Unicode character offset, not bytes.")
    delete_count: int = Field(ge=0)
    expected: str = Field(
        description="Exact deleted text, empty for insertion. All offsets refer to the original."
    )
    insert: str

    @model_validator(mode="after")
    def exact_deletion(self):
        if len(self.expected) != self.delete_count:
            raise ValueError("expected length must equal delete_count")
        return self


class SaveItineraryInput(StrictModel):
    base_version_id: str | None = Field(default=None, description="Current published version ID.")
    retry_from_call_id: str | None = Field(
        default=None,
        min_length=1,
        description="To repair the latest failed save_itinerary in THIS run, copy its "
        "tool result call_id and supply patch or argument_edits plus change_reason. "
        "The server rebuilds "
        "that rejected candidate from durable history and reruns all validation/protection "
        "checks. This also works before the first version exists; keep base_version_id "
        "equal to the current published version (null for first save). Never reference "
        "an older run or a successful save. Omitted sources/releases are inherited.",
    )
    document: ItineraryDocument | None = None
    patch: ItineraryPatch | None = Field(
        default=None,
        description=(
            "Preferred for local edits: send just changed fields/IDs "
            "rather than repeating the whole plan."
        ),
    )
    argument_edits: list[ArgumentEdit] | None = Field(
        default=None,
        min_length=1,
        description="Alternative to document/patch for JSON syntax errors: reference the latest "
        "failed save with retry_from_call_id and edit its candidate argument text by offsets. "
        "If that failure was itself a syntax repair, earlier edits are reapplied first; "
        "use positions from the latest json_error. "
        "Use json_error.character_offset; line/column are one-based. Edits must not overlap. "
        "The server applies only explicit edits, parses JSON, then reruns all validation. "
        "For a missing closing brace at the end, insert it at character_count with delete_count=0 "
        "and expected=empty string. No guessing or bypass of locks/sources.",
    )
    change_reason: str = Field(min_length=1)
    available_source_ids: list[Identifier] = Field(
        default_factory=list,
        description=(
            "Import exact source records from available_sources in runtime context. "
            "Use the same IDs in source_ids/evidence; "
            "no manual copying of provider/time/URL required."
        ),
    )
    release_protections: list[ReleaseProtection] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_candidate(self):
        if sum(v is not None for v in (self.document, self.patch, self.argument_edits)) != 1:
            raise ValueError("supply exactly one of document, patch or argument_edits")
        if (
            self.retry_from_call_id is not None
            and self.patch is None
            and self.argument_edits is None
        ):
            raise ValueError("retry_from_call_id requires patch or argument_edits")
        if self.argument_edits is not None and not self.retry_from_call_id:
            raise ValueError("argument_edits requires retry_from_call_id")
        if self.patch is not None and not self.base_version_id and not self.retry_from_call_id:
            raise ValueError("patch requires current base_version_id")
        return self


SAVE_ITINERARY_TOOL = {
    "type": "function",
    "function": {
        "name": "save_itinerary",
        "description": "保存可交付行程或修改版本的草案，复用validate_itinerary结构；"
        "成功完成本轮才发布，追问/失败不会覆盖当前方案。"
        "修改时提供当前base_version_id，保持稳定条目ID和未涉及安排，"
        "保留用户条件和锁定/自报已订安排，锁定需引用真实用户原话。"
        "来源与查询条件应来自已查结果，费用total为全体人数/房间/间夜的归一化总额；"
        "未知费用为kind=unknown且total=null；reference/query_quote金额必须有来源。"
        "转场时间只计算前项结束到后项开始的间隙，已在交通块内的行驶时长不要重复计入。"
        "景点开放时段放opening_hours，不作为必须完整占用的fixed_commitments。"
        "保存失败时可用retry_from_call_id引用本轮最近失败结果的call_id，配合patch只修正"
        "出错条目，无需重写整份行程；首次失败也适用，全部校验和保护仍执行。"
        "JSON语法错误可依据json_error的位置用argument_edits仅修正原参数字符串。"
        "来源优先用available_source_ids导入；省略可选默认字段，避免重复抄写来源和描述。"
        "估计缓冲需明确标记。普通查询不必保存行程。",
        "parameters": SaveItineraryInput.model_json_schema(),
    },
}
