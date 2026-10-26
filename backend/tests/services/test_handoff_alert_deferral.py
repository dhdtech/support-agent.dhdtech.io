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

The operator alert at human handoff: posted at transfer time when nothing is
going to be asked of the visitor, held back when the widget's contact form is
about to collect the details it renders - and posted exactly once either way,
by the form or by the fallback timer.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.api import widget_chat
from app.core.config import settings
from app.models.customer import Customer
from app.models.session_to_agent import SessionStatus, SessionToAgent
from app.services import human_routing
from app.services.human_routing import (
    HANDOFF_FALLBACK_DELAY_SECONDS,
    alert_handoff_after_transfer,
)

WEBHOOK = "https://hooks.slack.test/services/T000/B000"


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self):
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return _FakeResponse()


@pytest.fixture(autouse=True)
def pending_store(monkeypatch):
    """Exercise the process-local store, the way a deployment without Redis runs."""
    monkeypatch.setattr(human_routing, "_pending_handoffs", {})
    monkeypatch.setattr(human_routing, "_fallback_tasks", set())
    monkeypatch.setattr(human_routing, "get_redis", lambda: None)


def _customer(db, organization_id, email="1786455337798@noemail.com", full_name=None):
    customer = Customer(
        organization_id=organization_id,
        email=email,
        full_name=full_name,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _session(db, agent, organization_id, customer):
    session = SessionToAgent(
        session_id=uuid4(),
        agent_id=agent.id,
        customer_id=customer.id,
        organization_id=organization_id,
        status=SessionStatus.OPEN,
        assigned_at=datetime.now(timezone.utc),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@pytest.fixture
def anonymous_customer(db, test_organization):
    return _customer(db, test_organization.id)


@pytest.fixture
def identified_customer(db, test_organization):
    return _customer(
        db, test_organization.id,
        email="known@acme.com", full_name="Known Person")


def _slack_patches(client):
    """Patches that let the real text builder run against a fake HTTP client."""
    return [
        patch.object(settings, "SLACK_HANDOFF_WEBHOOK_URL", WEBHOOK),
        patch.object(settings, "SLACK_BRIDGE_CHANNEL_ID", ""),
        patch.object(settings, "FRONTEND_URL", "https://panel.example"),
        patch("app.services.human_routing.httpx.AsyncClient", return_value=client),
    ]


# ---------- the decision at transfer time ----------

@pytest.mark.asyncio
async def test_the_alert_is_held_when_the_form_will_ask_for_details(
    db, test_agent, test_organization, anonymous_customer,
):
    """The reported bug: posted at transfer time, the text could only say
    "Unknown customer" because the form asking for the name was still to come."""
    session = _session(db, test_agent, test_organization.id, anonymous_customer)
    slack = AsyncMock()

    with patch("app.services.human_routing.notify_handoff_slack", new=slack):
        await alert_handoff_after_transfer(
            db, session,
            reason="DIRECT_REQUEST",
            description="Visitor asked for a person",
            agent=test_agent,
            customer_id=anonymous_customer.id,
            channel="web",
        )

    slack.assert_not_awaited()
    assert human_routing.pop_pending_handoff(session.session_id) == {
        "reason": "DIRECT_REQUEST",
        "description": "Visitor asked for a person",
    }
    assert len(human_routing._fallback_tasks) == 1


@pytest.mark.asyncio
async def test_the_alert_posts_immediately_when_the_details_are_already_known(
    db, test_agent, test_organization, identified_customer,
):
    session = _session(db, test_agent, test_organization.id, identified_customer)
    slack = AsyncMock()

    with patch("app.services.human_routing.notify_handoff_slack", new=slack):
        await alert_handoff_after_transfer(
            db, session,
            reason="DIRECT_REQUEST",
            agent=test_agent,
            customer_id=identified_customer.id,
            channel="web",
        )

    slack.assert_awaited_once()
    assert human_routing.pop_pending_handoff(session.session_id) is None


@pytest.mark.asyncio
async def test_the_alert_posts_immediately_when_collection_is_switched_off(
    db, test_agent, test_organization, anonymous_customer,
):
    test_agent.handoff_collect_email = False
    test_agent.handoff_collect_name = False
    db.commit()
    session = _session(db, test_agent, test_organization.id, anonymous_customer)
    slack = AsyncMock()

    with patch("app.services.human_routing.notify_handoff_slack", new=slack):
        await alert_handoff_after_transfer(
            db, session,
            reason="DIRECT_REQUEST",
            agent=test_agent,
            customer_id=anonymous_customer.id,
            channel="web",
        )

    slack.assert_awaited_once()
    assert human_routing.pop_pending_handoff(session.session_id) is None


@pytest.mark.asyncio
async def test_the_alert_posts_immediately_on_a_non_widget_channel(
    db, test_agent, test_organization, anonymous_customer,
):
    """No widget renders a form there, so holding the alert would only delay it."""
    session = _session(db, test_agent, test_organization.id, anonymous_customer)
    slack = AsyncMock()

    with patch("app.services.human_routing.notify_handoff_slack", new=slack):
        await alert_handoff_after_transfer(
            db, session,
            reason="DIRECT_REQUEST",
            agent=test_agent,
            customer_id=anonymous_customer.id,
            channel="telegram",
        )

    slack.assert_awaited_once()


# ---------- the fallback timer ----------

@pytest.mark.asyncio
async def test_the_fallback_posts_without_details_when_the_form_never_arrives(
    db, test_agent, test_organization,
):
    customer = _customer(db, test_organization.id, email="")
    session = _session(db, test_agent, test_organization.id, customer)
    human_routing._store_pending_handoff(str(session.session_id), {
        "reason": "DIRECT_REQUEST",
        "description": "Visitor asked for a person",
    })
    client = _FakeClient()
    sleep = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(patch("app.services.human_routing._sleep", new=sleep))
        stack.enter_context(patch("app.services.human_routing.get_db", lambda: iter([db])))
        for p in _slack_patches(client):
            stack.enter_context(p)
        await human_routing._post_handoff_after_timeout(session.session_id)

    sleep.assert_awaited_once_with(HANDOFF_FALLBACK_DELAY_SECONDS)
    assert HANDOFF_FALLBACK_DELAY_SECONDS == 120
    assert len(client.posts) == 1
    text = client.posts[0][1]["text"]
    assert "Unknown customer" in text
    assert "DIRECT_REQUEST" in text
    assert "Visitor asked for a person" in text
    assert human_routing.pop_pending_handoff(session.session_id) is None


# ---------- the submit handler ----------

@pytest.fixture
def socket(monkeypatch, db):
    mock_sio = MagicMock()
    mock_sio.emit = AsyncMock()
    mock_sio.get_session = AsyncMock()
    monkeypatch.setattr(widget_chat, "sio", mock_sio)
    monkeypatch.setattr(widget_chat, "get_db", lambda: iter([db]))
    return mock_sio


async def _submit(socket, session, customer, form_data):
    socket.get_session.return_value = {
        "widget_id": "widget-1",
        "org_id": str(session.organization_id),
        # A UUID object, not its string form: the sqlite test schema cannot
        # coerce a str into its UUID column the way the real database driver does.
        "customer_id": customer.id,
        "session_id": str(session.session_id),
    }
    await widget_chat.handle_contact_info("sid", {"form_data": form_data})


@pytest.mark.asyncio
async def test_the_submitted_details_land_in_the_alert(
    db, socket, test_agent, test_organization, anonymous_customer,
):
    session = _session(db, test_agent, test_organization.id, anonymous_customer)
    human_routing._store_pending_handoff(str(session.session_id), {
        "reason": "DIRECT_REQUEST",
        "description": "Visitor asked for a person",
    })
    client = _FakeClient()

    with ExitStack() as stack:
        stack.enter_context(patch("app.services.human_routing.get_db", lambda: iter([db])))
        for p in _slack_patches(client):
            stack.enter_context(p)
        await _submit(socket, session, anonymous_customer, {
            "email": "new.person@acme.com",
            "name": "New Person",
        })

    assert len(client.posts) == 1
    text = client.posts[0][1]["text"]
    assert "New Person (new.person@acme.com)" in text
    assert "DIRECT_REQUEST" in text
    assert human_routing.pop_pending_handoff(session.session_id) is None


@pytest.mark.asyncio
async def test_a_form_submitted_after_the_fallback_does_not_post_twice(
    db, socket, test_agent, test_organization, anonymous_customer,
):
    session = _session(db, test_agent, test_organization.id, anonymous_customer)
    human_routing._store_pending_handoff(str(session.session_id), {
        "reason": "DIRECT_REQUEST",
        "description": "Visitor asked for a person",
    })
    client = _FakeClient()

    with ExitStack() as stack:
        stack.enter_context(patch("app.services.human_routing._sleep", new=AsyncMock()))
        stack.enter_context(patch("app.services.human_routing.get_db", lambda: iter([db])))
        for p in _slack_patches(client):
            stack.enter_context(p)
        await human_routing._post_handoff_after_timeout(session.session_id)
        await _submit(socket, session, anonymous_customer, {
            "email": "late@acme.com",
        })

    assert len(client.posts) == 1
    assert "late@acme.com" not in client.posts[0][1]["text"]


@pytest.mark.asyncio
async def test_the_fallback_after_the_form_does_not_post_twice(
    db, socket, test_agent, test_organization, anonymous_customer,
):
    session = _session(db, test_agent, test_organization.id, anonymous_customer)
    human_routing._store_pending_handoff(str(session.session_id), {
        "reason": "DIRECT_REQUEST",
        "description": "Visitor asked for a person",
    })
    client = _FakeClient()

    with ExitStack() as stack:
        stack.enter_context(patch("app.services.human_routing._sleep", new=AsyncMock()))
        stack.enter_context(patch("app.services.human_routing.get_db", lambda: iter([db])))
        for p in _slack_patches(client):
            stack.enter_context(p)
        await _submit(socket, session, anonymous_customer, {
            "email": "prompt@acme.com",
        })
        await human_routing._post_handoff_after_timeout(session.session_id)

    assert len(client.posts) == 1
    assert "prompt@acme.com" in client.posts[0][1]["text"]


@pytest.mark.asyncio
async def test_the_fallback_swallows_its_own_failures():
    human_routing._store_pending_handoff("some-session", {"reason": "DIRECT_REQUEST"})

    def _broken_db():
        raise RuntimeError("database is gone")

    with (
        patch("app.services.human_routing._sleep", new=AsyncMock()),
        patch("app.services.human_routing.get_db", _broken_db),
    ):
        await human_routing._post_handoff_after_timeout("some-session")
