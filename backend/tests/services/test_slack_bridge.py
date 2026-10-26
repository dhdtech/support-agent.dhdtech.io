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

The two-way Slack bridge: the handoff notice opens a thread, an operator reply
inside it reaches the visitor, and the visitor's later messages reach the
operator. Everything else in the bridge channel keeps the old behaviour.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main  # noqa: F401 — ensures routers are registered on the FastAPI app
from app.api import widget_chat
from app.core.application import app
from app.core.config import settings
from app.database import get_db
from app.models.chat_history import ChatHistory
from app.models.organization import Organization
from app.models.session_to_agent import (
    EndChatReasonType,
    SessionStatus,
    SessionToAgent,
)
from app.models.user import User, UserGroup
from app.repositories.channels import ChannelAccountRepository
from app.repositories.session_to_agent import SessionToAgentRepository
from app.repositories.slack_bridge import SlackHandoffThreadRepository
from app.services import slack_bridge
from app.services.human_routing import notify_handoff_slack
from app.services.message_delivery import DeliveryResult

BRIDGE = "C0BRIDGE"
TEAM = "T0B8FHAU9K6"
THREAD = "1720000000.000100"
WEBHOOK = "https://hooks.slack.test/services/T000/B000"
SECRET = "test-signing-secret"
ENDPOINT = "/api/v1/webhooks/slack"


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return _FakeResponse(self.status_code)


@pytest.fixture
def slack_account(db, test_organization):
    return ChannelAccountRepository(db).create_account(
        organization_id=test_organization.id,
        channel_type="slack",
        external_account_id=TEAM,
        credentials={"access_token": "xoxb-test"},
        display_name="Ops",
    )


