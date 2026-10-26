"""add slack handoff bridge threads

Revision ID: add_slack_bridge_001
Revises: idx_kq_org_status_001
Create Date: 2026-10-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_slack_bridge_001'
down_revision: Union[str, None] = 'idx_kq_org_status_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'slack_handoff_threads',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('channel_id', sa.String(), nullable=False),
        sa.Column('thread_ts', sa.String(), nullable=False),
        sa.Column('session_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['session_to_agents.session_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', 'thread_ts', name='uq_slack_handoff_thread'),
    )
    # Serves SlackHandoffThreadRepository.get_for_session — equality on
    # session_id, run on every handoff and every bridged visitor message. The
    # unique constraint above already serves the (channel_id, thread_ts) lookup.
    op.create_index(op.f('ix_slack_handoff_threads_session_id'),
                    'slack_handoff_threads', ['session_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_slack_handoff_threads_session_id'),
                  table_name='slack_handoff_threads')
    op.drop_table('slack_handoff_threads')
