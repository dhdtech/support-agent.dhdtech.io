"""add slack handoff thread last processed cursor

Revision ID: add_slack_bridge_poll_001
Revises: add_slack_bridge_001
Create Date: 2026-10-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_slack_bridge_poll_001'
down_revision: Union[str, None] = 'add_slack_bridge_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no default: catalog-only on PostgreSQL, no table rewrite and no
    # index — the column is read with its row, never filtered on.
    op.add_column(
        'slack_handoff_threads',
        sa.Column('last_processed_ts', sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('slack_handoff_threads', 'last_processed_ts')
