import json

import pytest

from travel_agent.tool_encoding import decode_tool_result, encode_tool_result


def result(data, status="ok"):
    return {
        "tool_name": "search_trains",
        "status": status,
        "data": data,
        "content_trust": "data_not_instructions",
        "error": None,
    }


def test_all_quotes_and_evidence_round_trip_with_large_reduction():
    source = {
        "provider": "supplier",
        "url": "https://example.com/quote",
        "retrieved_at": "2026-10-09T12:00:00+08:00",
        "attribution": "reference only",
    }
    original = result(
        {
            "offers": [
                {
                    "id": f"offer-{index}",
                    "amount": str(index * 10),
                    "currency": "CNY",
                    "inventory_status": "unknown",
                    "tax_inclusion": "unknown",
                    "room_type": None,
                    "sources": [source],
                    "conditions": "Missing price is unknown; do not guess. " * 10,
                }
                for index in range(20)
            ],
            "warnings": ["No inventory guarantee."],
        }
    )
    encoded = encode_tool_result(original)
    assert len(encoded) < len(json.dumps(original, ensure_ascii=False)) * 0.4
    assert decode_tool_result(encoded) == original
    assert len(decode_tool_result(encoded)["data"]["offers"]) == 20


def test_tables_preserve_null_missing_types_nested_steps_and_conflicting_sources():
    original = result(
        {
            "routes": [
                {
                    "distance_m": i,
                    "duration_s": None,
                    "steps": [
                        {"mode": "walking", "instruction": f"Step {j}", "duration_s": j}
                        for j in range(20)
                    ],
                }
                for i in range(3)
            ],
            "mixed": [{"x": None}, {}, {"x": 0}, {"x": False}, {"x": 0.0}],
            "sources": [
                {"provider": "a", "url": "https://a"},
                {"provider": "b", "url": "https://b"},
            ],
            "typed": [
                {"value": True, "unknown": None},
                {"value": 1, "unknown": None},
                {"value": 1.0, "unknown": None},
            ],
        }
    )
    restored = decode_tool_result(encode_tool_result(original))
    assert restored == original
    assert [type(item["value"]) for item in restored["data"]["typed"]] == [bool, int, float]
    assert "x" not in restored["data"]["mixed"][1]


@pytest.mark.parametrize(
    "data,status",
    [
        ({"value": "small"}, "ok"),
        (None, "error"),
        ({"records": [{"$ref": "literal data"}] * 30}, "ok"),
        ({"records": [{"$rows": "literal data"}] * 30}, "ok"),
        ({"records": [{"$json": "literal data"}] * 30}, "ok"),
    ],
)
def test_small_errors_or_reserved_data_remain_plain(data, status):
    original = result(data, status)
    assert json.loads(encode_tool_result(original)) == original


def test_repeated_nested_sources_and_lists_are_not_mutated():
    shared = {"provider": "example", "url": "https://example.com/" + "x" * 120}
    original = result(
        {
            "sources": [shared] * 10,
            "records": [{"id": i, "sources": [shared]} for i in range(10)],
        }
    )
    snapshot = json.dumps(original)
    assert decode_tool_result(encode_tool_result(original)) == original
    assert json.dumps(original) == snapshot


def test_embedded_supplier_json_remains_exact_string_with_unknown_price():
    canonical = json.dumps(
        {
            "segments": [{"station": f"station-{i}", "inventory": None} for i in range(20)],
            "raw_price": "hidden",
            "raw_currency": None,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    pretty = json.dumps(json.loads(canonical), indent=2)
    original = result({"conditions": canonical, "opaque": pretty})
    encoded = encode_tool_result(original)
    assert '"$json"' in encoded
    assert decode_tool_result(encoded) == original
