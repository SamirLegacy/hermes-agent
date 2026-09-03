"""Tests for CLI resume model restoration and /model session persistence.

Covers _restore_session_model, _persist_model_switch_to_session (cli.py) and
SessionDB.session_gateway_runtime (hermes_state.py) — the round trip that
makes `hermes --resume` reopen a session on the model/provider it actually
used instead of the ambient config default (#57588-class, #79536).
"""

import json

import pytest

import cli as cli_mod
from hermes_state import SessionDB


def _make_stub(**overrides):
    """Bare HermesCLI the way resume paths see it (no __init__)."""
    stub = object.__new__(cli_mod.HermesCLI)
    stub.model = "ambient-model"
    stub.provider = "openrouter"
    stub.requested_provider = "openrouter"
    stub.base_url = "https://openrouter.ai/api/v1"
    stub.api_key = "ambient-key"
    stub.api_mode = ""
    stub.agent = None
    stub._console_print = lambda s: None
    for key, value in overrides.items():
        setattr(stub, key, value)
    return stub


def _row(model="glm-4.7", model_config=None):
    return {
        "model": model,
        "model_config": json.dumps(model_config) if model_config else None,
    }


# ── SessionDB.session_gateway_runtime ───────────────────────────────


def test_session_gateway_runtime_prefers_nested_key():
    meta = _row(model_config={
        "gateway_runtime": {"provider": "custom:feather", "base_url": "https://f/v1"},
        "provider": "openrouter",
    })
    runtime = SessionDB.session_gateway_runtime(meta)
    assert runtime["provider"] == "custom:feather"
    assert runtime["base_url"] == "https://f/v1"


def test_session_gateway_runtime_falls_back_to_top_level_keys():
    # The TUI gateway's _runtime_model_config writes top-level keys only.
    meta = _row(model_config={"provider": "nous", "api_mode": "chat_completions"})
    runtime = SessionDB.session_gateway_runtime(meta)
    assert runtime == {"provider": "nous", "api_mode": "chat_completions"}


def test_session_gateway_runtime_tolerates_garbage():
    assert SessionDB.session_gateway_runtime(None) == {}
    assert SessionDB.session_gateway_runtime({}) == {}
    assert SessionDB.session_gateway_runtime({"model_config": "{not json"}) == {}
    assert SessionDB.session_gateway_runtime({"model_config": json.dumps([1, 2])}) == {}


# ── _restore_session_model ──────────────────────────────────────────


def test_restore_session_model_restores_model_and_provider():
    stub = _make_stub()
    stub._restore_session_model(_row(model_config={
        "gateway_runtime": {"provider": "custom:feather", "base_url": "https://f/v1"},
    }))
    assert stub.model == "glm-4.7"
    assert stub.provider == "custom:feather"
    assert stub.requested_provider == "custom:feather"
    assert stub.base_url == "https://f/v1"
    # Stale launch-time explicit overrides must not leak into the restored
    # provider's credential resolution.
    assert stub._explicit_api_key is None
    assert stub._explicit_base_url == "https://f/v1"


def test_restore_session_model_explicit_cli_flag_wins():
    stub = _make_stub(model="cli-flag-model", _explicit_model_override=True)
    stub._restore_session_model(_row())
    assert stub.model == "cli-flag-model"
    assert stub.provider == "openrouter"


def test_restore_session_model_no_stored_model_is_noop():
    stub = _make_stub()
    stub._restore_session_model(_row(model=None))
    assert stub.model == "ambient-model"


def test_restore_session_model_matching_state_is_silent_noop():
    notes = []
    stub = _make_stub(model="glm-4.7", provider="custom:feather",
                      requested_provider="custom:feather",
                      _console_print=lambda s: notes.append(s))
    stub._restore_session_model(_row(model_config={
        "gateway_runtime": {"provider": "custom:feather"},
    }))
    assert not notes


def test_restore_session_model_swaps_running_agent_in_place():
    calls = {}

    class _Agent:
        def switch_model(self, **kwargs):
            calls.update(kwargs)

    stub = _make_stub(agent=_Agent())
    stub._restore_session_model(_row())
    assert calls["new_model"] == "glm-4.7"


# ── _persist_model_switch_to_session ────────────────────────────────


class _Result:
    new_model = "deepseek-v4-flash-free"
    target_provider = "custom:opencode-zen"
    base_url = "https://oz/v1"
    api_mode = ""


