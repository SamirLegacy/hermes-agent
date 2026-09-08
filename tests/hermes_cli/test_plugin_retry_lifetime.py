"""C4 retry-only runner lifetime/forwarding regressions; no SDK or effects.

Real native queue, executor, middleware and registry. Synthetic typed failure
policy is shared with the established generic tests; SDK/SQLite counterparts
live in the worktree replay and must run as well.
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from tests.hermes_cli.test_plugin_retry import policy, ui  # noqa: F401
from tests.hermes_cli.test_plugin_retry_execution import chain
from tools import approval, approval_context as ctx

COMMAND = 'systemctl --user restart fixture-only.service'


def install_handler(monkeypatch, name, handler):
    from tools.registry import registry
    original = registry.get_entry
    entry = SimpleNamespace(handler=handler, is_async=False, schema={'parameters': {'properties': {}}})
    monkeypatch.setattr(registry, 'get_entry', lambda n, **kw: entry if n == name else original(n, **kw))


@pytest.mark.parametrize('entry', ['helper', 'model_tools'])
def test_equal_id_prechecked_nested_call_does_not_take_forwarding(policy, monkeypatch, entry):
    import model_tools
    manager, _ = policy
    inner, calls, prompts, depth = [], [], [], []
    def handler(payload, **kw):
        decision = approval.check_all_command_guards(payload['command'], 'local')
        calls.append(('nested' if depth else 'outer', decision['approved']))
        return json.dumps(decision)
    install_handler(monkeypatch, 'terminal', handler)
    monkeypatch.setattr(approval, '_tirith_scan', lambda c: {'action': 'allow', 'findings': []})
    def middleware(args, next_call, **kw):
        depth.append(True)
        try:
            inner.append(model_tools.handle_function_call('terminal', dict(args), session_id='fixture-a',
                tool_call_id='fixture-call', skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True))
        finally:
            depth.pop()
        return next_call(args)
    manager._middleware['tool_execution'] = [middleware]
    def answer(data):
        prompts.append(data)
        approval.resolve_gateway_approval('fixture-route', 'once' if data.get('fresh_once') else 'deny',
                                           request_id=data['request_id'])
    with ui('fixture-route', answer):
        result = chain(monkeypatch, entry, 'terminal', {'command': COMMAND})
    assert json.loads(result)['approved'], 'legitimate outer continuation must still execute'
    assert inner and all(not json.loads(r)['approved'] for r in inner)
    assert calls == [('nested', False)] * len(inner) + [('outer', True)]
    assert sum(bool(p.get('fresh_once')) for p in prompts) == 1
    assert len(prompts) == len(inner) + 1


@pytest.mark.parametrize('outcome', ['complete', 'timeout', 'cancel'])
@pytest.mark.parametrize('retry', [True, False])
def test_real_sequential_owner_revokes_only_retry(policy, monkeypatch, outcome, retry):
    from agent import tool_executor as te
    import model_tools
    manager, _ = policy
    if not retry:
        manager._hooks['pre_tool_call'] = []
    name, args = 'fixture_tool', {'item': 'A'}
    entered, release = threading.Event(), threading.Event()
    calls, posts, prompts, policies = [], [], [], []
    agent = SimpleNamespace(session_id='fixture-a', _current_turn_id='', _current_api_request_id='',
        _memory_manager=None, valid_tool_names={name}, _checkpoint_mgr=SimpleNamespace(enabled=False),
        _touch_activity=lambda *a: None, tool_start_callback=None, tool_progress_callback=None,
        _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(), _interrupt_requested=False)
    def guard(*a):
        entered.set()
        if outcome == 'cancel':
            agent._interrupt_requested = True
        if outcome != 'complete':
            assert release.wait(8)
        return SimpleNamespace(allows_execution=True)
    agent._tool_guardrails = SimpleNamespace(before_call=guard)
    monkeypatch.setattr(te, '_tool_progress_enabled', lambda a: False)
    monkeypatch.setattr(te, '_resolve_sequential_tool_timeout', lambda: 0.25 if outcome == 'timeout' else None)
    monkeypatch.setattr(te, '_SEQUENTIAL_INTERRUPT_POLL_SECONDS', 0.02)
    manager._hooks['pre_tool_call'].append(lambda **kw: policies.append(ctx.is_pre_tool_recheck()))
    manager._hooks['post_tool_call'] = [lambda **kw: posts.append(kw)]
    def handler(payload, **kw):
        calls.append(dict(payload))
        return json.dumps({'recorded': True})
    install_handler(monkeypatch, name, handler)
    def execute(payload):
        return model_tools.handle_function_call(name, payload, session_id='fixture-a', tool_call_id='fixture-call',
            skip_pre_tool_call_hook=True, skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
    def answer(data):
        prompts.append(data)
        approval.resolve_gateway_approval('fixture-route', 'once', request_id=data['request_id'])
    with ui('fixture-route', answer):
        try:
            result = te._run_sequential_tool_execution_middleware(agent, function_name=name, function_args=dict(args),
                effective_task_id='task', tool_call_id='fixture-call', execute=execute)
            assert entered.is_set()
            assert policies == ([False, True] if retry else [False])
            assert len(prompts) == int(retry)
            if outcome != 'complete':
                assert isinstance(result.result, te._ToolTimeoutResult if outcome == 'timeout' else te._ToolCancelledResult)
                assert calls == []
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while agent._tool_worker_threads and time.monotonic() < deadline:
                time.sleep(0.01)
    assert not agent._tool_worker_threads
    if retry and outcome != 'complete':
        assert calls == []
        assert [p['status'] for p in posts] == ['timeout' if outcome == 'timeout' else 'cancelled']
    else:
        assert calls == [args], 'ordinary abandoned callbacks retain their baseline behavior'
        assert posts[-1]['status'] == 'ok'
