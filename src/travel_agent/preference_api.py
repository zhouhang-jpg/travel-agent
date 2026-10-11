"""Explicit editing and confirmation API for one local user's preferences."""

from fastapi import APIRouter, HTTPException, Query, Request

from travel_agent.preferences import (
    ApplyPreferenceProposalInput,
    ConversationPreferencesInput,
    PreferenceConflict,
    PreferenceNotFound,
    UpdatePreferencesInput,
)

router = APIRouter()


async def _call(operation):
    try:
        return await operation
    except PreferenceConflict as exc:
        raise HTTPException(409, {"code": exc.code, "message": exc.message}) from None
    except PreferenceNotFound as exc:
        raise HTTPException(404, {"code": exc.code, "message": exc.message}) from None


@router.get("/preferences")
async def profile(request: Request):
    return await request.app.state.preferences.profile()


@router.put("/preferences")
async def update_profile(data: UpdatePreferencesInput, request: Request):
    return await _call(request.app.state.preferences.update(data))


@router.delete("/preferences")
async def clear_profile(request: Request, base_revision: int = Query(ge=0)):
    return await _call(request.app.state.preferences.clear(base_revision))


@router.get("/conversations/{conversation_id}/preferences")
async def conversation_control(conversation_id: str, request: Request):
    return await _call(request.app.state.preferences.conversation_control(conversation_id))


@router.put("/conversations/{conversation_id}/preferences")
async def set_conversation_control(
    conversation_id: str, data: ConversationPreferencesInput, request: Request
):
    return await _call(
        request.app.state.preferences.set_conversation_control(conversation_id, data.use_saved)
    )


@router.get("/conversations/{conversation_id}/preference-proposals")
async def proposals(conversation_id: str, request: Request):
    return await _call(request.app.state.preferences.pending(conversation_id))


@router.post("/conversations/{conversation_id}/preference-proposals/{proposal_id}/apply")
async def apply(
    conversation_id: str, proposal_id: str, data: ApplyPreferenceProposalInput, request: Request
):
    return await _call(
        request.app.state.preferences.apply(conversation_id, proposal_id, data.base_revision)
    )


@router.post("/conversations/{conversation_id}/preference-proposals/{proposal_id}/dismiss")
async def dismiss(conversation_id: str, proposal_id: str, request: Request):
    return await _call(request.app.state.preferences.dismiss(conversation_id, proposal_id))
