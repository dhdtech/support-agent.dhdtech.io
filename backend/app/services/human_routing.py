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
import asyncio
import json
from asyncio import sleep as _sleep
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.channels.constants import is_widget_channel
from app.core.config import settings
from app.core.logger import get_logger
from app.core.redis import get_redis
from app.database import get_db
from app.models.chat_history import ChatHistory
from app.models.session_to_agent import SessionStatus
from app.repositories.agent import AgentRepository
from app.repositories.chat import ChatRepository
from app.repositories.customer import CustomerRepository
from app.repositories.group import GroupRepository
from app.repositories.session_to_agent import SessionToAgentRepository
from app.repositories.user import UserRepository
from app.services.chat_notifications import UNASSIGNED_CHAT_VIEWERS
from app.services.contact_capture import build_handoff_contact_form
from app.services.message_delivery import deliver_to_customer
from app.services.notifications import ChatNotificationEvent, notify_chat_event
from app.services.slack_bridge import escape_slack_text, post_handoff_to_bridge

logger = get_logger(__name__)

SLACK_TIMEOUT_SECONDS = 10
LAST_MESSAGE_MAX_CHARS = 300

# The widget shows handover state in its own UI, so only external channels need
# a message spelling out what just happened.
WEB_CHANNEL = 'web'

# Sent when a chat is queued for the team but nobody has picked it up yet. The
# widget composes an availability-aware line of its own; channels get this.
QUEUED_FOR_HUMAN_NOTICE = (
    "I'm passing this to a member of our team. Someone will reply here shortly."
)

# First reply on an agent whose AI is switched off. Deliberately says nothing
# about transferring or passing anything on — there was no AI in the
# conversation to hand it over. It exists so the visitor is not left staring at
# an empty thread wondering whether the message arrived.
HUMAN_ONLY_ACK = (
    "Thanks for your message. Someone from our team will reply here shortly."
)


async def notify_customer(
    db: Session,
    session,
    message: str,
    user_id: Optional[str] = None,
    channels_only: bool = True,
) -> None:
    """Record a message in the thread and push it to the customer.

    Best-effort: a failed notice must never fail the action that triggered it.
    Skips the widget by default, which renders handover state in its own UI —
    pass channels_only=False for a message the visitor should actually read.
    """
    if channels_only and getattr(session, 'channel', None) in (None, WEB_CHANNEL):
        return
    try:
        ChatRepository(db).create_message({
            'message': message,
            'message_type': 'agent' if user_id else 'bot',
            'session_id': str(session.session_id),
            'organization_id': str(session.organization_id),
            'agent_id': str(session.agent_id) if session.agent_id else None,
            'customer_id': str(session.customer_id) if session.customer_id else None,
            'user_id': user_id,
            'attributes': {'channel': session.channel, 'handover_notice': True},
        })
        result = await deliver_to_customer(db, session, {
            'message': message,
            'type': 'chat_response',
        })
        if not result.ok:
            logger.warning(
                f"Handover notice not delivered on {session.channel}: {result.reason}")
    except Exception as e:
        logger.error(f"Failed sending handover notice: {str(e)}")


def _transfer_recipients(db: Session, session, group_id) -> list:
    """Who to tell that a chat is waiting.

    A group queue notifies that group; without one the chat belongs to nobody,
    so it goes to whoever could actually open it from the inbox.
    """
    if group_id:
        group = GroupRepository(db).get_group_with_users(str(group_id))
        users = group.users if group else []
    else:
        users = UserRepository(db).get_users_with_any_permission(
            session.organization_id, UNASSIGNED_CHAT_VIEWERS
        )
    return [user.id for user in users]


def _last_visitor_message(db: Session, session_id) -> str:
    """Most recent customer turn, decrypted, for the operator alert.

    Ordered by (created_at, id): the (session_id, created_at DESC, id DESC)
    index serves the order, and id breaks a same-second tie.
    """
    if isinstance(session_id, str):
        session_id = UUID(session_id)
    message = (
        db.query(ChatHistory.message)
        .filter(
            ChatHistory.session_id == session_id,
            ChatHistory.message_type == 'user',
        )
        .order_by(ChatHistory.created_at.desc(), ChatHistory.id.desc())
        .limit(1)
        .scalar()
    )
    return (message or '').strip()


