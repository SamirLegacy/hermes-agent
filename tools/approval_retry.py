"""Transient final-execution binding for a successfully rechecked native retry.

No permit storage. The pre-hook dispatcher explicitly fills an execution frame;
standalone prechecks have no frame to fill. Only prechecked framework forwarding
shares a frame, and only a final handler may use it for native gate deduplication.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
import os
import threading

from tools import approval_context as ctx


class RetryExecutionInvalid(RuntimeError):
    """An observed retry no longer identifies its one pending execution."""


@dataclass
class RetryExecution:
    tool_name: str
    session_id: str
    tool_call_id: str
    invocation: ctx._PreToolInvocation | None = None
    active: bool = True
    used: bool = False
    running: bool = False
    thread_id: int | None = None
    lock: Any = field(default_factory=threading.RLock, repr=False)
    observed: bool = False
    abandoned: bool = False

    def abandon(self):
        """Called by the existing runner BEFORE it returns a terminal abandonment.

        The frame exists before submit, even if policy/consent has not filled it.
        Closing an empty frame changes only a subsequently observed typed retry.
        """
        with self.lock:
            self.abandoned = True
            self.active = False

    def observe_retry(self):
        with self.lock:
            self.observed = True
            self.require_open()

    def require_open(self):
        with self.lock:
            if not self.active:
                raise RetryExecutionInvalid('BLOCKED: retry execution binding changed or closed')

    def bind(self, invocation, args):
        with self.lock:
            self.require_open()
            self.invocation = invocation
            self.check(args)

    def suppress_late_result(self):
        with self.lock:
            return self.abandoned and self.observed

    def check(self, args):
        if self.observed:
            self.require_open()
        if self.invocation is None:
            return
        from hermes_constants import get_hermes_home
        inv = self.invocation
        try:
            matches = (self.active and (self.tool_name, self.session_id, self.tool_call_id) ==
                       (inv.tool_name, inv.session_id, inv.tool_call_id) and
                       str(get_hermes_home()) == inv.home and ctx.get_current_session_key() == inv.session_key and
                       str(os.environ.get('TERMINAL_CWD') or os.getcwd()) == inv.cwd and
                       ctx._tool_arguments(args) == inv.arguments)
        except Exception:
            matches = False
        if not matches:
            self.active = False
            raise RetryExecutionInvalid('BLOCKED: retry execution binding changed or closed')


_current: ContextVar[RetryExecution | None] = ContextVar('retry_execution_frame', default=None)
_native: ContextVar[tuple | None] = ContextVar('retry_native_handler', default=None)


@dataclass
class _Forwarding:
    scope: RetryExecution
    thread_id: int
    available: bool = True


_forward: ContextVar[_Forwarding | None] = ContextVar('retry_framework_forward', default=None)


def forward_execution(scope, callback, *args, **kwargs):
    """Hand off only at an explicit framework continuation, not by public IDs.

    The callee consumes this transient handoff on entry, before its middleware.
    Copies share the consumed bit; a saved or foreign-thread context cannot reuse it.
    This is framework plumbing, not a sandbox against trusted Python code.
    """
    if scope.invocation is None:
        return callback(*args, **kwargs)
    handoff = _Forwarding(scope, threading.get_ident())
    token = _forward.set(handoff)
    try:
        return callback(*args, **kwargs)
    finally:
        with scope.lock:
            handoff.available = False
        _forward.reset(token)


def _take_forwarding(tool_name, session_id, tool_call_id, prechecked):
    handoff = _forward.get()
    _forward.set(None)
    if handoff is None:
        return None
    scope = handoff.scope
    with scope.lock:
        if not handoff.available:
            return None
        handoff.available = False
        if (not prechecked or handoff.thread_id != threading.get_ident() or
                scope is not _current.get() or scope.running):
            return None
        if (tool_name, session_id or '', tool_call_id or '') != (
                scope.tool_name, scope.session_id, scope.tool_call_id):
            raise RetryExecutionInvalid('BLOCKED: retry forwarding identity changed')
        scope.require_open()
        return scope


def current_execution():
    return _current.get()


@contextmanager
def execution_scope(tool_name, session_id, tool_call_id, *, prechecked=False, owner=None):
    forwarded = _take_forwarding(tool_name, session_id, tool_call_id, prechecked)
    if forwarded is not None:
        yield forwarded
        return
    scope = owner if owner is not None else RetryExecution(tool_name, session_id or '', tool_call_id or '')
    token = _current.set(scope)
    native_token = _native.set(None)
    try:
        yield scope
    finally:
        with scope.lock:
            scope.active = False
        _native.reset(native_token)
        _current.reset(token)


def run_handler(scope, args, handler):
    """The final inline/registry boundary, not an earlier middleware boundary."""
    if scope is None or scope.invocation is None:
        return handler(args)
    with scope.lock:
        scope.check(args)
        if scope.used:
            raise RetryExecutionInvalid('BLOCKED: retry handler already used')
        scope.used = True
        scope.running = True
        scope.thread_id = threading.get_ident()
    token = _native.set((scope, args))
    try:
        return handler(args)
    finally:
        with scope.lock:
            scope.running = False
        _native.reset(token)


def native_retry_covers_command(command):
    """Only the exact terminal command in the currently running retry handler."""
    native = _native.get()
    if native is None:
        return False
    scope, args = native
    if (scope is not _current.get() or not scope.running or
            scope.thread_id != threading.get_ident() or scope.tool_name != 'terminal' or
            args.get('command') != command):
        return False
    with scope.lock:
        scope.check(args)
        return True
