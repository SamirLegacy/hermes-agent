"""Tests for owner-gate routing in /approve (Owner directive 2026-08-06).

``/approve og-<id>`` must approve a held owner-gate action in the SAME chat surface
where the block appeared — no device switch. The gateway delegates to the
ultra_instinkt_guard plugin (which owns the pending store); these tests pin the
routing: og-ids reach the plugin, non-og args keep the dangerous-command meaning,
and a missing plugin degrades honestly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._pending_approvals = {}
    runner._session_db = None
    return runner


def _fake_plugin_manager(fn):
    loaded = SimpleNamespace(module=SimpleNamespace(owner_gate_approve_from_chat=fn))
    return SimpleNamespace(_plugins={"ultra_instinkt_guard": loaded})


class TestOwnerGateApproveRouting:
    @pytest.mark.asyncio
    async def test_og_id_routes_to_plugin(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "hermes_cli.plugins.get_plugin_manager",
            lambda: _fake_plugin_manager(lambda gid: calls.append(gid) or f"✅ owner gate {gid} approved"),
        )
        runner = _make_runner()
        result = await runner._handle_approve_command(_make_event("/approve og-abc123def456"))
        assert calls == ["og-abc123def456"]
        assert "approved" in result

    @pytest.mark.asyncio
    async def test_og_routing_works_without_pending_dangerous_approval(self, monkeypatch):
        """The og path must not depend on a blocking dangerous-command approval."""
        monkeypatch.setattr(
            "hermes_cli.plugins.get_plugin_manager",
            lambda: _fake_plugin_manager(lambda gid: "ok"),
        )
        runner = _make_runner()
        # no tools.approval state at all — a bare /approve would say no_pending
        result = await runner._handle_approve_command(_make_event("/approve og-f00df00d"))
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_bare_approve_keeps_dangerous_command_meaning(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.plugins.get_plugin_manager",
            lambda: _fake_plugin_manager(lambda gid: "MUST-NOT-BE-CALLED"),
        )
        runner = _make_runner()
        from tools import approval as mod
        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        mod._session_approved.clear()
        mod._permanent_approved.clear()
        mod._pending.clear()
        result = await runner._handle_approve_command(_make_event("/approve"))
        assert result == "No pending command to approve."

    @pytest.mark.asyncio
    async def test_missing_plugin_degrades_honestly(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.plugins.get_plugin_manager",
            lambda: SimpleNamespace(_plugins={}),
        )
        runner = _make_runner()
        result = await runner._handle_approve_command(_make_event("/approve og-abc123def456"))
        assert "not loaded" in result
        assert "ultra-instinkt-cli owner-gate-approve approve og-abc123def456" in result

    @pytest.mark.asyncio
    async def test_plugin_error_degrades_honestly(self, monkeypatch):
        def _boom(_gid):
            raise RuntimeError("store exploded")

        monkeypatch.setattr(
            "hermes_cli.plugins.get_plugin_manager",
            lambda: _fake_plugin_manager(_boom),
        )
        runner = _make_runner()
        result = await runner._handle_approve_command(_make_event("/approve og-abc123def456"))
        assert "failed" in result
