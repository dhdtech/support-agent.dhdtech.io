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
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main  # noqa: F401 — ensures routers are registered on the FastAPI app
from app.core.application import app
from app.core.config import settings
from app.database import get_db
from app.models.chat_history import ChatHistory
from app.models.organization import Organization
from app.models.session_to_agent import SessionStatus, SessionToAgent
from app.models.user import User
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
async def test_a_reply_without_a_matching_user_still_silences_the_ai(
        db, bridge_session, bridged, slack_account, monkeypatch):
    deliver = await _run_operator_reply(db, monkeypatch, None)

    stored = SessionToAgentRepository(db).get_session(bridge_session.session_id)
    assert stored.user_id is None
    assert stored.status == SessionStatus.TRANSFERRED

    message = db.query(ChatHistory).filter(
        ChatHistory.session_id == bridge_session.session_id).one()
    assert message.user_id is None
    deliver.assert_awaited_once()


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
