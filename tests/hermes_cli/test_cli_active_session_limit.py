from cli import HermesCLI
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    try_acquire_active_session,
)


def test_claim_active_session_fails_closed_when_registry_raises(
    tmp_path, monkeypatch, caplog
):
    """R3/5: a claim that ERRORS has not proven the session is unowned —
    ``return True`` there is a fail-open second-writer hole. It must log at
    WARNING with exc_info and return False (the callers treat False as "not
    owned": run() returns, the -q entry exits 1)."""
    import logging

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cli = object.__new__(HermesCLI)
    cli.session_id = "claim-session"
    cli.config = {}
    cli._active_session_lease = None

    def _explode(*a, **k):
        raise RuntimeError("registry file corrupted")

    monkeypatch.setattr(
        "hermes_cli.active_sessions.try_acquire_active_session", _explode
    )
    with caplog.at_level(logging.WARNING, logger="cli"):
        assert cli._claim_active_session("cli") is False
    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "Failed to claim active session slot" in r.getMessage()
    ]
    assert warning_records, caplog.records
    assert warning_records[0].exc_info is not None
    assert cli._active_session_lease is None


def test_cli_claim_active_session_respects_global_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cfg = {"max_concurrent_sessions": 1}
    held, message = try_acquire_active_session(
        session_id="held-session",
        surface="tui",
        config=cfg,
    )
    assert message is None
    assert held is not None

    cli = object.__new__(HermesCLI)
    cli.session_id = "new-cli-session"
    cli.config = cfg
    cli._active_session_lease = None
    printed: list[str] = []
    cli._console_print = lambda text: printed.append(text)

    try:
        assert cli._claim_active_session("cli") is False
        assert len(printed) == 1
        assert "active session limit (1/1)" in printed[0]
        # Names the holding surface ("tui"), not the blocked one.
        assert "Held by: tui" in printed[0]

        held.release()

        assert cli._claim_active_session("cli") is True
        assert [entry["session_id"] for entry in active_session_registry_snapshot()] == [
            "new-cli-session"
        ]
    finally:
        held.release()
        cli._release_active_session()


