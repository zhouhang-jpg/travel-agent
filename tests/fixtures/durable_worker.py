"""Separate OS process used only by recovery acceptance tests, never production."""

import asyncio
import json
import os
import sys
from pathlib import Path

from travel_agent.models import ModelReply
from travel_tools.api import create_app
from travel_tools.common import StrictModel, ToolPayload, utc_now
from travel_tools.config import Settings
from travel_tools.registry import ToolRegistry, ToolSpec


async def main():
    options = json.loads(await asyncio.to_thread(Path(sys.argv[1]).read_text, encoding="utf-8"))

    def trace(value):
        with Path(options["trace"]).open("a", encoding="utf-8") as file:
            file.write(value + "\n")

    async def failpoint(name):
        if options["scenario"] == "parallel" and name == "tool_effect_saved:1":
            name = "parallel_fast_saved_before_slow"
        if name == options.get("crash"):
            trace("crash:" + name)
            os._exit(73)

    class Model:
        async def complete(self, messages, tools):
            trace("model")
            if options.get("crash") == "model_inflight":
                await asyncio.sleep(0.65)
            await failpoint("model_inflight")
            history = messages[1:]
            tool_count = len([m for m in history if m["role"] == "tool"])
            if options["scenario"] == "question" and tool_count < 2:
                name, arguments = "ask_user", {"message": f"第 {tool_count + 1} 个问题？"}
            elif options["scenario"] in {"query", "parallel"} and tool_count < 2:
                name, arguments = "probe", {}
            elif options["scenario"] == "budget":
                name, arguments = "probe", {}
            elif options["scenario"] == "plan" and tool_count == 0:
                name, arguments = (
                    "save_itinerary",
                    {
                        "change_reason": "独立进程版本恢复验证",
                        "document": {
                            "title": "恢复测试方案",
                            "planning_window": {
                                "start": "2026-10-12T08:00:00+08:00",
                                "end": "2026-10-12T18:00:00+08:00",
                            },
                            "items": [
                                {
                                    "id": "walk",
                                    "title": "待核实散步候选",
                                    "start": "2026-10-12T10:00:00+08:00",
                                    "end": "2026-10-12T11:00:00+08:00",
                                }
                            ],
                        },
                    },
                )
            else:
                if options["scenario"] == "parallel":
                    results = [m for m in history if m["role"] == "tool"]
                    assert [r["tool_call_id"] for r in results] == [
                        "repeated-provider-id",
                        "second-provider-id",
                    ]
                    assert all(json.loads(r["content"])["status"] == "ok" for r in results)
                return ModelReply(
                    {
                        "role": "assistant",
                        "content": "完成行程",
                        "reasoning_content": "private-reasoning",
                    },
                    "stop",
                    {},
                )
            calls = [
                {
                    "id": "repeated-provider-id",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ]
            if options["scenario"] in {"query", "parallel"}:
                calls.append({**calls[0], "id": "second-provider-id"})
                if options["scenario"] == "parallel":
                    calls[0]["function"] = {"name": name, "arguments": '{"value": 0}'}
                    calls[1]["function"] = {"name": name, "arguments": '{"value": 1}'}
            return ModelReply(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": calls,
                    "reasoning_content": "private-reasoning",
                    "opaque": {"preserve": [1, True, None]},
                },
                "tool_calls",
                {},
            )

    class Input(StrictModel):
        value: int = 0

    class Output(ToolPayload):
        value: str

    slow_started = asyncio.Event()

    async def probe(arguments):
        trace("tool")
        if options["scenario"] == "parallel":
            if arguments.value == 0:
                slow_started.set()
            else:
                await slow_started.wait()
            trace("tool:" + str(arguments.value))
            await asyncio.sleep(0.8 if arguments.value == 0 else 0.02)
        return Output(value=utc_now().isoformat())

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "probe",
            "只读测试查询",
            Input,
            Output,
            probe,
            "ready",
            execution="parallel_read" if options["scenario"] == "parallel" else "exclusive",
        )
    )
    app = create_app(
        Settings(
            _env_file=None,
            database_url=options["database_url"],
            agent_engine="langgraph",
            agent_max_steps=options.get("steps", 12),
            deepseek_api_key=None,
        ),
        registry,
        model=Model(),
    )
    async with app.router.lifespan_context(app):
        service = app.state.agent.durable
        service.runtime.failpoint = failpoint
        if options["mode"] != "recover":
            queue = await service.start(
                options["conversation_id"],
                options.get("content", "测试需求"),
                options["request_id"],
                options.get("question_id"),
            )
            while (await queue.get())["type"] != "done":
                pass
        else:
            await asyncio.gather(*list(service.tasks))
        public = await service.get(options["conversation_id"])
        await asyncio.to_thread(
            Path(options["output"]).write_text,
            json.dumps(public, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    asyncio.run(main())
