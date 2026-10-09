"""Local development HTTP surface. Bind to 127.0.0.1; no production auth layer yet."""

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings
from travel_tools.registry import DispatchRequest, ToolRegistry, ToolResult


def create_app(settings: Settings | None = None, registry: ToolRegistry | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            app.state.registry = registry or build_registry(settings or Settings(), client)
            yield

    app = FastAPI(title="Travel Agent Tools", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok", "scope": "tool_layer"}

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
            tool_name, parsed.arguments, call_id=parsed.call_id
        )

    return app


app = create_app()
