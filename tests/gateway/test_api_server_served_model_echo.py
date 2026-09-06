"""Served-model echo for /v1/chat/completions (chat.completion + chat.completion.chunk).

The response ``model`` field must echo the model that ACTUALLY served the turn — after a
fallback swap the live ``agent.model`` is the truth — and when it differs from the request
pin, the pin stays visible as ``hermes_requested_model`` (response object + first chunk +
final chunk). Requests without a real pin (omitted ``model``, the virtual alias, an empty
string) echo the served id and never carry the pin key.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

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


def _mock_sse_request():
    req = MagicMock()
    req.headers = {}
    return req


def _capturing_sse_response():
    """Mock StreamResponse recording every written SSE frame (test_sse_agent_cancel pattern)."""
    resp = AsyncMock(spec=web.StreamResponse)
    resp.prepare = AsyncMock()
    written: list = []

    async def _write(data):
        written.append(data.decode() if isinstance(data, (bytes, bytearray)) else data)

    resp.write = AsyncMock(side_effect=_write)
    return resp, written


def _chunk_frames(written: list):
    frames = []
    for text in written:
        for line in text.splitlines():
            if line.startswith("data: ") and line.strip() != "data: [DONE]":
                try:
                    payload = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("object") == "chat.completion.chunk":
                    frames.append(payload)
    return frames


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
        """(c) Streaming fallback before the first token: first chunk carries served model +
        requested pin; intermediate chunks carry neither; the final chunk re-states the pin."""
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
        for chunk in chunks[1:-1]:
            assert chunk["model"] == "model-b"
            assert "hermes_requested_model" not in chunk
        finish = chunks[-1]
        assert finish["choices"][0]["finish_reason"] == "stop"
        assert finish["model"] == "model-b"
        assert finish["hermes_requested_model"] == "model-a"


class TestServedModelEchoProducer:
    """Rework B3: no ``_run_agent`` stub — the REAL ``_run_agent``/``_finish_turn_result``
    run against a fake agent whose ``.model`` is swapped in place AFTER construction
    (the fallback-chain shape). Only ``_create_agent`` is patched (test_api_server.py
    TestAgentExecution pattern)."""

    @pytest.mark.asyncio
    async def test_finish_turn_result_swapped_model_reaches_response(self, adapter):
        class _SwappingAgent:
            model = "model-a"
            provider = "p1"
            session_prompt_tokens = 1
            session_completion_tokens = 2
            session_total_tokens = 3

            def run_conversation(self, *, user_message, conversation_history, task_id):
                # The fallback chain swapped the agent's model in place mid-turn.
                self.model = "model-b"
                self.provider = "p2"
                return {"final_response": "ok", "messages": [], "api_calls": 1}

        agent = _SwappingAgent()
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent", return_value=agent) as mock_create:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "provider": "p1",
                          "messages": [{"role": "user", "content": "hi"}]})
                assert resp.status == 200
                data = await resp.json()
        # The runtime metadata _finish_turn_result attached comes from the POST-swap agent
        # attributes; without it the echo would fall back to the requested "model-a".
        assert data["model"] == "model-b"
        assert data["hermes_requested_model"] == "model-a"
        # Real _finish_turn_result ran: token counts come from the agent's session counters.
        assert data["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        assert mock_create.call_args.kwargs["requested_model"] == "model-a"


class TestServedModelEchoNoPin:
    """Rework B4: no real pin — omitted ``model``, the virtual alias, an empty string.
    The served id echoes and ``hermes_requested_model`` NEVER appears, even when runtime
    metadata is attached (a route-attached runtime would make the served id differ from
    the request's model_name — the pin key must still stay off)."""

    @staticmethod
    def _run_agent_with_runtime(**kwargs):
        return (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
             "runtime": {"provider": "p2", "model": "kimi-k3", "route_source": "model_routes"}})

    async def _post(self, adapter, body):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(
                    adapter, "_run_agent", side_effect=self._run_agent_with_runtime) as mock_run:
                resp = await cli.post("/v1/chat/completions", json=body)
                assert resp.status == 200
                data = await resp.json()
        return data, mock_run

    @pytest.mark.asyncio
    async def test_request_without_model_key(self, adapter):
        data, mock_run = await self._post(
            adapter, {"messages": [{"role": "user", "content": "hi"}]})
        assert data["model"] == "kimi-k3"
        assert "hermes_requested_model" not in data
        assert mock_run.call_args.kwargs["requested_runtime"] is None

    @pytest.mark.asyncio
    async def test_request_with_virtual_alias_model(self, adapter):
        data, mock_run = await self._post(
            adapter, {"model": "hermes-agent", "messages": [{"role": "user", "content": "hi"}]})
        assert data["model"] == "kimi-k3"
        assert "hermes_requested_model" not in data
        assert mock_run.call_args.kwargs["requested_runtime"] is None

    @pytest.mark.asyncio
    async def test_request_with_empty_model_string(self, adapter):
        data, mock_run = await self._post(
            adapter, {"model": "", "messages": [{"role": "user", "content": "hi"}]})
        assert data["model"] == "kimi-k3"
        assert "hermes_requested_model" not in data
        assert mock_run.call_args.kwargs["requested_runtime"] is None