def _handoff_slack_text(db: Session, session, reason, description) -> str:
    """Slack mrkdwn summary of a chat that is now waiting for a human."""
    agent_name = 'Unknown agent'
    if session.agent_id:
        agent = AgentRepository(db).get_agent(session.agent_id)
        if agent:
            agent_name = agent.name

    customer_label = 'Unknown customer'
    if session.customer_id:
        customer = CustomerRepository(db).get_by_id(session.customer_id)
        if customer:
            customer_label = customer.full_name or customer.email or 'Unknown customer'
            if customer.full_name and customer.email:
                customer_label = f"{customer.full_name} ({customer.email})"

    last_message = _last_visitor_message(db, session.session_id)
    if len(last_message) > LAST_MESSAGE_MAX_CHARS:
        last_message = last_message[:LAST_MESSAGE_MAX_CHARS].rstrip() + '…'
    if not last_message:
        last_message = '(no visitor message yet)'

    link = (
        f"{settings.FRONTEND_URL.rstrip('/')}/conversations"
        f"?session={session.session_id}"
    )
    lines = [
        "*Chat waiting for a human*",
        f"*Agent:* {escape_slack_text(agent_name)}",
        f"*Customer:* {escape_slack_text(customer_label)}",
        f"*Reason:* {escape_slack_text(reason) or 'not specified'}",
        f"*Last message:* {escape_slack_text(last_message)}",
    ]
    if description:
        lines.append(f"*Details:* {escape_slack_text(description)}")
    lines.append(f"<{link}|Open the conversation>")
    return "\n".join(lines)


async def notify_handoff_slack(
    db: Session,
    session,
    *,
    reason: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    """Post a handoff alert to Slack.

    Prefers the two-way bridge: posted with the bot token so its ts can be
    stored and operator replies routed back to the visitor. Falls back to the
    one-way incoming webhook when the bridge is unset or unavailable.

    Best-effort by design: an unset URL and bridge are a no-op, and a failure,
    an error status or a timeout is logged and swallowed. The handoff has
    already happened — an alert must never undo or delay its outcome.
    """
    if session is None:
        return
    bridge_channel = settings.SLACK_BRIDGE_CHANNEL_ID
    url = settings.SLACK_HANDOFF_WEBHOOK_URL
    if not bridge_channel and not url:
        logger.debug("No Slack bridge channel or handoff webhook configured; skipping the alert")
        return
    try:
        text = _handoff_slack_text(db, session, reason, description)
        if bridge_channel and await post_handoff_to_bridge(db, session, text):
            return
        if not url:
            return
        async with httpx.AsyncClient(timeout=SLACK_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json={"text": text})
        if response.status_code >= 400:
            logger.warning(
                f"Slack handoff alert rejected with HTTP {response.status_code}")
    except Exception as e:
        logger.error(f"Slack handoff alert failed: {e}")


async def route_session_to_human(
    db: Session,
    session,
    *,
    reason: Optional[str] = None,
    description: Optional[str] = None,
    group_id: Optional[str | UUID] = None,
    notify: bool = True,
) -> bool:
    """Take the AI out of a conversation and queue it for the team.

    `TRANSFERRED` with no assignee is the state every AI path already checks:
    the widget stops calling the model and answers with an availability line,
    and channels relay straight to the dashboard. So this needs no new status —
    it puts the session into the one the guards already understand.

    Returns False without touching anything when there is nothing to route: a
    human already holds it, it is closed, or it is queued already. That makes
    the call idempotent, so a double click cannot double-notify the team or
    send the customer the holding message twice.
    """
    if session is None:
        return False
    if session.user_id is not None:
        return False
    if session.status in (SessionStatus.CLOSED, SessionStatus.TRANSFERRED):
        return False

    updates = {
        'status': SessionStatus.TRANSFERRED,
        'transfer_reason': reason,
        'transfer_description': description,
    }
    # Only when a queue was named: passing None would clear a group the AI's
    # own transfer had already set.
    if group_id:
        updates['group_id'] = group_id

    if not SessionToAgentRepository(db).update_session(session.session_id, updates):
        return False

    if notify:
        recipients = _transfer_recipients(db, session, group_id or session.group_id)
        if recipients:
            await notify_chat_event(
                db=db,
                user_ids=recipients,
                event=ChatNotificationEvent.CHAT_TRANSFER,
                title="Chat waiting for a human",
                message=description or "A chat is waiting for someone to take it over.",
                metadata={
                    "session_id": str(session.session_id),
                    "transfer_reason": reason,
                    "transfer_description": description,
                },
            )

    await notify_customer(db, session, QUEUED_FOR_HUMAN_NOTICE)
    await notify_handoff_slack(db, session, reason=reason, description=description)
    return True


# How long the operator alert waits for the widget's handoff contact form. The
# form is the visitor's answer to "share your details", so the alert is held
# until it arrives — but a visitor who closes the widget must still reach
# somebody, hence the fallback. The stored record outlives the fallback so a
# slow submit cannot race an expired key into a second post.
HANDOFF_FALLBACK_DELAY_SECONDS = 120
PENDING_HANDOFF_TTL_SECONDS = 300

# Redis when it is on, because the submit handler can be served by a different
# worker than the transfer; a process-local dict otherwise.
_pending_handoffs: dict[str, dict] = {}
_fallback_tasks: set = set()


def _pending_handoff_key(session_id) -> str:
    return f"handoff:pending:{session_id}"


def _store_pending_handoff(session_id, pending: dict) -> None:
    client = get_redis()
    if client:
        try:
            client.setex(
                _pending_handoff_key(session_id),
                PENDING_HANDOFF_TTL_SECONDS,
                json.dumps(pending),
            )
            return
        except Exception as e:
            logger.warning(f"Redis unavailable for the pending handoff alert: {e}")
    _pending_handoffs[str(session_id)] = pending


def pop_pending_handoff(session_id) -> Optional[dict]:
    """Take the pending handoff for a session, if one is waiting.

    Exactly one caller can win, which is what keeps the fallback timer and the
    contact-form submit handler from posting the same alert twice.
    """
    session_key = str(session_id)
    client = get_redis()
    if client:
        try:
            # GET+DEL in one MULTI/EXEC. GETDEL would be shorter but needs a
            # Redis 6.2 server, and nothing in this repo pins one.
            pipe = client.pipeline(transaction=True)
            pipe.get(_pending_handoff_key(session_key))
            pipe.delete(_pending_handoff_key(session_key))
            raw = pipe.execute()[0]
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"Redis unavailable for the pending handoff alert: {e}")
    return _pending_handoffs.pop(session_key, None)


