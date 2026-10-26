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

import asyncio
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.channels import get_adapter
from app.channels.slack import slack_api
from app.core.config import settings
from app.core.logger import get_logger
from app.database import SessionLocal
from app.models.agent import agent_usergroup
from app.models.channels import ChannelAccount, ChannelType, SlackHandoffThread
from app.models.chat_history import ChatHistory
from app.models.session_to_agent import SessionStatus
from app.models.user import User, user_groups
from app.repositories.channels import ChannelAccountRepository
from app.repositories.chat import ChatRepository
from app.repositories.session_to_agent import SessionToAgentRepository
from app.repositories.slack_bridge import SlackHandoffThreadRepository
from app.repositories.user import UserRepository
from app.services.message_delivery import deliver_to_customer
from app.utils.sanitize import sanitize_message

logger = get_logger(__name__)

# The inbound poll. Slack pushes `message` events only when its Events API can
# reach the app; polling `conversations.replies` is the fallback that needs no
# Slack-side configuration. Five seconds is well inside the method's rate tier
# and one call per bridged thread per tick.
POLL_INTERVAL_SECONDS = 5
REPLIES_PAGE_LIMIT = 100

# Set the first time a tick finds no active account, so an idle poller logs at
# debug once rather than every five seconds.
_account_absent_logged = False


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


def _ts_after(ts: str, cursor: str) -> bool:
    """Whether a Slack ts is newer than the stored cursor.

    Slack ts values are decimal-second strings ('1720000001.000200'), so they
    compare numerically; any malformed value is treated as not newer.
    """
    try:
        return float(ts) > float(cursor)
    except (TypeError, ValueError):
        return False


def _is_operator_message(message: dict, bot_user_id: str) -> bool:
    """A plain, human-authored message — never our bot, a bot, or an event.

    Slack marks a bot's own posts with `bot_id` and an edit/delete/join with
    `subtype`; either one means there is nothing for the operator reply path.
    """
    if message.get('bot_id') or message.get('subtype'):
        return False
    user = message.get('user')
    if not user or (bot_user_id and user == bot_user_id):
        return False
    return True


def _already_delivered(db: Session, session_id, slack_ts: str) -> bool:
    """Whether this Slack message already became an agent message.

    An operator reply can arrive both as an event and through the poll, so the
    Slack ts is stored on the message the first path writes and the second path
    skips it. The read is served by ix_chat_history_session_created and covers
    only the agent messages of one conversation.
    """
    if not slack_ts:
        return False
    rows = (
        db.query(ChatHistory.attributes)
        .filter(
            ChatHistory.session_id == session_id,
            ChatHistory.message_type == 'agent',
        )
        .all()
    )
    return any((attributes or {}).get('slack_ts') == slack_ts
               for attributes, in rows)


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


def _fallback_local_user(db: Session, agent_id, organization_id):
    """An active user of the organization to attribute a takeover to.

    `users.info` only returns the operator's email when the Slack app holds
    `users:read.email`; without it the reply resolves to no user and the
    session stays AI-handled. This picks a stand-in deterministically: a member
    of a group the agent serves, else the oldest active user in the org. The
    organization filter is not optional — attributing the chat to another
    tenant's user would leak the conversation into their inbox.

    Returns (user, path), either possibly None/''.
    """
    if not organization_id:
        return None, ''
    active_in_org = db.query(User).filter(
        User.organization_id == organization_id,
        User.is_active.is_(True),
    )
    if agent_id:
        member = (
            active_in_org
            .join(user_groups, user_groups.c.user_id == User.id)
            .join(agent_usergroup, agent_usergroup.c.group_id == user_groups.c.group_id)
            .filter(agent_usergroup.c.agent_id == agent_id)
            .order_by(User.created_at.asc(), User.id.asc())
            .first()
        )
        if member is not None:
            return member, 'group member'
    oldest = active_in_org.order_by(
        User.created_at.asc(), User.id.asc()).first()
    if oldest is not None:
        return oldest, 'org fallback'
    return None, ''


