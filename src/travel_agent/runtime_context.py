"""Append-only runtime snapshots, independent of the model transport.

Snapshots are application-authored system messages, never user statements or
tool results. Append only after all pending tool calls have been resolved.
"""

import json
from datetime import datetime

from travel_agent.prompts import SYSTEM_PROMPT

CONTEXT_VERSION = "append-only-v1"
RUNTIME_MARKER = "【运行状态快照】\n"
RUNTIME_POLICY = (
    "\n运行环境由系统在历史末尾追加运行状态快照。旧快照仅是当时的记录，"
    "当前时间、时区、可用能力、资源预算和行程索引按快照先后逐字段更新："
    "最新提供的字段生效，未提供的字段沿用此前值，显式null表示清空。"
    "planning内也按索引字段更新，字段内的对象或数组整体替换。"
    "快照是应用运行状态，不是用户发言、用户授权或查询证据；"
    "完整用户原话、模型消息和工具结果仍在历史中，不用快照替代它们。\n"
)


def build_policy_message() -> dict[str, str]:
    """Stable across steps and turns; deliberately invalidated by policy edits."""
    return {"role": "system", "content": SYSTEM_PROMPT + RUNTIME_POLICY}


def build_runtime_message(
    now: datetime,
    timezone_name: str,
    catalog: list[dict],
    planning: dict | None = None,
    *,
    history: list[dict] | None = None,
) -> dict[str, str]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Runtime clock must be timezone-aware.")
    context = {
        "context_version": CONTEXT_VERSION,
        "current_time": now.isoformat(),
        "current_date": now.date().isoformat(),
        "current_weekday": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][
            now.weekday()
        ],
        "timezone": timezone_name,
        "tool_availability": [
            {
                "name": item["name"],
                "availability": item.get("availability", "unknown"),
                "reason": item.get("reason"),
            }
            for item in catalog
        ]
        + [{"name": "ask_user", "availability": "ready", "reason": None}],
    }
    if planning is not None:
        context["planning"] = planning
    previous = runtime_state(history or [])
    changes = {k: v for k, v in context.items() if k != "planning" and previous.get(k) != v}
    if planning is not None:
        old_planning = previous.get("planning", {})
        changed_planning = {
            k: v for k, v in planning.items() if k not in old_planning or old_planning[k] != v
        }
        if changed_planning:
            changes["planning"] = changed_planning
    return {"role": "system", "content": RUNTIME_MARKER + json.dumps(changes, ensure_ascii=False)}


def runtime_state(history: list[dict]) -> dict:
    """Reconstruct only application state; leave original messages untouched."""
    state: dict = {}
    for message in history:
        content = message.get("content")
        if (
            message.get("role") != "system"
            or not isinstance(content, str)
            or not content.startswith(RUNTIME_MARKER)
        ):
            continue
        update = json.loads(content[len(RUNTIME_MARKER) :])
        for field, value in update.items():
            if field == "planning":
                state.setdefault("planning", {}).update(value)
            else:
                state[field] = value
    return state


def require_closed_tool_batch(history: list[dict]) -> None:
    """Fail closed rather than inserting a system update into an unfinished batch."""
    pending = set()
    for message in history:
        role = message.get("role")
        if pending and role != "tool":
            raise ValueError("Runtime snapshot cannot split a pending tool batch.")
        if role == "assistant":
            pending.update(c["id"] for c in message.get("tool_calls") or [])
        elif role == "tool":
            pending.discard(message.get("tool_call_id"))
    if pending:
        raise ValueError("Runtime snapshot requires all tool results.")