def test_persist_model_switch_writes_model_and_both_route_shapes():
    written = {}

    class _DB:
        def update_session_model(self, sid, model):
            written["model"] = (sid, model)

        def patch_session_model_config(self, sid, patch):
            written["patch"] = (sid, patch)

    stub = _make_stub(_session_db=_DB(), session_id="s1")
    stub._persist_model_switch_to_session(_Result())
    assert written["model"] == ("s1", "deepseek-v4-flash-free")
    sid, patch = written["patch"]
    # Nested shape for the CLI reader...
    assert patch["gateway_runtime"]["provider"] == "custom:opencode-zen"
    # ...and top-level for the TUI gateway's _stored_session_runtime_overrides.
    assert patch["provider"] == "custom:opencode-zen"
    assert patch["base_url"] == "https://oz/v1"
    # Both shapes use or-None so stale keys are deleted (not merely omitted)
    # in BOTH gateway_runtime and top-level — the asymmetry that caused the
    # original stale-key bug.
    assert patch["gateway_runtime"]["api_mode"] is None
    assert patch["api_mode"] is None


def test_persist_model_switch_clears_stale_route_keys(tmp_path, monkeypatch):
    """A later switch must not inherit the previous switch's api_mode/base_url.

    patch_session_model_config merges key-level and only deletes on explicit
    None — dropping falsy values from the patch left the FIRST switch's
    api_mode (e.g. anthropic_messages) alive under the SECOND switch's
    provider, corrupting the wire protocol on TUI/desktop resume.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="stale1", source="cli", model="m0")
    stub = _make_stub(_session_db=db, session_id="stale1")

    class _First:
        new_model = "claude-x"
        target_provider = "custom:feather"
        base_url = "https://feather/v1"
        api_mode = "anthropic_messages"

    class _Second:
        new_model = "gpt-5.4"
        target_provider = "openrouter"
        base_url = "https://openrouter.ai/api/v1"
        api_mode = ""  # openrouter default — must ERASE the anthropic mode

    stub._persist_model_switch_to_session(_First())
    stub._persist_model_switch_to_session(_Second())

    meta = db.get_session("stale1")
    config = json.loads(meta["model_config"])
    # Top-level keys: stale values deleted.
    assert config["provider"] == "openrouter"
    assert "api_mode" not in config, config  # stale anthropic_messages deleted
    # Nested gateway_runtime: stale values replaced with None (the merge
    # replaces the entire gateway_runtime dict, not deep-merging its keys).
    # The reader's `or None` / `if v` filtering treats None the same as
    # absent, so stale values are effectively erased.
    gw = config.get("gateway_runtime", {})
    assert gw.get("provider") == "openrouter"
    assert gw.get("api_mode") is None  # stale anthropic_messages erased
    runtime = SessionDB.session_gateway_runtime(meta)
    assert runtime["provider"] == "openrouter"
    assert "api_mode" not in runtime


def test_persist_model_switch_noop_without_db_or_session():
    stub = _make_stub()  # no _session_db / session_id attributes at all
    stub._persist_model_switch_to_session(_Result())  # must not raise


def test_persist_model_switch_swallows_db_errors():
    class _DB:
        def update_session_model(self, *a):
            raise RuntimeError("disk full")

    stub = _make_stub(_session_db=_DB(), session_id="s1")
    stub._persist_model_switch_to_session(_Result())  # must not raise


def test_persist_model_switch_heals_bare_custom(monkeypatch):
    """Bare 'custom' is not routable — heal to custom:<name> or drop (C1)."""
    written = {}

    class _DB:
        def update_session_model(self, sid, model):
            written["model"] = model

        def patch_session_model_config(self, sid, patch):
            written["patch"] = patch

    class _BareResult:
        new_model = "qwen3.6-plus"
        target_provider = "custom"
        base_url = "https://my-endpoint/v1"
        api_mode = ""

    import hermes_cli.runtime_provider as rp
    monkeypatch.setattr(rp, "canonical_custom_identity",
                        lambda base_url=None, model=None: "custom:myendpoint")
    stub = _make_stub(_session_db=_DB(), session_id="s1")
    stub._persist_model_switch_to_session(_BareResult())
    assert written["patch"]["provider"] == "custom:myendpoint"

    # Healing fails -> provider dropped (explicit None deletes any stale
    # persisted provider), never persisted bare.
    monkeypatch.setattr(rp, "canonical_custom_identity",
                        lambda base_url=None, model=None: None)
    written.clear()
    stub._persist_model_switch_to_session(_BareResult())
    assert written["patch"]["provider"] is None
    assert written["patch"]["gateway_runtime"]["provider"] is None


def test_restore_session_model_heals_bare_custom_stored_rows(monkeypatch):
    """Rows persisted by older builds may carry bare 'custom' — heal or drop."""
    import hermes_cli.runtime_provider as rp
    monkeypatch.setattr(rp, "canonical_custom_identity",
                        lambda base_url=None, model=None: None)
    stub = _make_stub()
    stub._restore_session_model(_row(model_config={
        "gateway_runtime": {"provider": "custom"},
    }))
    # Provider dropped -> model restored but provider stays ambient.
    assert stub.model == "glm-4.7"
    assert stub.provider == "openrouter"


# ── round trip: persist → get_session shape → restore ───────────────


def test_round_trip_persist_then_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="rt1", source="cli", model="ambient-model")

    stub = _make_stub(_session_db=db, session_id="rt1")
    stub._persist_model_switch_to_session(_Result())

    meta = db.get_session("rt1")
    restored = _make_stub()
    restored._restore_session_model(meta)
    assert restored.model == "deepseek-v4-flash-free"
    assert restored.provider == "custom:opencode-zen"
    assert restored.base_url == "https://oz/v1"


# ── update_session_model provider persistence (#79536) ──────────────


def test_update_session_model_persists_provider(tmp_path, monkeypatch):
    """update_session_model writes $.model + $.provider into model_config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli", model="m0")
    db.update_session_model("s1", "claude-x", provider="custom:feather")
    meta = db.get_session("s1")
    assert meta["model"] == "claude-x"
    config = json.loads(meta["model_config"])
    assert config["model"] == "claude-x"
    assert config["provider"] == "custom:feather"