async def handle_operator_reply(
    team_id: str,
    channel_id: str,
    thread_ts: str,
    slack_user_id: str,
    text: str,
    slack_ts: str = "",
) -> bool:
    """Background task: deliver an operator's Slack reply to the visitor.

    Opens its own session because the webhook has already acked inside Slack's
    3s budget. The reply is stored as an agent message, the session is taken
    over so the AI stops answering, and the text is pushed to the widget.

    Returns True once the reply is settled — stored and delivered, already
    stored under this `slack_ts`, or provably undeliverable (empty or unsafe
    text). False means a retry could still succeed, and the poller leaves its
    cursor where it was so the next tick tries again.
    """
    db = SessionLocal()
    try:
        reply = (text or '').strip()
        if not reply or not slack_user_id:
            return False
        account = ChannelAccountRepository(db).get_by_external_id(
            ChannelType.SLACK.value, team_id)
        if account is None or not account.is_active:
            return False
        thread = SlackHandoffThreadRepository(db).get_by_thread(channel_id, thread_ts)
        if thread is None:
            return False
        if _already_delivered(db, thread.session_id, slack_ts):
            logger.info(f"Slack bridge reply {slack_ts} already delivered; skipping")
            return True
        # ponytail: check-then-insert, not atomic — an event and a poll tick
        # landing in the same instant could both pass this check. A unique index
        # on (session_id, attributes->>'slack_ts') is the upgrade path.
        session_record = SessionToAgentRepository(db).get_session(thread.session_id)
        if session_record is None:
            return False

        local_user = None
        matched_by = ''
        try:
            profile = await get_adapter(ChannelType.SLACK.value).fetch_profile(
                account, slack_user_id)
            local_user = _matched_local_user(
                db, (profile or {}).get('email'), session_record.organization_id)
            matched_by = 'matched email' if local_user is not None else ''
        except Exception as e:
            logger.error(f"Slack bridge users.info failed: {str(e)}")

        if local_user is None:
            if session_record.user_id is not None:
                # Already claimed in the dashboard: keep that assignee rather
                # than overwriting it with a fallback picked from the org.
                local_user = UserRepository(db).get_user(session_record.user_id)
                matched_by = 'existing assignee'
            else:
                local_user, matched_by = _fallback_local_user(
                    db, session_record.agent_id, session_record.organization_id)
        if local_user is not None:
            logger.info(
                f"Slack bridge reply for session {thread.session_id} attributed to "
                f"user {local_user.id} ({matched_by})")
        else:
            logger.warning(
                f"Slack bridge reply for session {thread.session_id} has no active "
                f"user in org {session_record.organization_id}; takeover skipped")

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
            # Retrying cannot make unsafe text safe, so the poller may move on.
            logger.warning("Slack bridge reply stripped as unsafe; dropping it")
            return True
        message_data = {
            'message': message,
            'message_type': 'agent',
            'session_id': session_record.session_id,
            'organization_id': session_record.organization_id,
            'agent_id': session_record.agent_id,
            'customer_id': session_record.customer_id,
            'user_id': user_id,
        }
        if slack_ts:
            # Read by `_already_delivered` so the event and poll paths cannot
            # both deliver this reply.
            message_data['attributes'] = {'slack_ts': slack_ts}
        ChatRepository(db).create_message(message_data)

        result = await deliver_to_customer(db, session_record, {
            'message': message,
            'type': 'agent_message',
            'message_type': 'agent',
            'session_id': str(session_record.session_id),
            'agent_name': local_user.full_name if local_user else None,
        })
        if not result.ok:
            # The message is stored, so a retry would duplicate it; delivery is
            # best-effort exactly as it is for the event path.
            logger.warning(f"Slack bridge reply not delivered: {result.reason}")
        return True
    except Exception as e:
        logger.error(f"Slack bridge operator reply failed: {str(e)}")
        return False
    finally:
        db.close()


# --- inbound polling --------------------------------------------------------
# The Events API is the fast path; this is the path that works when Slack
# cannot reach the app at all. Both funnel into `handle_operator_reply`, and
# the stored `slack_ts` keeps a reply from being delivered twice.

