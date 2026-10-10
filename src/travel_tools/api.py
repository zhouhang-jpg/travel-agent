"""Local development HTTP surface. Bind to 127.0.0.1; no production auth layer yet."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from travel_agent.api import router as conversation_router
from travel_agent.durable_service import DurableService
from travel_agent.models import ModelError, OpenAICompatibleModel
from travel_agent.runner import RunLimits, repair_history
from travel_agent.runtime import RuntimeService
from travel_agent.saver_lifecycle import open_saver
from travel_agent.service import AgentService
from travel_agent.storage import ConversationStore
from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings, has_secret
from travel_tools.registry import DispatchRequest, ToolRegistry, ToolResult


def create_app(
    settings: Settings | None = None,
    registry: ToolRegistry | None = None,
    *,
    model=None,
    store: ConversationStore | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                httpx.AsyncClient(trust_env=False, follow_redirects=False)
            )
            config = settings or Settings()
            app.state.registry = registry or build_registry(config, client)
            if config.llm_provider == "deepseek":
                key, base_url, model_name = (
                    config.deepseek_api_key,
                    config.deepseek_base_url,
                    config.deepseek_model,
                )
            else:
                key, base_url, model_name = (
                    config.llm_api_key,
                    config.llm_base_url,
                    config.llm_model,
                )
            selected_model = model
            if selected_model is None and has_secret(key) and base_url and model_name:
                try:
                    selected_model = OpenAICompatibleModel(
                        api_key=key.get_secret_value(),
                        base_url=base_url,
                        model=model_name,
                        client=client,
                        provider=config.llm_provider,
                        thinking=config.llm_thinking,
                        reasoning_effort=config.llm_reasoning_effort,
                        timeout_seconds=config.llm_timeout_seconds,
                    )
                except ModelError:
                    selected_model = None
            database = store or ConversationStore(config.database_url)
            await database.initialize()
            await database.recover_runs(repair_history)
            limits = RunLimits(
                max_steps=config.agent_max_steps, max_run_seconds=config.agent_max_run_seconds
            )
            # SDK saver schema lifecycle is separate from application Alembic.
            saver = await stack.enter_async_context(open_saver(database.engine.url))
            legacy = AgentService(
                database,
                selected_model,
                app.state.registry,
                limits,
            )
            durable = DurableService(database, selected_model, app.state.registry, limits, saver)
            app.state.agent = RuntimeService(legacy, durable, config.agent_engine)
            await durable.recover()
            app.state.agent_health = {
                "model_configured": selected_model is not None,
                "model": model_name or "",
                "provider": config.llm_provider,
                "engine": config.agent_engine,
            }
            try:
                yield
            finally:
                await app.state.agent.close()
                await app.state.registry.close()
                if store is None:
                    await database.close()

    app = FastAPI(title="Travel Assistant", version="0.2.0", lifespan=lifespan)
    app.include_router(conversation_router)

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request: Request, exc: SQLAlchemyError):
        return JSONResponse(status_code=503, content={"detail": "会话存储暂时不可用，请稍后重试。"})

    @app.get("/health")
    async def health():
        return {"status": "ok", "scope": "agent_and_tools"}

    @app.get("/tools")
    async def catalog(request: Request):
        return request.app.state.registry.catalog()

    @app.get("/tools/model-definitions")
    async def model_definitions(request: Request):
        return request.app.state.registry.model_definitions()

    @app.post(
        "/tools/{tool_name}/call",
        response_model=ToolResult,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": DispatchRequest.model_json_schema()}},
            }
        },
    )
    async def call(tool_name: str, request: Request):
        body = bytearray()
        try:
            # Receiving the envelope has its own deadline: dispatch has not
            # started yet, and a stalled upload must not wait indefinitely.
            async with asyncio.timeout(request.app.state.registry.timeout_seconds):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 256 * 1024:
                        raise HTTPException(413, "Request exceeds the 256 KiB limit.")
                    body.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Request body receipt exceeded its deadline.") from None
        try:
            parsed = DispatchRequest.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Invalid JSON tool-call envelope.") from None
        return await request.app.state.registry.dispatch(
            tool_name, parsed.arguments, call_id=parsed.call_id, refresh=parsed.force_refresh
        )

    return app


app = create_app()
