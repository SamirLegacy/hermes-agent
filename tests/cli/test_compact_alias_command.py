"""Regression tests for the /compact alias to manual compression."""

from __future__ import annotations

from types import SimpleNamespace


def _make_cli_stub():
    """Minimal HermesCLI-shaped object for dispatching compression commands."""
    from cli import HermesCLI

    calls: list[str] = []
    self_ = SimpleNamespace(
        config={},
        _manual_compress=lambda cmd: calls.append(cmd),
    )
    self_.process_command = HermesCLI.process_command.__get__(self_, type(self_))
    return self_, calls


def test_compact_dispatches_to_manual_compress():
    cli, calls = _make_cli_stub()

    assert cli.process_command("/compact") is True

    assert calls == ["/compact"]


def test_compact_here_preserves_arguments_for_manual_compress():
    cli, calls = _make_cli_stub()

    assert cli.process_command("/compact here 3") is True

    assert calls == ["/compact here 3"]
