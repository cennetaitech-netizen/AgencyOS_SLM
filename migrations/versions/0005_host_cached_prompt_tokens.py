"""host cached prompt tokens (prefix caching)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-23 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_usage_events", sa.Column("host_cached_prompt_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_usage_events", "host_cached_prompt_tokens")
