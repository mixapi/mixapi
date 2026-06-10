"""store provider connection names in accounting records

Revision ID: 20260610_0002
Revises: 20260610_0001
Create Date: 2026-06-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260610_0002"
down_revision: Union[str, Sequence[str], None] = "20260610_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_events",
        sa.Column("provider_name", sa.String(length=200), nullable=True),
    )
    op.execute(
        "UPDATE usage_events SET provider_name = provider_protocol "
        "WHERE provider_name IS NULL"
    )
    op.alter_column("usage_events", "provider_name", nullable=False)
    op.add_column(
        "route_decisions",
        sa.Column("selected_provider_name", sa.String(length=200), nullable=True),
    )
    op.execute(
        "UPDATE route_decisions SET selected_provider_name = selected_provider_protocol "
        "WHERE selected_provider_name IS NULL"
    )


def downgrade() -> None:
    op.drop_column("route_decisions", "selected_provider_name")
    op.drop_column("usage_events", "provider_name")
