"""Lossless, readable tool data tables; never summarize or discard evidence."""

import json
from collections import Counter
from copy import deepcopy
from typing import Any

ENCODING = "travel-table-v1"
_MARKERS = {"$ref", "$rows", "$json"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _embedded_json(value: str) -> dict | list | None:
    # Only canonical strings can be reconstructed byte-for-byte. Other strings,
    # including supplier prose/HTML and differently formatted JSON, stay opaque.
    if len(value) < 80 or not value.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (dict, list)) and _json(parsed) == value:
            return parsed
    except (ValueError, TypeError):
        pass
    return None


def encode_tool_result(result: dict) -> str:
    """Encode only new successful results, retaining the ordinary envelope.

    Tables have exact common fields and positional rows. Shared values are local
    to this result, including provenance and unknowns. Use plain JSON if smaller.
    Reserved input keys cause a plain-JSON fallback to avoid marker ambiguity.
    """
    plain = _json(result)
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return plain
    counts: Counter[str] = Counter()
    reserved = False

    def scan(value: Any) -> None:
        nonlocal reserved
        if isinstance(value, dict):
            reserved |= bool(_MARKERS.intersection(value))
            key = _json(value)
            if len(key) >= 80:
                counts[key] += 1
            for child in value.values():
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)
        elif isinstance(value, str):
            if len(value) >= 120:
                counts[_json(value)] += 1
            embedded = _embedded_json(value)
            if embedded is not None:
                scan(embedded)

    scan(result["data"])
    if reserved:
        return plain
    shared: list[Any] = []
    indexes: dict[str, int] = {}

    def encode(value: Any, *, intern: bool = True) -> Any:
        if intern and isinstance(value, (dict, str)):
            key = _json(value)
            if counts[key] >= 3:
                if key not in indexes:
                    index = len(shared)
                    indexes[key] = index
                    shared.append(None)
                    shared[index] = encode(value, intern=False)
                return {"$ref": indexes[key]}
        if isinstance(value, dict):
            return {key: encode(child) for key, child in value.items()}
        if isinstance(value, list):
            children = [encode(child) for child in value]
            if (
                len(value) >= 3
                and all(isinstance(child, dict) for child in value)
                and all(list(child) == list(value[0]) for child in value)
            ):
                fields = list(value[0])
                common = {
                    key: encode(value[0][key])
                    for key in fields
                    if all(_json(child[key]) == _json(value[0][key]) for child in value[1:])
                }
                columns = [key for key in fields if key not in common]
                table = {
                    "common": common,
                    "fields": columns,
                    "count": len(value),
                    "$rows": [[encode(child[key]) for key in columns] for child in value],
                }
                if common:
                    # JSON-in-string evidence needs its original key order too.
                    table["order"] = fields
                if len(_json(table)) < len(_json(children)):
                    return table
            return children
        if isinstance(value, str):
            embedded = _embedded_json(value)
            if embedded is not None:
                representation = {"$json": encode(embedded)}
                if len(_json(representation)) < len(_json(value)):
                    return representation
        return value

    value = encode(result["data"])
    compact = deepcopy(result)
    compact["data"] = {"encoding": ENCODING, "shared": shared, "value": value}
    encoded = _json(compact)
    return encoded if len(encoded) < len(plain) else plain


def decode_tool_result(content: str) -> dict:
    """Recover every original field/value; useful for audit and regression checks."""
    result = json.loads(content)
    data = result.get("data")
    if not isinstance(data, dict) or data.get("encoding") != ENCODING:
        return result
    shared = data["shared"]

    def decode(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                return decode(shared[value["$ref"]])
            if set(value) == {"$json"}:
                return _json(decode(value["$json"]))
            if set(value) in (
                {"common", "fields", "count", "$rows"},
                {"common", "fields", "count", "$rows", "order"},
            ):
                if value["count"] != len(value["$rows"]):
                    raise ValueError("Tool table row count does not match its records.")
                common = decode(value["common"])
                records = [
                    {**deepcopy(common), **dict(zip(value["fields"], decode(row), strict=True))}
                    for row in value["$rows"]
                ]
                if "order" in value:
                    records = [{key: row[key] for key in value["order"]} for row in records]
                return records
            return {key: decode(child) for key, child in value.items()}
        if isinstance(value, list):
            return [decode(child) for child in value]
        return value

    result["data"] = decode(data["value"])
    return result
