"""Canonical final-boundary / ordinary-compatibility tests, no SDK dependency.

Uses the real hook manager, middleware, all three framework execution paths and
native approval queue. Only policies, native replies and the recording handler
are fixtures. Real guard/SQLite integration lives separately in .scratch.
"""
import contextvars
import json
import threading
from types import SimpleNamespace

import pytest
from tests.hermes_cli.test_plugin_retry import policy, ui  # noqa: F401
from tools import approval, approval_context as ctx


ENTRIES = ['agent', 'helper', 'model_tools']


def precheck(entry, args):
    agent = SimpleNamespace(session_id='fixture-a', _current_turn_id='', _current_api_request_id='')
    if entry == 'agent':
        from agent.tool_executor import _pre_tool_block, _ToolCallRef
        return _pre_tool_block(agent, _ToolCallRef('fixture_tool', args, 'task', 'call', []))[0]
    if entry == 'helper':
        from agent.agent_runtime_helpers import _pre_tool_block_message
        return _pre_tool_block_message(agent, 'fixture_tool', args, 'task', 'call', [])[0]
    from model_tools import _CallIds, _pre_dispatch_guards
    return _pre_dispatch_guards('fixture_tool', args, False,
        _CallIds(session_id='fixture-a', tool_call_id='call'), [])[1]


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('kind', ['bytes', 'nan', 'uncopyable'])
def test_ordinary_identity_is_best_effort(policy, entry, kind):
    manager, _ = policy
    class Uncopyable:
        def __deepcopy__(self, memo):
            raise TypeError('ordinary Python value is not deepcopy-able')
    value = b'fixture' if kind == 'bytes' else float('nan') if kind == 'nan' else Uncopyable()
    seen = []
    args = {'value': value}
    def hook(**kw):
        seen.append(kw['args'])
        kw['args']['observed'] = True
    manager._hooks['pre_tool_call'] = [hook]
    assert precheck(entry, args) is None
    assert seen == [args]
    assert args['observed'] is True, 'retain baseline same-dict callback behavior'
    assert seen[0]['value'] is value


@pytest.mark.parametrize('entry', ENTRIES)
def test_ordinary_dispatcher_errors_reach_baseline_caller(policy, monkeypatch, entry):
    import hermes_cli.lifecycle
    def broken(*args, **kw):
        raise RuntimeError('ordinary dispatcher fixture failure')
    monkeypatch.setattr(hermes_cli.lifecycle, 'invoke_hook', broken)
    assert precheck(entry, {'item': 'fixture'}) is None


@pytest.mark.parametrize('kind', ['bytes', 'nan'])
def test_unbindable_failed_call_blocks_without_modal(policy, kind):
    value = b'fixture' if kind == 'bytes' else float('nan')
    with ui('fixture-route', lambda data: pytest.fail('unbound retry must not ask')):
        assert precheck('agent', {'value': value}) is not None


def chain(monkeypatch, entry, name, args, call='fixture-call'):
    import model_tools
    from agent import tool_executor as te
    from agent.agent_runtime_helpers import invoke_tool
    agent = SimpleNamespace(session_id='fixture-a', _current_turn_id='', _current_api_request_id='',
        _memory_manager=None, valid_tool_names={name}, _checkpoint_mgr=SimpleNamespace(enabled=False),
        _touch_activity=lambda *a: None, tool_start_callback=None, tool_progress_callback=None,
        _tool_guardrails=SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True)))
    monkeypatch.setattr(te, '_tool_progress_enabled', lambda a: False)
    if entry == 'helper':
        return invoke_tool(agent, name, args, 'task', tool_call_id=call)
    if entry == 'agent':
        def execute(payload):
            return model_tools.handle_function_call(name, payload, task_id='task', session_id=agent.session_id,
                tool_call_id=call, skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
                skip_tool_execution_middleware=True)
        return te._run_agent_tool_execution_middleware(agent, function_name=name, function_args=args,
            effective_task_id='task', tool_call_id=call, execute=execute).result
    return model_tools.handle_function_call(name, args, task_id='task', session_id=agent.session_id, tool_call_id=call)


def answering(prompts, choice='once', route='fixture-route'):
    def answer(data):
        prompts.append(data)
        approval.resolve_gateway_approval(route, choice, request_id=data['request_id'])
    return answer


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('rewrite', [False, True])
def test_retry_cannot_execute_different_final_args(policy, monkeypatch, entry, rewrite):
    import model_tools
    manager, _ = policy
    executed, posted, prompts = [], [], []
    manager._hooks['post_tool_call'] = [lambda **kw: posted.append(kw)]
    def handler(tool, args, **kw):
        executed.append(dict(args))
        return json.dumps({'recorded': True})
    monkeypatch.setattr(model_tools.registry, 'dispatch', handler)
    manager._middleware['tool_execution'] = [lambda args, next_call, **kw: next_call(
        {**args, 'item': 'B'} if rewrite else args)]
    with ui('fixture-route', answering(prompts)):
        result = chain(monkeypatch, entry, 'fixture_tool', {'item': 'A'})
    assert len(prompts) == 1
    if rewrite and entry != 'agent':
        assert json.loads(result).get('error') and executed == []
    else:
        expected = {'item': 'B' if rewrite else 'A'}
        assert json.loads(result)['recorded'] and executed == [expected]
        assert posted[0]['args'] == expected
        assert json.dumps(expected, separators=(',', ':')) in prompts[0]['description']
    assert len(posted) == 1