def test_update_session_model_without_provider_preserves_existing(tmp_path, monkeypatch):
    """Without provider, existing $.provider is left untouched."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s2", source="cli", model="m0")
    db.update_session_model("s2", "claude-x", provider="custom:feather")
    db.update_session_model("s2", "gpt-5.4")  # no provider
    meta = db.get_session("s2")
    config = json.loads(meta["model_config"])
    assert config["model"] == "gpt-5.4"
    assert config["provider"] == "custom:feather"  # preserved


def test_update_session_model_null_model_config_with_provider(tmp_path, monkeypatch):
    """Provider persistence works when model_config starts as NULL."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s3", source="cli", model="m0")
    # model_config is NULL at creation — update_session_model must create it
    db.update_session_model("s3", "claude-x", provider="minimax")
    meta = db.get_session("s3")
    config = json.loads(meta["model_config"])
    assert config["model"] == "claude-x"
    assert config["provider"] == "minimax"


# ── session_gateway_runtime billing_provider fallback (#85721) ─────


def test_session_gateway_runtime_falls_back_to_billing_provider():
    """Sessions that never ran /model have only billing_provider."""
    meta = {
        "model": "glm-4.7",
        "model_config": None,
        "billing_provider": "minimax",
    }
    runtime = SessionDB.session_gateway_runtime(meta)
    assert runtime == {"provider": "minimax"}


def test_session_gateway_runtime_billing_provider_bare_bucket_ignored():
    """Bare billing buckets (auto/custom) are not routable — skip them."""
    for bare in ("auto", "custom"):
        meta = {
            "model": "m",
            "model_config": None,
            "billing_provider": bare,
        }
        assert SessionDB.session_gateway_runtime(meta) == {}


def test_session_gateway_runtime_explicit_provider_wins_over_billing():
    """Explicit model_config provider takes precedence over billing_provider."""
    meta = _row(model_config={"provider": "nous"})
    meta["billing_provider"] = "minimax"
    runtime = SessionDB.session_gateway_runtime(meta)
    assert runtime == {"provider": "nous"}


def test_restore_session_model_restores_billing_provider_fallback():
    """End-to-end: _restore_session_model uses billing_provider fallback."""
    stub = _make_stub()
    stub._restore_session_model({
        "model": "glm-4.7",
        "model_config": None,
        "billing_provider": "minimax",
    })
    assert stub.model == "glm-4.7"
    assert stub.provider == "minimax"