def test_cli_claim_refusal_names_live_holder_and_quit_hint(tmp_path, monkeypatch):
    """A refusal without --takeover exits 1-equivalent (claim returns False) and,
    for a LIVE holder, names the holder + quit-first/`hermes status` next steps —
    --takeover is only advertised for dead/stale holders."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    held, _ = try_acquire_active_session(
        session_id="owned-session",
        surface="desktop",
        config={},
        metadata={"live_session_id": "desktop-owner"},
    )
    assert held is not None

    cli = object.__new__(HermesCLI)
    cli.session_id = "owned-session"
    cli.config = {}
    cli._active_session_lease = None
    cli._active_session_takeover = False
    printed: list[str] = []
    cli._console_print = lambda text: printed.append(text)

    try:
        assert cli._claim_active_session("cli") is False
        assert len(printed) == 1
        # Names the holder and the live-holder next steps: quit the holding
        # surface first, inspect owners with hermes status.
        assert "hermes status" in printed[0]
        assert "--takeover" not in printed[0]
    finally:
        held.release()


def test_cli_takeover_refused_while_holder_is_alive(tmp_path, monkeypatch):
    """--takeover against a LIVE holder refuses: the old owner keeps writing
    from its in-memory lease no matter what the registry says, so the steal
    would create two writers on one session."""
    import os

    from hermes_cli.active_sessions import active_session_registry_snapshot

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    held, _ = try_acquire_active_session(
        session_id="owned-session",
        surface="desktop",
        config={},
        metadata={"live_session_id": "desktop-owner"},
    )
    assert held is not None

    cli = object.__new__(HermesCLI)
    cli.session_id = "owned-session"
    cli.config = {}
    cli._active_session_lease = None
    cli._active_session_takeover = True
    printed: list[str] = []
    cli._console_print = lambda text: printed.append(text)

    try:
        assert cli._claim_active_session("cli") is False
        assert len(printed) == 1
        assert (
            f"holder pid {os.getpid()} (desktop) is alive — quit it first (or wait); "
            "--takeover only reclaims stale/dead leases" in printed[0]
        )
        # The holder's registry entry is untouched.
        entries = active_session_registry_snapshot()
        assert [entry["session_id"] for entry in entries] == ["owned-session"]
        assert entries[0]["pid"] == os.getpid()
        assert cli._active_session_lease is None
    finally:
        held.release()


def test_cli_takeover_succeeds_when_holder_is_dead(tmp_path, monkeypatch):
    """--takeover reclaims a dead holder's lease: pruning already removed the
    corpse, so the claim succeeds as a plain acquire."""
    import os

    from hermes_cli import active_sessions
    from hermes_cli.active_sessions import active_session_registry_snapshot

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    held, _ = try_acquire_active_session(
        session_id="owned-session",
        surface="desktop",
        config={},
        metadata={"live_session_id": "desktop-owner"},
    )
    assert held is not None
    # Kill the holder: rewrite its entry to a dead pid, then drop the
    # in-memory lease object (its release must not fire later).
    state_path = active_sessions._state_path()
    entries = active_sessions._read_entries(state_path)
    entries[0]["pid"] = 0x7FFFFFFE
    entries[0]["process_start_time"] = 1.0
    active_sessions._write_entries(state_path, entries)
    held.released = True

    cli = object.__new__(HermesCLI)
    cli.session_id = "owned-session"
    cli.config = {}
    cli._active_session_lease = None
    cli._active_session_takeover = True
    printed: list[str] = []
    cli._console_print = lambda text: printed.append(text)

    try:
        assert cli._claim_active_session("cli") is True
        entries = active_session_registry_snapshot()
        assert [entry["session_id"] for entry in entries] == ["owned-session"]
        assert entries[0]["pid"] == os.getpid()
        # No takeover warning: a dead holder cannot keep writing.
        assert not any("Warning: --takeover" in text for text in printed)
    finally:
        cli._release_active_session()


def test_cli_reanchors_lease_after_compression_rekey(tmp_path, monkeypatch):
    """E: when compression rotates the CLI's live session id, the registry entry
    follows it, so the continuation session is protected and the refusal names
    the LIVE id (L3 §5 defect)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    cli = object.__new__(HermesCLI)
    cli.session_id = "parent-session"
    cli.config = {}
    cli._active_session_lease = None
    cli._active_session_takeover = False
    cli._console_print = lambda text: None

    assert cli._claim_active_session("cli") is True
    entries = active_session_registry_snapshot()
    assert [(entry["session_id"], entry["metadata"]["live_session_id"]) for entry in entries] == [
        ("parent-session", "parent-session")
    ]

    # The compression sync assigns the new id before the re-anchor (both call sites).
    cli.session_id = "child-session"
    cli._reanchor_active_session_lease()

    entries = active_session_registry_snapshot()
    assert [(entry["session_id"], entry["metadata"]["live_session_id"]) for entry in entries] == [
        ("child-session", "child-session")
    ]
    cli._release_active_session()
    assert active_session_registry_snapshot() == []


