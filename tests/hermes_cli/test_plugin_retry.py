"""Generic retry protocol tests. UI replies and failures are TEST-ONLY fixtures.

No SDK/plugin dependency here. The real guard/SQLite integration is a separate
staged fixture; these tests exercise the native transport and generic runtime.
"""
import os
import threading
from contextlib import contextmanager

import pytest

from hermes_cli import plugins
from tools import approval, approval_context as ctx


@pytest.fixture
def policy(monkeypatch, tmp_path):
    for name in ("HERMES_SINGLE_QUERY_SESSION", "HERMES_CRON_SESSION", "HERMES_EXEC_ASK"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "gui")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr(approval, "_session_approved", {})
    monkeypatch.setattr(approval, "_permanent_approved", set())
    monkeypatch.setattr(approval, "_session_yolo", set())
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(ctx, "_get_approval_timeout", lambda: 2)
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    monkeypatch.setattr(plugins, "_plugin_managers_by_home", {})
    seen = []

    def check(tool_name, args, session_id, tool_call_id, **kw):
        request = ctx.make_pre_tool_retry(
            profile="fixture-profile", failure_id="fixture-failure", generation="fixture-generation",
            action_fingerprint="fixture-existing-fingerprint", tool_name=tool_name, args=args,
            session_id=session_id, tool_call_id=tool_call_id, cwd=os.environ["TERMINAL_CWD"],
        )
        if ctx.is_pre_tool_recheck():
            granted = ctx.consume_pre_tool_retry(request)
            seen.append((session_id, tool_call_id, granted))
            assert not ctx.consume_pre_tool_retry(request), "same continuation cannot be consumed twice"
            if granted:
                return None
        return {"action": "block", "message": "fixture verified failure", "retry": request}

    manager._hooks["pre_tool_call"] = [check]
    return manager, seen


def dispatch(session="fixture-a", call="fixture-call", args=None):
    return plugins._dispatch_pre_tool_call_hooks(
        "fixture_tool", {"item": "fixture"} if args is None else args,
        session_id=session, tool_call_id=call,
    )[0]


@contextmanager
def ui(key, callback):
    token = ctx.set_current_session_key(key)
    approval.register_gateway_notify(key, callback)
    try:
        yield
    finally:
        approval.unregister_gateway_notify(key)
        ctx.reset_current_session_key(token)


def test_native_once_is_stack_scoped(policy):
    manager, seen = policy
    requests = []

    def answer(data):
        requests.append(data)
        assert not data["allow_session"] and not data["allow_permanent"]
        assert approval.resolve_gateway_approval("fixture-route", "once", request_id=data["request_id"]) == 1

    with ui("fixture-route", answer):
        assert dispatch() is None
        assert dispatch(call="fixture-next") is None
    assert len(requests) == 2
    assert seen == [("fixture-a", "fixture-call", True), ("fixture-a", "fixture-next", True)]
    assert ctx._pre_tool_retry_grant.get() is None
    assert ctx._pre_tool_invocation.get() is None
    assert approval._session_approved == {}
    assert approval._permanent_approved == set()


@pytest.mark.parametrize("answer", ["session", "always", "deny", "timeout", "invalid"])
def test_only_explicit_once_can_mint_retry(policy, answer):
    _, seen = policy
    with ui("fixture-route", lambda data: approval.resolve_gateway_approval(
            "fixture-route", answer, request_id=data["request_id"])):
        assert dispatch() is not None
    assert seen == []
    assert approval._session_approved == {}
    assert approval._permanent_approved == set()


def test_ordinary_first_directive_and_modify_prefix_are_preserved(policy):
    manager, _ = policy
    calls, prompts = [], []

    def callback(value):
        def run(**kw):
            calls.append(value)
            return value
        return run

    manager._hooks["pre_tool_call"] = [callback(value) for value in [
        {"action": "modify", "args": {"item": "prefix"}},
        {"action": "approve", "message": "fixture permission"},
        {"action": "modify", "args": {"item": "after-first-directive"}},
        {"action": "block", "message": "fixture later veto"},
    ]]
    def answer(data):
        prompts.append(data)
        approval.resolve_gateway_approval("fixture-route", "once", request_id=data["request_id"])
    with ui("fixture-route", answer):
        block, args = plugins._dispatch_pre_tool_call_hooks(
            "fixture_tool", {"item": "fixture"}, session_id="fixture-a", tool_call_id="fixture-call")
    assert block is None
    assert args == {"item": "prefix"}
    assert len(prompts) == 1
    assert len(calls) == 4, "scope discovery must not invoke callbacks again"


@pytest.mark.parametrize("phase", ["initial", "recheck"])
def test_later_veto_dominates_only_an_actual_retry(policy, phase):
    manager, _ = policy
    def later(**kw):
        if phase == "initial" or ctx.is_pre_tool_recheck():
            return {"action": "block", "message": "fixture later veto"}
    manager._hooks["pre_tool_call"].insert(0, lambda **kw: {"action": "approve"})
    manager._hooks["pre_tool_call"].append(later)
    prompts = []
    def answer(data):
        prompts.append(data)
        approval.resolve_gateway_approval("fixture-route", "once", request_id=data["request_id"])
    with ui("fixture-route", answer):
        assert dispatch() == "fixture later veto"
    assert len(prompts) == (phase == "recheck")


@pytest.mark.parametrize("missing", ["session", "call"])
def test_missing_correlation_never_requests_retry(policy, missing):
    with ui("fixture-route", lambda data: pytest.fail("missing correlation must fail closed")):
        assert dispatch(session="" if missing == "session" else "fixture-a",
                        call="" if missing == "call" else "fixture-call") is not None


