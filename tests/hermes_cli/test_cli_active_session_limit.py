from cli import HermesCLI
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    try_acquire_active_session,
)


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


# --- F2 port: claim fail-closed, --takeover wiring, lease re-anchor fail-closed ---


def _bare_cli(tmp_path, monkeypatch, session_id="claim-session"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cli = object.__new__(HermesCLI)
    cli.session_id = session_id
    cli.config = {}
    cli.takeover = False
    cli._active_session_lease = None
    cli._should_exit = False
    cli._lease_reanchor_failed = False
    cli._console_print = lambda text: None
    return cli


def test_claim_active_session_fails_closed_when_registry_raises(tmp_path, monkeypatch, caplog):
    """A claim that ERRORS has not proven the session is unowned — returning
    True there is a fail-open second-writer hole. It must log at WARNING with
    exc_info and return False (run() returns; the -q entry exits 1)."""
    import logging

    cli = _bare_cli(tmp_path, monkeypatch)

    def _explode(*a, **k):
        raise RuntimeError("registry file corrupted")

    monkeypatch.setattr(
        "hermes_cli.active_sessions.try_acquire_active_session", _explode
    )
    with caplog.at_level(logging.WARNING, logger="cli"):
        assert cli._claim_active_session("cli") is False
    warning_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Failed to claim active session slot" in r.getMessage()
    ]
    assert warning_records, caplog.records
    assert warning_records[0].exc_info is not None
    assert cli._active_session_lease is None


def test_claim_active_session_uses_takeover_when_flag_set(tmp_path, monkeypatch):
    """--takeover routes the claim through takeover_active_session."""
    cli = _bare_cli(tmp_path, monkeypatch)
    cli.takeover = True
    calls = []

    def _fake_takeover(**kwargs):
        calls.append(kwargs)
        return None, "simulated live-holder refusal"

    monkeypatch.setattr(
        "hermes_cli.active_sessions.takeover_active_session", _fake_takeover
    )
    monkeypatch.setattr(
        "hermes_cli.active_sessions.try_acquire_active_session",
        lambda **k: (_ for _ in ()).throw(AssertionError("plain acquire must not run")),
    )
    assert cli._claim_active_session("cli") is False
    assert calls and calls[0]["session_id"] == "claim-session"


def test_reanchor_failure_fails_closed_noninteractive(tmp_path, monkeypatch):
    """Non-interactive: a failed re-anchor flags _lease_reanchor_failed for the
    entry point's non-zero exit, and returns False so the caller stops."""
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    cli = _bare_cli(tmp_path, monkeypatch)
    assert cli._claim_active_session("cli") is True
    # Post-compression: agent rotated to a child id; the transfer is refused.
    class _Agent:
        session_id = "child-session"
    cli.agent = _Agent()
    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session",
        lambda *a, **k: False,
    )
    try:
        assert cli._reanchor_active_session_lease() is False
        assert cli._lease_reanchor_failed is True
        assert cli._should_exit is False
    finally:
        cli._release_active_session()


def test_reanchor_failure_exits_interactive(tmp_path, monkeypatch):
    """Interactive: a failed re-anchor prints the stop and sets _should_exit."""
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    cli = _bare_cli(tmp_path, monkeypatch)
    printed = []
    cli._console_print = printed.append
    assert cli._claim_active_session("cli") is True

    class _Agent:
        session_id = "child-session"
    cli.agent = _Agent()
    monkeypatch.setattr(
        "hermes_cli.active_sessions.transfer_active_session",
        lambda *a, **k: False,
    )
    try:
        assert cli._reanchor_active_session_lease() is False
        assert cli._should_exit is True
        assert cli._lease_reanchor_failed is False
        assert any("no longer" in t or "stopping" in t for t in printed)
    finally:
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        cli._release_active_session()


def test_reanchor_success_returns_true(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    cli = _bare_cli(tmp_path, monkeypatch)
    cli.config = {"max_concurrent_sessions": 1}
    assert cli._claim_active_session("cli") is True
    original_lease = cli._active_session_lease

    class _Agent:
        session_id = "child-session"
    cli.agent = _Agent()
    cli.session_id = "child-session"  # the caller rotates the id before re-anchoring
    try:
        assert cli._reanchor_active_session_lease() is True
        assert cli._active_session_lease is not None
        assert cli._active_session_lease.session_id == "child-session"
        assert cli._lease_reanchor_failed is False
        assert cli._active_session_lease is original_lease
        assert [e["session_id"] for e in active_session_registry_snapshot()] == [
            "child-session"
        ]
    finally:
        cli._release_active_session()


def test_reanchor_refuses_owned_child_without_losing_parent_lease(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    cli = _bare_cli(tmp_path, monkeypatch)
    assert cli._claim_active_session("cli") is True
    original_lease = cli._active_session_lease
    held, message = try_acquire_active_session(
        session_id="child-session", surface="tui", config={},
    )
    assert message is None
    assert held is not None

    class _Agent:
        session_id = "child-session"
    cli.agent = _Agent()
    cli.session_id = "child-session"
    try:
        assert cli._reanchor_active_session_lease() is False
        assert cli._lease_reanchor_failed is True
        assert cli._active_session_lease is original_lease
        assert sorted(e["session_id"] for e in active_session_registry_snapshot()) == [
            "child-session", "claim-session"
        ]
    finally:
        cli._release_active_session()
        held.release()


def test_quiet_single_query_exits_1_on_reanchor_failure(tmp_path, monkeypatch, capsys):
    """-Q: after a failed lease re-anchor the one-shot reports the failure on
    stderr and exits 1 instead of printing a session id it does not own."""
    import cli as cli_module

    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    cli = _bare_cli(tmp_path, monkeypatch)

    class _Agent:
        session_id = "claim-session"  # no rotation -> sync is a no-op

        def run_conversation(self, **kwargs):
            return {"final_response": "ok"}

    cli.agent = _Agent()
    cli.conversation_history = []
    cli._lease_reanchor_failed = True  # set by the failed re-anchor upstream
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    try:
        cli_module._run_quiet_single_query(cli, "hello")
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 1
    assert "could not prove ownership" in capsys.readouterr().err