class TestServedModelEchoStreamingChunks:
    """Rework B2: direct ``_write_sse_chat_completion`` calls (test_sse_agent_cancel.py
    pattern) — deterministic control over WHEN the fake agent's ``.model`` swaps."""

    def test_mid_run_fallback_re_resolves_served_model_per_chunk(self):
        """Agent model changes between the first and a later stream item: the early chunk
        echoes the old id, later chunks the new id, the final chunk carries the pin."""
        adapter = _make_adapter()

        class _Agent:
            def __init__(self):
                self.model = "model-a"
                self.provider = "p1"

        async def run():
            from gateway.platforms.api_server import ThreadSafeAsyncQueue

            stream_q = ThreadSafeAsyncQueue()
            agent = _Agent()
            release = asyncio.Event()

            async def fake_agent():
                await release.wait()
                return ({"final_response": "hello", "messages": [], "api_calls": 1},
                        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

            agent_task = asyncio.ensure_future(fake_agent())
            resp, written = _capturing_sse_response()

            async def _frames_at_least(n):
                deadline = time.monotonic() + 5
                while len(_chunk_frames(written)) < n:
                    if time.monotonic() > deadline:
                        raise AssertionError(
                            f"only {len(_chunk_frames(written))} chunk frames written: {written}")
                    await asyncio.sleep(0.01)

            with patch("gateway.platforms.api_server.web.StreamResponse", return_value=resp):
                writer = asyncio.ensure_future(adapter._write_sse_chat_completion(
                    _mock_sse_request(), "cmpl-mid", "model-a", 1234567890,
                    stream_q, agent_task, [agent], requested_model="model-a"))
                stream_q.put_nowait("he")
                await _frames_at_least(2)  # role chunk + first content chunk (old id)
                agent.model = "model-b"    # fallback chain swaps agent.model in place
                agent.provider = "p2"
                stream_q.put_nowait("llo")
                await _frames_at_least(3)
                stream_q.put_nowait(None)  # EOS sentinel
                release.set()
                await writer

            frames = _chunk_frames(written)
            role, first, second, finish = frames[0], frames[1], frames[2], frames[3]
            assert role["choices"][0]["delta"] == {"role": "assistant"}
            assert role["model"] == "model-a"
            assert "hermes_requested_model" not in role  # served == pin at first chunk
            assert first["model"] == "model-a"
            assert first["choices"][0]["delta"] == {"content": "he"}
            assert "hermes_requested_model" not in first
            assert second["model"] == "model-b"
            assert second["choices"][0]["delta"] == {"content": "llo"}
            assert "hermes_requested_model" not in second
            assert finish["choices"][0]["finish_reason"] == "stop"
            assert finish["model"] == "model-b"
            assert finish["hermes_requested_model"] == "model-a"

        asyncio.run(run())

    def test_no_pin_stream_never_carries_pin_key(self):
        """No requested pin on the SSE writer: chunks echo the live agent model and
        ``hermes_requested_model`` never appears, even when it differs from the
        request-level ``model`` value."""
        adapter = _make_adapter()

        class _Agent:
            model = "kimi-k3"
            provider = "p2"

        async def run():
            from gateway.platforms.api_server import ThreadSafeAsyncQueue

            stream_q = ThreadSafeAsyncQueue()
            stream_q.put_nowait("hi")
            stream_q.put_nowait(None)

            async def fake_agent():
                return ({"final_response": "hi", "messages": [], "api_calls": 1},
                        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

            agent_task = asyncio.ensure_future(fake_agent())
            resp, written = _capturing_sse_response()
            with patch("gateway.platforms.api_server.web.StreamResponse", return_value=resp):
                await adapter._write_sse_chat_completion(
                    _mock_sse_request(), "cmpl-nopin", "gpt-4", 1234567890,
                    stream_q, agent_task, [_Agent()])

            frames = _chunk_frames(written)
            assert frames, "expected chat.completion.chunk frames"
            for frame in frames:
                assert frame["model"] == "kimi-k3"
                assert "hermes_requested_model" not in frame

        asyncio.run(run())
