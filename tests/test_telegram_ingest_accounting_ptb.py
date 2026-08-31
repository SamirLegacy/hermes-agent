"""Integration coverage for ingest accounting + self-healing against the installed PTB runtime.

Exercises the silent-ingest fix in ``plugins/platforms/telegram/adapter.py``:

* every consumed Telegram update is accounted at INFO (update_id + has_message,
  never content) by a group -1 catch-all ``TypeHandler``;
* the disconnect/fatal-error drop paths in the text/photo/media-group batchers
  emit WARNING with a monotonic ``dropped_total`` counter instead of a DEBUG
  line (the production incident saw updates consumed with zero INFO/WARNING);
* a registered PTB error handler routes handler exceptions onto the gateway
  logger with a traceback;
* ``disconnect()`` drains undispatched PTB queue updates with a WARNING count
  so stop-time consumed-but-undispatched updates are accounted;
* ``_cancel_pending_delivery_tasks()`` accounts for discarded buffered events
  with a WARNING and a ``dropped_total`` bump.
"""

import logging
from types import SimpleNamespace

import pytest
pytest.importorskip("telegram", reason="python-telegram-bot not installed")
from telegram.ext import TypeHandler  # noqa: E402

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.base import MessageEvent  # noqa: E402
from plugins.platforms.telegram import adapter as tg_adapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


_ADAPTER_LOGGER_NAME = tg_adapter.logger.name


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="123456:test-token"))


def _make_text_event(text: str = "hello world") -> MessageEvent:
    return MessageEvent(text=text)


def _dropped_warning_records(caplog):
    return [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "DROPPED inbound event" in r.getMessage()
    ]


