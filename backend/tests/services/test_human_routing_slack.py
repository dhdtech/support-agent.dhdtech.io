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

The operator alert on handoff: posted when a webhook is configured, a no-op
when it is not, and never allowed to disturb the handoff that already happened.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from app.core.config import settings
from app.models.chat_history import ChatHistory
from app.models.session_to_agent import SessionToAgent, SessionStatus
from app.services.human_routing import notify_handoff_slack, route_session_to_human

WEBHOOK = "https://hooks.slack.test/services/T000/B000"


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, status_code: int = 200, raises: Exception | None = None):
        self.status_code = status_code
        self.raises = raises
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        self.posts.append((url, json))
        if self.raises:
            raise self.raises
        return _FakeResponse(self.status_code)


@pytest.fixture
def waiting_session(db, test_agent, test_customer, test_organization) -> SessionToAgent:
    session = SessionToAgent(
        session_id=uuid4(),
        agent_id=test_agent.id,
        customer_id=test_customer.id,
        organization_id=test_organization.id,
        status=SessionStatus.OPEN,
        assigned_at=datetime.now(timezone.utc),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    db.add(ChatHistory(
        message="My invoice is wrong and nobody answers.",
        message_type="user",
        session_id=session.session_id,
        organization_id=test_organization.id,
        agent_id=test_agent.id,
        customer_id=test_customer.id,
    ))
    db.commit()
    return session


@pytest.mark.asyncio
async def test_posts_the_handoff_summary_to_the_webhook(db, waiting_session):
    client = _FakeClient(status_code=200)

    with patch.object(settings, "SLACK_HANDOFF_WEBHOOK_URL", WEBHOOK), \
         patch.object(settings, "FRONTEND_URL", "https://panel.example"), \
         patch("app.services.human_routing.httpx.AsyncClient", return_value=client):
        await notify_handoff_slack(
            db, waiting_session, reason="DIRECT_REQUEST",
            description="Visitor asked for a person")

    assert len(client.posts) == 1
    url, payload = client.posts[0]
    assert url == WEBHOOK
    text = payload["text"]
    assert "Test Agent" in text
    assert "Test Customer" in text
    assert "test.customer@example.com" in text
    assert "DIRECT_REQUEST" in text
    assert "My invoice is wrong" in text
    assert "Visitor asked for a person" in text
    assert f"/conversations?session={waiting_session.session_id}" in text


@pytest.mark.asyncio
async def test_unset_webhook_posts_nothing(db, waiting_session):
    client = _FakeClient()

    with patch.object(settings, "SLACK_HANDOFF_WEBHOOK_URL", ""), \
         patch("app.services.human_routing.httpx.AsyncClient", return_value=client):
        await notify_handoff_slack(db, waiting_session, reason="DIRECT_REQUEST")

    assert client.posts == []


@pytest.mark.asyncio
async def test_an_error_status_never_raises(db, waiting_session):
    client = _FakeClient(status_code=500)

    with patch.object(settings, "SLACK_HANDOFF_WEBHOOK_URL", WEBHOOK), \
         patch("app.services.human_routing.httpx.AsyncClient", return_value=client):
        await notify_handoff_slack(db, waiting_session, reason="DIRECT_REQUEST")

    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_a_timeout_never_raises(db, waiting_session):
    client = _FakeClient(
        raises=httpx.TimeoutException("timed out", request=httpx.Request("POST", WEBHOOK)))

    with patch.object(settings, "SLACK_HANDOFF_WEBHOOK_URL", WEBHOOK), \
         patch("app.services.human_routing.httpx.AsyncClient", return_value=client):
        await notify_handoff_slack(db, waiting_session, reason="DIRECT_REQUEST")

    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_the_last_visitor_message_is_truncated(db, waiting_session):
    db.add(ChatHistory(
        message="x" * 500,
        message_type="user",
        session_id=waiting_session.session_id,
        organization_id=waiting_session.organization_id,
        agent_id=waiting_session.agent_id,
        customer_id=waiting_session.customer_id,
    ))
    db.commit()
    client = _FakeClient()

    with patch.object(settings, "SLACK_HANDOFF_WEBHOOK_URL", WEBHOOK), \
         patch("app.services.human_routing.httpx.AsyncClient", return_value=client):
        await notify_handoff_slack(db, waiting_session, reason="DIRECT_REQUEST")

    text = client.posts[0][1]["text"]
    last_line = next(line for line in text.splitlines() if line.startswith("*Last message:*"))
    assert len(last_line) <= len("*Last message:* ") + 301


@pytest.mark.asyncio
async def test_untrusted_visitor_text_cannot_inject_a_slack_link(db, waiting_session):
    db.add(ChatHistory(
        message="<https://evil.example|click here>",
        message_type="user",
        session_id=waiting_session.session_id,
        organization_id=waiting_session.organization_id,
        agent_id=waiting_session.agent_id,
        customer_id=waiting_session.customer_id,
    ))
    db.commit()
    client = _FakeClient()

    with patch.object(settings, "SLACK_HANDOFF_WEBHOOK_URL", WEBHOOK), \
         patch("app.services.human_routing.httpx.AsyncClient", return_value=client):
        await notify_handoff_slack(db, waiting_session, reason="DIRECT_REQUEST")

    text = client.posts[0][1]["text"]
    assert "&lt;https://evil.example|click here&gt;" in text
    assert "<https://evil.example" not in text


@pytest.mark.asyncio
async def test_routing_sends_the_alert_exactly_once(db, waiting_session):
    with patch("app.services.human_routing.notify_chat_event", new=AsyncMock()), \
         patch("app.services.human_routing.notify_customer", new=AsyncMock()), \
         patch("app.services.human_routing.notify_handoff_slack", new=AsyncMock()) as slack:
        assert await route_session_to_human(
            db, waiting_session, reason="DIRECT_REQUEST") is True

    assert slack.await_count == 1
