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

"""Two-way Slack bridge for human handoffs.

The handoff notice is posted to one Slack channel with the workspace's bot
token so the returned `ts` can be stored; an operator replying in that thread
becomes an agent message in the originating chat, and the visitor's later
messages are mirrored into the same thread. The one-way incoming webhook stays
as the fallback whenever the bridge is unset or unavailable.

Slack contract, read from docs.slack.dev on 2026-10-26:
  - chat.postMessage: POST https://slack.com/api/chat.postMessage, JSON body
    accepted, bot scope `chat:write`, returns {ok, channel, ts, message};
    `thread_ts` is the parent message ts for a thread reply.
  - users.info: form-encoded, bot scope `users:read` for the call and
    `users:read.email` for the `email` field in the profile.
  - Events API `message`: public-channel delivery needs the `message.channels`
    event subscription and the `channels:history` bot scope.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.channels import get_adapter
from app.channels.slack import slack_api
from app.core.config import settings
from app.core.logger import get_logger
from app.database import SessionLocal
from app.models.channels import ChannelAccount, ChannelType, SlackHandoffThread
from app.models.session_to_agent import SessionStatus
from app.repositories.channels import ChannelAccountRepository
from app.repositories.chat import ChatRepository
from app.repositories.session_to_agent import SessionToAgentRepository
from app.repositories.slack_bridge import SlackHandoffThreadRepository
from app.repositories.user import UserRepository
from app.services.message_delivery import deliver_to_customer
from app.utils.sanitize import sanitize_message

logger = get_logger(__name__)


def escape_slack_text(value) -> str:
    """Neutralize mrkdwn/links in untrusted text before it reaches Slack.

    Both a visitor's message and their profile are rendered in the operator's
    Slack; without this, a message containing <https://evil|click here> would
    arrive as a live link.
    """
    return (
        str(value or '')
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def _active_slack_account(db: Session, organization_id) -> Optional[ChannelAccount]:
    """The organization's active Slack account — the one holding the bot token.

    A Slack workspace has a single bot token, so the first active account for
    the organization is the one that can post in the bridge channel.
    """
    if not organization_id:
        return None
    accounts = ChannelAccountRepository(db).list_by_org(
        organization_id, ChannelType.SLACK.value
    )
    return next((account for account in accounts if account.is_active), None)


def _bot_token(db: Session, account: ChannelAccount) -> str:
    credentials = ChannelAccountRepository(db).get_credentials(account) or {}
    return credentials.get('access_token') or ''


async def post_handoff_to_bridge(db: Session, session, text: str) -> bool:
    """Post the handoff notice top-level and remember the thread it opened.

    True once the bridge channel holds the notice — including the case where
    this session is already bridged, so a second call neither reposts nor lets
    the caller fall back to the webhook and duplicate the alert. False means
    "not bridged", and the caller uses the incoming webhook instead.
    """
    channel_id = settings.SLACK_BRIDGE_CHANNEL_ID
    if not channel_id or session is None:
        return False
    try:
        threads = SlackHandoffThreadRepository(db)
        if threads.get_for_session(session.session_id) is not None:
            return True
        account = _active_slack_account(db, session.organization_id)
        if account is None:
            logger.info(
                f"No active Slack account for org {session.organization_id}; handoff not bridged")
            return False
        token = _bot_token(db, account)
        if not token:
            logger.warning(f"Slack account {account.id} has no bot token; handoff not bridged")
            return False
        data = await slack_api('chat.postMessage', token, {
            'channel': channel_id,
            'text': text,
            # Never let Slack fetch an attacker-controlled URL in the summary.
            'unfurl_links': False,
        })
        if not data.get('ok') or not data.get('ts'):
            logger.warning(f"Slack bridge handoff rejected: {data.get('error')}")
            return False
        threads.create(
            organization_id=session.organization_id,
            channel_id=data.get('channel') or channel_id,
            thread_ts=str(data['ts']),
            session_id=session.session_id,
        )
        return True
    except Exception as e:
        logger.error(f"Slack bridge handoff failed: {str(e)}")
        return False


async def forward_visitor_message(db: Session, session_id: UUID | str, text: str) -> None:
    """Mirror a visitor message into the session's Slack thread, best-effort."""
    if not (text or '').strip():
        return
    try:
        thread = SlackHandoffThreadRepository(db).get_for_session(session_id)
        if thread is None:
            return
        account = _active_slack_account(db, thread.organization_id)
        if account is None:
            return
        token = _bot_token(db, account)
        if not token:
            return
        data = await slack_api('chat.postMessage', token, {
            'channel': thread.channel_id,
            'thread_ts': thread.thread_ts,
            'text': f"Visitor: {escape_slack_text(text)}",
            'unfurl_links': False,
        })
        if not data.get('ok'):
            logger.warning(f"Slack bridge visitor forward rejected: {data.get('error')}")
    except Exception as e:
        logger.error(f"Slack bridge visitor forward failed: {str(e)}")


