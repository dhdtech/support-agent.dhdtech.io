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

from typing import Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.models.channels import SlackHandoffThread

logger = get_logger(__name__)


class SlackHandoffThreadRepository:
    """CRUD for the Slack thread a handoff notice opened."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_thread(self, channel_id: str, thread_ts: str) -> Optional[SlackHandoffThread]:
        """The thread an inbound operator reply belongs to, if we opened it."""
        try:
            return self.db.query(SlackHandoffThread).filter(
                SlackHandoffThread.channel_id == channel_id,
                SlackHandoffThread.thread_ts == thread_ts,
            ).first()
        except Exception as e:
            logger.error(f"Error getting Slack handoff thread: {str(e)}")
            return None

    def get_for_session(self, session_id: UUID | str) -> Optional[SlackHandoffThread]:
        """The thread this conversation is bridged to, if any."""
        try:
            if isinstance(session_id, str):
                session_id = UUID(session_id)
            return (
                self.db.query(SlackHandoffThread)
                .filter(SlackHandoffThread.session_id == session_id)
                .order_by(SlackHandoffThread.created_at.desc())
                .first()
            )
        except Exception as e:
            logger.error(f"Error getting Slack handoff thread for session: {str(e)}")
            return None

    def create(
        self,
        *,
        organization_id: UUID | str,
        channel_id: str,
        thread_ts: str,
        session_id: UUID | str,
    ) -> Optional[SlackHandoffThread]:
        """Store the mapping; None when this thread is already recorded."""
        try:
            if isinstance(organization_id, str):
                organization_id = UUID(organization_id)
            if isinstance(session_id, str):
                session_id = UUID(session_id)
            thread = SlackHandoffThread(
                organization_id=organization_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                session_id=session_id,
            )
            self.db.add(thread)
            self.db.commit()
            self.db.refresh(thread)
            return thread
        except IntegrityError:
            # A duplicate (channel_id, thread_ts) is the expected race, not a
            # failure: the thread is already bridged.
            self.db.rollback()
            logger.info(f"Slack handoff thread {channel_id}:{thread_ts} already stored")
            return None
        except Exception as e:
            self.db.rollback()
            logger.error(f"Error creating Slack handoff thread: {str(e)}")
            return None