def _poll_account(db: Session, organization_id):
    """The org's active Slack account and decrypted credentials, or (None, {})."""
    account = _active_slack_account(db, organization_id)
    if account is None:
        return None, {}
    try:
        return account, ChannelAccountRepository(db).get_credentials(account) or {}
    except Exception as e:
        logger.warning(f"Slack account {account.id} credentials unreadable: {str(e)}")
        return None, {}


def _log_account_absent_once(organization_id) -> None:
    global _account_absent_logged
    if not _account_absent_logged:
        logger.debug(
            f"Slack bridge poller idle: no active Slack account for org {organization_id}")
        _account_absent_logged = True


async def _poll_thread(
    db: Session,
    thread: SlackHandoffThread,
    *,
    team_id: str,
    token: str,
    bot_user_id: str,
) -> None:
    """Deliver every unhandled operator reply in one thread, then advance it.

    The cursor moves only past replies that are settled. A reply that returns
    False stops this thread's tick, so the next one retries it.
    """
    cursor = thread.last_processed_ts or thread.thread_ts
    data = await slack_api('conversations.replies', token, {
        'channel': thread.channel_id,
        'ts': thread.thread_ts,
        # `oldest` is exclusive by default, so the handoff notice itself (ts ==
        # thread_ts) and everything already handled stay out of the page.
        'oldest': cursor,
        'limit': REPLIES_PAGE_LIMIT,
    }, form=True)
    if not data.get('ok'):
        logger.warning(f"Slack bridge replies poll rejected: {data.get('error')}")
        return

    handled_ts = cursor
    for message in data.get('messages') or []:
        ts = str(message.get('ts') or '')
        if not ts or not _ts_after(ts, cursor):
            continue
        if not _is_operator_message(message, bot_user_id):
            handled_ts = ts
            continue
        text = (message.get('text') or '').strip()
        if not text:
            handled_ts = ts
            continue
        if not await handle_operator_reply(
                team_id, thread.channel_id, thread.thread_ts,
                str(message.get('user') or ''), text, ts):
            break
        handled_ts = ts

    if handled_ts != cursor:
        SlackHandoffThreadRepository(db).advance(thread, handled_ts)


async def poll_bridge_threads() -> None:
    """One poll tick: read every bridged thread in the bridge channel."""
    channel_id = settings.SLACK_BRIDGE_CHANNEL_ID
    if not channel_id:
        return
    db = SessionLocal()
    try:
        accounts: dict = {}
        # ponytail: every bridged thread is polled each tick, as specified —
        # idle threads accumulate, so a channel holding hundreds of them will
        # approach conversations.replies' rate limit. Narrow this list (closed
        # sessions, an age cut-off) if the channel ever gets that busy.
        for thread in SlackHandoffThreadRepository(db).list_for_channel(channel_id):
            organization_id = thread.organization_id
            if organization_id not in accounts:
                accounts[organization_id] = _poll_account(db, organization_id)
            account, credentials = accounts[organization_id]
            if account is None:
                _log_account_absent_once(organization_id)
                continue
            token = str(credentials.get('access_token') or '')
            if not token:
                continue
            try:
                await _poll_thread(
                    db, thread,
                    team_id=account.external_account_id,
                    token=token,
                    bot_user_id=str(credentials.get('bot_user_id') or ''),
                )
            except Exception as e:
                # One unreachable thread must not starve the rest of the tick.
                logger.error(f"Slack bridge thread poll failed: {str(e)}")
    finally:
        db.close()


async def run_bridge_poller_loop() -> None:
    """Poll the bridge channel for operator replies until the app shuts down.

    Inert — one debug line and a return — when the bridge is unconfigured. A
    failed tick is logged and swallowed: a Slack outage or a rotated token must
    never take the API down with it.
    """
    if not settings.SLACK_BRIDGE_CHANNEL_ID:
        logger.debug("Slack bridge poller idle: SLACK_BRIDGE_CHANNEL_ID is unset")
        return
    logger.info(f"Slack bridge poller started ({POLL_INTERVAL_SECONDS}s interval)")
    try:
        while True:
            try:
                await poll_bridge_threads()
            except Exception as e:
                logger.error(f"Slack bridge poller tick failed: {str(e)}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Slack bridge poller stopped")
        raise