def bridge_thread_for_event(db: Session, event: dict) -> Optional[SlackHandoffThread]:
    """The handoff thread an inbound Slack event answers, or None.

    Only a plain (non-bot, non-subtype) message posted inside a thread we opened
    qualifies. Edits, deletes, joins, bot echoes, top-level channel posts, other
    channels and unknown threads all return None, so the event continues down
    the existing customer-channel path untouched.
    """
    channel_id = settings.SLACK_BRIDGE_CHANNEL_ID
    if not channel_id:
        return None
    if event.get('type') != 'message' or event.get('bot_id') or event.get('subtype'):
        return None
    if event.get('channel') != channel_id:
        return None
    thread_ts = event.get('thread_ts')
    if not thread_ts or not (event.get('text') or '').strip():
        return None
    return SlackHandoffThreadRepository(db).get_by_thread(channel_id, thread_ts)


def _matched_local_user(db: Session, email: Optional[str], organization_id) -> Optional:
    """The organization's active user with this email, or None.

    Emails are globally unique, but this attributes a message and hands over a
    conversation: an address belonging to another organization must not do that.
    """
    if not email:
        return None
    user = UserRepository(db).get_user_by_email(email)
    if user is None or not user.is_active:
        return None
    if str(user.organization_id) != str(organization_id):
        return None
    return user


async def handle_operator_reply(
    team_id: str,
    channel_id: str,
    thread_ts: str,
    slack_user_id: str,
    text: str,
) -> None:
    """Background task: deliver an operator's Slack reply to the visitor.

    Opens its own session because the webhook has already acked inside Slack's
    3s budget. The reply is stored as an agent message, the session is taken
    over so the AI stops answering, and the text is pushed to the widget.
    """
    db = SessionLocal()
    try:
        reply = (text or '').strip()
        if not reply or not slack_user_id:
            return
        account = ChannelAccountRepository(db).get_by_external_id(
            ChannelType.SLACK.value, team_id)
        if account is None or not account.is_active:
            return
        thread = SlackHandoffThreadRepository(db).get_by_thread(channel_id, thread_ts)
        if thread is None:
            return
        session_record = SessionToAgentRepository(db).get_session(thread.session_id)
        if session_record is None:
            return

        local_user = None
        try:
            profile = await get_adapter(ChannelType.SLACK.value).fetch_profile(
                account, slack_user_id)
            local_user = _matched_local_user(
                db, (profile or {}).get('email'), session_record.organization_id)
        except Exception as e:
            logger.error(f"Slack bridge users.info failed: {str(e)}")

        user_id = str(local_user.id) if local_user else None
        if user_id:
            SessionToAgentRepository(db).assign_user(session_record.session_id, user_id)
        else:
            # No local user to attribute it to, but a human has answered:
            # TRANSFERRED with no assignee is the state that keeps the AI quiet.
            SessionToAgentRepository(db).update_session(
                session_record.session_id, {'status': SessionStatus.TRANSFERRED})

        # The widget renders this: strip anything unsafe rather than storing it.
        message = sanitize_message(reply)
        if not message or not message.strip():
            logger.warning("Slack bridge reply stripped as unsafe; dropping it")
            return
        ChatRepository(db).create_message({
            'message': message,
            'message_type': 'agent',
            'session_id': session_record.session_id,
            'organization_id': session_record.organization_id,
            'agent_id': session_record.agent_id,
            'customer_id': session_record.customer_id,
            'user_id': user_id,
        })

        result = await deliver_to_customer(db, session_record, {
            'message': message,
            'type': 'agent_message',
            'message_type': 'agent',
            'session_id': str(session_record.session_id),
            'agent_name': local_user.full_name if local_user else None,
        })
        if not result.ok:
            logger.warning(f"Slack bridge reply not delivered: {result.reason}")
    except Exception as e:
        logger.error(f"Slack bridge operator reply failed: {str(e)}")
    finally:
        db.close()
