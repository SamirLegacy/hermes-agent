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
