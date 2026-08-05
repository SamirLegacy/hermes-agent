"""Shared approval-command redaction seam.

Extracted from ``gateway.run`` (#48456 / #50767) so callers that need this
function do not have to import it lazily inside a hot code path. A prior
version of ``tui_gateway/server.py`` imported ``_redact_approval_command``
from ``gateway.run`` inside ``_emit_approval_request()`` at call time; when
``gateway.run`` was mid-write during an in-place source update, that
function-local import raised ``ImportError`` and the exception propagated up
through the approval-notify path, where it was misreported as a user denial
instead of a delivery failure (2026-08-04 incident). Module-level imports at
both call sites remove the load-bearing lazy import entirely.
"""


def _redact_approval_command(cmd: "str | None") -> str:
    """Redact credentials from a command before it goes into an approval prompt.

    Tirith's *findings* are already redacted, but the gateway approval prompt
    is built from the raw command string, so a credential-shaped value Tirith
    flagged would otherwise be echoed verbatim to the chat platform (#48456).
    Uses ``redact_sensitive_text(force=True)`` — the same Tirith-grade redactor
    — so the prompt honors redaction even when ``security.redact_secrets`` is
    off. Module-level so the wiring is unit-testable (the call site is a deeply
    nested gateway closure that cannot be driven directly).
    """
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(str(cmd or ""), force=True)