@pytest.mark.parametrize('entry', ENTRIES)
def test_ordinary_first_directive_controls_actual_handler_and_post(policy, monkeypatch, entry):
    import model_tools
    manager, _ = policy
    executed, posted, callbacks, prompts = [], [], [], []
    def hook(value):
        def run(**kw):
            callbacks.append(value)
            return value
        return run
    manager._hooks['pre_tool_call'] = [hook(value) for value in [
        {'action': 'modify', 'args': {'item': 'prefix'}},
        {'action': 'approve', 'message': 'ordinary permission'},
        {'action': 'modify', 'args': {'item': 'ignored-suffix'}},
        {'action': 'block', 'message': 'ordinary later directive'},
    ]]
    manager._hooks['post_tool_call'] = [lambda **kw: posted.append(kw)]
    def handler(tool, args, **kw):
        executed.append(dict(args))
        return json.dumps({'recorded': True})
    monkeypatch.setattr(model_tools.registry, 'dispatch', handler)
    with ui('fixture-route', answering(prompts)):
        assert json.loads(chain(monkeypatch, entry, 'fixture_tool', {'item': 'A'}))['recorded']
    assert executed == [{'item': 'prefix'}]
    assert len(posted) == 1 and posted[0]['args'] == executed[0]
    assert len(callbacks) == 4 and len(prompts) == 1
    assert not prompts[0].get('fresh_once', False)


@pytest.mark.parametrize('entry', ENTRIES)
def test_same_action_approvals_and_native_gate_share_one_choice(policy, monkeypatch, entry):
    import model_tools
    manager, _ = policy
    evaluations, executed, posted, prompts = [], [], [], []
    def later(**kw):
        evaluations.append(ctx.is_pre_tool_recheck())
        return {'action': 'approve', 'message': 'another same-action predicate'}
    manager._hooks['pre_tool_call'].append(later)
    manager._hooks['post_tool_call'] = [lambda **kw: posted.append(kw)]
    def handler(tool, args, **kw):
        result = approval.check_dangerous_command(args['command'], 'local')
        if result['approved']:
            executed.append(dict(args))
        return json.dumps(result)
    monkeypatch.setattr(model_tools.registry, 'dispatch', handler)
    args = {'command': 'systemctl --user restart fixture-only.service'}
    with ui('fixture-route', answering(prompts)):
        assert json.loads(chain(monkeypatch, entry, 'terminal', args))['approved']
    assert executed == [args] and len(posted) == 1 and posted[0]['args'] == args
    assert evaluations == [False, True] and len(prompts) == 1
    assert prompts[0]['fresh_once'] and not prompts[0]['allow_session'] and not prompts[0]['allow_permanent']
    assert approval._session_approved == {} and approval._permanent_approved == set()


def test_helper_second_middleware_boundary_cannot_change_approved_payload(policy, monkeypatch):
    import model_tools
    manager, _ = policy
    executed, passes, posted, prompts = [], [], [], []
    manager._hooks['post_tool_call'] = [lambda **kw: posted.append(kw)]
    monkeypatch.setattr(model_tools.registry, 'dispatch', lambda *a, **kw: executed.append(a))
    def middleware(args, next_call, **kw):
        passes.append(dict(args))
        return next_call({**args, 'item': 'inner-B'} if len(passes) == 2 else args)
    manager._middleware['tool_execution'] = [middleware]
    with ui('fixture-route', answering(prompts)):
        result = chain(monkeypatch, 'helper', 'fixture_tool', {'item': 'A'})
    assert json.loads(result).get('error')
    assert len(passes) == 2 and len(prompts) == 1
    assert executed == [] and len(posted) == 1 and posted[0]['status'] == 'blocked'


