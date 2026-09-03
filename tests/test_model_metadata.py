"""Regression coverage for get_model_context_length's probe-down messages.

Incident 2026-09-02 (L2 forensics D2): a custom endpoint whose probes fail
logged "Could not detect context length ... defaulting to 256,000 tokens"
BEFORE the hardcoded-catalog rescue ran — the announced default never took
effect (the catalog match returned 1,000,000 0ms later). The advice also
named only ``model.context_length`` although
``providers.<name>.models.<model>.context_length`` works for every call site
that threads ``custom_providers``.
"""

import logging

import pytest

from agent import model_metadata
from agent.model_metadata import DEFAULT_FALLBACK_CONTEXT, get_model_context_length

_CUSTOM_BASE_URL = "http://127.0.0.1:8317/v1"


@pytest.fixture
def probes_down(monkeypatch):
    """All endpoint/local probes fail; no cache entries; no cache writes."""
    monkeypatch.setattr(
        model_metadata, "_resolve_endpoint_context_length",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        model_metadata, "_query_local_context_length",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        model_metadata, "_query_ollama_api_show",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        model_metadata, "get_cached_context_length",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        model_metadata, "save_context_length",
        lambda *a, **k: None,
    )


def _metadata_messages(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "agent.model_metadata"
    ]


def test_catalog_rescue_logs_the_final_returned_value(probes_down, caplog):
    """The catalog match is the value the caller gets — say so, and never
    announce the 256K default the rescue overwrote."""
    with caplog.at_level(logging.INFO, logger="agent.model_metadata"):
        ctx = get_model_context_length(
            "claude-fable-5",
            base_url=_CUSTOM_BASE_URL,
            api_key="",
            provider="custom",
        )

    assert ctx == 1_000_000
    messages = _metadata_messages(caplog)
    assert any("1,000,000" in m for m in messages), messages
    assert not any("defaulting to 256,000" in m for m in messages), messages
    # The advice must name both working override keys.
    assert any(
        "model.context_length" in m
        and "providers.<name>.models.<model>.context_length" in m
        for m in messages
    ), messages


def test_genuine_probe_down_names_actual_default_and_both_keys(
    probes_down, caplog
):
    """No catalog match: the 256K default IS what returns — the message may
    say 'defaulting to 256,000', and must name both override keys."""
    with caplog.at_level(logging.INFO, logger="agent.model_metadata"):
        ctx = get_model_context_length(
            "totally-unknown-model-xyz",
            base_url=_CUSTOM_BASE_URL,
            api_key="",
            provider="custom",
        )

    assert ctx == DEFAULT_FALLBACK_CONTEXT
    messages = _metadata_messages(caplog)
    assert any(
        "defaulting to 256,000" in m
        and "model.context_length" in m
        and "providers.<name>.models.<model>.context_length" in m
        for m in messages
    ), messages


def test_caller_without_custom_providers_still_honors_catalog(monkeypatch, probes_down):
    """D2 (R2 review): a caller passing custom_providers=None still gets the
    providers.<name>.models.<model>.context_length value — the function loads
    the compatible providers itself instead of silently skipping step 0c."""
    import json

    fake_config = {
        "custom_providers": [
            {
                "name": "vibeproxy-claude",
                "base_url": _CUSTOM_BASE_URL,
                "models": {
                    "claude-fable-5": {"context_length": 777_777},
                },
            }
        ]
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: fake_config, raising=True
    )

    ctx = get_model_context_length(
        "claude-fable-5",
        base_url=_CUSTOM_BASE_URL,
        api_key="",
        provider="custom",
        custom_providers=None,
    )
    assert ctx == 777_777

