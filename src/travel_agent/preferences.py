"""Explicit, local-user travel preferences and reviewable conversation proposals.

The owner/namespace boundary is storage scoping, not authentication. Model-origin
changes are staged separately and only a user-facing apply operation edits a profile.
"""

import asyncio
from copy import deepcopy
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, Field, StrictBool, StringConstraints, model_validator
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from travel_agent.storage import Base, Conversation
from travel_tools.common import StrictModel, utc_now
from travel_tools.schemas.itinerary import UserReference

OWNER_ID = "local-user"
NAMESPACE = "travel-preferences"
PreferenceId = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, pattern=r"^[a-z][a-z0-9_-]*$")
]
PreferenceText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class PreferenceProfile(Base):
    __tablename__ = "preference_profiles"
    owner_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    entries: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[str] = mapped_column(String(40))


class PreferenceConversationControl(Base):
    __tablename__ = "preference_conversation_controls"
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    use_saved: Mapped[bool] = mapped_column(Boolean, default=True)


class PreferenceProposal(Base):
    __tablename__ = "preference_proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(String(36))
    effect_key: Mapped[str] = mapped_column(String(200), unique=True)
    base_revision: Mapped[int] = mapped_column(Integer)
    patch: Mapped[dict] = mapped_column(JSON)
    user_reference: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_at: Mapped[str] = mapped_column(String(40))


class PreferenceValue(StrictModel):
    id: PreferenceId
    label: PreferenceText
    value: PreferenceText


class PreferenceSource(StrictModel):
    kind: Literal["manual", "conversation"]
    message_id: str | None = None
    quote: str | None = None
    conversation_id: str | None = None

    @model_validator(mode="after")
    def attributed(self):
        references = (self.message_id, self.quote, self.conversation_id)
        if self.kind == "manual" and any(value is not None for value in references):
            raise ValueError("manual preferences have no conversation source")
        if self.kind == "conversation" and not all(value and value.strip() for value in references):
            raise ValueError("conversation preferences need an exact attributed user quote")
        return self


class PreferenceEntry(PreferenceValue):
    updated_at: AwareDatetime
    source: PreferenceSource


class PreferenceChanges(StrictModel):
    base_revision: int = Field(strict=True, ge=0)
    upsert: list[PreferenceValue] = Field(default_factory=list)
    remove_ids: list[PreferenceId] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_changes(self):
        identifiers = [entry.id for entry in self.upsert]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("upsert IDs must be unique")
        if len(self.remove_ids) != len(set(self.remove_ids)):
            raise ValueError("remove_ids must be unique")
        if set(identifiers) & set(self.remove_ids):
            raise ValueError("an ID cannot be upserted and removed together")
        return self


class UpdatePreferencesInput(PreferenceChanges):
    enabled: StrictBool | None = None

    @model_validator(mode="after")
    def has_change(self):
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled must be a boolean when provided")
        if self.enabled is None and not self.upsert and not self.remove_ids:
            raise ValueError("supply a preference change or enabled setting")
        return self


class ProposeUserPreferencesInput(PreferenceChanges):
    user_reference: UserReference

    @model_validator(mode="after")
    def has_change(self):
        if not self.upsert and not self.remove_ids:
            raise ValueError("a proposal needs at least one change")
        return self


class ApplyPreferenceProposalInput(StrictModel):
    base_revision: int = Field(strict=True, ge=0)


class ConversationPreferencesInput(StrictModel):
    use_saved: StrictBool