@pytest.mark.parametrize('mutation', ['home', 'route', 'cwd', 'nan'])
def test_binding_drift_after_prehooks_is_rejected(policy, monkeypatch, tmp_path, mutation):
    import model_tools
    manager, _ = policy
    executed, prompts = [], []
    monkeypatch.setattr(model_tools.registry, 'dispatch', lambda *a, **kw: executed.append(a))
    def middleware(args, next_call, **kw):
        if mutation == 'home':
            monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other-home'))
        elif mutation == 'route':
            ctx.set_current_session_key('fixture-other')
        elif mutation == 'cwd':
            monkeypatch.setenv('TERMINAL_CWD', str(tmp_path / 'other-cwd'))
        else:
            args['item'] = float('nan')
        return next_call(args)
    manager._middleware['tool_execution'] = [middleware]
    with ui('fixture-route', answering(prompts)):
        assert json.loads(chain(monkeypatch, 'model_tools', 'fixture_tool', {'item': 'A'})).get('error')
    assert len(prompts) == 1 and executed == []


def test_nested_same_ids_and_completed_context_cannot_inherit_native_consent(policy, monkeypatch):
    import model_tools
    from tools.approval_retry import current_execution
    manager, _ = policy
    args = {'command': 'systemctl --user restart fixture-only.service'}
    prompts, decisions, saved = [], [], []
    depth = []
    def handler(tool, payload, **kw):
        if not depth:
            depth.append(True)
            saved.append(contextvars.copy_context())
            assert approval.check_dangerous_command(payload['command'], 'local')['approved']
            hooks = manager._hooks['pre_tool_call']
            manager._hooks['pre_tool_call'] = []
            try:
                inner = model_tools.handle_function_call('terminal', dict(payload), session_id='fixture-a',
                                                         tool_call_id='fixture-call')
            finally:
                manager._hooks['pre_tool_call'] = hooks
            assert not json.loads(inner)['approved']
            result = approval.check_dangerous_command(payload['command'], 'local')
        else:
            result = approval.check_dangerous_command(payload['command'], 'local')
        decisions.append(result['approved'])
        return json.dumps(result)
    monkeypatch.setattr(model_tools.registry, 'dispatch', handler)
    def answer(data):
        prompts.append(data)
        choice = 'once' if data.get('fresh_once') else 'deny'
        approval.resolve_gateway_approval('fixture-route', choice, request_id=data['request_id'])
    with ui('fixture-route', answer):
        assert json.loads(chain(monkeypatch, 'model_tools', 'terminal', args))['approved']
        assert not saved[0].run(approval.check_dangerous_command, args['command'], 'local')['approved']
    assert decisions == [False, True]
    assert len(prompts) == 3
    assert current_execution() is None


@pytest.mark.parametrize('choice', ['deny', 'timeout', 'cancel'])
@pytest.mark.parametrize('entry', ENTRIES)
def test_cancel_or_timeout_never_reaches_handler(policy, monkeypatch, choice, entry):
    import model_tools
    executed, prompts = [], []
    monkeypatch.setattr(model_tools.registry, 'dispatch', lambda *a, **kw: executed.append(a))
    def answer(data):
        prompts.append(data)
        if choice == 'cancel':
            approval.unregister_gateway_notify('fixture-route')
        elif choice == 'deny':
            approval.resolve_gateway_approval('fixture-route', choice, request_id=data['request_id'])
    monkeypatch.setattr(ctx, '_get_approval_timeout', lambda: 0)
    with ui('fixture-route', answer):
        assert json.loads(chain(monkeypatch, entry, 'fixture_tool', {'item': 'A'})).get('error')
    assert len(prompts) == 1 and executed == []
    assert not approval.list_gateway_approvals('fixture-route')


def test_concurrent_same_identity_has_separate_execution_lifetime(policy, monkeypatch):
    import model_tools
    prompts, outputs, executed, errors = {}, {}, [], []
    both = threading.Barrier(2, timeout=5)
    def answer(data):
        name = threading.current_thread().name
        prompts[name] = data
        both.wait()
        approval.resolve_gateway_approval('fixture-route', 'once' if name == 'allow' else 'deny',
                                           request_id=data['request_id'])
    def handler(tool, args, **kw):
        executed.append(threading.current_thread().name)
        return json.dumps(approval.check_dangerous_command(args['command'], 'local'))
    monkeypatch.setattr(model_tools.registry, 'dispatch', handler)
    approval.register_gateway_notify('fixture-route', answer)
    def worker():
        token = ctx.set_current_session_key('fixture-route')
        try:
            outputs[threading.current_thread().name] = model_tools.handle_function_call(
                'terminal', {'command': 'systemctl --user restart fixture-only.service'},
                session_id='fixture-a', tool_call_id='same-public-id')
        except BaseException as exc:
            errors.append(exc)
        finally:
            ctx.reset_current_session_key(token)
    threads = [threading.Thread(target=worker, name=name) for name in ['allow', 'deny']]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
        assert not any(thread.is_alive() for thread in threads)
    finally:
        approval.unregister_gateway_notify('fixture-route')
    assert not errors
    assert len(prompts) == 2 and prompts['allow']['request_id'] != prompts['deny']['request_id']
    assert executed == ['allow']
    assert json.loads(outputs['allow'])['approved']
    assert json.loads(outputs['deny']).get('error')
