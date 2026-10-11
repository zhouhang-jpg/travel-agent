"""Local-user preferences require explicit, versioned user-side edits."""

import asyncio
import sqlite3
from copy import deepcopy

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select

from travel_agent.durable_storage import Journal
from travel_agent.preference_api import router
from travel_agent.preferences import (
    OWNER_ID,
    PreferenceConflict,
    PreferenceEntry,
    PreferenceNotFound,
    PreferenceProposal,
    PreferenceService,
    ProposeUserPreferencesInput,
    UpdatePreferencesInput,
    stage_proposal,
)
from travel_agent.storage import Conversation, ConversationStore


@pytest.fixture
async def preferences(tmp_path):
    store = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "preferences.db").as_posix())
    await store.initialize()
    try:
        yield PreferenceService(store)
    finally:
        await store.close()


def upsert(value="不坐飞机", *, base_revision=0, identifier="transport"):
    return {
        "base_revision": base_revision,
        "upsert": [
            {
                "id": identifier,
                "label": "交通偏好",
                "value": value,
            }
        ],
    }


async def conversation(preferences):
    return (await preferences.store.create("Asia/Shanghai"))["id"]


async def state(preferences, cid, status):
    async with preferences.store.sessions.begin() as session:
        row = await session.get(Conversation, cid)
        row.status = status
        row.active_run_id = "run-" + cid if status == "running" else None


async def prepared(preferences, cid, *, revision=0, key=None, message_id=None):
    await state(preferences, cid, "running")
    message_id = message_id or "user-" + cid
    async with preferences.store.sessions.begin() as session:
        if await session.get(Journal, message_id) is None:
            session.add(
                Journal(
                    id=message_id,
                    conversation_id=cid,
                    seq=1,
                    payload={"role": "user", "content": "以后出行都不坐飞机，请作为偏好保存。"},
                )
            )
    return await preferences.prepare_proposal(
        {"conversation_id": cid, "run_id": "run-" + cid},
        key or "effect-" + cid,
        {
            **upsert(base_revision=revision),
            "user_reference": {
                "message_id": message_id,
                "quote": "以后出行都不坐飞机",
            },
        },
    )


async def staged(preferences, cid, **fields):
    proposal = await prepared(preferences, cid, **fields)
    async with preferences.store.sessions.begin() as session:
        await stage_proposal(session, proposal)
    return proposal


async def test_profile_crud_revision_sources_and_delete_preserves_disabled(preferences):
    assert await preferences.profile() == {
        "owner_id": OWNER_ID,
        "revision": 0,
        "enabled": True,
        "entries": [],
        "updated_at": None,
    }
    cid = await conversation(preferences)
    profile = await preferences.update(upsert())
    assert profile["revision"] == 1 and profile["entries"][0]["source"] == {"kind": "manual"}
    PreferenceEntry.model_validate(profile["entries"][0])
    updated = await preferences.update(upsert("优先铁路", base_revision=1))
    assert updated["revision"] == 2 and len(updated["entries"]) == 1
    assert updated["entries"][0]["id"] == "transport"
    assert (await preferences.context(cid))["entries"][0]["value"] == "优先铁路"
    disabled = await preferences.update({"base_revision": 2, "enabled": False})
    assert disabled["revision"] == 3
    assert (await preferences.context(cid))["entries"] == []
    cleared = await preferences.clear(3)
    assert cleared["entries"] == [] and cleared["enabled"] is False
    assert cleared["revision"] == 4


async def test_cas_prevents_overwrite_including_concurrent_initial_edits(preferences):
    results = await asyncio.gather(
        preferences.update(upsert()),
        preferences.update(upsert("优先经济舱")),
        return_exceptions=True,
    )
    assert len([r for r in results if isinstance(r, dict)]) == 1
    failed = next(r for r in results if isinstance(r, PreferenceConflict))
    assert failed.code == "preference_revision_conflict"
    before = await preferences.profile()
    with pytest.raises(PreferenceConflict, match="偏好档案已变化"):
        await preferences.clear(0)
    assert await preferences.profile() == before


async def test_cross_conversation_profiles_and_per_chat_opt_out(preferences):
    first, second = await conversation(preferences), await conversation(preferences)
    await preferences.update(upsert())
    assert (await preferences.context(first))["entries"] == (await preferences.context(second))[
        "entries"
    ]
    await preferences.set_conversation_control(first, False)
    off = await preferences.context(first)
    assert off["use_saved"] is False and off["enabled"] is True and off["entries"] == []
    assert (await preferences.context(second))["entries"]
    await preferences.set_conversation_control(first, True)
    assert (await preferences.context(first))["entries"]
    with pytest.raises(PreferenceNotFound):
        await preferences.context("missing")