def _make_reanchor_cli(tmp_path, monkeypatch, *, single_query: bool):
    """CLI with a live lease on parent-session, session_id rotated to the child."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cli = object.__new__(HermesCLI)
    cli.session_id = "parent-session"
    cli.config = {}
    cli._active_session_lease = None
    cli._active_session_takeover = False
    cli._should_exit = False
    cli._single_query_mode = single_query
    cli._lease_reanchor_failed = False
    printed: list[str] = []
    cli._console_print = lambda text: printed.append(text)
    assert cli._claim_active_session("cli") is True
    cli.session_id = "child-session"
    return cli, printed


def test_reanchor_failure_fails_closed_interactive(tmp_path, monkeypatch, caplog):
    """Neither transfer nor re-acquire yields a lease on the NEW id: do NOT
    continue the turn silently — red console line and the interactive loop
    stops (a second surface could otherwise claim the live continuation
    mid-write)."""
    import logging

    cli, printed = _make_reanchor_cli(tmp_path, monkeypatch, single_query=False)
    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "hermes_cli.active_sessions.try_acquire_active_session",
        lambda *a, **k: (None, "simulated capacity refusal"),
    )
    try:
        with caplog.at_level(logging.WARNING, logger="cli"):
            ok = cli._reanchor_active_session_lease()
        assert ok is False
        assert len(printed) == 1
        assert "child-session" in printed[0]
        assert "simulated capacity refusal" in printed[0]
        assert cli._should_exit is True
        assert cli._lease_reanchor_failed is True
        assert any(
            record.levelno == logging.WARNING
            and "did not re-anchor" in record.getMessage()
            for record in caplog.records
        )
    finally:
        cli._release_active_session()


def test_reanchor_failure_prints_stderr_in_single_query(tmp_path, monkeypatch, capsys):
    """-q: the failure goes to stderr (the sanctioned quiet-mode channel) and
    flags the run for a non-zero exit."""
    cli, printed = _make_reanchor_cli(tmp_path, monkeypatch, single_query=True)
    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "hermes_cli.active_sessions.try_acquire_active_session",
        lambda *a, **k: (None, "simulated capacity refusal"),
    )
    try:
        ok = cli._reanchor_active_session_lease()
        assert ok is False
        err = capsys.readouterr().err
        assert "child-session" in err
        assert "simulated capacity refusal" in err
        assert printed == []  # nothing on the interactive console channel
        assert cli._should_exit is False  # -q exits via the caller, not the loop
        assert cli._lease_reanchor_failed is True
    finally:
        cli._release_active_session()


def test_reanchor_exception_is_logged_with_exc_info_and_fails_closed(
    tmp_path, monkeypatch, caplog, capsys
):
    """No swallowed exceptions: a transfer that raises logs at WARNING with
    exc_info and still fails the turn closed."""
    import logging

    cli, printed = _make_reanchor_cli(tmp_path, monkeypatch, single_query=True)

    def _explode(*a, **k):
        raise RuntimeError("registry lock broke")

    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session", _explode
    )
    try:
        with caplog.at_level(logging.WARNING, logger="cli"):
            ok = cli._reanchor_active_session_lease()
        assert ok is False
        assert "registry lock broke" in capsys.readouterr().err
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "did not re-anchor" in r.getMessage()
        ]
        assert warning_records, caplog.records
        assert warning_records[0].exc_info is not None
    finally:
        cli._release_active_session()


# --- R3/1: the /compress and post-turn call sites must honor the re-anchor bool ---

def _reanchor_fail_cli(tmp_path, monkeypatch, *, single_query: bool):
    """CLI whose mid-compress re-anchor FAILS (neither transfer nor
    re-acquire yields a lease on the continuation id)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cli = object.__new__(HermesCLI)
    cli.session_id = "parent-session"
    cli.config = {}
    cli._active_session_lease = None
    cli._active_session_takeover = False
    cli._should_exit = False
    cli._single_query_mode = single_query
    cli._lease_reanchor_failed = False
    cli._console_print = lambda text: None
    assert cli._claim_active_session("cli") is True
    # The CLI stays on the parent id: the handler itself rotates
    # self.session_id onto the agent's continuation id before re-anchoring.
    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "hermes_cli.active_sessions.try_acquire_active_session",
        lambda *a, **k: (None, "simulated capacity refusal"),
    )
    return cli


class _CompressAgentStub:
    """Agent double for the /compress handler: compression succeeded and
    rotated the agent onto a continuation session."""

    def __init__(self, child_id):
        self.session_id = child_id
        self.conversation_history = []
        self._flush_messages_to_session_db_calls = []

    def _compress_context(self, *a, **k):
        return ([{"role": "user", "content": "compressed handoff"}], None)

    def _flush_messages_to_session_db(self, history, offset):
        self._flush_messages_to_session_db_calls.append((history, offset))


def test_manual_compress_reanchor_failure_does_not_flush_continuation(
    tmp_path, monkeypatch, capsys
):
    """/compress: when the lease cannot re-anchor onto the continuation id,
    the handler must NOT flush the transcript onto it (a second surface may
    already hold it) and must return without the post-compression summary —
    the fail-closed path has already surfaced its stop."""
    cli = _reanchor_fail_cli(tmp_path, monkeypatch, single_query=False)
    agent = _CompressAgentStub("child-session")
    cli.agent = agent
    cli.conversation_history = [
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "m2"},
        {"role": "user", "content": "m3"},
        {"role": "assistant", "content": "m4"},
    ]
    cli._compression_skipped_due_to_lock = None
    try:
        cli._manual_compress("/compress")
        assert agent._flush_messages_to_session_db_calls == [], (
            "re-anchor failure must not flush the continuation transcript"
        )
        assert cli._lease_reanchor_failed is True
        out = capsys.readouterr().out
        assert "Compression failed" not in out  # no fake exception path
    finally:
        cli._release_active_session()


def test_manual_compress_reanchor_success_still_flushes(
    tmp_path, monkeypatch, capsys
):
    """Counterpart: a successful re-anchor keeps the documented /compress
    behavior — the continuation transcript is persisted from offset 0."""
    cli = _reanchor_fail_cli(tmp_path, monkeypatch, single_query=False)
    # Re-anchor now succeeds (plain transfer).
    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session",
        lambda *a, **k: True,
    )
    agent = _CompressAgentStub("child-session")
    cli.agent = agent
    cli.conversation_history = [
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "m2"},
        {"role": "user", "content": "m3"},
        {"role": "assistant", "content": "m4"},
    ]
    cli._compression_skipped_due_to_lock = None
    try:
        cli._manual_compress("/compress")
        assert len(agent._flush_messages_to_session_db_calls) == 1
    finally:
        cli._release_active_session()


