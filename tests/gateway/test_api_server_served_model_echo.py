"""Served-model echo for /v1/chat/completions (chat.completion + chat.completion.chunk).

The response ``model`` field must echo the model that ACTUALLY served the turn — after a
fallback swap the live ``agent.model`` is the truth — and when it differs from the request
pin, the pin stays visible as ``hermes_requested_model`` (response object + first chunk).
"""

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)


def _make_adapter() -> APIServerAdapter:
    config = PlatformConfig(enabled=True)
    return APIServerAdapter(config)


def _create_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _sse_chunks(body: str):
    """Decode ``chat.completion.chunk`` frames from an SSE body, in order."""
    for line in body.splitlines():
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            try:
                payload = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("object") == "chat.completion.chunk":
                yield payload


@pytest.fixture
def adapter():
    return _make_adapter()


def _runtime(model: str, provider: str, requested_model: str, requested_provider: str) -> dict:
    return {
        "provider": provider, "model": model, "route_source": "raw_request",
        "requested": {"provider": requested_provider, "model": requested_model}}


class TestServedModelEcho:
    @pytest.mark.asyncio
    async def test_served_equals_requested_model_unchanged_no_pin_key(self, adapter):
        """(a) No fallback: ``model`` stays the requested id and no pin key appears."""
        async def _mock_run_agent(**kwargs):
            return (
                {"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                 "runtime": _runtime("model-a", "p1", "model-a", "p1")})

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent) as mock_run:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "provider": "p1",
                          "messages": [{"role": "user", "content": "hi"}]})
                assert resp.status == 200
                data = await resp.json()
        assert data["model"] == "model-a"
        assert "hermes_requested_model" not in data
        # The pin must reach _run_agent as requested_runtime so the turn result carries
        # the runtime metadata the echo reads.
        assert mock_run.call_args.kwargs["requested_runtime"] == {
            "model": "model-a", "provider": "p1"}

    @pytest.mark.asyncio
    async def test_fallback_served_model_echoed_with_requested_pin(self, adapter):
        """(b) Fallback served model-b: ``model`` == served, pin preserved."""
        async def _mock_run_agent(**kwargs):
            return (
                {"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                 "runtime": _runtime("model-b", "p2", "model-a", "p1")})

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "provider": "p1",
                          "messages": [{"role": "user", "content": "hi"}]})
                assert resp.status == 200
                data = await resp.json()
        assert data["model"] == "model-b"
        assert data["hermes_requested_model"] == "model-a"

    @pytest.mark.asyncio
    async def test_stream_fallback_served_model_on_first_chunk(self, adapter):
        """(c) Streaming fallback: first chunk carries served model + requested pin."""
        class _FallbackAgent:
            model = "model-b"
            provider = "p2"

        async def _mock_run_agent(**kwargs):
            agent_ref = kwargs.get("agent_ref")
            if agent_ref is not None:
                agent_ref[0] = _FallbackAgent()
            cb = kwargs.get("stream_delta_callback")
            if cb:
                cb("ok")
            return (
                {"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                 "runtime": _runtime("model-b", "p2", "model-a", "p1")})

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "provider": "p1", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
                assert resp.status == 200
                body = await resp.text()
        chunks = list(_sse_chunks(body))
        assert chunks, "expected chat.completion.chunk frames"
        assert "[DONE]" in body
        first = chunks[0]
        assert first["model"] == "model-b"
        assert first.get("hermes_requested_model") == "model-a"
        for chunk in chunks[1:]:
            assert chunk["model"] == "model-b"
            assert "hermes_requested_model" not in chunk