# ── single-query (-q) resume: the CONSTRUCTED agent runs the stored model ──
#
# Incident 2026-09-02 (L2 forensics D1): `hermes chat --resume <id> -q "..."`
# snapshots self.model/runtime into turn_route BEFORE _init_agent runs the
# session-model restore, then passes that stale snapshot back as
# model_override/runtime_override — which outranked the restored self.model,
# so the resumed session silently ran the ambient config default.
#
# These tests drive the real CLIAgentSetupMixin._init_agent on a bare
# HermesCLI with a fake session DB and a capturing AIAgent, asserting the
# CONSTRUCTED agent's route — not the CLI attribute.


class _FakeResumeSessionDB:
    """Minimal session-DB double for _init_agent's resume block."""

    def __init__(self, meta, messages):
        self._meta = meta
        self._messages = messages

    def get_session(self, session_id):
        return self._meta

    def resolve_resume_session_id(self, session_id):
        return session_id

    def get_messages_as_conversation(self, session_id, repair_alternation=True):
        return list(self._messages)

    def reopen_session(self, session_id):
        pass


class _CapturingAgent:
    """Stands in for cli.AIAgent: records the constructor kwargs."""

    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs


def _make_init_stub(session_meta, **overrides):
    """Bare HermesCLI wired for _init_agent's single-query resume path."""
    stub = _make_stub(
        agent=None,
        _resumed=True,
        _explicit_model_override=False,
        conversation_history=[],
        session_id="RQ1",
        _session_db=_FakeResumeSessionDB(
            session_meta,
            [
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
            ],
        ),
        tool_progress_mode="off",  # -q quiet: status lines go to stderr
        _single_query_mode=True,
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        max_tokens=None,
        max_turns=300,
        enabled_toolsets=None,
        disabled_toolsets=None,
        verbose=False,
        system_prompt=None,
        prefill_messages=[],
        reasoning_config=None,
        service_tier=None,
        _providers_only=None,
        _providers_ignore=None,
        _providers_order=None,
        _provider_sort=None,
        _provider_require_params=None,
        _provider_data_collection=None,
        _openrouter_min_coding_score=None,
        _fallback_model=None,
        checkpoints_enabled=False,
        checkpoint_max_snapshots=0,
        checkpoint_max_total_size_mb=0,
        checkpoint_max_file_size_mb=0,
        pass_session_id=False,
        ignore_rules=True,
        _inline_diffs_enabled=False,
        streaming_enabled=False,
        show_reasoning=False,
        _pending_title=None,
        # Neutralize side-effecting startup steps — not under test here.
        finalize_preloaded_skills=lambda: None,
        _install_tool_callbacks=lambda: None,
        _ensure_tirith_security=lambda: None,
        _ensure_runtime_credentials=lambda: True,
        _restore_session_cwd=lambda *a, **k: None,
        _restore_session_yolo=lambda *a, **k: None,
    )
    for key, value in overrides.items():
        setattr(stub, key, value)
    return stub


@pytest.fixture
def _patched_agent_construction(monkeypatch):
    """Capture AIAgent construction; silence MCP/deferred startup."""
    _CapturingAgent.last_kwargs = None
    monkeypatch.setattr(cli_mod, "AIAgent", _CapturingAgent)
    monkeypatch.setattr(cli_mod, "_prepare_deferred_agent_startup", lambda: None)
    import hermes_cli.mcp_startup as mcp_startup

    monkeypatch.setattr(
        mcp_startup, "ensure_mcp_discovery_before_agent_build", lambda **k: None
    )
    return _CapturingAgent


def _stored_kimi_meta():
    return {
        "model": "kimi-k3",
        "model_config": json.dumps({
            "gateway_runtime": {
                "provider": "kimi-coding",
                "base_url": "https://api.moonshot.cn/v1",
            },
        }),
    }


def _stale_turn_route():
    """The pre-restore snapshot the -q branch passes back into _init_agent."""
    return {
        "api_key": "ambient-key",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "api_mode": "",
        "command": None,
        "args": [],
        "credential_pool": None,
    }


def test_single_query_resume_constructs_agent_on_stored_model(
    _patched_agent_construction, monkeypatch
):
    """No -m: the restored session model/route must beat the stale snapshot."""
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(
        rp, "resolve_runtime_provider",
        lambda requested=None, **k: {"api_key": "kimi-key", "credential_pool": None},
    )
    stub = _make_init_stub(_stored_kimi_meta())
    ok = stub._init_agent(
        model_override="ambient-model",
        runtime_override=_stale_turn_route(),
    )
    assert ok is True
    kwargs = _CapturingAgent.last_kwargs
    assert kwargs is not None, "_init_agent never constructed the agent"
    assert kwargs["model"] == "kimi-k3"
    assert kwargs["provider"] == "kimi-coding"
    assert kwargs["base_url"] == "https://api.moonshot.cn/v1"
    assert kwargs["api_key"] == "kimi-key"