def _schedule_handoff_fallback(session_id) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "No running event loop; the handoff alert is only posted if the "
            "contact form arrives")
        return
    task = loop.create_task(_post_handoff_after_timeout(session_id))
    # A bare task can be garbage-collected mid-flight, and a plain thread-based
    # timer would hold the process open on shutdown — a loop task does neither.
    _fallback_tasks.add(task)
    task.add_done_callback(_fallback_tasks.discard)


async def _post_handoff_after_timeout(session_id) -> None:
    """Post the held alert when the contact form never arrives.

    Best-effort like the alert itself: any failure is logged and swallowed, so
    nothing a visitor does (or does not do) surfaces as a server error.
    """
    try:
        await _sleep(HANDOFF_FALLBACK_DELAY_SECONDS)
        pending = pop_pending_handoff(session_id)
        if not pending:
            return
        db = next(get_db())
        try:
            session = SessionToAgentRepository(db).get_session(session_id)
            if session is None:
                return
            await notify_handoff_slack(
                db,
                session,
                reason=pending.get('reason'),
                description=pending.get('description'),
            )
        finally:
            db.close()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Deferred handoff alert failed: {e}")


def _contact_form_will_be_shown(db, agent, customer_id, channel, is_workflow_transfer) -> bool:
    """Whether the widget will emit its handoff contact form for this transfer.

    Mirrors the emit site in widget_chat: widget surfaces only, the plain agent
    path (a workflow transfer returns before reaching the form block), and
    something still to ask for — decided by the same builder, so the two can
    never disagree about which fields are missing.
    """
    if is_workflow_transfer or not is_widget_channel(channel):
        return False
    if agent is None or not customer_id:
        return False
    customer = CustomerRepository(db).get_by_id(customer_id)
    if customer is None:
        return False
    return build_handoff_contact_form(
        customer,
        collect_email=getattr(agent, 'handoff_collect_email', True),
        collect_name=getattr(agent, 'handoff_collect_name', True),
    ) is not None


async def alert_handoff_after_transfer(
    db: Session,
    session,
    *,
    reason: Optional[str] = None,
    description: Optional[str] = None,
    agent=None,
    customer_id=None,
    channel: Optional[str] = None,
    is_workflow_transfer: bool = False,
) -> None:
    """Post the operator alert now, or hold it for the handoff contact form.

    The widget asks for the visitor's email/name *after* the response that ended
    in the handoff, so an alert posted at transfer time reads "Unknown customer"
    for someone who is about to type their name. When that form will be shown,
    the alert is held against the session and posted by the submit handler with
    the details in it.
    """
    if session is None:
        return
    try:
        hold = _contact_form_will_be_shown(db, agent, customer_id, channel, is_workflow_transfer)
    except Exception as e:
        # The handoff has already happened; a bad look at the customer row must
        # not turn it into an error. Post now, which is what always happened.
        logger.error(f"Could not tell whether the contact form will be shown: {e}")
        hold = False
    if hold:
        _store_pending_handoff(session.session_id, {
            'reason': reason,
            'description': description,
        })
        _schedule_handoff_fallback(session.session_id)
        return
    await notify_handoff_slack(db, session, reason=reason, description=description)
