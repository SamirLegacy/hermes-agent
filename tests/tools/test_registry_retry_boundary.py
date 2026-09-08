"""Optional final-entry callback contracts, with no plugin dependency.

The twelve real owner/native/SDK lock-wait cases are preserved in C5 scratch.
These canonical controls exercise the actual registry, including ordinary calls.
"""
import json
from types import SimpleNamespace

import pytest

from tools.approval_retry import RetryExecution, RetryExecutionInvalid
from tools.registry import ToolRegistry


@pytest.mark.parametrize('is_async', [False, True])
@pytest.mark.parametrize('outcome', ['complete', 'revoked', 'lookup_error', 'unknown', 'missing_handler'])
def test_entry_check_follows_lookup_and_never_reaches_revoked_handler(monkeypatch, is_async, outcome):
    registry = ToolRegistry()
    execution = RetryExecution('record_only', 'session', 'call')
    events = []
    args = {'item': 'A'}

    def handler(payload, **kw):
        events.append('handler')
        assert payload is args and kw == {'task_id': 'task'}
        return json.dumps({'recorded': True})

    async def async_handler(payload, **kw):
        return handler(payload, **kw)

    entry = SimpleNamespace(handler=async_handler if is_async else handler, is_async=is_async)
    if outcome == 'missing_handler':
        del entry.handler
    monkeypatch.setitem(registry._tools, 'record_only', entry)
    original = registry.get_entry
    lookup_error = LookupError('test lookup failure')

    def lookup(name, **kw):
        events.append('lookup')
        if outcome == 'lookup_error':
            raise lookup_error
        if outcome == 'revoked':
            execution.abandon()
        return None if outcome == 'unknown' else original(name, **kw)

    def before_handler():
        events.append('check')
        execution.require_open()

    monkeypatch.setattr(registry, 'get_entry', lookup)
    if outcome == 'lookup_error':
        with pytest.raises(LookupError) as error:
            registry.dispatch('record_only', args, task_id='task', _before_handler=before_handler)
        assert error.value is lookup_error and events == ['lookup']
        return
    result = registry.dispatch('record_only', args, task_id='task', _before_handler=before_handler)
    assert isinstance(result, str)
    if outcome == 'complete':
        assert json.loads(result) == {'recorded': True}
        assert events == ['lookup', 'check', 'handler']
    elif outcome == 'unknown':
        assert 'Unknown tool: record_only' in json.loads(result)['error']
        assert events == ['lookup']
    else:
        expected_error = RetryExecutionInvalid.__name__ if outcome == 'revoked' else 'AttributeError'
        assert expected_error in json.loads(result)['error']
        assert events == ['lookup', 'check']


@pytest.mark.parametrize('is_async', [False, True])
@pytest.mark.parametrize('outcome', ['complete', 'handler_error', 'lookup_error', 'unknown', 'missing_handler'])
def test_ordinary_dispatch_preserves_payload_kwargs_and_error_ownership(monkeypatch, is_async, outcome):
    registry = ToolRegistry()
    args = {'opaque': object(), 'bytes': b'ordinary'}
    events = []
    result_bytes = '{ "ordinary": true }'

    def handler(payload, **kw):
        events.append('handler')
        assert payload is args
        assert kw == {'task_id': 'task', 'session_id': 'session', 'extra': 'kept'}
        if outcome == 'handler_error':
            raise ValueError('ordinary handler failure')
        return result_bytes

    async def async_handler(payload, **kw):
        return handler(payload, **kw)

    entry = SimpleNamespace(handler=async_handler if is_async else handler, is_async=is_async)
    if outcome == 'missing_handler':
        del entry.handler
    monkeypatch.setitem(registry._tools, 'record_only', entry)
    original = registry.get_entry
    lookup_error = LookupError('ordinary lookup failure')

    def lookup(name, **kw):
        events.append('lookup')
        if outcome == 'lookup_error':
            raise lookup_error
        return None if outcome == 'unknown' else original(name, **kw)

    monkeypatch.setattr(registry, 'get_entry', lookup)
    kwargs = {'task_id': 'task', 'session_id': 'session', 'extra': 'kept'}
    if outcome == 'lookup_error':
        with pytest.raises(LookupError) as error:
            registry.dispatch('record_only', args, **kwargs)
        assert error.value is lookup_error and events == ['lookup']
        return
    result = registry.dispatch('record_only', args, **kwargs)
    assert isinstance(result, str)
    if outcome == 'complete':
        assert result == result_bytes and events == ['lookup', 'handler']
    elif outcome == 'handler_error':
        assert 'ValueError: ordinary handler failure' in json.loads(result)['error']
        assert events == ['lookup', 'handler']
    else:
        assert ('Unknown tool' if outcome == 'unknown' else 'AttributeError') in json.loads(result)['error']
        assert events == ['lookup']