def test_single_query_resume_explicit_model_flag_still_wins(
    _patched_agent_construction,
):
    """An explicit -m on the command line outranks the stored session model."""
    stub = _make_init_stub(
        _stored_kimi_meta(),
        model="cli-flag-model",
        _explicit_model_override=True,
    )
    ok = stub._init_agent(
        model_override="cli-flag-model",
        runtime_override=_stale_turn_route(),
    )
    assert ok is True
    kwargs = _CapturingAgent.last_kwargs
    assert kwargs["model"] == "cli-flag-model"
    assert kwargs["provider"] == "openrouter"


def test_single_query_resume_fails_loud_on_unresolvable_stored_route(
    _patched_agent_construction, monkeypatch, capsys
):
    """Stored provider cannot be resolved and no -m was given: refuse to
    silently run the config default — _init_agent fails (the -q branch exits
    non-zero on a falsy return) and names the reason on stderr."""
    import hermes_cli.runtime_provider as rp

    def _no_route(requested=None, **k):
        raise RuntimeError("no credentials for stored provider")

    monkeypatch.setattr(rp, "resolve_runtime_provider", _no_route)
    stub = _make_init_stub(_stored_kimi_meta())
    ok = stub._init_agent(
        model_override="ambient-model",
        runtime_override=_stale_turn_route(),
    )
    assert ok is False
    err = capsys.readouterr().err
    assert "Cannot resume session RQ1 on its stored model route" in err
    assert "kimi-coding" in err


def test_single_query_resume_quiet_stderr_names_ignored_config_default(
    _patched_agent_construction, monkeypatch, capsys
):
    """Quiet-mode resume announces the stored route AND the ignored default."""
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(
        rp, "resolve_runtime_provider",
        lambda requested=None, **k: {"api_key": "kimi-key", "credential_pool": None},
    )
    stub = _make_init_stub(_stored_kimi_meta())
    assert stub._init_agent(
        model_override="ambient-model",
        runtime_override=_stale_turn_route(),
    ) is True
    err = capsys.readouterr().err
    assert (
        "Resumed session RQ1 on stored model kimi-k3 (kimi-coding) — "
        "config default ambient-model ignored"
    ) in err


# ── D4 overlay: resume restores the stored PRIMARY, never the fallback ────
#
# Incident 2026-09-02 follow-up: the first D4 fix persisted the FALLBACK
# route into the session row, pinning every later resume to kimi-k3 even
# after fable-5 recovered. The row now keeps the stored primary and carries
# model_config.fallback = {from, to, provider, reason, at} as a timestamped
# overlay; resume prints one line naming the detour.


def _fallback_overlay_row():
    return _row(
        model="claude-fable-5",
        model_config={
            "gateway_runtime": {
                "provider": "vibeproxy-claude",
                "base_url": "http://127.0.0.1:8317/v1",
            },
            "fallback": {
                "from": "claude-fable-5",
                "to": "kimi-k3",
                "provider": "kimi-coding",
                "reason": "server_error",
                "at": 1788438000.0,
            },
        },
    )


def test_restore_session_model_keeps_primary_and_announces_fallback_overlay():
    notes = []
    stub = _make_stub(_console_print=lambda s: notes.append(s))
    stub._restore_session_model(_fallback_overlay_row())
    # The stored PRIMARY is restored — never the fallback that answered last.
    assert stub.model == "claude-fable-5"
    assert stub.provider == "vibeproxy-claude"
    line = next(
        (n for n in notes if "last turn of this session" in n), None
    )
    assert line is not None, notes
    assert "fallback kimi-k3 (server_error," in line
    assert "resuming on primary claude-fable-5" in line


def test_restore_session_model_fallback_overlay_goes_to_stderr_when_quiet(capsys):
    stub = _make_stub()
    stub._restore_session_model(_fallback_overlay_row(), quiet=True)
    err = capsys.readouterr().err
    assert "last turn of this session was answered by fallback kimi-k3" in err
    assert "resuming on primary claude-fable-5" in err


def test_restore_session_model_without_overlay_prints_nothing():
    notes = []
    stub = _make_stub(_console_print=lambda s: notes.append(s))
    stub._restore_session_model(_row())
    assert not [n for n in notes if "last turn of this session" in n]


