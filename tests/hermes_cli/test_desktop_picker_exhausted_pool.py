"""Desktop model.options discovers choices, not immediately usable credentials."""

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def picker_home(tmp_path, monkeypatch):
    from agent import credential_pool as cp
    from hermes_cli import auth, inventory, models, model_switch_providers, providers

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "picker"
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    config = {"model": {"provider": "minimax-oauth", "default": "other-model"}}
    (profile / "config.yaml").write_text(json.dumps(config))
    # Root-owned OAuth: the active profile has no provider singleton. A 429
    # makes OpenAI unavailable for inference, not absent from a manual picker.
    pool_rows = {
        "openai-codex": [{
            "id": "codex", "source": "device_code", "auth_type": "oauth",
            "access_token": "test-codex-token", "last_status": "exhausted",
            "last_status_at": time.time(), "last_error_code": 429,
            "last_error_reset_at": time.time() + 3600,
        }],
        "minimax-oauth": [{
            "id": "other", "source": "manual", "auth_type": "oauth",
            "access_token": "test-other-token",
        }],
        "copilot": [{
            "id": "ambient", "source": "gh_cli", "auth_type": "oauth",
            "access_token": "test-ambient-token",
        }],
    }
    auth_path = root / "auth.json"
    auth_path.write_text(json.dumps({"providers": {}, "credential_pool": pool_rows}))
    # Keep real disk fallback + real pool availability and explicit-auth gates;
    # avoid unrelated auto-seeding/token healing at the load_pool I/O boundary.
    monkeypatch.setattr(cp, "load_pool", lambda slug: cp.CredentialPool(
        slug, [cp.PooledCredential.from_dict(slug, row) for row in auth.read_credential_pool(slug)
               if isinstance(row, dict)]))
    catalogs = {"openai-codex": ["subscription-model"], "minimax-oauth": ["other-model"],
                "copilot": ["ambient-model"], "qwen-oauth": ["unconfigured-model"]}
    monkeypatch.setattr(providers, "HERMES_OVERLAYS", {
        slug: providers.HERMES_OVERLAYS[slug]
        for slug in ("openai-codex", "minimax-oauth", "github-copilot", "qwen-oauth")})
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(model_switch_providers, "_build_curated_lists", lambda *args: catalogs)
    monkeypatch.setattr(models, "CANONICAL_PROVIDERS", [])
    monkeypatch.setattr(models, "cached_provider_model_ids", lambda slug, **kwargs: catalogs.get(slug, []))
    # Pricing/featured metadata and their background workers are unrelated to
    # membership; no network or threads are needed to prove the discovery seam.
    for name in ("_apply_pricing", "_apply_capabilities", "_apply_featured", "_prewarm_pricing_async"):
        monkeypatch.setattr(inventory, name, lambda *args, **kwargs: None)
    return inventory.load_picker_context(), auth_path, profile / "config.yaml"


@pytest.mark.parametrize("refresh", [False, True])
def test_desktop_picker_keeps_exhausted_subscription_without_changing_runtime_auth(picker_home, refresh):
    from agent.credential_pool import load_pool
    from hermes_cli.inventory import build_models_payload
    from tui_gateway.server import _methods

    ctx, auth_path, config_path = picker_home
    before = auth_path.read_bytes(), config_path.read_bytes()
    pool = load_pool("openai-codex")
    assert pool.has_credentials() and not pool.has_available()
    # Non-picker resolution must still reject exhausted pools (#45759).
    runtime = build_models_payload(ctx, explicit_only=True)
    runtime_slugs = {row["slug"] for row in runtime["providers"]}
    assert "openai-codex" not in runtime_slugs

    response = _methods["model.options"](1, {"explicit_only": True, "refresh": refresh})
    assert "error" not in response
    payload = response["result"]
    rows = {row["slug"]: row for row in payload["providers"]}
    assert "openai-codex" in rows
    assert rows["openai-codex"]["models"] == ["subscription-model"]
    assert rows["minimax-oauth"]["models"] == ["other-model"]
    assert set(rows) == runtime_slugs | {"openai-codex"}
    assert "copilot" not in rows and "qwen-oauth" not in rows
    assert payload["provider"] == ctx.current_provider
    assert payload["model"] == ctx.current_model
    assert not load_pool("openai-codex").has_available()
    assert (auth_path.read_bytes(), config_path.read_bytes()) == before


def test_desktop_picker_preserves_explicit_exclusion_and_disabled_provider(picker_home):
    from dataclasses import replace
    from hermes_cli.inventory import build_model_options_payload

    ctx, _, _ = picker_home
    for restricted in (
        replace(ctx, excluded_providers=["openai-codex"]),
        replace(ctx, user_providers={"openai-codex": {"enabled": False}}),
    ):
        payload = build_model_options_payload(restricted, explicit_only=True)
        assert {row["slug"] for row in payload["providers"]} == {"minimax-oauth"}
