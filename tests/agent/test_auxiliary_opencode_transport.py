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


def test_configured_compression_sends_responses_wire_without_route_override(tmp_path, monkeypatch):
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
    route = {}
    result = aux.call_llm(task="compression", messages=[{"role": "user", "content": "Summarize the investigation."}],
                          route_info=route, timeout=5)
    assert aux.extract_content_or_reasoning(result) == "## Goal\nContinue the investigation."
    assert route == {"provider": "opencode-go", "model": "gpt-5.6-luna"}
    assert len(requests) == 1
