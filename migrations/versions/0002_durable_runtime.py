"""Durable run journal, idempotency, public events and canonical graph references."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_heads",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("engine", sa.String(40), primary_key=False, nullable=False),
        sa.Column("owner", sa.String(36), primary_key=False, nullable=True),
        sa.Column("epoch", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("version", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("cursor", sa.JSON(), primary_key=False, nullable=True),
        sa.Column("question_id", sa.String(36), primary_key=False, nullable=True),
        sa.Column("journal_seq", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("event_seq", sa.Integer(), primary_key=False, nullable=False),
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("conversation_id", sa.String(36), primary_key=False, nullable=False),
        sa.Column("request_id", sa.String(100), primary_key=False, nullable=False),
        sa.Column("status", sa.String(24), primary_key=False, nullable=False),
        sa.Column("model_requests", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("active_seconds", sa.Float(), primary_key=False, nullable=False),
        sa.Column("result", sa.JSON(), primary_key=False, nullable=True),
        sa.Column("resume", sa.JSON(), primary_key=False, nullable=True),
        sa.Column("runtime_spec", sa.JSON(), primary_key=False, nullable=True),
    )
    op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"])
    op.create_table(
        "agent_requests",
        sa.Column("id", sa.String(150), primary_key=True, nullable=False),
        sa.Column("payload_hash", sa.String(64), primary_key=False, nullable=False),
        sa.Column("run_id", sa.String(36), primary_key=False, nullable=False),
        sa.Column("start_seq", sa.Integer(), primary_key=False, nullable=False),
    )
    op.create_table(
        "agent_journal",
        sa.Column("id", sa.String(200), primary_key=True, nullable=False),
        sa.Column("conversation_id", sa.String(36), primary_key=False, nullable=False),
        sa.Column("seq", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("payload", sa.JSON(), primary_key=False, nullable=False),
    )
    op.create_index("ix_agent_journal_conversation_id", "agent_journal", ["conversation_id"])
    op.create_table(
        "agent_effects",
        sa.Column("id", sa.String(200), primary_key=True, nullable=False),
        sa.Column("run_id", sa.String(36), primary_key=False, nullable=False),
        sa.Column("kind", sa.String(24), primary_key=False, nullable=False),
        sa.Column("attempts", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("status", sa.String(24), primary_key=False, nullable=False),
        sa.Column("payload", sa.JSON(), primary_key=False, nullable=True),
        sa.Column("attempt_log", sa.JSON(), primary_key=False, nullable=False),
    )
    op.create_index("ix_agent_effects_run_id", "agent_effects", ["run_id"])
    op.create_table(
        "agent_questions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("conversation_id", sa.String(36), primary_key=False, nullable=False),
        sa.Column("run_id", sa.String(36), primary_key=False, nullable=False),
        sa.Column("effect_id", sa.String(200), primary_key=False, nullable=False),
        sa.Column("message", sa.String(), primary_key=False, nullable=False),
        sa.Column("status", sa.String(24), primary_key=False, nullable=False),
        sa.Column("checkpoint", sa.JSON(), primary_key=False, nullable=True),
        sa.Column("answered_run", sa.String(36), primary_key=False, nullable=True),
    )
    op.create_index("ix_agent_questions_conversation_id", "agent_questions", ["conversation_id"])
    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(200), primary_key=True, nullable=False),
        sa.Column("conversation_id", sa.String(36), primary_key=False, nullable=False),
        sa.Column("run_id", sa.String(36), primary_key=False, nullable=False),
        sa.Column("seq", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("payload", sa.JSON(), primary_key=False, nullable=False),
    )
    op.create_index("ix_agent_events_conversation_id", "agent_events", ["conversation_id"])
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])
    op.create_table(
        "agent_cursor_snapshots",
        sa.Column("id", sa.String(100), primary_key=True, nullable=False),
        sa.Column("config", sa.JSON(), primary_key=False, nullable=False),
    )


def downgrade():
    op.drop_table("agent_cursor_snapshots")
    op.drop_table("agent_events")
    op.drop_table("agent_questions")
    op.drop_table("agent_effects")
    op.drop_table("agent_journal")
    op.drop_table("agent_requests")
    op.drop_table("agent_runs")
    op.drop_table("agent_heads")
