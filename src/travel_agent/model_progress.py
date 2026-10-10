"""Public phases derived from committed facts, never hidden reasoning."""

from travel_agent.tool_encoding import decode_tool_result


def model_phase(history):
    for message in reversed(history):
        if message.get("role") == "user":
            return "deciding"
        if message.get("role") != "tool":
            continue
        try:
            result = decode_tool_result(message["content"])
        except (ValueError, TypeError, KeyError):
            continue
        if result.get("tool_name") == "save_itinerary":
            return (
                "repairing_itinerary"
                if result.get("status") == "error"
                else "reviewing_saved_itinerary"
            )
        return "reviewing_results"
    return "deciding"