class PreferenceConflict(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class PreferenceNotFound(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _profile(row):
    return {
        "owner_id": OWNER_ID,
        "revision": row.revision if row else 0,
        "enabled": row.enabled if row else True,
        "entries": deepcopy(row.entries) if row else [],
        "updated_at": row.updated_at if row else None,
    }


def public_proposal(row):
    return {
        field: deepcopy(getattr(row, field))
        for field in (
            "id",
            "owner_id",
            "conversation_id",
            "run_id",
            "effect_key",
            "base_revision",
            "patch",
            "user_reference",
            "status",
            "created_at",
        )
    }


async def stage_proposal(session, proposal):
    """Stage once in the same transaction as the fenced durable tool effect."""
    existing = await session.get(PreferenceProposal, proposal["id"])
    if existing:
        if any(
            getattr(existing, field) != proposal[field]
            for field in ("owner_id", "conversation_id", "run_id", "effect_key")
        ):
            raise PreferenceConflict("preference_proposal_conflict", "偏好建议执行标识冲突。")
        if existing.status == "pending" and (
            existing.patch != proposal["patch"]
            or existing.user_reference != proposal["user_reference"]
        ):
            raise PreferenceConflict(
                "preference_proposal_conflict", "同一执行不能生成不同偏好建议。"
            )
        return public_proposal(existing)
    if proposal["owner_id"] != OWNER_ID:
        raise PreferenceNotFound("preference_owner_not_found", "偏好归属不可用。")
    profile = await session.get(PreferenceProfile, OWNER_ID)
    revision = profile.revision if profile else 0
    saved = deepcopy(proposal)
    if saved["base_revision"] != revision:
        saved.update(status="stale", patch={})
    row = PreferenceProposal(**saved)
    session.add(row)
    return saved


class PreferenceService:
    def __init__(self, store):
        self.store = store
        # Runtime accept and all preference mutations share this singleton lock.
        self.operation_lock = asyncio.Lock()

    async def profile(self):
        async with self.store.sessions() as session:
            return _profile(await session.get(PreferenceProfile, OWNER_ID))

    @staticmethod
    async def _conversation(session, conversation_id):
        row = await session.get(Conversation, conversation_id)
        if row is None:
            raise PreferenceNotFound("preference_conversation_not_found", "会话不存在。")
        return row

    @staticmethod
    async def _idle(session, conversation_id=None):
        query = select(Conversation.id).where(Conversation.status == "running")
        if conversation_id is not None:
            query = query.where(Conversation.id == conversation_id)
        if (await session.scalar(query.limit(1))) is not None:
            raise PreferenceConflict(
                "preferences_busy", "Agent 正在执行，请等待本轮结束后修改相关偏好。"
            )

    async def conversation_control(self, conversation_id):
        async with self.store.sessions() as session:
            await self._conversation(session, conversation_id)
            row = await session.get(PreferenceConversationControl, conversation_id)
            return {"use_saved": row.use_saved if row else True}

    async def context(self, conversation_id):
        async with self.store.sessions() as session:
            await self._conversation(session, conversation_id)
            profile = _profile(await session.get(PreferenceProfile, OWNER_ID))
            control = await session.get(PreferenceConversationControl, conversation_id)
            use_saved = control.use_saved if control else True
            context = {
                "namespace": NAMESPACE,
                "owner_id": OWNER_ID,
                "revision": profile["revision"],
                "enabled": profile["enabled"],
                "use_saved": use_saved,
                "entries": [],
            }
            if profile["enabled"] and use_saved:
                context["entries"] = profile["entries"]
            return context

    async def set_conversation_control(self, conversation_id, use_saved):
        request = ConversationPreferencesInput(use_saved=use_saved)
        async with self.operation_lock, self.store.sessions.begin() as session:
            await self._conversation(session, conversation_id)
            await self._idle(session, conversation_id)
            current = await session.get(PreferenceConversationControl, conversation_id)
            if current is None:
                session.add(
                    PreferenceConversationControl(
                        conversation_id=conversation_id, use_saved=request.use_saved
                    )
                )
            else:
                result = await session.execute(
                    update(PreferenceConversationControl)
                    .where(
                        PreferenceConversationControl.conversation_id == conversation_id,
                        PreferenceConversationControl.use_saved == current.use_saved,
                    )
                    .values(use_saved=request.use_saved)
                )
                if result.rowcount != 1:
                    raise PreferenceConflict(
                        "preference_revision_conflict", "会话偏好已变化，请刷新。"
                    )
        return {"use_saved": request.use_saved}

    @staticmethod
    def _revision(row, expected):
        if (row.revision if row else 0) != expected:
            raise PreferenceConflict(
                "preference_revision_conflict", "偏好档案已变化，请刷新后再确认。"
            )

    @staticmethod
    def _entries(current, patch, source):
        entries = {entry["id"]: deepcopy(entry) for entry in current}
        for identifier in patch["remove_ids"]:
            entries.pop(identifier, None)
        now = utc_now().isoformat()
        for entry in patch["upsert"]:
            entries[entry["id"]] = {**entry, "updated_at": now, "source": deepcopy(source)}
        return list(entries.values())

    @staticmethod
    def _removals(current, identifiers):
        missing = set(identifiers) - {entry["id"] for entry in current}
        if missing:
            raise PreferenceConflict(
                "unknown_preference_targets", "删除的偏好ID不存在：" + "、".join(sorted(missing))
            )

    @staticmethod
    async def _invalidate_pending(session, *, excluding=None):
        query = update(PreferenceProposal).where(
            PreferenceProposal.owner_id == OWNER_ID, PreferenceProposal.status == "pending"
        )
        if excluding is not None:
            query = query.where(PreferenceProposal.id != excluding)
        await session.execute(query.values(status="stale", patch={}))

    async def _write(self, session, current, entries, enabled, *, excluding=None):
        snapshot = {
            "owner_id": OWNER_ID,
            "revision": (current.revision if current else 0) + 1,
            "entries": entries,
            "enabled": enabled,
            "updated_at": utc_now().isoformat(),
        }
        if current is None:
            session.add(PreferenceProfile(**snapshot))
        else:
            result = await session.execute(
                update(PreferenceProfile)
                .where(
                    PreferenceProfile.owner_id == OWNER_ID,
                    PreferenceProfile.revision == current.revision,
                )
                .values(**snapshot)
            )
            if result.rowcount != 1:
                raise PreferenceConflict("preference_revision_conflict", "偏好档案已变化，请刷新。")
        await self._invalidate_pending(session, excluding=excluding)
        return deepcopy(snapshot)

    async def update(self, arguments):
        request = (
            arguments
            if isinstance(arguments, UpdatePreferencesInput)
            else UpdatePreferencesInput.model_validate(arguments)
        )
        async with self.operation_lock, self.store.sessions.begin() as session:
            await self._idle(session)
            current = await session.get(PreferenceProfile, OWNER_ID)
            self._revision(current, request.base_revision)
            profile = _profile(current)
            self._removals(profile["entries"], request.remove_ids)
            entries = self._entries(
                profile["entries"], request.model_dump(mode="json"), {"kind": "manual"}
            )
            return await self._write(
                session,
                current,
                entries,
                profile["enabled"] if request.enabled is None else request.enabled,
            )

    async def clear(self, base_revision):
        request = ApplyPreferenceProposalInput(base_revision=base_revision)
        async with self.operation_lock, self.store.sessions.begin() as session:
            await self._idle(session)
            current = await session.get(PreferenceProfile, OWNER_ID)
            self._revision(current, request.base_revision)
            return await self._write(session, current, [], current.enabled if current else True)

    async def prepare_proposal(self, lease, key, arguments):
        from travel_agent.durable_storage import Journal

        request = ProposeUserPreferencesInput.model_validate(arguments)
        async with self.store.sessions() as session:
            conversation = await self._conversation(session, lease["conversation_id"])
            if conversation.active_run_id != lease["run_id"] or conversation.status != "running":
                raise PreferenceConflict("preference_proposal_conflict", "偏好建议不属于当前执行。")
            profile = await session.get(PreferenceProfile, OWNER_ID)
            self._revision(profile, request.base_revision)
            self._removals(profile.entries if profile else [], request.remove_ids)
            records = (
                await session.scalars(
                    select(Journal)
                    .where(Journal.conversation_id == lease["conversation_id"])
                    .order_by(Journal.seq.desc())
                )
            ).all()
            latest = next((r for r in records if r.payload.get("role") == "user"), None)
            reference = request.user_reference
            if (
                latest is None
                or latest.id != reference.message_id
                or not reference.quote.strip()
                or reference.quote not in latest.payload["content"]
            ):
                raise PreferenceConflict(
                    "invalid_preference_user_reference",
                    "偏好建议需要引用本会话最新用户消息中的真实原话。",
                )
        return {
            "id": str(uuid5(NAMESPACE_URL, key)),
            "owner_id": OWNER_ID,
            "conversation_id": lease["conversation_id"],
            "run_id": lease["run_id"],
            "effect_key": key,
            "base_revision": request.base_revision,
            "patch": request.model_dump(mode="json", include={"upsert", "remove_ids"}),
            "user_reference": reference.model_dump(mode="json"),
            "status": "pending",
            "created_at": utc_now().isoformat(),
        }

    async def pending(self, conversation_id):
        async with self.store.sessions() as session:
            await self._conversation(session, conversation_id)
            rows = (
                await session.scalars(
                    select(PreferenceProposal)
                    .where(
                        PreferenceProposal.owner_id == OWNER_ID,
                        PreferenceProposal.conversation_id == conversation_id,
                        PreferenceProposal.status == "pending",
                    )
                    .order_by(PreferenceProposal.created_at, PreferenceProposal.id)
                )
            ).all()
            return {"items": [public_proposal(row) for row in rows]}

    @staticmethod
    async def _proposal(session, conversation_id, proposal_id):
        row = await session.get(PreferenceProposal, proposal_id)
        if row is None or row.owner_id != OWNER_ID or row.conversation_id != conversation_id:
            raise PreferenceNotFound(
                "preference_proposal_not_found", "偏好建议不存在或不属于该会话。"
            )
        return row

    @staticmethod
    def _pending_proposal(row):
        if row.status != "pending":
            stale = row.status == "stale"
            raise PreferenceConflict(
                "preference_proposal_stale" if stale else "preference_proposal_closed",
                "偏好建议已过期，请重新核对。" if stale else "偏好建议已处理，不能重复应用。",
            )

    async def apply(self, conversation_id, proposal_id, base_revision):
        request = ApplyPreferenceProposalInput(base_revision=base_revision)
        async with self.operation_lock, self.store.sessions.begin() as session:
            await self._conversation(session, conversation_id)
            await self._idle(session)
            proposal = await self._proposal(session, conversation_id, proposal_id)
            self._pending_proposal(proposal)
            current = await session.get(PreferenceProfile, OWNER_ID)
            self._revision(current, request.base_revision)
            if proposal.base_revision != request.base_revision:
                raise PreferenceConflict(
                    "preference_proposal_stale", "偏好建议依据的档案版本已过期。"
                )
            patch = PreferenceChanges.model_validate(
                {"base_revision": request.base_revision, **proposal.patch}
            ).model_dump(mode="json")
            profile = _profile(current)
            self._removals(profile["entries"], patch["remove_ids"])
            entries = self._entries(
                profile["entries"],
                patch,
                {
                    "kind": "conversation",
                    "conversation_id": conversation_id,
                    **proposal.user_reference,
                },
            )
            result = await self._write(
                session, current, entries, profile["enabled"], excluding=proposal.id
            )
            changed = await session.execute(
                update(PreferenceProposal)
                .where(
                    PreferenceProposal.id == proposal.id,
                    PreferenceProposal.status == "pending",
                    PreferenceProposal.base_revision == request.base_revision,
                )
                .values(status="applied", patch={})
            )
            if changed.rowcount != 1:
                raise PreferenceConflict("preference_proposal_stale", "偏好建议已变化，请刷新。")
            return result

    async def dismiss(self, conversation_id, proposal_id):
        async with self.operation_lock, self.store.sessions.begin() as session:
            await self._conversation(session, conversation_id)
            await self._idle(session, conversation_id)
            proposal = await self._proposal(session, conversation_id, proposal_id)
            if proposal.status == "dismissed":
                return {"id": proposal.id, "status": "dismissed"}
            self._pending_proposal(proposal)
            result = await session.execute(
                update(PreferenceProposal)
                .where(
                    PreferenceProposal.id == proposal.id,
                    PreferenceProposal.status == "pending",
                )
                .values(status="dismissed", patch={})
            )
            if result.rowcount != 1:
                raise PreferenceConflict("preference_proposal_stale", "偏好建议已变化，请刷新。")
            return {"id": proposal.id, "status": "dismissed"}
