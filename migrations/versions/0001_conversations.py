"""Create persistent conversations with private model history and public transcript."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active_run_id", sa.String(36), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.JSON(), nullable=True),
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])


def downgrade():
    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.drop_table("conversations")
