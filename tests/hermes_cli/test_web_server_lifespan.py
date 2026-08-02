import pytest

from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


def test_lifespan_shuts_down_mcp_servers(monkeypatch):
    calls = []

    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.setattr(web_server, "_warm_gateway_module", lambda: None)
    monkeypatch.setattr(
        web_server,
        "_shutdown_mcp_servers_best_effort",
        lambda: calls.append("shutdown"),
    )

    with TestClient(web_server.app):
        pass

    assert calls == ["shutdown"]
