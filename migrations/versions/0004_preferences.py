"""Explicit local-user preference profile, per-chat controls and pending proposals."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "preference_profiles",
        sa.Column("owner_id", sa.String(80), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("entries", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "preference_conversation_controls",
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("use_saved", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "preference_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("effect_key", sa.String(200), nullable=False, unique=True),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("patch", sa.JSON(), nullable=False),
        sa.Column("user_reference", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    for field in ("owner_id", "conversation_id", "status"):
        op.create_index("ix_preference_proposals_" + field, "preference_proposals", [field])


def downgrade():
    op.drop_table("preference_proposals")
    op.drop_table("preference_conversation_controls")
    op.drop_table("preference_profiles")