def test_args_mutation_while_waiting_fails_closed(policy):
    _, seen = policy
    args = {"item": "fixture"}

    def answer(data):
        args["item"] = "changed"
        approval.resolve_gateway_approval("fixture-route", "once", request_id=data["request_id"])

    with ui("fixture-route", answer):
        assert dispatch(args=args) is not None
    assert seen == []


def test_no_blanket_or_cross_request_reply(policy):
    def answer(data):
        assert approval.resolve_gateway_approval("fixture-route", "once") == 0
        assert approval.resolve_gateway_approval("fixture-route", "once", resolve_all=True) == 0
        assert approval.resolve_gateway_approval("fixture-other-route", "once", request_id=data["request_id"]) == 0
        assert approval.resolve_gateway_approval("fixture-route", "once", request_id="stale") == 0
        assert approval.resolve_gateway_approval("fixture-route", "deny", request_id=data["request_id"]) == 1
    with ui("fixture-route", answer):
        assert dispatch() is not None


def test_cli_reuses_native_once_only_prompt(policy, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "")
    token = ctx.set_hermes_interactive_context(True)
    shown = []

    def human(command, description, **kwargs):
        shown.append(kwargs)
        assert not kwargs["allow_session"] and not kwargs["allow_permanent"]
        return "once"

    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: human)
    try:
        assert dispatch() is None
    finally:
        ctx.reset_hermes_interactive_context(token)
    assert len(shown) == 1


def test_unattended_autoapprove_is_not_a_human_retry(policy, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
    monkeypatch.setattr(ctx, "_get_unattended_approval_mode", lambda: "approve")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    assert dispatch() is not None


def test_concurrent_pending_sessions_cannot_cross_consume(policy):
    _, seen = policy
    # Wait for BOTH pending requests before resolving either: they overlap in
    # the real queue; no sleeps or fabricated decisions.
    barrier = threading.Barrier(2, timeout=5)
    first_pending = threading.Event()
    ids = {}
    outputs = {}
    errors = []

    def run(key, answer):
        try:
            def reply(data):
                ids[key] = data["request_id"]
                if key == "fixture-a":
                    first_pending.set()
                barrier.wait()
                other = "fixture-b" if key == "fixture-a" else "fixture-a"
                assert approval.resolve_gateway_approval(other, "once", request_id=data["request_id"]) == 0
                assert approval.resolve_gateway_approval(key, answer, request_id=data["request_id"]) == 1
            with ui(key, reply):
                outputs[key] = dispatch(session=key, call=f"{key}-call")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=("fixture-a", "once")),
               threading.Thread(target=run, args=("fixture-b", "deny"))]
    threads[0].start()
    assert first_pending.wait(timeout=5)
    threads[1].start()
    for thread in threads:
        thread.join(timeout=8)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert set(ids) == {"fixture-a", "fixture-b"}
    assert outputs["fixture-a"] is None
    assert outputs["fixture-b"] is not None
    assert seen == [("fixture-a", "fixture-a-call", True)]


def test_wait_is_outside_bounded_callback(policy):
    manager, _ = policy
    def reply(data):
        assert manager._hook_running_callbacks == {}, "no timed hook callback may wait for a human"
        approval.resolve_gateway_approval("fixture-route", "once", request_id=data["request_id"])
    with ui("fixture-route", reply):
        assert dispatch() is None


def test_abandoned_recheck_callback_cannot_consume_late(policy, monkeypatch):
    manager, _ = policy
    original = manager._hooks["pre_tool_call"][0]
    release, finished = threading.Event(), threading.Event()
    late = []
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.1)

    def delayed(**kw):
        if not ctx.is_pre_tool_recheck():
            return original(**kw)
        try:
            release.wait(timeout=5)
            request = ctx.make_pre_tool_retry(
                profile="fixture-profile", failure_id="fixture-failure", generation="fixture-generation",
                action_fingerprint="fixture-existing-fingerprint", tool_name=kw["tool_name"], args=kw["args"],
                session_id=kw["session_id"], tool_call_id=kw["tool_call_id"], cwd=os.environ["TERMINAL_CWD"],
            )
            late.append(ctx.consume_pre_tool_retry(request))
        finally:
            finished.set()

    manager._hooks["pre_tool_call"] = [delayed]
    try:
        with ui("fixture-route", lambda data: approval.resolve_gateway_approval(
                "fixture-route", "once", request_id=data["request_id"])):
            assert dispatch() is not None
    finally:
        release.set()
    assert finished.wait(timeout=5)
    assert late == [False]
    assert ctx._pre_tool_retry_grant.get() is None


def test_recheck_does_not_duplicate_firstparty_observation(policy, monkeypatch):
    observed = []
    monkeypatch.setattr("hermes_cli.lifecycle._observe", lambda name, **kw: observed.append(name))
    with ui("fixture-route", lambda data: approval.resolve_gateway_approval(
            "fixture-route", "once", request_id=data["request_id"])):
        assert dispatch() is None
    assert observed.count("pre_tool_call") == 1
    assert observed.count("pre_approval_request") == 1
    assert observed.count("post_approval_response") == 1


def test_later_policy_exception_is_a_recheck_veto(policy):
    manager, _ = policy
    def later(**kw):
        if ctx.is_pre_tool_recheck():
            raise RuntimeError("fixture unavailable permission policy")
    manager._hooks["pre_tool_call"].append(later)
    with ui("fixture-route", lambda data: approval.resolve_gateway_approval(
            "fixture-route", "once", request_id=data["request_id"])):
        assert dispatch() == "BLOCKED: pre-tool policy recheck failed"