async def test_busy_boundary_global_profile_but_independent_chat_controls(preferences):
    first, second = await conversation(preferences), await conversation(preferences)
    await state(preferences, first, "running")
    with pytest.raises(PreferenceConflict) as failure:
        await preferences.update(upsert())
    assert failure.value.code == "preferences_busy"
    with pytest.raises(PreferenceConflict):
        await preferences.clear(0)
    with pytest.raises(PreferenceConflict):
        await preferences.set_conversation_control(first, False)
    assert await preferences.set_conversation_control(second, False) == {"use_saved": False}
    assert (await preferences.profile())["revision"] == 0


async def test_prepare_is_read_only_stage_is_idempotent_and_apply_is_explicit(preferences):
    cid = await conversation(preferences)
    proposal = await prepared(preferences, cid)
    assert await preferences.pending(cid) == {"items": []}
    assert (await preferences.profile())["entries"] == []
    async with preferences.store.sessions.begin() as session:
        one = await stage_proposal(session, proposal)
    async with preferences.store.sessions.begin() as session:
        two = await stage_proposal(session, {**proposal, "created_at": "2099-01-01T00:00:00Z"})
    assert one["id"] == two["id"] and two["created_at"] == proposal["created_at"]
    assert len((await preferences.pending(cid))["items"]) == 1
    with pytest.raises(PreferenceConflict) as failure:
        await preferences.apply(cid, proposal["id"], 0)
    assert failure.value.code == "preferences_busy"
    await state(preferences, cid, "completed")
    saved = await preferences.apply(cid, proposal["id"], 0)
    assert saved["revision"] == 1
    entry = saved["entries"][0]
    assert entry["source"] == {
        "kind": "conversation",
        "conversation_id": cid,
        "message_id": "user-" + cid,
        "quote": "以后出行都不坐飞机",
    }
    PreferenceEntry.model_validate(entry)
    assert await preferences.pending(cid) == {"items": []}
    with pytest.raises(PreferenceConflict) as failure:
        await preferences.apply(cid, proposal["id"], 1)
    assert failure.value.code == "preference_proposal_closed"
    async with preferences.store.sessions.begin() as session:
        replay = await stage_proposal(session, proposal)
        assert replay["status"] == "applied" and replay["patch"] == {}
    assert (await preferences.profile())["revision"] == 1


@pytest.mark.parametrize("case", ["wrong_conversation", "not_latest", "fake_quote", "wrong_run"])
async def test_proposal_requires_latest_real_user_quote_and_running_lease(preferences, case):
    cid, other = await conversation(preferences), await conversation(preferences)
    proposal = await prepared(preferences, cid)
    arguments = {
        **proposal["patch"],
        "base_revision": 0,
        "user_reference": deepcopy(proposal["user_reference"]),
    }
    lease = {"conversation_id": cid, "run_id": "run-" + cid}
    if case == "wrong_conversation":
        other_proposal = await prepared(preferences, other)
        arguments["user_reference"] = other_proposal["user_reference"]
    elif case == "not_latest":
        async with preferences.store.sessions.begin() as session:
            session.add(
                Journal(
                    id="new-user",
                    conversation_id=cid,
                    seq=2,
                    payload={"role": "user", "content": "暂时不保存"},
                )
            )
    elif case == "fake_quote":
        arguments["user_reference"]["quote"] = "每次都坐飞机"
    elif case == "wrong_run":
        lease["run_id"] = "unrelated-run"
    with pytest.raises(PreferenceConflict):
        await preferences.prepare_proposal(lease, "new-effect", arguments)
    assert (await preferences.profile())["entries"] == []


async def test_profile_edits_stale_and_erase_pending_patches_across_chats(preferences):
    first, second = await conversation(preferences), await conversation(preferences)
    a = await staged(preferences, first)
    b = await staged(preferences, second)
    await state(preferences, first, "completed")
    await state(preferences, second, "completed")
    await preferences.update(upsert("优先铁路"))
    for cid, proposal in ((first, a), (second, b)):
        assert await preferences.pending(cid) == {"items": []}
        with pytest.raises(PreferenceConflict) as failure:
            await preferences.apply(cid, proposal["id"], 1)
        assert failure.value.code == "preference_proposal_stale"
    async with preferences.store.sessions() as session:
        rows = (await session.scalars(select(PreferenceProposal))).all()
        assert all(row.status == "stale" and row.patch == {} for row in rows)
    await preferences.clear(1)
    async with preferences.store.sessions() as session:
        raw_users = (await session.scalars(select(Journal))).all()
        assert len(raw_users) == 2  # Clearing the profile does not delete original conversations.


