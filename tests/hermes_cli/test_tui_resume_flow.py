from argparse import Namespace
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import types

import pytest
from hermes_cli import main_tui_launch


def _args(**overrides):
    base = {
        "continue_last": None,
        "model": None,
        "provider": None,
        "resume": None,
        "toolsets": None,
        "tui": True,
        "tui_dev": False,
    }
    base.update(overrides)
    return Namespace(**base)


def _raise_exit(rc):
    raise SystemExit(rc)


@pytest.fixture
def main_mod(monkeypatch):
    import hermes_cli.main as mod

    monkeypatch.setattr(mod, "_has_any_provider_configured", lambda: True)
    # Reset the idempotency guard so each test starts fresh.
    monkeypatch.setattr(mod, "_oneshot_cleanup_done", False)
    return mod
















def test_termux_skips_bundled_skill_sync_when_stamp_fresh(monkeypatch, tmp_path, main_mod):
    calls = []

    monkeypatch.setenv("TERMUX_VERSION", "1")
    monkeypatch.setattr(main_mod, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(main_mod, "_termux_bundled_skills_fingerprint", lambda: "fp1")
    main_mod._mark_termux_bundled_skills_synced()
    monkeypatch.setitem(
        sys.modules,
        "tools.skills_sync",
        types.SimpleNamespace(sync_skills=lambda quiet: calls.append(quiet)),
    )

    assert main_mod._sync_bundled_skills_for_startup() is False
    assert calls == []






def test_exit_after_oneshot_flushes_stdio_and_calls_os_exit(
    monkeypatch, main_mod
):
    flushed = []
    exits = []

    class FakeStream:
        def __init__(self, name):
            self.name = name

        def flush(self):
            flushed.append(self.name)

    def fake_exit(rc):
        exits.append(rc)
        raise SystemExit(rc)

    monkeypatch.setattr(main_mod.sys, "stdout", FakeStream("stdout"))
    monkeypatch.setattr(main_mod.sys, "stderr", FakeStream("stderr"))
    monkeypatch.setattr(main_mod.os, "_exit", fake_exit)
    monkeypatch.setattr("logging.shutdown", lambda: None)

    with pytest.raises(SystemExit) as exc:
        main_mod._exit_after_oneshot(17)

    assert exc.value.code == 17
    assert exits == [17]
    assert flushed == ["stdout", "stderr"]






def test_oneshot_subprocess_exits_without_teardown_abort():
    program = textwrap.dedent(
        """
        import hermes_cli.oneshot as oneshot
        from hermes_cli.main import _exit_after_oneshot

        oneshot._run_agent = lambda *args, **kwargs: ("ok", {"final_response": "ok"})
        _exit_after_oneshot(oneshot.run_oneshot("hello"))
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == b"ok\n"
    # Don't demand byte-empty stderr — an import-time warning from the heavy
    # CLI import chain shouldn't fail this. What matters is no crash traceback.
    assert b"Traceback" not in result.stderr








def _stub_plugin_discovery(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=lambda: None),
    )




@pytest.mark.parametrize(
    ("primary_failure", "stale_env_provider"),
    [("auth", False), ("disabled", False), ("auth", True)],
)
def test_oneshot_run_agent_uses_ordered_fallback_when_default_is_unavailable(
    monkeypatch, primary_failure, stale_env_provider,
):
    import hermes_cli.oneshot as oneshot_mod
    from hermes_cli.auth import AuthError

    captured = {}
    resolve_calls = []
    fallback_chain = [
        {"provider": "kimi-coding", "model": "k3", "key_env": "TEST_KIMI_KEY"},
        {"provider": "zai", "model": "glm-5.2"},
        {"provider": "xai-oauth", "model": "grok-4.5"},
    ]

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()

        def run_conversation(self, _prompt, **_kwargs):
            return {"final_response": "fallback ok"}

        def shutdown_memory_provider(self, messages=None):
            pass

        def close(self):
            pass

    def fake_resolve_runtime_provider(**kwargs):
        resolve_calls.append(kwargs)
        if kwargs.get("requested") is None:
            if primary_failure == "disabled":
                raise ValueError("provider 'openai-codex' is disabled in config")
            raise AuthError(
                "primary quota exhausted",
                provider="openai-codex",
                code="codex_rate_limited",
            )
        if kwargs.get("requested") == "kimi-coding":
            return {
                "api_key": "fallback-key",
                "base_url": "https://api.kimi.example/v1",
                "provider": "kimi-coding",
                "api_mode": "chat_completions",
                "credential_pool": None,
            }
        raise AssertionError(f"unexpected provider: {kwargs.get('requested')}")

    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    if stale_env_provider:
        monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "stale-provider")
    monkeypatch.setenv("TEST_KIMI_KEY", "fallback-key")
    monkeypatch.setitem(
        sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAgent)
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"default": "gpt-5.6-sol", "provider": "openai-codex"},
            "fallback_providers": fallback_chain,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )
    monkeypatch.setattr(oneshot_mod, "_create_session_db_for_oneshot", lambda: None)

    assert oneshot_mod._run_agent("hello", use_config_toolsets=False) == (
        "fallback ok",
        {"final_response": "fallback ok"},
    )
    assert [call.get("requested") for call in resolve_calls] == [None, "kimi-coding"]
    assert resolve_calls[1]["target_model"] == "k3"
    assert resolve_calls[1]["explicit_api_key"] == "fallback-key"
    assert captured["provider"] == "kimi-coding"
    assert captured["model"] == "k3"
    assert captured["fallback_model"] == fallback_chain[1:]


@pytest.mark.parametrize("selection_source", ["cli", "environment"])
def test_oneshot_run_agent_does_not_fallback_for_explicit_provider(
    monkeypatch, selection_source,
):
    import hermes_cli.oneshot as oneshot_mod
    from hermes_cli.auth import AuthError

    error = AuthError(
        "explicit provider unavailable",
        provider="openai-codex",
        code="codex_rate_limited",
    )
    model_config = {"default": "gpt-5.6-sol"}
    if selection_source == "cli":
        model_config["provider"] = "openai-codex"

    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        types.SimpleNamespace(AIAgent=lambda **_kwargs: pytest.fail("agent built")),
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": model_config,
            "fallback_providers": [{"provider": "kimi-coding", "model": "k3"}],
        },
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)

    kwargs = {}
    if selection_source == "cli":
        kwargs = {"model": "gpt-5.6-sol", "provider": "openai-codex"}
    else:
        monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openai-codex")

    with pytest.raises(AuthError) as exc_info:
        oneshot_mod._run_agent(
            "hello",
            use_config_toolsets=False,
            **kwargs,
        )

    assert exc_info.value is error


def test_oneshot_run_agent_preserves_primary_auth_error_when_fallbacks_fail(
    monkeypatch,
):
    import hermes_cli.oneshot as oneshot_mod
    from hermes_cli.auth import AuthError

    primary_error = AuthError(
        "primary quota exhausted",
        provider="openai-codex",
        code="codex_rate_limited",
    )

    def fake_resolve_runtime_provider(**kwargs):
        if kwargs.get("requested") is None:
            raise primary_error
        raise AuthError("fallback unavailable", provider=str(kwargs.get("requested")))

    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        types.SimpleNamespace(AIAgent=lambda **_kwargs: pytest.fail("agent built")),
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"default": "gpt-5.6-sol", "provider": "openai-codex"},
            "fallback_providers": [
                {"provider": "kimi-coding", "model": "k3"},
                {"provider": "zai", "model": "glm-5.2"},
            ],
        },
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )

    with pytest.raises(AuthError) as exc_info:
        oneshot_mod._run_agent("hello", use_config_toolsets=False)

    assert exc_info.value is primary_error


def test_oneshot_wires_session_db_for_recall(monkeypatch):
    """hermes -z bypasses HermesCLI, but recall still needs SessionDB."""
    from hermes_cli.oneshot import _run_agent

    captured = {}
    sentinel_db = object()

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()

        def run_conversation(self, prompt, **_kwargs):
            captured["prompt"] = prompt
            return {"final_response": "ok", "failed": False, "partial": False}

    class FakeSessionDB:
        def __new__(cls):
            return sentinel_db

    def mod(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        return module

    monkeypatch.setitem(sys.modules, "run_agent", mod("run_agent", AIAgent=FakeAgent))
    monkeypatch.setitem(sys.modules, "hermes_state", mod("hermes_state", SessionDB=FakeSessionDB))
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        mod("hermes_cli.config", load_config=lambda: {"model": {"default": "m"}}),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.models",
        mod("hermes_cli.models", detect_provider_for_model=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        mod(
            "hermes_cli.runtime_provider",
            resolve_runtime_provider=lambda **_kwargs: {
                "api_key": "k",
                "base_url": "u",
                "provider": "p",
                "api_mode": "chat_completions",
                "credential_pool": None,
            },
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.tools_config",
        mod("hermes_cli.tools_config", _get_platform_tools=lambda *_args, **_kwargs: {"session_search"}),
    )

    text, result = _run_agent("recall this")
    assert text == "ok"
    assert not result.get("failed")
    assert captured["session_db"] is sentinel_db
    assert captured["enabled_toolsets"] == ["session_search"]
    assert captured["prompt"] == "recall this"


def test_launch_tui_exports_model_provider_and_toolsets(monkeypatch, main_mod):
    captured = {}
    active_path_during_call = None

    monkeypatch.setattr(main_tui_launch, "_make_tui_argv",
        lambda tui_dir, tui_dev: (["node", "dist/entry.js"], Path(".")),
    )

    def fake_call(argv, cwd=None, env=None):
        nonlocal active_path_during_call
        captured.update({"argv": argv, "cwd": cwd, "env": env})
        active_path_during_call = Path(env["HERMES_TUI_ACTIVE_SESSION_FILE"])
        assert active_path_during_call.exists()
        return 1

    monkeypatch.setattr(main_mod.subprocess, "call", fake_call)

    with pytest.raises(SystemExit):
        main_mod._launch_tui(
            model="nous/hermes-test", provider="nous", toolsets="web, terminal"
        )

    env = captured["env"]
    assert env["HERMES_MODEL"] == "nous/hermes-test"
    assert env["HERMES_INFERENCE_MODEL"] == "nous/hermes-test"
    assert env["HERMES_TUI_PROVIDER"] == "nous"
    assert env["HERMES_INFERENCE_PROVIDER"] == "nous"
    assert env["HERMES_TUI_TOOLSETS"] == "web,terminal"
    active_path = Path(env["HERMES_TUI_ACTIVE_SESSION_FILE"])
    assert active_path.name.startswith("hermes-tui-active-session-")
    assert active_path.suffix == ".json"
    assert active_path_during_call == active_path
    assert not active_path.exists()
    assert env["NODE_ENV"] == "production"




def test_make_tui_argv_dev_prebuilds_hermes_ink(monkeypatch, main_mod, tmp_path):
    tui_dir = tmp_path / "ui-tui"
    tsx = tui_dir / "node_modules" / ".bin" / "tsx"
    ink_dir = tui_dir / "packages" / "hermes-ink"
    tsx.parent.mkdir(parents=True)
    ink_dir.mkdir(parents=True)
    tsx.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    monkeypatch.setattr(main_tui_launch, "_ensure_tui_node", lambda: None)
    monkeypatch.setattr(main_tui_launch, "_tui_need_npm_install", lambda _tui_dir: False)
    monkeypatch.delenv("HERMES_TUI_DIR", raising=False)
    monkeypatch.setattr(main_mod.shutil, "which", lambda bin_name: f"/usr/bin/{bin_name}")

    calls = []

    def fake_run(cmd, cwd=None, **_kwargs):
        calls.append((cmd, cwd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)

    argv, cwd = main_tui_launch._make_tui_argv(tui_dir, tui_dev=True)

    assert argv == [str(tsx), "src/entry.tsx"]
    assert cwd == tui_dir
    assert calls == [(["/usr/bin/npm", "run", "build"], str(ink_dir))]




