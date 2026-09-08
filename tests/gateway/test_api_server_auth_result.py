"""Provider setup failures use the same result flags as executed agent failures."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _ProviderAuthResolutionError


@pytest.fixture
def adapter():
    return APIServerAdapter(PlatformConfig(enabled=True))


@pytest.mark.asyncio
async def test_provider_auth_result_preserves_text_and_marks_failure(adapter):
    error = "The local model server is turned off."
    with patch.object(adapter, "_create_agent", side_effect=_ProviderAuthResolutionError(error)):
        result, usage = await adapter._run_agent(user_message="hello", conversation_history=[])

    assert result["final_response"] == f"⚠️ Provider authentication failed: {error}"
    assert result["messages"] == []
    assert result["tools"] == []
    assert result["api_calls"] == 0
    assert usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert result["completed"] is False
    assert result["failed"] is True
    assert result["error"] == error


@pytest.mark.asyncio
async def test_chat_completion_projects_real_provider_auth_result(adapter):
    error = "The local model server is turned off."
    request = AsyncMock()
    request.headers = {}
    request.path = "/v1/chat/completions"
    request.json.return_value = {"messages": [{"role": "user", "content": "hello"}]}
    with patch.object(adapter, "_create_agent", side_effect=_ProviderAuthResolutionError(error)):
        response = await adapter._handle_chat_completions(request)

    # Keep the existing HTTP/text contract; structured metadata carries the failure.
    assert response.status == 200
    data = json.loads(response.text)
    assert data["choices"][0]["message"]["content"] == f"⚠️ Provider authentication failed: {error}"
    assert data["choices"][0]["finish_reason"] == "error"
    assert data["hermes"]["completed"] is False
    assert data["hermes"]["failed"] is True
    assert data["hermes"]["partial"] is False
    assert data["hermes"]["error"] == error
    assert data["hermes"]["error_code"] == "agent_error"
    assert response.headers["X-Hermes-Completed"] == "false"