async def test_apply_invalidates_other_proposals_and_dismiss_is_local(preferences):
    first, second = await conversation(preferences), await conversation(preferences)
    a = await staged(preferences, first)
    b = await staged(preferences, second)
    await state(preferences, second, "completed")
    assert await preferences.dismiss(second, b["id"]) == {"id": b["id"], "status": "dismissed"}
    assert (await preferences.profile())["revision"] == 0
    await state(preferences, first, "completed")
    saved = await preferences.apply(first, a["id"], 0)
    assert saved["revision"] == 1
    with pytest.raises(PreferenceNotFound):
        await preferences.apply(second, a["id"], 1)


async def test_apply_stales_other_pending_proposals_without_resurrection(preferences):
    first, second = await conversation(preferences), await conversation(preferences)
    a = await staged(preferences, first)
    b = await staged(preferences, second)
    await state(preferences, first, "completed")
    await state(preferences, second, "completed")
    await preferences.apply(first, a["id"], 0)
    assert await preferences.pending(second) == {"items": []}
    with pytest.raises(PreferenceConflict) as failure:
        await preferences.apply(second, b["id"], 1)
    assert failure.value.code == "preference_proposal_stale"
    async with preferences.store.sessions() as session:
        other = await session.get(PreferenceProposal, b["id"])
        assert other.status == "stale" and other.patch == {}


async def test_clear_stales_and_erases_pending_patch_even_for_empty_profile(preferences):
    cid = await conversation(preferences)
    proposal = await staged(preferences, cid)
    await state(preferences, cid, "completed")
    cleared = await preferences.clear(0)
    assert cleared["revision"] == 1 and cleared["entries"] == []
    assert await preferences.pending(cid) == {"items": []}
    async with preferences.store.sessions() as session:
        row = await session.get(PreferenceProposal, proposal["id"])
        assert row.status == "stale" and row.patch == {}


async def test_newer_profile_invalidates_stage_and_wrong_source_identity(preferences):
    cid = await conversation(preferences)
    proposal = await prepared(preferences, cid)
    await state(preferences, cid, "completed")
    await preferences.update(upsert())
    async with preferences.store.sessions.begin() as session:
        stale = await stage_proposal(session, proposal)
    assert stale["status"] == "stale" and stale["patch"] == {}
    with pytest.raises(PreferenceNotFound):
        async with preferences.store.sessions.begin() as session:
            await stage_proposal(session, {**proposal, "id": "other", "owner_id": "someone-else"})


async def test_disabled_and_deleted_preferences_survive_service_restart(preferences):
    cid = await conversation(preferences)
    await preferences.update(upsert())
    await preferences.update({"base_revision": 1, "enabled": False})
    restarted = PreferenceService(preferences.store)
    assert (await restarted.profile())["entries"][0]["value"] == "不坐飞机"
    assert (await restarted.context(cid))["entries"] == []
    await restarted.clear(2)
    assert (await PreferenceService(preferences.store).profile())["entries"] == []


async def test_profile_and_chat_controls_survive_new_database_connection(preferences):
    cid = await conversation(preferences)
    saved = await preferences.update(upsert())
    await preferences.set_conversation_control(cid, False)
    reopened = ConversationStore(str(preferences.store.engine.url))
    await reopened.initialize()
    try:
        new_service = PreferenceService(reopened)
        assert await new_service.profile() == saved
        assert await new_service.conversation_control(cid) == {"use_saved": False}
        assert (await new_service.context(cid))["entries"] == []
    finally:
        await reopened.close()


