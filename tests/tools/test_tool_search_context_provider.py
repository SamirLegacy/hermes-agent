"""Regression coverage for provider-aware context sizing in the tool-search gate.

``model_tools._resolve_active_context_length()`` feeds ``should_activate``'s
window-fraction check. Providers like Codex OAuth enforce a lower context
window than the direct API for the same slug (e.g. gpt-5.5 is 1.05M on the
API but 272K on the Codex route), and ``get_model_context_length()`` only
applies those provider-aware resolutions when it receives the provider,
base_url, and credential. Before this coverage existed the gate called the
resolver with the model id alone, so Codex sessions sized activation against
generic direct-API metadata.
"""

from unittest.mock import patch


def _model_cfg(**overrides):
    cfg = {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "base_url": "",
    }
    cfg.update(overrides)
    return {"model": cfg}


class TestResolveActiveContextLengthProviderAware:
    def test_passes_provider_base_url_and_key_from_runtime(self):
        """Resolved runtime credentials must reach get_model_context_length."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider="", custom_providers=None):
            captured.update(
                model=model_id, base_url=base_url, api_key=api_key,
                config_ctx=config_context_length, provider=provider,
            )
            return 272_000

        with patch("hermes_cli.config.load_config", return_value=_model_cfg()), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value={"base_url": "https://chatgpt.com/backend-api/codex",
                                 "api_key": "tok-live"}) as mock_rt, \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 272_000
        assert captured["provider"] == "openai-codex"
        assert captured["base_url"] == "https://chatgpt.com/backend-api/codex"
        assert captured["api_key"] == "tok-live"
        mock_rt.assert_called_once_with(
            requested="openai-codex", target_model="gpt-5.6-sol"
        )

    def test_offline_credential_failure_degrades_to_config_values(self):
        """Runtime resolution raising must not zero the gate — the resolver is
        still called with the configured provider/base_url and an empty key so
        static provider-aware fallbacks apply."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider="", custom_providers=None):
            captured.update(base_url=base_url, api_key=api_key, provider=provider)
            return 272_000

        with patch("hermes_cli.config.load_config",
                   return_value=_model_cfg(base_url="https://chatgpt.com/backend-api/codex")), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   side_effect=RuntimeError("no credentials")), \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 272_000
        assert captured["provider"] == "openai-codex"
        assert captured["base_url"] == "https://chatgpt.com/backend-api/codex"
        assert captured["api_key"] == ""

    def test_no_provider_configured_skips_runtime_resolution(self):
        """Without a provider in config, behavior matches the legacy path: no
        runtime resolution attempt, resolver called with empty routing."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider="", custom_providers=None):
            captured.update(base_url=base_url, provider=provider)
            return 200_000

        with patch("hermes_cli.config.load_config",
                   return_value={"model": {"model": "some-model"}}), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider") as mock_rt, \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 200_000
        assert captured["provider"] == ""
        mock_rt.assert_not_called()

    def test_config_context_length_still_short_circuits(self):
        """Explicit model.context_length must keep winning (issue #46620)."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider="", custom_providers=None):
            captured["config_ctx"] = config_context_length
            return config_context_length or 0

        with patch("hermes_cli.config.load_config",
                   return_value=_model_cfg(context_length=150_000)), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value={"base_url": "https://chatgpt.com/backend-api/codex",
                                 "api_key": "tok"}), \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 150_000
        assert captured["config_ctx"] == 150_000

    def test_provider_catalog_context_length_honored_without_probe(self):
        """providers.<name>.models.<model>.context_length sizes the gate.

        A proxied custom endpoint (e.g. VibeProxy) rarely reports context
        lengths, so the gate used to probe, fail, and size against the 256K
        probe-down default despite an explicit operator-set 1M entry. The
        provider-catalog entry must be honored BEFORE any cache or network
        probe — exactly like the compressor's startup path.
        """
        import model_tools

        cfg = {
            "model": {
                "model": "claude-fable-5",
                "provider": "vibeproxy-claude",
                "base_url": "http://127.0.0.1:8317/v1",
            },
            "providers": {
                "vibeproxy-claude": {
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api": "chat_completions",
                    "models": {
                        "claude-fable-5": {"context_length": 1_000_000},
                    },
                },
            },
        }

        def _explode_if_called(*args, **kwargs):
            raise AssertionError(
                "get_model_context_length must not run — the provider-catalog "
                "entry resolves the gate without a probe"
            )

        with patch("hermes_cli.config.load_config", return_value=cfg), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   side_effect=RuntimeError("no credentials in test")), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.get_model_context_length",
                   side_effect=_explode_if_called) as mock_get:
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 1_000_000
        mock_get.assert_not_called()

    def test_provider_catalog_threaded_into_full_resolver(self):
        """When the catalog entry does NOT cover the model, the full resolver
        still runs — with custom_providers threaded so its step-0c lookup can
        honor provider-catalog entries for other models on the same route."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider="", custom_providers=None):
            captured["custom_providers"] = custom_providers
            return 256_000

        cfg = {
            "model": {
                "model": "some-other-model",
                "provider": "vibeproxy-claude",
                "base_url": "http://127.0.0.1:8317/v1",
            },
            "providers": {
                "vibeproxy-claude": {
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api": "chat_completions",
                    "models": {
                        "claude-fable-5": {"context_length": 1_000_000},
                    },
                },
            },
        }

        with patch("hermes_cli.config.load_config", return_value=cfg), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   side_effect=RuntimeError("no credentials in test")), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 256_000
        cps = captured["custom_providers"]
        assert isinstance(cps, list) and cps, "custom_providers must be threaded"
        assert any(
            (cp.get("models") or {}).get("claude-fable-5", {}).get("context_length")
            == 1_000_000
            for cp in cps
        )
