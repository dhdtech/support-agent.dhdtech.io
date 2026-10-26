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

The handoff as a tool call: once the model calls it, the turn's ChatResponse
must carry the structured flag even when the reply text did not.
"""

from app.agents.chat_agent import apply_transfer_capture, build_transfer_tool
from app.models.schemas.chat import ChatResponse, TransferReasonType


def _response(**overrides) -> ChatResponse:
    fields = dict(
        message="One moment please.",
        transfer_to_human=False,
        end_chat=False,
        request_rating=False,
        create_ticket=False,
    )
    fields.update(overrides)
    return ChatResponse(**fields)


def test_the_tool_call_sets_the_flag_and_its_reason():
    capture = {}
    tool = build_transfer_tool(capture)

    assert tool.name == "transfer_to_human"
    tool.entrypoint(reason="FRUSTRATED", description="Customer asked for a manager")

    result = apply_transfer_capture(_response(), capture)

    assert result.transfer_to_human is True
    assert result.transfer_reason == TransferReasonType.FRUSTRATED
    assert result.transfer_description == "Customer asked for a manager"


def test_an_unrecognized_reason_falls_back_to_direct_request():
    capture = {}
    build_transfer_tool(capture).entrypoint(reason="NOT_A_REASON", description=None)

    result = apply_transfer_capture(_response(), capture)

    assert result.transfer_to_human is True
    assert result.transfer_reason == TransferReasonType.DIRECT_REQUEST


def test_a_missing_reason_falls_back_to_direct_request():
    capture = {}
    build_transfer_tool(capture).entrypoint()

    result = apply_transfer_capture(_response(), capture)

    assert result.transfer_to_human is True
    assert result.transfer_reason == TransferReasonType.DIRECT_REQUEST


def test_without_a_tool_call_the_response_is_untouched():
    result = apply_transfer_capture(_response(), {})

    assert result.transfer_to_human is False
    assert result.transfer_reason is None