def test_persist_model_switch_clears_fallback_overlay(tmp_path, monkeypatch):
    """A deliberate /model switch supersedes the fallback detour: the overlay
    is deleted (explicit None), so the next resume prints no stale line."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="ov1", source="cli", model="claude-fable-5")
    db.patch_session_model_config("ov1", {
        "fallback": {
            "from": "claude-fable-5",
            "to": "kimi-k3",
            "provider": "kimi-coding",
            "reason": "server_error",
            "at": 1788438000.0,
        },
    })
    stub = _make_stub(_session_db=db, session_id="ov1")
    stub._persist_model_switch_to_session(_Result())
    config = json.loads(db.get_session("ov1")["model_config"])
    assert "fallback" not in config
    assert config["model"] == "deepseek-v4-flash-free"


# ── D1: restore-wins on EVERY resume entry ─────────────────────────────
#
# --continue, --resume latest, and the chat --oneshot single-query resume
# all resolve to args.resume and flow through _init_agent ->
# _restore_session_model. These tests pin each entry to that contract so a
# future fork in any of them cannot silently drop restore-wins.


def test_continue_resolves_into_resume_and_restores_stored_route(
    _patched_agent_construction, monkeypatch
):
    """--continue -> args.resume -> _init_agent restores the stored route."""
    import hermes_cli.main as main_mod
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(
        rp, "resolve_runtime_provider",
        lambda requested=None, **k: {"api_key": "kimi-key", "credential_pool": None},
    )

    args = type("Args", (), {"continue_last": True, "resume": None,
                             "create_if_missing": False})()
    monkeypatch.setattr(
        "hermes_cli.terminal_breadcrumbs.resolve_breadcrumb_session",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        main_mod, "_resolve_last_session", lambda source="cli": "RQ_CONT"
    )
    main_mod._resolve_continue_arg(args, use_tui=False)
    assert args.resume == "RQ_CONT"

    stub = _make_init_stub(_stored_kimi_meta(), session_id=args.resume)
    assert stub._init_agent(
        model_override="ambient-model",
        runtime_override=_stale_turn_route(),
    ) is True
    kwargs = _CapturingAgent.last_kwargs
    assert kwargs["model"] == "kimi-k3"  # stored route won over ambient default
    assert kwargs["provider"] == "kimi-coding"


def test_resume_latest_resolves_mru_and_restores_stored_route(
    _patched_agent_construction, monkeypatch
):
    """--resume latest -> MRU id -> _init_agent restores the stored route."""
    import hermes_cli.main as main_mod
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(
        rp, "resolve_runtime_provider",
        lambda requested=None, **k: {"api_key": "kimi-key", "credential_pool": None},
    )

    args = type("Args", (), {"resume": "latest"})()
    monkeypatch.setattr(
        main_mod, "_resolve_last_session", lambda source="cli": "RQ_LATEST"
    )
    monkeypatch.setattr(
        main_mod, "_resolve_session_by_name_or_id", lambda v: v
    )
    if isinstance(args.resume, str) and args.resume.strip().lower() == "latest":
        args.resume = main_mod._resolve_last_session(source="cli")

    stub = _make_init_stub(_stored_kimi_meta(), session_id=args.resume)
    assert stub._init_agent(
        model_override="ambient-model",
        runtime_override=_stale_turn_route(),
    ) is True
    kwargs = _CapturingAgent.last_kwargs
    assert kwargs["model"] == "kimi-k3"
    assert kwargs["provider"] == "kimi-coding"


def test_oneshot_exit_resume_uses_same_restore_wins_path(
    _patched_agent_construction, monkeypatch
):
    """chat --oneshot (-q single-query) resume: the exact _init_agent branch
    single-query mode takes (quiet banner + restore) restores the stored
    route — no separate oneshot resume fork exists, and this pins that."""
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(
        rp, "resolve_runtime_provider",
        lambda requested=None, **k: {"api_key": "kimi-key", "credential_pool": None},
    )
    stub = _make_init_stub(
        _stored_kimi_meta(),
        session_id="RQ1",
        tool_progress_mode="off",
        _single_query_mode=True,
    )
    assert stub._init_agent(
        model_override="ambient-model",
        runtime_override=_stale_turn_route(),
    ) is True
    kwargs = _CapturingAgent.last_kwargs
    assert kwargs["model"] == "kimi-k3"
    assert kwargs["provider"] == "kimi-coding"