@pytest.fixture
def bridge_session(db, test_agent, test_customer, test_organization):
    session = SessionToAgent(
        session_id=uuid4(),
        agent_id=test_agent.id,
        customer_id=test_customer.id,
        organization_id=test_organization.id,
        status=SessionStatus.OPEN,
        channel="web",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@pytest.fixture
def bridged(db, bridge_session, slack_account):
    return SlackHandoffThreadRepository(db).create(
        organization_id=bridge_session.organization_id,
        channel_id=BRIDGE,
        thread_ts=THREAD,
        session_id=bridge_session.session_id,
    )


def _reply_event(**overrides):
    event = {
        "type": "message",
        "channel": BRIDGE,
        "user": "U0OPERATOR",
        "text": "On it now",
        "ts": "1720000001.000200",
        "thread_ts": THREAD,
    }
    event.update(overrides)
    return {"type": "event_callback", "team_id": TEAM, "event_id": "Ev1", "event": event}


@pytest.fixture
def client(db):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        SECRET.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return client.post(ENDPOINT, content=body, headers={
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
        "content-type": "application/json",
    })


# --- outbound handoff -------------------------------------------------------

@pytest.mark.asyncio
async def test_handoff_posts_to_the_bridge_channel_and_records_the_thread(
        db, bridge_session, slack_account, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    monkeypatch.setattr(settings, "SLACK_HANDOFF_WEBHOOK_URL", "")
    post = AsyncMock(return_value={"ok": True, "channel": BRIDGE, "ts": THREAD})
    monkeypatch.setattr(slack_bridge, "slack_api", post)

    await notify_handoff_slack(db, bridge_session, reason="DIRECT_REQUEST")

    post.assert_awaited_once()
    method, token, payload = post.await_args.args
    assert method == "chat.postMessage"
    assert token == "xoxb-test"
    assert payload["channel"] == BRIDGE
    # Top-level message: no thread_ts, so Slack returns the parent ts.
    assert "thread_ts" not in payload

    thread = SlackHandoffThreadRepository(db).get_for_session(bridge_session.session_id)
    assert thread is not None
    assert thread.channel_id == BRIDGE
    assert thread.thread_ts == THREAD


@pytest.mark.asyncio
async def test_a_second_handoff_does_not_repost_or_duplicate_the_thread(
        db, bridge_session, slack_account, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    monkeypatch.setattr(settings, "SLACK_HANDOFF_WEBHOOK_URL", "")
    post = AsyncMock(return_value={"ok": True, "channel": BRIDGE, "ts": THREAD})
    monkeypatch.setattr(slack_bridge, "slack_api", post)

    await notify_handoff_slack(db, bridge_session, reason="DIRECT_REQUEST")
    await notify_handoff_slack(db, bridge_session, reason="DIRECT_REQUEST")

    assert post.await_count == 1
    assert db.query(slack_bridge.SlackHandoffThread).count() == 1


@pytest.mark.asyncio
async def test_handoff_falls_back_to_the_webhook_when_the_bridge_post_fails(
        db, bridge_session, slack_account, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    monkeypatch.setattr(settings, "SLACK_HANDOFF_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(slack_bridge, "slack_api",
                        AsyncMock(return_value={"ok": False, "error": "not_in_channel"}))
    client = _FakeClient()
    monkeypatch.setattr("app.services.human_routing.httpx.AsyncClient",
                        lambda **kwargs: client)

    await notify_handoff_slack(db, bridge_session, reason="DIRECT_REQUEST")

    assert len(client.posts) == 1
    assert client.posts[0][0] == WEBHOOK
    assert SlackHandoffThreadRepository(db).get_for_session(
        bridge_session.session_id) is None


@pytest.mark.asyncio
async def test_handoff_falls_back_to_the_webhook_without_a_slack_account(
        db, bridge_session, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    monkeypatch.setattr(settings, "SLACK_HANDOFF_WEBHOOK_URL", WEBHOOK)
    post = AsyncMock()
    monkeypatch.setattr(slack_bridge, "slack_api", post)
    client = _FakeClient()
    monkeypatch.setattr("app.services.human_routing.httpx.AsyncClient",
                        lambda **kwargs: client)

    await notify_handoff_slack(db, bridge_session, reason="DIRECT_REQUEST")

    post.assert_not_awaited()
    assert len(client.posts) == 1
    assert client.posts[0][0] == WEBHOOK


# --- inbound operator reply -------------------------------------------------

def _operator_adapter(email):
    adapter = MagicMock()
    adapter.fetch_profile = AsyncMock(return_value={"name": "Ops", "email": email})
    return adapter


async def _run_operator_reply(db, monkeypatch, email, text="On it now"):
    monkeypatch.setattr(slack_bridge, "SessionLocal", lambda: db)
    monkeypatch.setattr(slack_bridge, "get_adapter",
                        lambda *_: _operator_adapter(email))
    deliver = AsyncMock(return_value=DeliveryResult(ok=True))
    monkeypatch.setattr(slack_bridge, "deliver_to_customer", deliver)
    await slack_bridge.handle_operator_reply(
        TEAM, BRIDGE, THREAD, "U0OPERATOR", text)
    return deliver


@pytest.mark.asyncio
async def test_operator_reply_is_stored_taken_over_and_delivered(
        db, bridge_session, bridged, slack_account, test_user, monkeypatch):
    deliver = await _run_operator_reply(db, monkeypatch, test_user.email)

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id == test_user.id
    assert stored.status == SessionStatus.TRANSFERRED

    message = db.query(ChatHistory).filter(
        ChatHistory.session_id == bridge_session.session_id).one()
    assert message.message_type == "agent"
    assert message.user_id == test_user.id
    assert message.message == "On it now"

    deliver.assert_awaited_once()
    payload = deliver.await_args.args[2]
    assert payload["message"] == "On it now"
    assert payload["type"] == "agent_message"


@pytest.mark.asyncio
async def test_a_reply_with_no_local_user_at_all_is_delivered_and_skips_takeover(
        db, bridge_session, bridged, slack_account, monkeypatch, caplog):
    # The org has no active user, so there is nobody to take the chat over to.
    # The reply still reaches the visitor; only the assignment is skipped.
    with caplog.at_level(logging.WARNING, logger="app.services.slack_bridge"):
        deliver = await _run_operator_reply(db, monkeypatch, None)

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id is None
    assert stored.status == SessionStatus.TRANSFERRED

    message = db.query(ChatHistory).filter(
        ChatHistory.session_id == bridge_session.session_id).one()
    assert message.user_id is None
    deliver.assert_awaited_once()
    assert "takeover skipped" in caplog.text


def _org_user(db, organization, email, *, created_at, is_active=True):
    user = User(
        id=uuid4(),
        organization_id=organization.id,
        email=email,
        full_name=email,
        is_active=is_active,
        created_at=created_at,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _add_to_agent_group(db, agent, organization, user):
    group = UserGroup(name=f"Group {user.email}", organization_id=organization.id)
    db.add(group)
    db.commit()
    db.refresh(group)
    group.users.append(user)
    agent.groups.append(group)
    db.commit()
    return group


@pytest.mark.asyncio
async def test_a_matching_email_wins_over_the_fallback(
        db, bridge_session, bridged, slack_account, test_agent,
        test_organization, test_user, monkeypatch):
    # An older group member is what the fallback would pick; the email match
    # must still be the one attributed.
    member = _org_user(db, test_organization, "ops@example.com",
                       created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    _add_to_agent_group(db, test_agent, test_organization, member)

    await _run_operator_reply(db, monkeypatch, test_user.email)

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id == test_user.id
    message = db.query(ChatHistory).filter(
        ChatHistory.session_id == bridge_session.session_id).one()
    assert message.user_id == test_user.id


@pytest.mark.asyncio
async def test_no_email_falls_back_to_a_member_of_the_agents_group(
        db, bridge_session, bridged, slack_account, test_agent,
        test_organization, monkeypatch):
    _org_user(db, test_organization, "oldest@example.com",
              created_at=datetime(2019, 1, 1, tzinfo=timezone.utc))
    member = _org_user(db, test_organization, "ops@example.com",
                       created_at=datetime(2021, 1, 1, tzinfo=timezone.utc))
    _add_to_agent_group(db, test_agent, test_organization, member)

    await _run_operator_reply(db, monkeypatch, None)

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id == member.id
    assert stored.status == SessionStatus.TRANSFERRED


@pytest.mark.asyncio
async def test_no_group_member_falls_back_to_the_oldest_active_org_user(
        db, bridge_session, bridged, slack_account, test_organization, monkeypatch):
    _org_user(db, test_organization, "inactive@example.com", is_active=False,
              created_at=datetime(2018, 1, 1, tzinfo=timezone.utc))
    oldest = _org_user(db, test_organization, "oldest@example.com",
                       created_at=datetime(2019, 1, 1, tzinfo=timezone.utc))
    _org_user(db, test_organization, "newest@example.com",
              created_at=datetime(2021, 1, 1, tzinfo=timezone.utc))

    await _run_operator_reply(db, monkeypatch, None)

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id == oldest.id
    assert stored.status == SessionStatus.TRANSFERRED


@pytest.mark.asyncio
async def test_an_existing_assignee_is_not_overwritten_by_the_fallback(
        db, bridge_session, bridged, slack_account, test_organization,
        test_user, monkeypatch):
    _org_user(db, test_organization, "oldest@example.com",
              created_at=datetime(2019, 1, 1, tzinfo=timezone.utc))
    SessionToAgentRepository(db).assign_user(bridge_session.session_id, test_user.id)

    deliver = await _run_operator_reply(db, monkeypatch, None)

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id == test_user.id
    message = db.query(ChatHistory).filter(
        ChatHistory.session_id == bridge_session.session_id).one()
    assert message.user_id == test_user.id
    deliver.assert_awaited_once()


@pytest.fixture
def widget_sio():
    sio = MagicMock()
    sio.emit = AsyncMock()
    sio.get_session = AsyncMock()
    sio.get_environ = MagicMock(return_value={})
    sio.save_session = AsyncMock()
    return sio


@pytest.mark.asyncio
async def test_the_next_visitor_message_does_not_reach_the_ai_after_the_takeover(
        db, bridge_session, bridged, slack_account, test_widget,
        test_user, widget_sio, monkeypatch):
    # The production case: Slack returns no email, so the fallback takes the
    # chat over. The very next visitor message must be relayed to the human,
    # not answered by the AI.
    await _run_operator_reply(db, monkeypatch, None)

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id == test_user.id

    monkeypatch.setattr(widget_chat, "sio", widget_sio)
    monkeypatch.setattr(widget_chat, "get_db", lambda: iter([db]))
    monkeypatch.setattr(widget_chat, "notify_new_chat", AsyncMock())
    forward = AsyncMock()
    monkeypatch.setattr(widget_chat, "forward_visitor_message", forward)
    create_async = AsyncMock()
    monkeypatch.setattr(widget_chat.ChatAgent, "create_async", create_async)
    availability = AsyncMock()
    monkeypatch.setattr(widget_chat, "get_agent_availability_response", availability)
    widget_sio.get_session.return_value = {
        "widget_id": str(test_widget.id),
        "org_id": str(stored.organization_id),
        "agent_id": str(stored.agent_id),
        "customer_id": str(stored.customer_id),
        "session_id": str(stored.session_id),
        "conversation_token": "test-token",
    }
    # The real lookup binds the string customer id SQLite's UUID column cannot
    # take; the session fetched here is the one widget_chat would get.
    monkeypatch.setattr(
        SessionToAgentRepository, "get_active_customer_session",
        lambda self, customer_id, agent_id=None: stored)

    await widget_chat.handle_widget_chat("sid-1", {"message": "Are you still there?"})

    create_async.assert_not_awaited()
    availability.assert_not_awaited()
    forward.assert_awaited_once()
    events = [call[0][0] for call in widget_sio.emit.call_args_list]
    assert "bot_typing" not in events
    assert "chat_response" not in events
    assert "error" not in events
    assert "chat_reply" in events

    visitor_message = db.query(ChatHistory).filter(
        ChatHistory.session_id == bridge_session.session_id,
        ChatHistory.message_type == "user").one()
    assert visitor_message.user_id == test_user.id


@pytest.mark.asyncio
async def test_a_user_from_another_organization_is_not_attributed(
        db, bridge_session, bridged, slack_account, monkeypatch):
    other_org = Organization(name="Other Org", domain="other.example", timezone="UTC")
    db.add(other_org)
    db.commit()
    db.add(User(
        id=uuid4(),
        organization_id=other_org.id,
        email="ops@other.example",
        full_name="Other Ops",
        is_active=True,
    ))
    db.commit()

    await _run_operator_reply(db, monkeypatch, "ops@other.example")

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id is None
    message = db.query(ChatHistory).filter(
        ChatHistory.session_id == bridge_session.session_id).one()
    assert message.user_id is None


# --- event filtering --------------------------------------------------------

def test_only_a_plain_message_in_a_known_thread_is_a_bridge_reply(
        db, bridged, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    base = {"type": "message", "channel": BRIDGE, "thread_ts": THREAD,
            "user": "U0OPERATOR", "text": "On it now"}

    assert slack_bridge.bridge_thread_for_event(db, base) is not None
    assert slack_bridge.bridge_thread_for_event(db, {**base, "bot_id": "B1"}) is None
    assert slack_bridge.bridge_thread_for_event(
        db, {**base, "subtype": "message_changed"}) is None
    assert slack_bridge.bridge_thread_for_event(
        db, {**base, "channel": "C0OTHER"}) is None
    assert slack_bridge.bridge_thread_for_event(
        db, {**base, "thread_ts": "999.000"}) is None
    assert slack_bridge.bridge_thread_for_event(db, {**base, "thread_ts": None}) is None
    assert slack_bridge.bridge_thread_for_event(db, {**base, "type": "app_mention"}) is None


def test_the_bridge_is_inert_when_the_channel_is_unset(db, bridged, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", "")
    event = {"type": "message", "channel": BRIDGE, "thread_ts": THREAD,
             "user": "U0OPERATOR", "text": "On it now"}
    assert slack_bridge.bridge_thread_for_event(db, event) is None


def test_the_webhook_dispatches_an_operator_reply(
        db, client, bridged, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    handler = MagicMock()
    monkeypatch.setattr("app.api.webhooks.slack.handle_operator_reply", handler)

    response = _post(client, _reply_event())

    assert response.status_code == 200
    handler.assert_called_once_with(
        TEAM, BRIDGE, THREAD, "U0OPERATOR", "On it now", "1720000001.000200")


@pytest.mark.parametrize("event_overrides", [
    {"bot_id": "B1"},
    {"subtype": "message_changed"},
    {"channel": "C0OTHER"},
    {"thread_ts": "999.000"},
])
def test_the_webhook_ignores_everything_else_in_the_bridge_channel(
        db, client, bridged, monkeypatch, event_overrides):
    monkeypatch.setattr(settings, "SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    handler = MagicMock()
    monkeypatch.setattr("app.api.webhooks.slack.handle_operator_reply", handler)
    inbound = MagicMock()
    monkeypatch.setattr("app.api.webhooks.slack.process_channel_message", inbound)

    response = _post(client, _reply_event(**event_overrides))

    assert response.status_code == 200
    handler.assert_not_called()
    inbound.assert_not_called()


# --- visitor forwarding -----------------------------------------------------

@pytest.mark.asyncio
async def test_a_visitor_message_is_forwarded_into_the_thread(
        db, bridge_session, bridged, slack_account, monkeypatch):
    post = AsyncMock(return_value={"ok": True, "ts": "1720000002.000300"})
    monkeypatch.setattr(slack_bridge, "slack_api", post)

    await slack_bridge.forward_visitor_message(
        db, bridge_session.session_id, "Still there?")

    post.assert_awaited_once()
    method, token, payload = post.await_args.args
    assert method == "chat.postMessage"
    assert token == "xoxb-test"
    assert payload["channel"] == BRIDGE
    assert payload["thread_ts"] == THREAD
    assert "Still there?" in payload["text"]


@pytest.mark.asyncio
async def test_a_visitor_message_is_not_forwarded_without_a_thread(
        db, bridge_session, slack_account, monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(slack_bridge, "slack_api", post)

    await slack_bridge.forward_visitor_message(db, bridge_session.session_id, "Hello")

    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwarded_visitor_text_cannot_inject_a_slack_link(
        db, bridge_session, bridged, slack_account, monkeypatch):
    post = AsyncMock(return_value={"ok": True, "ts": "1720000002.000300"})
    monkeypatch.setattr(slack_bridge, "slack_api", post)

    await slack_bridge.forward_visitor_message(
        db, bridge_session.session_id, "<https://evil.example|click here>")

    text = post.await_args.args[2]["text"]
    assert "&lt;https://evil.example|click here&gt;" in text
    assert "<https://evil.example" not in text


# --- inbound polling fallback ----------------------------------------------
# Slack delivers nothing to the events endpoint in production, so the same
# replies are read from conversations.replies. The stored cursor and the
# slack_ts marker are what make that safe to retry and safe alongside events.

BOT_USER = "U0BOT"


def _poll_env(db, monkeypatch, messages, *, ok=True):
    """Point the poller at the test database and fake one replies page."""
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    monkeypatch.setattr(slack_bridge, "SessionLocal", lambda: db)
    calls = AsyncMock(return_value={"ok": ok, "messages": messages})
    monkeypatch.setattr(slack_bridge, "slack_api", calls)
    deliver = AsyncMock(return_value=DeliveryResult(ok=True))
    monkeypatch.setattr(slack_bridge, "deliver_to_customer", deliver)
    monkeypatch.setattr(slack_bridge, "get_adapter",
                        lambda *_: _operator_adapter(None))
    return calls, deliver


def _reply(ts="1720000001.000200", **overrides):
    message = {
        "type": "message",
        "user": "U0OPERATOR",
        "text": "On it now",
        "ts": ts,
        "thread_ts": THREAD,
    }
    message.update(overrides)
    return message


@pytest.mark.asyncio
async def test_a_polled_reply_is_delivered_and_advances_the_cursor(
        db, bridge_session, bridged, slack_account, monkeypatch):
    calls, deliver = _poll_env(db, monkeypatch, [_reply()])

    await slack_bridge.poll_bridge_threads()

    method, token, payload = calls.await_args.args
    assert method == "conversations.replies"
    assert token == "xoxb-test"
    # The first tick starts at the handoff notice and asks for one page.
    assert payload == {"channel": BRIDGE, "ts": THREAD, "oldest": THREAD,
                       "limit": 100}
    assert calls.await_args.kwargs == {"form": True}

    db.refresh(bridged)
    assert bridged.last_processed_ts == "1720000001.000200"

    message = db.query(ChatHistory).filter(
        ChatHistory.session_id == bridge_session.session_id).one()
    assert message.message_type == "agent"
    assert message.message == "On it now"
    assert message.attributes["slack_ts"] == "1720000001.000200"
    deliver.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_reply_leaves_the_cursor_for_the_next_tick(
        db, bridged, slack_account, monkeypatch):
    _poll_env(db, monkeypatch, [_reply()])
    handler = AsyncMock(return_value=False)
    monkeypatch.setattr(slack_bridge, "handle_operator_reply", handler)

    await slack_bridge.poll_bridge_threads()

    handler.assert_awaited_once()
    db.refresh(bridged)
    assert bridged.last_processed_ts is None
    assert db.query(ChatHistory).count() == 0


@pytest.mark.asyncio
async def test_bot_and_system_messages_are_ignored_but_do_not_block_the_thread(
        db, bridged, slack_account, monkeypatch):
    ChannelAccountRepository(db).update_credentials(
        slack_account, {"access_token": "xoxb-test", "bot_user_id": BOT_USER})
    handler = AsyncMock(return_value=True)
    _poll_env(db, monkeypatch, [
        _reply("1720000001.000100", bot_id="B1", user=BOT_USER, text="bot echo"),
        _reply("1720000001.000200", subtype="message_changed", text="edited"),
        _reply("1720000001.000300", user=BOT_USER, text="our own handoff"),
        _reply("1720000001.000400", text="   "),
    ])
    monkeypatch.setattr(slack_bridge, "handle_operator_reply", handler)

    await slack_bridge.poll_bridge_threads()

    handler.assert_not_awaited()
    assert db.query(ChatHistory).count() == 0
    db.refresh(bridged)
    # Skipped messages still move the cursor, or they would block it forever.
    assert bridged.last_processed_ts == "1720000001.000400"


@pytest.mark.asyncio
async def test_a_reply_delivered_by_event_is_not_delivered_again_by_the_poll(
        db, bridge_session, bridged, slack_account, monkeypatch):
    _poll_env(db, monkeypatch, [_reply()])

    # The event path got there first and recorded its Slack ts.
    await slack_bridge.handle_operator_reply(
        TEAM, BRIDGE, THREAD, "U0OPERATOR", "On it now", "1720000001.000200")
    assert db.query(ChatHistory).count() == 1

    await slack_bridge.poll_bridge_threads()

    assert db.query(ChatHistory).count() == 1
    db.refresh(bridged)
    assert bridged.last_processed_ts == "1720000001.000200"


@pytest.mark.asyncio
async def test_the_poll_is_a_no_op_when_unconfigured(
        db, bridged, slack_account, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", "")
    calls = AsyncMock()
    monkeypatch.setattr(slack_bridge, "slack_api", calls)

    await slack_bridge.poll_bridge_threads()

    calls.assert_not_awaited()
    db.refresh(bridged)
    assert bridged.last_processed_ts is None


@pytest.mark.asyncio
async def test_the_poll_is_a_no_op_without_an_active_account(
        db, bridged, slack_account, monkeypatch):
    ChannelAccountRepository(db).set_active(slack_account, False)
    calls = AsyncMock()
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    monkeypatch.setattr(slack_bridge, "SessionLocal", lambda: db)
    monkeypatch.setattr(slack_bridge, "slack_api", calls)

    await slack_bridge.poll_bridge_threads()

    calls.assert_not_awaited()
    db.refresh(bridged)
    assert bridged.last_processed_ts is None


@pytest.mark.asyncio
async def test_the_loop_returns_at_once_when_the_bridge_is_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", "")
    await asyncio.wait_for(slack_bridge.run_bridge_poller_loop(), timeout=1)


@pytest.mark.asyncio
async def test_a_failing_tick_is_swallowed_and_the_loop_keeps_running(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    monkeypatch.setattr(slack_bridge, "POLL_INTERVAL_SECONDS", 0)
    ticks = 0
    third_tick = asyncio.Event()

    async def failing_tick():
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            third_tick.set()
        raise RuntimeError("slack unreachable")

    monkeypatch.setattr(slack_bridge, "poll_bridge_threads", failing_tick)
    task = asyncio.create_task(slack_bridge.run_bridge_poller_loop())

    await asyncio.wait_for(third_tick.wait(), timeout=2)
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- ending the conversation from the thread --------------------------------
# A thread reply that is a close command ends the conversation through the
# same repository call the dashboard uses, instead of being sent to the
# visitor. Both inbound paths reach this through handle_operator_reply.

async def _run_command(db, monkeypatch, text, slack_ts="1720000001.000200"):
    monkeypatch.setattr(slack_bridge, "SessionLocal", lambda: db)
    monkeypatch.setattr(slack_bridge, "get_adapter", lambda *_: _operator_adapter(None))
    deliver = AsyncMock(return_value=DeliveryResult(ok=True))
    monkeypatch.setattr(slack_bridge, "deliver_to_customer", deliver)
    post = AsyncMock(return_value={"ok": True, "ts": "1720000002.000300"})
    monkeypatch.setattr(slack_bridge, "slack_api", post)
    settled = await slack_bridge.handle_operator_reply(
        TEAM, BRIDGE, THREAD, "U0OPERATOR", text, slack_ts)
    return settled, deliver, post


@pytest.mark.asyncio
async def test_end_command_closes_the_session_and_tells_both_sides(
        db, bridge_session, bridged, slack_account, monkeypatch):
    settled, deliver, post = await _run_command(db, monkeypatch, "/end")

    assert settled is True
    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.status == SessionStatus.CLOSED
    assert stored.end_chat_reason == EndChatReasonType.CUSTOMER_REQUEST

    # The command itself is never a message in the conversation.
    assert db.query(ChatHistory).count() == 0

    deliver.assert_awaited_once()
    payload = deliver.await_args.args[2]
    assert payload["message"] == slack_bridge.CLOSING_MESSAGE
    assert payload["type"] == "agent_message"
    assert payload["session_id"] == str(bridge_session.session_id)

    post.assert_awaited_once()
    method, token, body = post.await_args.args
    assert method == "chat.postMessage"
    assert token == "xoxb-test"
    assert body["channel"] == BRIDGE
    assert body["thread_ts"] == THREAD
    assert body["text"] == slack_bridge.CLOSE_CONFIRMATION


@pytest.mark.parametrize("text", ["!end", "END", "  /end  ", "End"])
@pytest.mark.asyncio
async def test_every_end_command_spelling_closes(
        db, bridge_session, bridged, slack_account, monkeypatch, text):
    settled, deliver, post = await _run_command(db, monkeypatch, text)

    assert settled is True
    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.status == SessionStatus.CLOSED
    assert db.query(ChatHistory).count() == 0
    deliver.assert_awaited_once()
    post.assert_awaited_once()


@pytest.mark.parametrize("text", ["end of story", "ended", "the end", "/end please"])
@pytest.mark.asyncio
async def test_ordinary_text_containing_end_is_still_a_message(
        db, bridge_session, bridged, slack_account, monkeypatch, text):
    settled, deliver, post = await _run_command(db, monkeypatch, text)

    assert settled is True
    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.status == SessionStatus.TRANSFERRED
    assert deliver.await_args.args[2]["message"] == text
    assert db.query(ChatHistory).count() == 1
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_end_does_not_message_the_visitor_again(
        db, bridge_session, bridged, slack_account, monkeypatch):
    await _run_command(db, monkeypatch, "/end")
    settled, deliver, post = await _run_command(
        db, monkeypatch, "/end", slack_ts="1720000003.000400")

    assert settled is True
    deliver.assert_not_awaited()
    # The operator still sees the command was accepted.
    post.assert_awaited_once()
    assert db.query(ChatHistory).count() == 0


@pytest.mark.asyncio
async def test_end_on_a_session_closed_in_the_dashboard_is_a_visitor_no_op(
        db, bridge_session, bridged, slack_account, monkeypatch):
    SessionToAgentRepository(db).close_session(bridge_session.session_id)

    settled, deliver, post = await _run_command(db, monkeypatch, "/end")

    assert settled is True
    deliver.assert_not_awaited()
    post.assert_awaited_once()
    assert db.query(ChatHistory).count() == 0


@pytest.mark.asyncio
async def test_the_poll_path_ends_the_conversation_the_same_way(
        db, bridge_session, bridged, slack_account, monkeypatch):
    calls, deliver = _poll_env(db, monkeypatch, [_reply(text="/end")])

    await slack_bridge.poll_bridge_threads()

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.status == SessionStatus.CLOSED
    assert stored.end_chat_reason == EndChatReasonType.CUSTOMER_REQUEST
    assert db.query(ChatHistory).count() == 0

    deliver.assert_awaited_once()
    assert deliver.await_args.args[2]["message"] == slack_bridge.CLOSING_MESSAGE

    # One read of the thread, one confirmation back into it.
    methods = [call.args[0] for call in calls.await_args_list]
    assert methods == ["conversations.replies", "chat.postMessage"]
    confirmation = calls.await_args_list[-1].args[2]
    assert confirmation["thread_ts"] == THREAD
    assert confirmation["text"] == slack_bridge.CLOSE_CONFIRMATION

    db.refresh(bridged)
    assert bridged.last_processed_ts == "1720000001.000200"


def test_the_webhook_dispatches_an_end_command_to_the_same_handler(
        db, client, bridged, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(settings, "SLACK_BRIDGE_CHANNEL_ID", BRIDGE)
    handler = MagicMock()
    monkeypatch.setattr("app.api.webhooks.slack.handle_operator_reply", handler)

    response = _post(client, _reply_event(text="/end"))

    assert response.status_code == 200
    handler.assert_called_once_with(
        TEAM, BRIDGE, THREAD, "U0OPERATOR", "/end", "1720000001.000200")
