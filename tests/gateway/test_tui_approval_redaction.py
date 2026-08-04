"""Regression test for TUI approval-prompt credential redaction (#48456).

Follow-up to #50767, which redacted the chat-platform and SSE/API approval
transports. The TUI JSON-RPC transport is the third egress: three
`register_gateway_notify` callbacks in `tui_gateway/server.py` emit the raw
`approval_data` (with an unredacted `command`) to the TUI client. They now
route through the module-level `_emit_approval_request` helper, which redacts
`payload["command"]` via the shared `gateway.run._redact_approval_command` seam
before emitting.
"""

import inspect

import pytest


class TestTuiApprovalEmitRedaction:
    def test_emit_approval_request_redacts_command_in_payload(self, monkeypatch):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server, "_emit",
            lambda event, sid, payload=None: emitted.update(
                {"event": event, "sid": sid, "payload": payload}
            ),
        )
        raw = "curl -H 'Authorization: token ghp_01...6789' https://api.github.com"
        tui_server._emit_approval_request("sess-1", {"command": raw, "description": "x"})

        assert emitted["event"] == "approval.request"
        # credential removed, non-command field + command structure preserved
        assert "ghp_01...6789" not in emitted["payload"]["command"]
        assert emitted["payload"]["description"] == "x"
        assert "github.com" in emitted["payload"]["command"]


# =========================================================================
# No lazy in-function import for the redaction seam (2026-08-04 incident)
#
# _emit_approval_request used to do
# ``from gateway.run import _redact_approval_command`` INSIDE the function
# body, on every call. When gateway/run.py was mid-write during an in-place
# source update, that import raised ImportError mid-approval; the exception
# propagated into _await_gateway_decision's notify_cb try/except and was
# misreported as the user denying the request (see
# tests/tools/test_approval.py::TestApprovalNotifyFailureIsHonestNotADenial
# for the Fix-B side of this incident).
#
# Fix A: the helper now lives in gateway/redact_approval.py (a small module
# with no import-time dependency on gateway.run's ~2000-name module body),
# and both gateway/run.py and tui_gateway/server.py import it at module
# top-level. These tests pin that the lazy import does not come back.
# =========================================================================


class TestNoLazyRedactionImport:
    def test_redact_approval_command_is_module_level_in_tui_server(self):
        """_emit_approval_request must NOT contain a function-local
        ``from gateway.run import _redact_approval_command`` (or from
        gateway.redact_approval) — that is exactly the lazy import that
        raised ImportError mid-approval during an in-place source update
        and got misreported as a denial. The import must be a module-level
        name already bound before the function runs."""
        import ast
        import inspect

        from tui_gateway import server as tui_server

        source = inspect.getsource(tui_server)
        tree = ast.parse(source)

        target_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_emit_approval_request":
                target_fn = node
                break
        assert target_fn is not None, "_emit_approval_request not found in tui_gateway/server.py"

        for node in ast.walk(target_fn):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in ("gateway.run", "gateway.redact_approval"), (
                    "_emit_approval_request must not import the redaction helper "
                    "locally — a lazy import here is the exact defect that "
                    "misreported an approval-delivery ImportError as a user denial."
                )

        # The name must already be resolvable at module scope (bound by a
        # top-level import), not first defined inside the function.
        assert hasattr(tui_server, "_redact_approval_command"), (
            "_redact_approval_command must be importable at module scope in "
            "tui_gateway/server.py"
        )

    def test_redact_approval_command_import_is_top_level_in_module_source(self):
        """AST-check the module body (not nested in any function/class) for a
        top-level ``from gateway.redact_approval import _redact_approval_command``
        — refactor-robust against moving the call site around."""
        import ast
        import inspect

        from tui_gateway import server as tui_server

        source = inspect.getsource(tui_server)
        tree = ast.parse(source)

        found = False
        for node in tree.body:  # tree.body = top-level statements only
            if isinstance(node, ast.ImportFrom) and node.module == "gateway.redact_approval":
                if any(alias.name == "_redact_approval_command" for alias in node.names):
                    found = True
                    break
        assert found, (
            "gateway.redact_approval._redact_approval_command must be imported "
            "at module top level in tui_gateway/server.py"
        )

    def test_gateway_redact_approval_module_imports_standalone(self):
        """The extracted module must import cleanly on its own, with no
        dependency on gateway.run having already been imported — proving the
        seam is a genuinely standalone module, not a thin re-export that
        still requires the heavy gateway.run import graph to succeed first."""
        import importlib
        import sys

        # Drop any cached import of the heavy gateway.run module so this
        # test actually proves standalone importability rather than reusing
        # an already-successful prior import from earlier in the suite.
        sys.modules.pop("gateway.run", None)
        sys.modules.pop("gateway.redact_approval", None)

        mod = importlib.import_module("gateway.redact_approval")
        assert callable(mod._redact_approval_command)
        assert mod._redact_approval_command("echo hi") == "echo hi"

        # gateway.run must not have been pulled in as a side effect of
        # importing the small redaction module.
        assert "gateway.run" not in sys.modules

    def test_gateway_run_still_reexports_redact_approval_command(self):
        """gateway/run.py's existing call sites (and tests importing
        ``from gateway.run import _redact_approval_command``) must keep
        working after the extraction."""
        from gateway.run import _redact_approval_command

        assert callable(_redact_approval_command)
        assert _redact_approval_command("echo hi") == "echo hi"


