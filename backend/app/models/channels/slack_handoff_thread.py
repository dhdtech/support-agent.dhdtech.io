"""
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class SlackHandoffThread(Base):
    """The Slack thread a handoff notice opened, keyed to its chat session.

    An operator's reply is matched on (channel_id, thread_ts) and the visitor's
    later messages are posted back to the same pair, so this row is the only
    durable link between a Slack thread and the conversation it belongs to.
    """
    __tablename__ = 'slack_handoff_threads'
    __table_args__ = (
        UniqueConstraint('channel_id', 'thread_ts', name='uq_slack_handoff_thread'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey('organizations.id', ondelete='CASCADE'),
        nullable=False,
    )
    channel_id = Column(String, nullable=False)
    # Parent message ts of the handoff notice; Slack's thread_ts for a reply.
    thread_ts = Column(String, nullable=False)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey('session_to_agents.session_id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    # Slack ts of the last reply the poller handled. NULL means "nothing yet";
    # the poll starts from thread_ts in that case.
    last_processed_ts = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