def _make_chat_cli(monkeypatch):
    """Full HermesCLI via the proven prompt_toolkit-stub import harness
    (same pattern as tests/cli/test_cli_interrupt_ack_race.py)."""
    import importlib
    import sys
    from unittest.mock import MagicMock, patch

    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}, clear=False
    ):
        import cli as _cli_mod

        _cli_mod = importlib.reload(_cli_mod)
        with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            _cli_mod.__dict__, {"CLI_CONFIG": _clean_config}
        ):
            return _cli_mod.HermesCLI()


class _TurnAgentStub:
    """Agent whose turn succeeds and rotates onto a continuation session
    (auto-compression fired mid-turn)."""

    def __init__(self, child_id):
        self.session_id = child_id
        self._pending_cli_user_message = None
        self.max_iterations = 90
        self.model = "test/model"
        self.platform = "cli"
        self._active_children = []

    def run_conversation(self, **kwargs):
        return {
            "final_response": "turn done",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "turn done"},
            ],
            "api_calls": 1,
            "completed": True,
            "partial": True,  # skip auto-title thread in the test
            "response_previewed": True,  # skip Rich Panel under pt stubs
        }


def test_chat_post_turn_reanchor_failure_stops_before_owner_steps(
    tmp_path, monkeypatch, capsys
):
    """Post-turn sync: when the continuation lease cannot be re-anchored,
    chat() returns None immediately — no breadcrumb write, no pending-title
    reset on the unowned id. The fail-closed path has already surfaced its
    stop (red line + _should_exit / -q flag)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cli = _make_chat_cli(monkeypatch)
    agent = _TurnAgentStub("child-session")
    cli.agent = agent
    cli.session_id = "parent-session"
    parent_id = cli.session_id
    assert cli._claim_active_session("cli") is True
    cli.session_id = parent_id  # claim used the parent id
    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "hermes_cli.active_sessions.try_acquire_active_session",
        lambda *a, **k: (None, "simulated capacity refusal"),
    )
    breadcrumbs = []
    monkeypatch.setattr(
        cli, "_write_terminal_breadcrumb", lambda: breadcrumbs.append(1)
    )
    from unittest.mock import patch

    try:
        with patch.object(cli, "_ensure_runtime_credentials", return_value=True), \
             patch.object(cli, "_resolve_turn_agent_config", return_value={
                 "signature": cli._active_agent_route_signature,
                 "model": None, "runtime": None, "request_overrides": None,
             }), \
             patch.object(cli, "_init_agent", return_value=True):
            ret = cli.chat("hello")
        assert ret is None
        assert breadcrumbs == [], (
            "post-turn owner steps must not run on an unowned continuation id"
        )
        assert cli._lease_reanchor_failed is True
        assert cli._should_exit is True
    finally:
        cli._release_active_session()


def test_chat_post_turn_reanchor_success_runs_owner_steps(
    tmp_path, monkeypatch, capsys
):
    """Counterpart: with the re-anchor succeeding (plain transfer), the
    post-turn sync proceeds exactly as before."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cli = _make_chat_cli(monkeypatch)
    agent = _TurnAgentStub("child-session")
    cli.agent = agent
    cli.session_id = "parent-session"
    assert cli._claim_active_session("cli") is True
    cli.session_id = "parent-session"
    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session",
        lambda *a, **k: True,
    )
    breadcrumbs = []
    monkeypatch.setattr(
        cli, "_write_terminal_breadcrumb", lambda: breadcrumbs.append(1)
    )
    from unittest.mock import patch

    try:
        with patch.object(cli, "_ensure_runtime_credentials", return_value=True), \
             patch.object(cli, "_resolve_turn_agent_config", return_value={
                 "signature": cli._active_agent_route_signature,
                 "model": None, "runtime": None, "request_overrides": None,
             }), \
             patch.object(cli, "_init_agent", return_value=True):
            ret = cli.chat("hello")
        assert ret == "turn done"
        assert breadcrumbs == [1]
        assert cli.session_id == "child-session"
        assert getattr(cli, "_lease_reanchor_failed", False) is False
    finally:
        cli._release_active_session()
