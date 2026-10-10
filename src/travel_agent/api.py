"""Conversation HTTP API. Run one application worker for this local MVP."""

import asyncio
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, ValidationError, field_validator

from travel_agent.durable_storage import RequestConflict
from travel_agent.service import ModelNotConfigured, stream_events
from travel_agent.storage import ConversationBusy, ConversationNotFound
from travel_tools.common import StrictModel

router = APIRouter()


class NewConversation(StrictModel):
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA timezone name.") from None
        return value


class UserMessage(StrictModel):
    content: str = Field(min_length=1, max_length=16000)
    request_id: str | None = Field(default=None, min_length=1, max_length=100)
    question_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message cannot be blank.")
        return value


async def read_payload(request: Request, schema):
    body = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    raise HTTPException(413, "Message envelope exceeds 64 KiB.")
    except TimeoutError:
        raise HTTPException(408, "Message upload timed out.") from None
    try:
        return schema.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "Invalid conversation request.") from None


@router.get("/agent/health")
async def agent_health(request: Request):
    return request.app.state.agent_health


@router.get("/conversations")
async def conversations(request: Request):
    return {"items": await request.app.state.agent.list()}


@router.post("/conversations", status_code=201)
async def new_conversation(request: Request):
    data = await read_payload(request, NewConversation)
    return await request.app.state.agent.store.create(data.timezone)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request):
    try:
        return await request.app.state.agent.get(conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found.") from None


@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, request: Request):
    data = await read_payload(request, UserMessage)
    try:
        queue = await request.app.state.agent.start(
            conversation_id, data.content, data.request_id, data.question_id
        )
    except ModelNotConfigured:
        raise HTTPException(503, "模型未配置，请在本地填写 DEEPSEEK_API_KEY 并重启服务。") from None
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found.") from None
    except ConversationBusy:
        raise HTTPException(409, "Agent 正在处理本轮需求，结束后才能发送下一条消息。") from None
    except RequestConflict:
        raise HTTPException(409, "请求标识或待回答问题不匹配，请刷新会话后重试。") from None
    return StreamingResponse(
        stream_events(queue),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations/{conversation_id}/events")
async def replay_events(conversation_id: str, request: Request, after: int = 0):
    service = request.app.state.agent
    if not hasattr(service, "replay"):
        raise HTTPException(409, "当前会话运行引擎不支持事件重放。")
    try:
        await service.get(conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found.") from None
    last_id = request.headers.get("Last-Event-ID")
    if last_id:
        try:
            after = max(after, int(last_id))
        except ValueError:
            raise HTTPException(422, "Invalid Last-Event-ID.") from None
    if after < 0:
        raise HTTPException(422, "Invalid event cursor.")
    return StreamingResponse(
        service.replay(conversation_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