def test_enqueue_text_event_drop_logs_warning_and_counts(caplog):
    """Dropping a text enqueue must WARN (not DEBUG) and bump the counter.

    This fails on the unmodified adapter, which logs the drop at DEBUG only.
    """
    adapter = _make_adapter()
    adapter._drop_delayed_deliveries = True
    caplog.set_level(logging.DEBUG, logger=_ADAPTER_LOGGER_NAME)

    adapter._enqueue_text_event(_make_text_event("hello world"))

    assert adapter._updates_dropped_total == 1
    dropped = _dropped_warning_records(caplog)
    assert dropped, "expected a WARNING drop record"
    assert "stage=text-enqueue" in dropped[-1].getMessage()
    # Never logs message content — only the character count.
    assert all("hello world" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_flush_text_batch_drop_logs_warning_and_counts(caplog):
    """Dropping a buffered text flush must WARN (not DEBUG) and bump the counter."""
    adapter = _make_adapter()
    adapter._drop_delayed_deliveries = True
    # Keep the flush quiet-period short so the test does not wait on the timer.
    adapter._text_batch_delay_seconds = 0.01
    caplog.set_level(logging.DEBUG, logger=_ADAPTER_LOGGER_NAME)

    key = "session-key"
    adapter._pending_text_batches[key] = _make_text_event("hello world")
    await adapter._flush_text_batch(key)

    assert adapter._updates_dropped_total == 1
    dropped = _dropped_warning_records(caplog)
    assert dropped, "expected a WARNING drop record"
    assert "stage=text-flush" in dropped[-1].getMessage()
    assert "key=%s" not in dropped[-1].getMessage()  # format already applied
    assert "key=session-key" in dropped[-1].getMessage()
    assert all("hello world" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_accounting_handler_registered_and_logs_info(caplog):
    """The group -1 TypeHandler + PTB error handler register, and accounting logs INFO."""
    adapter = _make_adapter()
    app = (
        tg_adapter.Application.builder()
        .token("123456:test-token")
        .build()
    )
    adapter._app = app
    adapter._register_ingest_handlers()

    # The catch-all accounting handler lives in group -1 and points at our callback.
    # NOTE: each attribute access on a bound method creates a NEW object, so
    # `handler.callback is adapter._account_inbound_update` is always False.
    # Compare the underlying function + bound instance instead.
    group_minus_one = app.handlers.get(-1, [])
    assert any(
        isinstance(handler, TypeHandler)
        and getattr(handler.callback, "__func__", None) is TelegramAdapter._account_inbound_update
        and getattr(handler.callback, "__self__", None) is adapter
        for handler in group_minus_one
    ), "group -1 must contain the ingest-accounting TypeHandler"
    # Group -1 sorts ahead of the default group 0, so it runs first but does not
    # stop propagation (PTB runs one handler per group across all groups).
    assert min(app.handlers) == -1

    # The PTB error handler is registered too.
    assert adapter._on_ptb_error in app.error_handlers

    caplog.set_level(logging.INFO, logger=_ADAPTER_LOGGER_NAME)
    fake_update = SimpleNamespace(update_id=4242, effective_message=object())
    await adapter._account_inbound_update(fake_update, SimpleNamespace())

    assert adapter._updates_consumed_total == 1
    assert adapter._last_update_consumed_monotonic is not None
    accounting = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "update consumed" in r.getMessage()
    ]
    assert accounting, "expected an INFO 'update consumed' record"
    msg = accounting[-1].getMessage()
    assert "update_id=4242" in msg
    assert "has_message=True" in msg
    # Never logs message content.
    assert all("hello world" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_on_ptb_error_logs_error_with_update_id_and_never_raises(caplog):
    """The PTB error handler logs ERROR with update_id + traceback and never raises."""
    adapter = _make_adapter()
    caplog.set_level(logging.ERROR, logger=_ADAPTER_LOGGER_NAME)

    class _OddError(Exception):
        """An error whose str() itself raises — exercises the redaction guard."""

        def __str__(self):
            raise RuntimeError("str() is broken")

    update = SimpleNamespace(update_id=99)
    # Must not raise even when context.error is odd.
    await adapter._on_ptb_error(update, SimpleNamespace(error=_OddError("never-leak")))

    record = caplog.records[-1]
    assert record.levelno >= logging.ERROR
    assert "PTB handler raised" in record.getMessage()
    assert "update_id=99" in record.getMessage()
    assert record.exc_info is not None  # traceback carried to gateway logs
    # The odd error's str() raises, so the redactor must not crash or leak content.
    assert "never-leak" not in record.getMessage()

    # Must also not raise on degenerate inputs (no update, no error).
    caplog.clear()
    await adapter._on_ptb_error(None, SimpleNamespace())


@pytest.mark.asyncio
async def test_disconnect_drains_update_queue_with_warning(caplog):
    """disconnect() drains undispatched PTB queue updates with a WARNING count."""
    adapter = _make_adapter()
    app = (
        tg_adapter.Application.builder()
        .token("123456:test-token")
        .build()
    )
    adapter._app = app
    # Simulate updates consumed by getUpdates but not yet dispatched.
    for i in range(3):
        app.updater.update_queue.put_nowait(SimpleNamespace(update_id=100 + i))

    caplog.set_level(logging.WARNING, logger=_ADAPTER_LOGGER_NAME)
    await adapter.disconnect()

    drained = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "Drained" in r.getMessage()
    ]
    assert drained, "expected a WARNING about drained updates"
    assert "3 undispatched" in drained[-1].getMessage()


@pytest.mark.asyncio
async def test_cancel_pending_delivery_tasks_accounts_discarded(caplog):
    """Teardown-discarded buffered events bump dropped_total and log WARNING."""
    adapter = _make_adapter()
    adapter._pending_text_batches["k1"] = _make_text_event("a")
    adapter._pending_text_batches["k2"] = _make_text_event("b")
    adapter._pending_photo_batches["p1"] = _make_text_event("c")

    caplog.set_level(logging.WARNING, logger=_ADAPTER_LOGGER_NAME)
    await adapter._cancel_pending_delivery_tasks()

    assert adapter._updates_dropped_total == 3
    discarded = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "DROPPED" in r.getMessage()
    ]
    assert discarded, "expected a WARNING about discarded buffered events"
    assert "3 buffered" in discarded[-1].getMessage()
