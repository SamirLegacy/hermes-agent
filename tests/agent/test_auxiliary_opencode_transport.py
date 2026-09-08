"""Auxiliary OpenCode requests must use the same wire as main turns."""
from __future__ import annotations

import json

import httpx
import pytest

from agent import auxiliary_client as aux
from hermes_cli.models import opencode_model_api_mode


@pytest.mark.parametrize("provider,model,explicit_mode", [
    ("opencode-go", "gpt-5.6-luna", None),
    ("opencode-zen", "gpt-5.6-luna", None),
    ("opencode-go", "minimax-m3", None),
    ("opencode-go", "glm-5", None),
    ("opencode-go", "gpt-5.6-luna", "chat_completions"),
])
def test_auxiliary_opencode_transport_matches_main_contract(
    monkeypatch, provider, model, explicit_mode
):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-opencode-key")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "test-opencode-key")
    client, resolved_model = aux.resolve_provider_client(provider, model, api_mode=explicit_mode)
    expected_mode = explicit_mode or opencode_model_api_mode(provider, model)
    if isinstance(client, aux.CodexAuxiliaryClient):
        actual_mode = "codex_responses"
    elif isinstance(client, aux.AnthropicAuxiliaryClient):
        actual_mode = "anthropic_messages"
    else:
        actual_mode = "chat_completions"
    assert resolved_model == model
    assert actual_mode == expected_mode


@pytest.mark.parametrize("entry", ["auxiliary", "context", "payload"])
def test_configured_compression_sends_responses_wire_without_route_override(tmp_path, monkeypatch, entry):
    """Real config -> aux resolution -> OpenAI SDK -> HTTP request/response parsing.

    Only HTTP is simulated. A Chat Completions request is not a valid Go GPT
    request; neither a different model nor a fallback may repair that mismatch.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-opencode-key")
    tmp_path.joinpath("config.yaml").write_text(
        "auxiliary:\n  compression:\n    provider: opencode-go\n    model: gpt-5.6-luna\n"
    )
    requests = []
    failing = []
    response = {
        "id": "resp_summary", "object": "response", "created_at": 0,
        "status": "completed", "model": "gpt-5.6-luna",
        "output": [{"type": "message", "id": "msg_summary", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text",
                    "text": "## Goal\nContinue the investigation.", "annotations": []}]}],
    }

    def respond(request):
        requests.append(request)
        assert request.url.path == "/zen/go/v1/responses"
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.6-luna"
        assert "input" in body and "messages" not in body
        if failing:
            return httpx.Response(500, json={"error": {"message": "fixture server failure", "type": "server_error"}})
        if body.get("stream"):
            events = [
                {"type": "response.output_item.done", "output_index": 0, "item": response["output"][0]},
                {"type": "response.completed", "response": response},
            ]
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(
                f"data: {json.dumps(event)}\n\n" for event in events
            ) + "data: [DONE]\n\n")
        return httpx.Response(200, json=response)

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda *_a, **_k: {
        "http_client": httpx.Client(transport=httpx.MockTransport(respond))
    })
    if entry != "auxiliary":
        _exercise_real_overflow(tmp_path, entry, failing, requests)
        return
    route = {}
    result = aux.call_llm(task="compression", messages=[{"role": "user", "content": "Summarize the investigation."}],
                          route_info=route, timeout=5)
    assert aux.extract_content_or_reasoning(result) == "## Goal\nContinue the investigation."
    assert route == {"provider": "opencode-go", "model": "gpt-5.6-luna"}
    assert len(requests) == 1


def _exercise_real_overflow(tmp_path, entry, failing, requests):
    """Reuse the existing isolated agent fixture; replace only HTTP responses."""
    import copy
    from types import SimpleNamespace
    from typing import Any

    from agent.conversation_compression import compression_blocked_transiently
    from agent.error_classifier import FailoverReason
    from agent.model_metadata import estimate_messages_tokens_rough
    from agent.turn_overflow import recover_from_overflow
    from agent.turn_retry_state import TurnRetryState
    from tests.agent.test_compression_attempt_lifecycle import _build_agent

    config = tmp_path / "config.yaml"
    configured_bytes = config.read_bytes()
    session_id = "COMPOSED_HTTP_OVERFLOW_" + entry
    agent: Any
    db, agent = _build_agent(tmp_path, session_id)
    original_route = (agent.model, agent.provider)
    compressor = agent.context_compressor
    compressor.update_model("test/model", 128_000)
    live: list[dict[str, Any]] = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"row {i} " * 3000}
        for i in range(40)
    ]
    for message in live:
        db.append_message(session_id, message["role"], message["content"])
        message["_db_persisted"] = True
    original = copy.deepcopy(live)
    stored_before = db.get_messages_as_conversation(session_id)
    tokens = estimate_messages_tokens_rough(live)
    assert tokens > compressor.context_length
    error = RuntimeError("context_length_exceeded" if entry == "context" else "payload too large")
    classification = SimpleNamespace(reason=(
        FailoverReason.context_overflow if entry == "context" else FailoverReason.payload_too_large
    ))
    kwargs: dict[str, Any] = dict(
        status_code=400 if entry == "context" else 413,
        error_msg=str(error), wrapped_output_cap_budget=None,
        messages=live, api_messages=live, system_message="sys",
        active_system_prompt="sys", conversation_history=list(live),
        approx_tokens=tokens, compression_attempts=0, max_compression_attempts=5,
        api_call_count=1, effective_task_id=session_id,
    )
    try:
        failing.append(True)
        failed = recover_from_overflow(agent, error, classification, TurnRetryState(), **kwargs)
        assert requests
        assert failed.result is not None
        assert compressor._last_compress_aborted
        assert failed.action == "return" and failed.result["compression_deferred"]
        assert not failed.result.get("compression_exhausted") and not failed.result["failed"]
        assert live == original and db.get_messages_as_conversation(session_id) == stored_before
        assert agent.session_id == session_id
        session = db.get_session(session_id)
        assert session is not None and session["ended_at"] is None
        assert db.get_compression_failure_cooldown(session_id) is not None

        failures = len(requests)
        failing.clear()
        retry = TurnRetryState()
        recovered = recover_from_overflow(agent, error, classification, retry, **kwargs)
        assert len(requests) > failures
        assert recovered.action == "break" and retry.restart_with_compressed_messages
        assert recovered.messages is not None
        assert not compression_blocked_transiently(agent)
        assert estimate_messages_tokens_rough(recovered.messages) < tokens * 0.5
        assert db.get_compression_failure_cooldown(session_id) is None
        assert len(db.get_messages_as_conversation(session_id)) < len(stored_before)
        assert agent.session_id == session_id
        session = db.get_session(session_id)
        assert session is not None and session["ended_at"] is None
        db.append_message(session_id, "user", "Continue after the HTTP-backed summary.")
        assert db.get_messages_as_conversation(session_id)[-1]["content"] == "Continue after the HTTP-backed summary."
        assert config.read_bytes() == configured_bytes
        assert (agent.model, agent.provider) == original_route
    finally:
        db.close()