async def test_unknown_removal_ids_are_rejected_and_existing_removal_succeeds(preferences):
    cid = await conversation(preferences)
    with pytest.raises(PreferenceConflict) as failure:
        await preferences.update({"base_revision": 0, "remove_ids": ["absent"]})
    assert failure.value.code == "unknown_preference_targets"
    assert (await preferences.profile())["revision"] == 0
    await preferences.update(upsert())
    proposal = await prepared(preferences, cid, revision=1)
    with pytest.raises(PreferenceConflict) as failure:
        await preferences.prepare_proposal(
            {"conversation_id": cid, "run_id": "run-" + cid},
            "remove-absent",
            {
                "base_revision": 1,
                "remove_ids": ["absent"],
                "user_reference": proposal["user_reference"],
            },
        )
    assert failure.value.code == "unknown_preference_targets"
    await state(preferences, cid, "completed")
    removed = await preferences.update({"base_revision": 1, "remove_ids": ["transport"]})
    assert removed["entries"] == [] and removed["revision"] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "false"},
        {"enabled": None},
        {"unknown": True},
        {"base_revision": "0"},
        {"upsert": [{"id": "中文", "label": "标签", "value": "值"}]},
        {"upsert": [{"id": "a", "label": "标签", "value": {"nested": True}}]},
        {"upsert": [{"id": "a", "label": "标签", "value": "  "}]},
        {"upsert": [{"id": "a", "label": "标签", "value": "值"}] * 2},
        {"remove_ids": ["a", "a"]},
        {"upsert": [{"id": "a", "label": "标签", "value": "值"}], "remove_ids": ["a"]},
    ],
)
def test_strict_changes_reject_invalid_or_ambiguous_fields(changes):
    with pytest.raises(ValidationError):
        UpdatePreferencesInput.model_validate({"base_revision": 0, **changes})


def test_empty_proposals_and_noop_profile_edits_rejected():
    with pytest.raises(ValidationError):
        UpdatePreferencesInput(base_revision=0)
    with pytest.raises(ValidationError):
        ProposeUserPreferencesInput.model_validate(
            {
                "base_revision": 0,
                "user_reference": {"message_id": "u", "quote": "保存"},
            }
        )


async def test_http_endpoints_crud_cas_and_safe_errors(preferences):
    app = FastAPI()
    app.state.preferences = preferences
    app.include_router(router)
    cid = await conversation(preferences)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/preferences")).json()["revision"] == 0
        changed = await client.put("/preferences", json=upsert())
        assert changed.status_code == 200 and changed.json()["revision"] == 1
        stale = await client.put("/preferences", json=upsert("覆盖旧值"))
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "preference_revision_conflict"
        assert (
            await client.put("/preferences", json={"base_revision": 1, "enabled": "false"})
        ).status_code == 422
        path = f"/conversations/{cid}/preferences"
        assert (await client.get(path)).json() == {"use_saved": True}
        assert (await client.put(path, json={"use_saved": False})).json() == {"use_saved": False}
        assert (await client.get("/conversations/other/preferences")).status_code == 404
        assert (await client.delete("/preferences", params={"base_revision": 0})).status_code == 409
        assert (await client.delete("/preferences", params={"base_revision": 1})).json()[
            "entries"
        ] == []
        proposal = await staged(preferences, cid, revision=2)
        proposals = await client.get(f"/conversations/{cid}/preference-proposals")
        assert proposals.json()["items"][0]["id"] == proposal["id"]
        apply_path = f"/conversations/{cid}/preference-proposals/{proposal['id']}/apply"
        assert (await client.post(apply_path, json={"base_revision": 2})).status_code == 409
        await state(preferences, cid, "completed")
        applied = await client.post(apply_path, json={"base_revision": 2})
        assert applied.status_code == 200 and applied.json()["revision"] == 3


def test_preferences_migration_upgrade_downgrade_preserves_old_conversation(tmp_path, monkeypatch):
    database = tmp_path / "preference-migration.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + database.as_posix())
    config = Config("alembic.ini")
    command.upgrade(config, "0003")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO conversations VALUES "
            "('old', '旧会话', 'idle', 'Asia/Shanghai', '', '', 0, NULL, '[]', '[]', NULL)"
        )
    command.upgrade(config, "0004")
    with sqlite3.connect(database) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "preference_profiles",
            "preference_conversation_controls",
            "preference_proposals",
        } <= names
        connection.execute(
            "INSERT INTO preference_profiles (owner_id, updated_at) VALUES ('local-user', 'now')"
        )
        assert connection.execute(
            "SELECT revision, enabled, entries FROM preference_profiles"
        ).fetchone() == (0, 1, "[]")
    command.downgrade(config, "0003")
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT title FROM conversations WHERE id='old'").fetchone()[0]
            == "旧会话"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='preference_profiles'"
            ).fetchone()
            is None
        )
    command.upgrade(config, "0004")
