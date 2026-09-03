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


def test_cli_claim_refusal_names_holder_and_takeover_hint(tmp_path, monkeypatch):
    """A refusal without --takeover exits 1-equivalent (claim returns False) and names the holder + next steps."""
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
        # Names the holder and the next step: the takeover command and the live-owner inspection.
        assert "--takeover" in printed[0]
        assert "hermes chat --resume owned-session --takeover" in printed[0]
        assert "hermes status" in printed[0]
    finally:
        held.release()


def test_cli_takeover_claim_steals_and_warns(tmp_path, monkeypatch, caplog):
    """--takeover steals the live owner's lease and warns about the
    in-memory-lease limitation of the old owner's process."""
    import logging
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
        assert cli._claim_active_session("cli") is True
        # Stole the lease: the registry now names our process as the owner.
        entries = active_session_registry_snapshot()
        assert [entry["session_id"] for entry in entries] == ["owned-session"]
        # One-line warning names the limitation (old owner's in-memory lease
        # keeps writing until its process exits).
        assert len(printed) == 1
        assert "Warning: --takeover" in printed[0]
        assert "never re-reads it" in printed[0]
        assert f"pid {os.getpid()}" in printed[0]
    finally:
        held.release()
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
