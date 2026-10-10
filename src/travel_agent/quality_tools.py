"""Conversation-scoped, read-only reviews reuse the normal preparation boundary."""

from copy import deepcopy

from sqlalchemy import select

from travel_agent.durable_storage import Journal
from travel_agent.itineraries import ItineraryVersion, PlanConflict, evidence_review, source_bank
from travel_agent.itinerary_schema import ItineraryDocument
from travel_agent.plan_grounding import ReviewItineraryInput, review_itinerary
from travel_tools.schemas.plan_comparison import CompareItineraryOptionsInput


async def bind_comparison_protections(service, conversation_id, arguments):
    """Bind existing protected arrangements even when the model omits them.

    Other natural-language constraints remain the model's responsibility. This
    read-only comparison cannot release protections or authenticate its supplied
    supplier evidence, as stated in the comparison contract.
    """
    prepared = CompareItineraryOptionsInput.model_validate(arguments).model_dump(mode="json")
    current = await service.current(conversation_id)
    if current:
        basis = prepared.setdefault("basis", {})
        for section in ("items", "lodging"):
            field = "protected_" + section
            provided = {entry["id"]: entry for entry in basis.get(field, [])}
            provided.update(
                (entry["id"], deepcopy(entry))
                for entry in current.document[section]
                if entry.get("protection")
            )
            basis[field] = list(provided.values())
    return CompareItineraryOptionsInput.model_validate(prepared).model_dump(mode="json")


async def review_for_conversation(service, lease, key, arguments):
    """Check a candidate/current version without staging or publishing a version.

    Preparing a candidate pins actual query sources, normalizes quote scope and
    enforces the same user-reference/lock checks as saving. No durable effect or
    version is written here; the graph owns the read-only tool effect as usual.
    """
    previous = await service.current(lease["conversation_id"])
    version_id = arguments.get("base_version_id")
    document = arguments.get("document")
    if (version_id is not None) == (document is not None):
        raise ValueError("Supply a version ID or a candidate document, exclusively.")
    version = None
    if version_id is not None:
        # Reject malformed IDs/claims before passing them to database lookups.
        ReviewItineraryInput.model_validate(arguments)
    async with service.store.sessions() as session:
        if version_id is not None:
            version = await session.get(ItineraryVersion, version_id)
            if (
                version is None
                or version.conversation_id != lease["conversation_id"]
                or not (
                    (previous is not None and version.id == previous.id)
                    or (version.status == "draft" and version.run_id == lease["run_id"])
                )
            ):
                raise PlanConflict(
                    "unavailable_review_version",
                    "请检查当前已发布版本或本轮草案；版本必须属于当前会话。",
                )
            document = deepcopy(version.document)
        records = (
            await session.scalars(
                select(Journal)
                .where(Journal.conversation_id == lease["conversation_id"])
                .order_by(Journal.seq)
            )
        ).all()
    users = {
        record.id: {"content": record.payload["content"], "seq": record.seq}
        for record in records
        if record.payload.get("role") == "user"
    }
    bank = source_bank([record.payload for record in records])
    if version is not None:
        if arguments.get("available_source_ids"):
            raise ValueError("A saved version is immutable; import sources into a new candidate.")
        # A version was already accepted by the saving/authorization boundary.
        # Reapplying old-head protections here would reject a legitimately
        # released arrangement. Refresh evidence without rewriting that version.
        checked = ItineraryDocument.model_validate(document)
        validation = deepcopy(version.review)
        validation["evidence"] = evidence_review(checked, bank, users, version)
        if any(r["state"] != "observed" for r in validation["evidence"].values()):
            validation["status"] = "unknown"
        candidate = {"document": checked.model_dump(mode="json"), "review": validation}
        previous_document = checked
    else:
        candidate = await service.prepare(
            lease,
            key + "/read-only",
            {
                "base_version_id": previous.id if previous else None,
                "document": deepcopy(document),
                "available_source_ids": arguments.get("available_source_ids", []),
                "change_reason": "只读需求与结论检查",
            },
        )
        previous_document = (
            ItineraryDocument.model_validate(previous.document) if previous else None
        )
    # Validate the review request after preparation has resolved imported source
    # IDs. Preserve every other request field and reject unknown fields normally.
    prepared = {**deepcopy(arguments), "base_version_id": None, "document": candidate["document"]}
    request = ReviewItineraryInput.model_validate(prepared)
    report = review_itinerary(
        request.document,
        request.requirement_assessments,
        request.claims,
        users=users,
        source_reports=candidate["review"]["evidence"],
        bank=bank,
        previous_document=previous_document,
    )
    return {
        **report.model_dump(mode="json"),
        "checked_version_id": version_id,
        "candidate_validation": candidate["review"],
        "state": "read_only",
    }
