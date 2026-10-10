"""Precise JSON diagnostics without echoing inputs or repairing by guesswork."""

import json


class ArgumentJSONError(ValueError):
    def __init__(self, error, call_id):
        super().__init__("Tool arguments are not valid JSON.")
        self.details = {
            "source_call_id": call_id,
            "message": error.msg,
            "line": error.lineno,
            "column": error.colno,
            "character_offset": error.pos,
            "character_count": len(error.doc),
            "at_end": error.pos == len(error.doc),
            "offset_unit": "zero_based_unicode_characters",
        }


def parse_arguments(text, call_id):
    try:
        arguments = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArgumentJSONError(exc, call_id) from None
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")
    json.dumps(arguments, allow_nan=False)
    return arguments


def edit_arguments(text, edits):
    """Apply explicit non-overlapping edits against the immutable original."""
    ordered = sorted(edits, key=lambda edit: edit.start)
    end, last_start = 0, -1
    for edit in ordered:
        if (
            edit.start < end
            or edit.start == last_start
            or edit.start + edit.delete_count > len(text)
        ):
            raise ValueError("Argument edits overlap or lie outside the original text.")
        if text[edit.start : edit.start + edit.delete_count] != edit.expected:
            raise ValueError("Argument edit does not match the original text.")
        end, last_start = edit.start + edit.delete_count, edit.start
    for edit in reversed(ordered):
        text = text[: edit.start] + edit.insert + text[edit.start + edit.delete_count :]
    return text
