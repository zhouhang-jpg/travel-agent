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


class SaveItineraryInput(StrictModel):
    base_version_id: str | None = Field(default=None, description="Current published version ID.")
    document: ItineraryDocument | None = None
    patch: ItineraryPatch | None = Field(
        default=None,
        description=(
            "Preferred for local edits: send just changed fields/IDs "
            "rather than repeating the whole plan."
        ),
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
        if (self.document is None) == (self.patch is None):
            raise ValueError("supply exactly one of document or patch")
        if self.patch is not None and not self.base_version_id:
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
        "未知费用为null，估计缓冲需明确标记。普通查询不必保存行程。",
        "parameters": SaveItineraryInput.model_json_schema(),
    },
}
