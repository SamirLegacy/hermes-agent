"""Background memory/skill review — fork the agent to evaluate the turn. After every turn
``AIAgent.run_conversation`` may spawn a daemon thread that replays the conversation snapshot in a
forked :class:`AIAgent` and asks "should any skill/memory be saved or updated?". Writes go
straight to the memory + skill stores; the main conversation and prompt cache are never touched.
The fork inherits the parent's live runtime (provider, model, credentials, cached system prompt)
so it hits the same prefix cache, and runs under a dispatch-side tool whitelist."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from agent.thread_scoped_output import thread_scoped_silence

logger = logging.getLogger(__name__)

_BACKGROUND_REVIEW_CANCEL_TIMEOUT_SECONDS = 2.0


class _BackgroundReviewRun:
    """Per-review cancellation and request-completion handshake."""

    def __init__(self) -> None:
        self.cancel_requested = threading.Event()
        self.request_done = threading.Event()
        self._lock = threading.Lock()
        self._review_agent = None
        self._request_finished = self._cancel_dispatched = False

    def begin_request(self, review_agent: Any) -> bool:
        """Atomically admit the first provider-capable review phase."""
        with self._lock:
            if self.cancel_requested.is_set() or self._request_finished:
                return False
            self._review_agent = review_agent
            return True

    def cancel(self) -> Any:
        """Fence startup and return the running fork, if one was admitted."""
        with self._lock:
            self.cancel_requested.set()
            if self._review_agent is None or self._cancel_dispatched:
                return None
            self._cancel_dispatched = True
            return self._review_agent

    def mark_request_finished(self) -> bool:
        """Latch request completion once; the caller publishes the event."""
        with self._lock:
            if self._request_finished:
                return False
            self._request_finished, self._review_agent = True, None
            return True


@contextmanager
def _optional_lock(agent: Any, attr: str) -> Iterator[None]:
    """``with`` over a lock attribute that may be absent (direct test stubs)."""
    lock = getattr(agent, attr, None)
    if lock is None:
        yield
        return
    with lock:
        yield


def prepare_background_review_run(agent: Any) -> Optional[_BackgroundReviewRun]:
    """Install a unique run token on the parent before ``Thread.start()``."""
    run = _BackgroundReviewRun()
    try:
        lock = getattr(agent, "_background_review_lock", None)
        if lock is None:
            lock = agent._background_review_lock = threading.Lock()
        with lock:
            current = getattr(agent, "_background_review_run", None)
            if current is not None and not current.request_done.is_set():
                return None
            agent._background_review_run = run
    except (AttributeError, TypeError):
        return None
    return run


def finish_background_review_run(agent: Any, run: Optional[_BackgroundReviewRun]) -> None:
    """Publish one run's request exit without clearing a successor (ABA-safe)."""
    if run is None or not run.mark_request_finished():
        return
    with _optional_lock(agent, "_background_review_lock"):
        if getattr(agent, "_background_review_run", None) is run:
            agent._background_review_run = None
    run.request_done.set()


def _interrupt_background_review(review_agent: Any) -> None:
    """Request abort off-thread so a wedged abort hook cannot stall the live turn (the bounded
    ``request_done`` wait in the canceller relies on this returning fast)."""
    def _interrupt() -> None:
        try:
            from agent.interrupt_compat import request_hard_interrupt

            request_hard_interrupt(
                review_agent, "superseded by a new live turn", tool_reason="background review superseded"
            )
        except Exception:
            logger.debug("Failed to cancel in-flight background review for a new turn", exc_info=True)

    try:
        threading.Thread(target=_interrupt, daemon=True, name="bg-review-cancel").start()
    except Exception:
        logger.debug("Failed to start background-review cancellation thread", exc_info=True)


def cancel_background_review_for_live_turn(agent: Any) -> None:
    """Cancel the current review and await its request-phase acknowledgement. Foreground priority:
    past the bounded deadline, warn and let the live turn proceed — self-improvement work must
    never block a user-facing turn.

    Foreground priority is preserved: if the review does not acknowledge within the bounded deadline, a
    warning is logged and the live turn proceeds anyway. See #84423.
    """
    with _optional_lock(agent, "_background_review_lock"):
        run = getattr(agent, "_background_review_run", None)
        legacy_agent = getattr(agent, "_background_review_agent", None)
    review_agent = legacy_agent if run is None else run.cancel()
    # Attribute the review fork's usage to the PARENT session. Snapshot BEFORE unregister/close so counters
    # survive teardown. Placed in this finally so a fork that consumed tokens and THEN raised is still
    # attributed (issue #87250). Best-effort: the recorder never raises into the review thread.
    if review_agent is not None:
        _interrupt_background_review(review_agent)
    if run is None:
        return
    if not run.request_done.wait(timeout=_BACKGROUND_REVIEW_CANCEL_TIMEOUT_SECONDS):
        logger.warning(
            "Background review did not acknowledge cancellation within %.1fs; "
            "proceeding with foreground live turn",
            _BACKGROUND_REVIEW_CANCEL_TIMEOUT_SECONDS,
        )


# Aux-model routing: by default ("auto") the fork runs on the MAIN model and replays the full
# conversation as warm cache reads. When auxiliary.background_review.{provider,model} routes it
# to a DIFFERENT model the cache is cold anyway, so the fork replays a compact digest instead.
_REVIEW_MAX_ITERATIONS = 16
# Aggregate INPUT-token budget for one review fork (checked in conversation_loop's
# ``_review_input_budget_exhausted``). Request #1 replays the full snapshot as a warm cache read
# (both compression gates deferred until the first response); compaction then bounds each
# request, but nothing else caps the SUM across the tool loop. 2x the historical 300k foreground
# trigger. Override via ``auxiliary.background_review.max_input_tokens``; <= 0 disables.
_REVIEW_MAX_INPUT_TOKENS_DEFAULT = 600_000


def _task_block(cfg: Any) -> Dict[str, Any]:
    """``cfg["auxiliary"]["background_review"]`` as a dict (``{}`` on any shape mismatch)."""
    aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
    task = aux.get("background_review", {})
    return task if isinstance(task, dict) else {}


def _background_review_task_config(task_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``auxiliary.background_review`` (or ``{}`` on any failure); pass a pre-loaded ``task_cfg``
    so the spawn / resolve / prompt paths do not re-read config on every turn."""
    if task_cfg is not None:
        return task_cfg if isinstance(task_cfg, dict) else {}
    try:
        from hermes_cli.config import load_config_readonly
        return _task_block(load_config_readonly())
    except Exception:
        return {}


def _review_input_token_budget(task_cfg: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Aggregate input-token budget for one review fork (None = unlimited; <= 0 disables)."""
    raw = _background_review_task_config(task_cfg).get("max_input_tokens", _REVIEW_MAX_INPUT_TOKENS_DEFAULT)
    try:
        budget = int(raw)
    except (TypeError, ValueError):
        budget = _REVIEW_MAX_INPUT_TOKENS_DEFAULT
    return budget if budget > 0 else None


def load_background_review_settings() -> tuple[bool, Dict[str, Any]]:
    """Single config read -> ``(enabled, task_cfg)``. Fail-open (``enabled=True``) so a broken
    config never silently disables reviews — but WARN so the cost is visible."""
    try:
        from hermes_cli.config import load_config_readonly
        from utils import is_truthy_value
        task = _task_block(load_config_readonly())
        return is_truthy_value(task.get("enabled"), default=True), task
    except Exception:
        logger.warning(
            "Failed to read background_review.enabled; leaving automatic "
            "review enabled (fail-open)",
            exc_info=True,
        )
        return True, {}


def _resolve_review_runtime(agent: Any, task_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve provider/model/credentials for the review fork. Default (auto / unset / same as
    parent): the parent's live runtime with ``routed=False`` (codex_app_server -> codex_responses
    downgrade applied). When ``auxiliary.background_review.{provider,model}`` names a different
    concrete model, resolve that runtime and set ``routed=True``."""
    parent_runtime = agent._current_main_runtime()
    parent_api_mode = parent_runtime.get("api_mode") or None
    parent = {
        "provider": agent.provider, "model": agent.model,
        "api_key": parent_runtime.get("api_key") or None, "base_url": parent_runtime.get("base_url") or None,
        "api_mode": "codex_responses" if parent_api_mode == "codex_app_server" else parent_api_mode,
        "credential_pool": getattr(agent, "_credential_pool", None),
        "request_overrides": dict(getattr(agent, "request_overrides", {}) or {}),
        "max_tokens": getattr(agent, "max_tokens", None), "command": getattr(agent, "acp_command", None),
        "args": list(getattr(agent, "acp_args", []) or []), "routed": False,
    }
    task = _background_review_task_config(task_cfg)
    task_provider, task_model, task_base_url, task_api_key = (
        str(task.get(key, "")).strip() or None for key in ("provider", "model", "base_url", "api_key")
    )
    if not (task_provider and task_provider != "auto" and task_model) or (
        task_provider == (agent.provider or "") and task_model == (agent.model or "")  # same as parent
    ):
        return parent
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        rp = resolve_runtime_provider(
            requested=task_provider, target_model=task_model,
            explicit_api_key=task_api_key, explicit_base_url=task_base_url,
        )
        return {
            "provider": rp.get("provider") or task_provider, "model": rp.get("model") or task_model,
            **{key: rp.get(key) for key in ("api_key", "base_url", "api_mode", "credential_pool", "command")},
            "request_overrides": dict(rp.get("request_overrides") or {}),
            "max_tokens": rp.get("max_output_tokens"), "args": list(rp.get("args") or []), "routed": True,
        }
    except Exception as e:
        logger.debug("background-review aux routing failed (%s); using main model", e)
        return parent


def _parent_can_emit_tool_calls(agent: Any) -> bool:
    """Whether a fork inheriting ``agent``'s runtime could act at all: an agent-as-provider client
    shim declaring ``SUPPORTS_HERMES_TOOL_CALLS = False`` (instance or class) is skipped — the fork
    would be a guaranteed no-op that still pays a full spawn. Silence means capable."""
    client = getattr(agent, "client", None)
    for candidate in (client, type(client) if client is not None else None):
        supported = getattr(candidate, "SUPPORTS_HERMES_TOOL_CALLS", None)
        if candidate is not None and supported is not None:
            return bool(supported)
    return True


def _msg_text(m: Dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return c.strip() if isinstance(c, str) else ""


def _digest_history(messages_snapshot: List[Dict], tail: int = 24) -> List[Dict]:
    """Compact replay for the routed (different-model) path only: keep the recent ``tail``
    messages verbatim (extended so the kept run never starts on a tool result) and collapse older
    turns into one synthetic user-role digest, preserving role alternation."""
    msgs = list(messages_snapshot or [])
    while len(msgs) > tail:
        keep = msgs[-tail:]
        if not (isinstance(keep[0], dict) and keep[0].get("role") == "tool"):
            break
        tail += 1
    else:
        return msgs
    lines: List[str] = []
    for m in msgs[:-len(keep)]:
        if not isinstance(m, dict):
            continue
        role, text = m.get("role"), _msg_text(m).replace("\n", " ")
        if role == "user" and text:
            lines.append(f"USER: {text[:300]}")
        elif role == "assistant":
            if m.get("tool_calls"):
                names = [(tc.get("function") or {}).get("name", "?") for tc in m["tool_calls"] if isinstance(tc, dict)]
                lines.append(f"ASSISTANT[tools: {', '.join(names)}]")
            if text:
                lines.append(f"ASSISTANT: {text[:200]}")
    digest = (
        "[Earlier conversation digest — older turns summarised to bound the "
        "review's cold-write cost on the routed aux model. Recent turns "
        "follow verbatim below.]\n" + "\n".join(lines)
    )
    return [{"role": "user", "content": digest}] + keep


# Review prompts. AIAgent exposes them as class attributes (``_MEMORY_REVIEW_PROMPT`` etc.) so
# per-agent overrides work; the text lives here. Background review is candidate-only: the fork
# must not directly call memory or skill tools — it emits evidence-backed candidate signals
# for the later score-gated promotion lane.
_CANDIDATE_SIGNAL_SCHEMA = (
    "Return ONLY compact JSON with this shape: "
    "{\"candidate_signals\":[{\"source_event\":{\"session_id\":str,\"event_id\":str,\"timestamp\":str},"
    "\"signal_type\":str,\"claim\":str,\"target\":{\"store\":str,\"path_or_name\":str},"
    "\"candidate_class\":\"skill_patch|memory|skill_merge|skill_create|external_action|runtime_patch\","
    "\"evidence\":[{\"path\":str,\"excerpt\":str}],\"confidence\":number,"
    "\"recurrence_count\":number,\"future_trigger\":str,\"authority_tier\":\"T0|T1|T2|T3\"}]}"
)

_BACKGROUND_REVIEW_GATE_INSTRUCTIONS = (
    "You are the background self-improvement reviewer. You may identify durable "
    "learning CANDIDATES only; you must not write memory, create skills, patch "
    "skills, delete skills, change config, send messages, commit, push, publish, "
    "deploy, or claim learning is complete. Permanent writes are handled later by "
    "the score-gated promotion lane.\n\n"
    "Emit a candidate only when the signal is evidence-backed, reusable, specific, "
    "and tied to a source event. Weak one-off comments, vague preferences, transient "
    "environment failures, duplicate observations, and generic quality language must "
    "produce an empty candidate_signals list.\n\n"
    "Every candidate must include evidence path/excerpt, source_event, confidence, "
    "target, candidate_class, authority_tier, and future_trigger. If the candidate "
    "touches Hermes runtime, governance, hooks, config, deletion, customer data, "
    "external send/publish/push/deploy, credentials, or cron/daemon authority, mark "
    "authority_tier as T3 or candidate_class as runtime_patch/external_action so the "
    "owner gate catches it.\n\n"
    + _CANDIDATE_SIGNAL_SCHEMA + "\n\n"
    "If no candidate survives these gates, return exactly {\"candidate_signals\":[]}."
)

_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above for durable user-memory candidate signals only.\n\n"
    + _BACKGROUND_REVIEW_GATE_INSTRUCTIONS
)

_SKILL_REVIEW_PROMPT = (
    "Review the conversation above for durable skill/procedure candidate signals only.\n\n"
    + _BACKGROUND_REVIEW_GATE_INSTRUCTIONS
)

_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above for durable memory and skill candidate signals only.\n\n"
    + _BACKGROUND_REVIEW_GATE_INSTRUCTIONS
)


def _preview(text: str, limit: int) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


# Memory op -> (glyph, which field carries the preview, preview length).
_MEMORY_OP_FORMATS: Dict[str, Tuple[str, str, int]] = {
    "add": ("➕", "content", 120), "replace": ("✏️", "content", 120), "remove": ("➖", "old_text", 60)
}


def _memory_op_line(label: str, action: str, fields: Dict[str, str]) -> Optional[str]:
    """Verbose line for one memory add/replace/remove, or None when no preview text."""
    glyph, field_name, limit = _MEMORY_OP_FORMATS.get(action) or (None, "", 0)
    text = fields.get(field_name) or "" if glyph else ""
    return f"{label} {glyph} {_preview(text, limit)}" if text else None


def _verbose_skill_line(data: Dict, detail: Dict, message: str) -> str:
    action = detail.get("action", "")
    skill_name = detail.get("name", "")
    # ``_change`` is free-form (wrapper MCP backends return lists/scalars).
    change_raw = data.get("_change")
    change: dict = change_raw if isinstance(change_raw, dict) else {}
    old_string = change.get("old", "") or detail.get("old_string", "")
    new_string = change.get("new", "") or detail.get("new_string", "")
    if action == "patch" and (old_string or new_string):
        old_preview, new_preview = (_preview(t, 80).replace("\n", " ") for t in (old_string, new_string))
        return f"📝 Skill '{skill_name}' patched: \"{old_preview}\" → \"{new_preview}\""
    verb = {"create": "created", "edit": "rewritten"}.get(action)
    if verb and change.get("description"):
        return f"📝 Skill '{skill_name}' {verb}: {change['description']}"
    return f"📝 {message}" if message else f"Skill {action}"


def _verbose_memory_lines(label: str, detail: Dict) -> List[str]:
    # ``operations`` may be any JSON value; only a list of dicts is usable.
    ops_raw = detail.get("operations")
    if isinstance(ops_raw, list) and ops_raw:
        lines = [_memory_op_line(label, op.get("action", ""), op) for op in ops_raw if isinstance(op, dict)]
        return [line for line in lines if line]
    return [_memory_op_line(label, detail.get("action", ""), detail) or f"{label} updated"]


# Tool-call argument fields surfaced in action summaries, with their defaults.
_CALL_DETAIL_DEFAULTS = (
    ("action", "?"), ("target", "memory"), ("content", ""), ("old_text", ""), ("name", ""),
    ("old_string", ""), ("new_string", ""),
)


def _collect_review_call_details(review_messages: List[Dict]) -> Tuple[set, dict]:
    """Map review-agent tool_call ids -> parsed call arguments for notify tools. Result JSON only
    says "Entry added"; the call arguments carry action, target and content previews. Restricting
    to notify tools keeps helper tools from surfacing as memory work just because they succeeded."""
    notify_tools = {"memory", "skill_manage"}
    all_tool_call_ids: set = set()
    call_details: dict = {}
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {}) or {}
            fn_name = fn.get("name", "")
            tcid = tc.get("id")
            if tcid:
                all_tool_call_ids.add(tcid)
            if fn_name not in notify_tools or not tcid:
                continue
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            call_details[tcid] = {
                "tool": fn_name, "operations": args.get("operations") or [],
                **{k: args.get(k, default) for k, default in _CALL_DETAIL_DEFAULTS},
            }
    return all_tool_call_ids, call_details


def _tool_messages(messages: List[Dict]) -> Iterator[Dict]:
    return (m for m in messages or [] if isinstance(m, dict) and m.get("role") == "tool")


def _prior_tool_keys(prior_snapshot: List[Dict]) -> Tuple[set, set]:
    """``(tool_call_ids, contents)`` of tool messages already in the parent snapshot."""
    priors = list(_tool_messages(prior_snapshot))
    ids = {m["tool_call_id"] for m in priors if m.get("tool_call_id")}
    contents = {m["content"] for m in priors if not m.get("tool_call_id") and isinstance(m.get("content"), str)}
    return ids, contents


def _action_lines(data: Dict, detail: Dict, verbose: bool) -> List[str]:
    """Summary line(s) for one successful notify-tool result (``[]`` when nothing to report)."""
    message = data.get("message", "")
    target = data.get("target", "") or detail.get("target", "")
    is_skill = detail.get("tool") == "skill_manage"
    lower = message.lower()
    if not verbose and ("created" in lower or "updated" in lower or (is_skill and "patched" in lower)):
        return [message]
    if not is_skill and not target:
        return []
    label = "Skill" if is_skill else {"memory": "Memory", "user": "User profile"}.get(target, target)
    if verbose:
        return [_verbose_skill_line(data, detail, message)] if is_skill else _verbose_memory_lines(label, detail)
    hit = any(k in lower for k in ("added", "replaced", "removed", "applied")) or (target and "add" in lower)
    return [f"{label} updated"] if hit else []


def summarize_background_review_actions(
    review_messages: List[Dict], prior_snapshot: List[Dict], notification_mode: str = "on"
) -> List[str]:
    """Human-facing action summary for a background review pass: successful memory /
    skill-management tool results from the review agent's messages, skipping tool messages already
    present in ``prior_snapshot`` so inherited results are not re-surfaced as fresh work.
    ``notification_mode``: ``off`` -> no actions; ``on`` -> generic "Memory updated"/tool messages;
    ``verbose`` -> content previews from the tool-call arguments.

    See #14944.
    """
    mode = str(notification_mode or "on").lower()
    if mode == "off":
        return []
    verbose = mode == "verbose"
    existing_tool_call_ids, existing_tool_contents = _prior_tool_keys(prior_snapshot)
    all_tool_call_ids, call_details = _collect_review_call_details(review_messages)
    actions: List[str] = []
    for msg in _tool_messages(review_messages):
        tcid = msg.get("tool_call_id")
        if tcid:
            if tcid in existing_tool_call_ids or (all_tool_call_ids and tcid not in call_details):
                continue
        elif isinstance(msg.get("content"), str) and msg["content"] in existing_tool_contents:
            continue
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        # Wrapper MCP servers may return a top-level list/scalar; only dict payloads carry
        # ``success``/``_change``.
        if not isinstance(data, dict) or not data.get("success"):
            continue
        actions.extend(_action_lines(data, call_details.get(tcid) or {}, verbose))
    return actions


def build_memory_write_metadata(
    agent: Any, *, write_origin: Optional[str] = None, execution_context: Optional[str] = None,
    task_id: Optional[str] = None, tool_call_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build provenance metadata for external memory-provider mirrors."""
    metadata: Dict[str, Any] = {
        "write_origin": write_origin or getattr(agent, "_memory_write_origin", "assistant_tool"),
        "execution_context": execution_context or getattr(agent, "_memory_write_context", "foreground"),
        "session_id": agent.session_id or "",
        "parent_session_id": agent._parent_session_id or "",
        "platform": agent.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
        "tool_name": "memory",
        "task_id": task_id or None,
        "tool_call_id": tool_call_id or None,
    }
    return {k: v for k, v in metadata.items() if v not in {None, ""}}


_USAGE_COUNTERS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "api_calls",
)


def _snapshot_review_usage(review_agent: Any) -> Dict[str, Any]:
    """Snapshot in-memory usage counters from a review fork (pre-close)."""
    return {
        **{key: getattr(review_agent, key, None) for key in ("model", "provider", "base_url")},
        **{key: int(getattr(review_agent, f"session_{key}", 0) or 0) for key in _USAGE_COUNTERS},
        "estimated_cost_usd": getattr(review_agent, "session_estimated_cost_usd", None),
    }


def _record_review_usage_to_parent(parent_agent: Any, usage: Dict[str, Any]) -> None:
    """Record a fork's usage against the parent session (best-effort, never raises). The fork has
    ``_session_db = None`` so conversation_loop's DB-gated accounting never sees its calls; route
    them through the aux-accounting chokepoint, which writes only ``session_model_usage`` — never
    the transcript or ``sessions`` row."""
    try:
        session_db = getattr(parent_agent, "_session_db", None)
        session_id = getattr(parent_agent, "session_id", None)
        counts = {key: int(usage.get(key) or 0) for key in _USAGE_COUNTERS}
        if session_db is None or not session_id or not any(counts.values()):
            return  # no DB, or the fork made no successful API calls (e.g. failed at spawn)
        session_db.record_auxiliary_usage(
            session_id, task="background_review", model=usage.get("model"),
            billing_provider=usage.get("provider"), billing_base_url=usage.get("base_url"),
            estimated_cost_usd=usage.get("estimated_cost_usd"),
            api_call_count=counts.pop("api_calls"), **counts,
        )
    except Exception as e:
        logger.debug("Background review usage recording failed (non-fatal): %s", e)


def _classify_review_result(actions: List[str]) -> str:
    """Map a review action summary to ``none`` / ``skill`` / ``memory`` / ``skill+memory``.
    Prefix-based on the formats :func:`summarize_background_review_actions` emits (``Skill …``,
    ``📝 Skill …``, ``Memory …``, ``User profile …``), so a free-text line like ``Skipped: no
    skill worth saving`` stays ``none``."""
    lowers = [str(action).lstrip().removeprefix("📝").lstrip().lower() for action in actions or []]
    has_skill = any(t.startswith("skill") for t in lowers)
    has_memory = any(t.startswith(("memory", "user profile")) for t in lowers)
    return "+".join(kind for kind, hit in (("skill", has_skill), ("memory", has_memory)) if hit) or "none"


def _log_review_completion(usage: Dict[str, Any], result: str) -> None:
    """Emit a per-fork completion line so cost is visible where it is incurred."""
    logger.info(
        "Background review complete: thread=bg-review calls=%d in=%d out=%d "
        "cache_read=%d result=%s",
        *(int(usage.get(k) or 0) for k in ("api_calls", "input_tokens", "output_tokens", "cache_read_tokens")),
        result,
    )


# OpenRouter provider-routing pins: prompt caches live per UPSTREAM provider, so a fork without
# the parent's pins can land on a different upstream and miss the warm cache even with
# byte-identical prompt/tools bytes.
_PROVIDER_PIN_ATTRS = (
    "providers_allowed", "providers_ignored", "providers_order", "provider_sort",
    "provider_require_parameters", "provider_data_collection",
)


def _same_model_parity_kwargs(agent: Any) -> Dict[str, Any]:
    """AIAgent kwargs that keep a SAME-model fork's request bytes identical to the parent's. Only
    for the un-routed path: on a different model the cache is cold anyway, and the parent's
    reasoning-effort vocabulary may be invalid for the routed provider (OpenRouter forwards
    ``reasoning.effort`` unclamped; codex_responses passes ``max``/``ultra`` through unmapped)."""
    kwargs: Dict[str, Any] = {
        # Anthropic's cache key is namespaced by ``thinking`` presence; the gateway session context
        # is appended to the cached system prompt at API-call time (without it the prompt diverges).
        "reasoning_config": getattr(agent, "reasoning_config", None),
        "ephemeral_system_prompt": getattr(agent, "ephemeral_system_prompt", None),
        **{attr: val for attr in _PROVIDER_PIN_ATTRS if (val := getattr(agent, attr, None))},
    }
    # Prefill sits right after the system message, so a parent with prefill would diverge at
    # index 1. Deep copy: unicode-error recovery sanitizes prefill entries IN PLACE and must not
    # rewrite the parent's bytes.
    if parent_prefill := copy.deepcopy(getattr(agent, "prefill_messages", None) or []):
        kwargs["prefill_messages"] = parent_prefill
    return kwargs


def _detach_fork_compression(review_agent: Any) -> None:
    """Detached in-memory compaction for a fork sharing the parent's session_id. Disabling
    compression (the old guard against compacting the parent's live session) removed the only
    bound on the review's snapshot. Persistence is already off, so compaction can only rewrite the
    fork's transcript — but the compressor's own SessionDB/session_id binding must be severed too,
    or cooldown/streak counters land on the parent's row. Force in-place mode and re-enable
    compression ONLY after the rebind succeeded (fail-closed); gates stay deferred until the first
    response so request #1 is a warm cache read."""
    bind = getattr(getattr(review_agent, "context_compressor", None), "bind_session_state", None)
    detached = False
    if callable(bind):
        try:
            # Plugin/third-party context engines may reject these kwargs; they own their
            # persistence policy, so a failed rebind never aborts the review.
            bind(session_db=None, session_id="")
            detached = True
        except Exception:
            # FAIL-CLOSED: the compressor may still point at the parent's SessionDB; enabling
            # compression would re-open the sibling race.
            logger.warning(
                "background-review compressor detachment failed; "
                "keeping compression DISABLED on this review fork "
                "(fail-closed, issue #93057 / #38727)",
                exc_info=True,
            )
    review_agent.compression_in_place = True
    review_agent.compression_enabled = detached
    if detached:
        review_agent._review_defer_compaction_before_first_response = True


def _fork_init_kwargs(agent: Any, rt: Dict[str, Any], routed: bool, max_iterations: int) -> Dict[str, Any]:
    """AIAgent constructor kwargs for the review fork. skip_memory=True: an external memory plugin
    scoped to the parent's session_id would leak the harness prompt into the user's real memory
    namespace; built-in MEMORY.md/USER.md state is re-bound by the caller. Toolsets match the
    parent so ``tools[]`` is byte-identical (Anthropic's cache key includes it); the runtime
    whitelist restricts dispatch."""
    kwargs: Dict[str, Any] = {
        "model": rt.get("model") or agent.model, "max_iterations": max_iterations, "quiet_mode": True,
        "platform": agent.platform, "provider": rt.get("provider") or agent.provider,
        "api_mode": rt.get("api_mode"), "base_url": rt.get("base_url") or None,
        "api_key": rt.get("api_key") or None, "credential_pool": rt.get("credential_pool"),
        "request_overrides": rt.get("request_overrides") or {}, "parent_session_id": agent.session_id,
        "enabled_toolsets": getattr(agent, "enabled_toolsets", None),
        "disabled_toolsets": getattr(agent, "disabled_toolsets", None), "skip_memory": True,
    }
    if isinstance(rt.get("max_tokens"), int):
        kwargs["max_tokens"] = rt["max_tokens"]
    if isinstance(rt.get("command"), str) and rt["command"]:
        kwargs.update(acp_command=rt["command"], acp_args=rt.get("args") or [])
    if not routed:
        kwargs.update(_same_model_parity_kwargs(agent))
    return kwargs


def build_cache_parity_fork(
    agent: Any, task_cfg: Optional[Dict[str, Any]] = None, *, max_iterations: int,
    write_origin: str = "background_review",
) -> Tuple[Any, Dict[str, Any], bool]:
    """Construct a detached AIAgent fork with warm prompt-cache parity (shared with ``/btw``): same
    runtime/credentials as the parent, byte-identical system prompt / tools[] / reasoning config on
    the same-model path, shared session_id for prefix warmth, full persistence detachment (no
    state.db writes, rotation, or external memory providers; in-place-only compaction). Returns
    ``(fork_agent, runtime_dict, routed)``; ``routed`` means a different model (cache cold —
    replay a digest). The caller owns registration, whitelisting, running, usage attribution and
    teardown."""
    from run_agent import AIAgent  # local: avoids a circular import at load
    # Inherit the parent's live runtime: AIAgent.__init__'s env auto-resolution fails for
    # OAuth-only providers, session-scoped creds and credential pools.
    _rt = _resolve_review_runtime(agent, task_cfg)
    _routed = bool(_rt.get("routed"))
    review_agent = AIAgent(**_fork_init_kwargs(agent, _rt, _routed, max_iterations))
    review_agent._memory_write_origin = review_agent._memory_write_context = write_origin
    review_agent._memory_store = agent._memory_store
    review_agent._memory_enabled = agent._memory_enabled
    review_agent._user_profile_enabled = agent._user_profile_enabled
    review_agent._memory_nudge_interval = review_agent._skill_nudge_interval = 0
    # _skip_mcp_refresh: the between-turns MCP refresh would add late-connecting MCP tools and
    # break tools[] parity. PERSISTENCE ISOLATION (curator-takeover root cause): sharing the
    # parent's session_id, the fork would otherwise write its harness turn into the REAL session,
    # which the next live turn re-reads as a standing instruction; close() must likewise not
    # finalize the parent's still-active session row. suppress_status_output: fork status/warning
    # emits go via _print_fn/status_callback, which bypass the stdout redirect.
    review_agent._skip_mcp_refresh = review_agent._persist_disabled = review_agent.suppress_status_output = True
    review_agent._session_json_enabled = review_agent._end_session_on_close = False
    review_agent._session_db = None
    review_agent.session_id = agent.session_id
    # Same model only: share the warm cached system prompt (~26% cost cut; a rebuilt prompt misses
    # the byte-exact prefix key) and pin session_start so any re-render (compression, plugin
    # hooks) stays byte-identical.
    # Inherit the parent's cached system prompt verbatim so the review fork's outbound HTTP request hits the
    # same Anthropic/OpenRouter prefix cache the parent warmed. Without this, the fork rebuilds the system
    # prompt from scratch (fresh _hermes_now() timestamp, fresh session_id, narrower toolset → different
    # skills_prompt) and the byte-exact prefix-cache key misses. See issue #25322 and PR #17276 for the full
    # analysis + measured impact (~26% end-to-end cost reduction on Sonnet 4.5). When routed to a different
    # model the parent's cached prompt is for the wrong model/cache key and would miss anyway, so let the
    # routed fork build its own.
    if not _routed:
        review_agent._cached_system_prompt = agent._cached_system_prompt
        review_agent.session_start = agent.session_start
    _detach_fork_compression(review_agent)
    # Compaction bounds a single request; this bounds the WHOLE review (checked in
    # conversation_loop via _review_input_budget_exhausted).
    review_agent._review_input_token_budget = _review_input_token_budget(task_cfg)
    return review_agent, _rt, _routed


# Install a non-interactive approval callback on this worker thread so any dangerous-command guard the
# review agent trips resolves to "deny" instead of falling back to input() -- which deadlocks against the
# parent's prompt_toolkit TUI (#15216). Same pattern as _subagent_auto_deny in tools/delegate_tool.py.
def _bg_review_auto_deny(command, description, **kwargs):
    """Non-interactive approval: dangerous-command guards resolve to "deny" instead of input(),
    which would deadlock against the parent's TUI."""
    logger.warning("Background review auto-denied dangerous command: %s (%s)", command, description)
    return "deny"


def _set_thread_approval_callback(callback: Any) -> None:
    from tools.terminal_tool import set_approval_callback

    with suppress(Exception):
        set_approval_callback(callback)


def _track_review_fork(agent: Any, review_agent: Any, *, register: bool) -> None:
    """Add (``register=True``) or remove the fork on the PARENT's tracking slots:
    ``_background_review_agent`` (direct pointer the next live turn interrupts) and
    ``_active_children`` (interrupt() fan-out). Removal is identity-scoped and idempotent; both
    are best-effort for direct test stubs — the prepared run token is the live-turn cancellation
    authority."""
    if review_agent is None:
        return
    if hasattr(agent, "_background_review_agent"):
        with _optional_lock(agent, "_background_review_lock"):
            if register:
                agent._background_review_agent = review_agent
            elif agent._background_review_agent is review_agent:
                agent._background_review_agent = None
    if hasattr(agent, "_active_children"):
        with _optional_lock(agent, "_active_children_lock"):
            if register:
                agent._active_children.append(review_agent)
            else:
                with suppress(ValueError, AttributeError):
                    agent._active_children.remove(review_agent)


def _review_tool_whitelist(review_agent: Any, task_cfg: Optional[Dict[str, Any]]) -> Tuple[set, set]:
    """``(whitelist, configured_extra_tools)`` for the review fork — DISPATCH-side only, so the
    advertised ``tools[]`` stays byte-identical to the parent's (prompt-cache parity)."""
    # Fork hardening: background review is candidate-ledger only — the default whitelist
    # is EMPTY (no memory/skills/file tools), so the review fork cannot write memory,
    # skills, config, or external side effects.
    whitelist: set = set()
    # Profile-configured opt-in tools (#44672, salvage #82146 by @BrinShadewater):
    # ``auxiliary.background_review.extra_tools`` admits named parent tools to the review
    # whitelist — e.g. a human-gated proposal tool. The whitelist can only admit, never
    # advertise: a listed tool must already exist in the parent's inherited schema. Read
    # from task_cfg (the auxiliary.background_review block already loaded for this spawn)
    # so no extra config I/O happens per review.
    configured_extra_tools: set = set()
    try:
        extra_raw = _background_review_task_config(task_cfg).get("extra_tools", [])
        if isinstance(extra_raw, list):
            configured_extra_tools = {name.strip() for name in extra_raw if isinstance(name, str) and name.strip()}
    except Exception:
        logger.debug("background_review extra_tools parse failed", exc_info=True)
    return whitelist | configured_extra_tools, configured_extra_tools


@dataclass
class _ReviewForkState:
    """Mutable hand-off between the fork phase and the outer worker's error/cleanup paths."""

    review_agent: Any = None
    review_messages: List[Dict] = field(default_factory=list)
    review_usage: Dict[str, Any] = field(default_factory=dict)


def _release_fork_clients(review_agent: Any) -> None:
    """The fork shares the foreground session ID: close() / shutdown_memory_provider() are
    session-bound (close() kills that session's terminal processes), so release only clients."""
    with suppress(Exception):
        review_agent.release_clients()


def _run_review_fork(
    agent: Any, messages_snapshot: List[Dict], prompt: str, task_cfg: Optional[Dict[str, Any]],
    review_run: Optional[_BackgroundReviewRun], st: _ReviewForkState,
) -> None:
    """Fork phase (inside thread-scoped silence): build the fork, run the prompt under the tool
    whitelist, snapshot its messages/usage, release its clients. Partial progress lands on ``st``
    so the caller's error path still sees usage and the fork to clean up."""
    st.review_agent, _rt, _routed = build_cache_parity_fork(agent, task_cfg, max_iterations=_REVIEW_MAX_ITERATIONS)
    _track_review_fork(agent, st.review_agent, register=True)
    from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
    review_whitelist, configured_extra_tools = _review_tool_whitelist(st.review_agent, task_cfg)
    extra_list = ", ".join(sorted(configured_extra_tools))
    deny_extra = f" Configured extra tools are allowed: {extra_list}." if configured_extra_tools else ""
    prompt_extra = f" Exception — these configured tools are allowed: {extra_list}." if configured_extra_tools else ""
    set_thread_tool_whitelist(
        review_whitelist,
        deny_msg_fmt=(
            "Background review denied tool call: {tool_name}. "
            "Background review is candidate-ledger only and cannot "
            "write memory, skills, config, or external side effects."
            + deny_extra
        ),
    )
    with suppress(Exception):
        from tools.skill_manager_guards import _reset_background_review_read_marks

        _reset_background_review_read_marks()
    try:
        if review_run is None or review_run.begin_request(st.review_agent):
            # Routed -> digest (cache cold anyway); same model -> full snapshot (warm cache reads).
            st.review_agent.run_conversation(
                user_message=(
                    # Fork hardening: background review is candidate-ledger only
                    # (empty default tool whitelist above), so the prompt promises
                    # deny-all — except any profile-configured extra_tools.
                    prompt + "\n\nDo not call tools. Tool calls are denied. "
                    "Return only the candidate_signals JSON object described above." + prompt_extra
                ),
                conversation_history=_digest_history(messages_snapshot) if _routed else messages_snapshot,
            )
    finally:
        clear_thread_tool_whitelist()
        # Attribute usage to the PARENT session. Snapshot BEFORE unregister/close so counters
        # survive teardown, and in this finally so a fork that consumed tokens then raised is
        # still attributed. The recorder never raises.
        if st.review_agent is not None:
            st.review_usage.update(_snapshot_review_usage(st.review_agent))
            _record_review_usage_to_parent(agent, st.review_usage)
        # Publish completion as soon as the provider-capable phase has returned or startup
        # cancellation has fenced it out (unregister + finish are identity-scoped and idempotent).
        _track_review_fork(agent, st.review_agent, register=False)
        finish_background_review_run(agent, review_run)
    st.review_messages = list(getattr(st.review_agent, "_session_messages", []))
    _release_fork_clients(st.review_agent)
    st.review_agent = None


def _publish_review_summary(agent: Any, actions: List[str]) -> None:
    summary = " · ".join(dict.fromkeys(actions))
    agent._safe_print(f"  💾 Self-improvement review: {summary}")
    if agent.background_review_callback:
        with suppress(Exception):
            agent.background_review_callback(f"💾 Self-improvement review: {summary}")


def _run_review_in_thread(
    agent: Any, messages_snapshot: List[Dict], prompt: str,
    task_cfg: Optional[Dict[str, Any]] = None, review_run: Optional[_BackgroundReviewRun] = None,
) -> None:
    """Daemon-thread worker: build the fork, run the prompt, surface the action summary via
    ``agent._safe_print`` / ``background_review_callback``. ``review_run`` (from
    :func:`prepare_background_review_run`) cancelled before the first provider call aborts
    without entering ``run_conversation()``.

    See #84423.
    """
    if review_run is not None and review_run.cancel_requested.is_set():
        finish_background_review_run(agent, review_run)
        return
    _set_thread_approval_callback(_bg_review_auto_deny)
    # A client that can't carry Hermes tool calls back would spawn a fork that cannot write
    # anything. Checked BEFORE the thread-scoped silence so the warning is not swallowed; cheap
    # check first so the normal path never resolves the runtime twice.
    if not _parent_can_emit_tool_calls(agent) and not _resolve_review_runtime(agent, task_cfg).get("routed"):
        logger.warning(
            "Background review skipped: provider %r cannot emit Hermes tool calls, "
            "so the review fork could not write memories or skills. Set "
            "auxiliary.background_review.{provider,model} to route the review to a normal model.",
            getattr(agent, "provider", "?"),
        )
        _set_thread_approval_callback(None)
        return
    st = _ReviewForkState()
    try:
        # Silence stdout/stderr for THIS thread only: a process-global redirect would blank every
        # other thread's console for the whole review.
        # A process-global ``contextlib.redirect_stdout(devnull)`` here would also blank
        # ``sys.stdout``/``sys.stderr`` for every other thread — including a gateway event-loop thread
        # driving a Telegram long-poll — for the full duration of the review (tens of seconds), swallowing
        # their console output (#55769 / #55925). ``thread_scoped_silence`` routes only this thread's writes
        # to devnull and leaves all other threads on the real streams.
        with thread_scoped_silence():
            _run_review_fork(agent, messages_snapshot, prompt, task_cfg, review_run, st)
        # Candidate-only background review: parse JSON candidate_signals from the review
        # agent's final text and append accepted/owner-gated records into the controlled
        # ledger. Direct memory/skill writes are no longer summarized because the review
        # fork cannot call those tools. The #59437 invariant carries onto the candidate
        # path: a failure while extracting review signals must not unwind the whole
        # review — coerce to an empty list so completed review work survives.
        try:
            candidate_signals = extract_background_review_candidate_signals(
                st.review_messages, agent=agent, messages_snapshot=messages_snapshot,
            )
        except Exception as e:
            logger.warning(
                "extract_background_review_candidate_signals returned partial "
                "results after exception (treating as empty); suppressing the "
                "error that previously aborted the entire review (#59437): %s",
                e,
            )
            candidate_signals = []
        candidate_actions: List[str] = []
        for signal in candidate_signals:
            result = record_background_review_signal(signal)
            if result.get("disposition") in {"accepted", "owner_gate_required", "duplicate"}:
                candidate_actions.append(
                    f"candidate {result.get('disposition')}:{result.get('dedupe_key')}"
                )
        _log_review_completion(st.review_usage, _classify_review_result(candidate_actions))
        if candidate_actions:
            _publish_review_summary(agent, candidate_actions)
    except Exception as e:
        logger.warning("Background memory/skill review failed: %s", e)
        if st.review_usage:
            _log_review_completion(st.review_usage, "error")
        agent._emit_auxiliary_failure("background review", e)
    finally:
        # Safety net for the exception path (setup failures before the request-phase finally).
        # Both cleanups are identity-scoped and idempotent; re-enter thread-scoped silence so
        # cleanup output stays quiet without blanking other threads.
        _track_review_fork(agent, st.review_agent, register=False)
        finish_background_review_run(agent, review_run)
        if st.review_agent is not None:
            with suppress(Exception), thread_scoped_silence():
                _release_fork_clients(st.review_agent)
        # Clear the approval callback so a recycled thread-id doesn't inherit it.
        _set_thread_approval_callback(None)


# (review_memory, review_skills) -> prompt attribute name; skills-only is also the default.
_PROMPT_NAME_BY_SCOPE = {
    (True, True): "_COMBINED_REVIEW_PROMPT", (True, False): "_MEMORY_REVIEW_PROMPT",
    (False, True): "_SKILL_REVIEW_PROMPT", (False, False): "_SKILL_REVIEW_PROMPT",
}


def spawn_background_review_thread(
    agent: Any, messages_snapshot: List[Dict], review_memory: bool = False,
    review_skills: bool = False, focus: Optional[str] = None,
    task_cfg: Optional[Dict[str, Any]] = None, review_run: Optional[_BackgroundReviewRun] = None,
):
    """Return ``(target, prompt)``; the caller builds the ``threading.Thread`` so test patches of
    ``run_agent.threading.Thread`` keep working. ``focus`` (``/refine [instructions]``) is appended
    to the chosen prompt; automatic reviews pass ``None``. ``task_cfg`` is the pre-loaded
    ``auxiliary.background_review`` block; when omitted it is read once here."""
    if task_cfg is None:
        task_cfg = _background_review_task_config()
    # Per-agent overrides (agent._MEMORY_REVIEW_PROMPT etc.) keep working.
    name = _PROMPT_NAME_BY_SCOPE[(review_memory, review_skills)]
    prompt = getattr(agent, name, globals()[name])
    if focus := (focus or "").strip():
        prompt = (
            f"{prompt}\n\nThe user explicitly requested this review with the following "
            f"focus — prioritize it over the general instructions above:\n{focus}"
        )

    def _target() -> None:  # resolves _run_review_in_thread at call time (tests patch it)
        _run_review_in_thread(agent, messages_snapshot, prompt, task_cfg=task_cfg, review_run=review_run)

    return _target, prompt


__all__ = [
    "_MEMORY_REVIEW_PROMPT", "_SKILL_REVIEW_PROMPT", "_COMBINED_REVIEW_PROMPT", "load_background_review_settings",
    "spawn_background_review_thread", "summarize_background_review_actions", "build_memory_write_metadata",
    "record_background_review_signal", "background_review_learning_complete",
    "extract_background_review_candidate_signals", "run_session_end_learning_hook",
]


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
def is_background_review_enabled(
    task_cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether automatic post-turn background review may spawn.

    Controlled by ``auxiliary.background_review.enabled`` (default ``true``).
    Explicit ``/refine`` (``focus`` set) bypasses this gate — same contract as
    zeroing the nudge intervals, which stops automatic forks but leaves manual
    refine working (issue #87250).

    Prefer :func:`load_background_review_settings` at the spawn call site so
    the task block is not re-read on the same turn.
    """
    if task_cfg is not None:
        try:
            from utils import is_truthy_value

            return is_truthy_value(task_cfg.get("enabled"), default=True)
        except Exception:
            logger.warning(
                "Failed to interpret background_review.enabled; leaving "
                "automatic review enabled (fail-open)",
                exc_info=True,
            )
            return True
    enabled, _ = load_background_review_settings()
    return enabled

# ---- END PLUGIN-COMPAT ----


def _background_review_default_ledger_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "evidence" / "self-improvement" / "candidates" / "background-review.jsonl"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _background_review_dedupe_key(signal: Dict[str, Any]) -> str:
    source_event = signal.get("source_event") if isinstance(signal.get("source_event"), dict) else {}
    basis = {
        "session_id": source_event.get("session_id"),
        "event_id": source_event.get("event_id"),
        "signal_type": signal.get("signal_type"),
        "candidate_class": signal.get("candidate_class"),
        "target": signal.get("target"),
        "claim": " ".join(str(signal.get("claim", "")).lower().split()),
    }
    return hashlib.sha256(_stable_json(basis).encode("utf-8")).hexdigest()[:24]


def _target_requires_owner_gate(signal: Dict[str, Any]) -> bool:
    target = signal.get("target") if isinstance(signal.get("target"), dict) else {}
    store = str(target.get("store", "")).lower()
    path = str(target.get("path_or_name", "")).lower()
    cls = str(signal.get("candidate_class", "")).lower()
    tier = str(signal.get("authority_tier", "")).upper()
    if tier == "T3" or cls in {"external_action", "skill_delete", "config_change", "governance_change", "runtime_patch"}:
        return True
    if store in {"external", "git", "cron", "daemon", "launchd", "hermes-runtime", "hermes-config", "governance", "hook", "customer-data"}:
        return True
    protected_markers = (
        "soul.md", "hermes.md", "agents.md", "claude.md", "rules/", "governance/",
        "hooks/", "agent/background_review.py", "gateway/", "config.yaml",
        "model-catalog", "forbidden-actions",
    )
    return any(marker in path for marker in protected_markers) or any(
        word in path for word in ("push", "publish", "send", "deploy", "secret", "credential", "oauth", "token")
    )


def _direct_write_blocked(signal: Dict[str, Any]) -> bool:
    requested = signal.get("requested_action") if isinstance(signal.get("requested_action"), dict) else {}
    tool = str(requested.get("tool", "")).lower()
    if tool not in {"memory", "skill_manage"}:
        return False
    return not bool(signal.get("promotion_authorized"))


def _normalize_background_review_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    source_event = signal.get("source_event") if isinstance(signal.get("source_event"), dict) else {}
    evidence = signal.get("evidence") if isinstance(signal.get("evidence"), list) else []
    clean_evidence: List[Dict[str, str]] = []
    for item in evidence:
        if isinstance(item, dict):
            clean_evidence.append({
                "path": str(item.get("path", "")),
                "excerpt": str(item.get("excerpt", ""))[:500],
            })
        elif isinstance(item, str):
            clean_evidence.append({"path": "", "excerpt": item[:500]})
    requested = signal.get("requested_action") if isinstance(signal.get("requested_action"), dict) else {}
    requested_action = dict(requested)
    requested_action["blocked"] = _direct_write_blocked(signal)
    return {
        "source": "background_review",
        "source_event": {
            "session_id": str(source_event.get("session_id", "")),
            "event_id": str(source_event.get("event_id", "")),
            "timestamp": str(source_event.get("timestamp", "")),
        },
        "signal_type": str(signal.get("signal_type", "")),
        "candidate_class": str(signal.get("candidate_class", "skill_patch")),
        "target": signal.get("target") if isinstance(signal.get("target"), dict) else {},
        "claim": str(signal.get("claim", "")).strip(),
        "evidence": clean_evidence,
        "confidence": float(signal.get("confidence", 0.0) or 0.0),
        "recurrence_count": int(signal.get("recurrence_count", 0) or 0),
        "future_trigger": str(signal.get("future_trigger", "")).strip(),
        "authority_tier": str(signal.get("authority_tier", "T1") or "T1"),
        "requested_action": requested_action,
        "promotion_authorized": bool(signal.get("promotion_authorized")),
    }


def _background_review_disposition(signal: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    if not signal.get("claim"):
        reasons.append("missing_claim")
    if not signal.get("evidence"):
        reasons.append("missing_evidence")
    if signal.get("confidence", 0.0) < 0.65:
        reasons.append("low_confidence")
    claim = str(signal.get("claim", ""))
    if len(claim) < 50 and not signal.get("future_trigger"):
        reasons.append("weak_single_signal")
    if signal.get("recurrence_count", 0) <= 0 and not signal.get("future_trigger"):
        reasons.append("not_reusable")
    if _target_requires_owner_gate(signal):
        reasons.append("owner_gate_required")
    blocking = {"missing_claim", "missing_evidence", "low_confidence", "weak_single_signal", "not_reusable"}
    if blocking & set(reasons):
        return {"disposition": "rejected_weak", "reasons": sorted(set(reasons))}
    if "owner_gate_required" in reasons:
        return {"disposition": "owner_gate_required", "reasons": sorted(set(reasons))}
    return {"disposition": "accepted", "reasons": sorted(set(reasons))}


def record_background_review_signal(signal: Dict[str, Any], ledger_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Validate and record one background-review learning signal.

    Background review is no longer allowed to write permanent memory/skills
    directly. It may only emit evidence-backed candidate signals into this
    append-only, deduped ledger. Promotion is handled by the later promotion
    lane, not by the review fork itself.
    """
    normalized = _normalize_background_review_signal(signal)
    disposition = _background_review_disposition(normalized)
    normalized["disposition"] = disposition["disposition"]
    normalized["reasons"] = disposition["reasons"]
    normalized["dedupe_key"] = _background_review_dedupe_key(normalized)
    normalized["recorded_at"] = int(time.time())
    direct_blocked = bool((normalized.get("requested_action") or {}).get("blocked"))
    path = Path(ledger_path) if ledger_path is not None else _background_review_default_ledger_path()
    if disposition["disposition"] == "rejected_weak":
        return {
            "ok": True,
            "written": False,
            "disposition": "rejected_weak",
            "reasons": disposition["reasons"],
            "direct_write_blocked": direct_blocked,
            "dedupe_key": normalized["dedupe_key"],
            "path": str(path),
        }
    existing: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                existing.add(str(json.loads(line).get("dedupe_key", "")))
            except Exception:
                continue
    if normalized["dedupe_key"] in existing:
        return {
            "ok": True,
            "written": False,
            "disposition": "duplicate",
            "reasons": ["duplicate"],
            "direct_write_blocked": direct_blocked,
            "dedupe_key": normalized["dedupe_key"],
            "path": str(path),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_stable_json(normalized) + "\n")
    return {
        "ok": True,
        "written": True,
        "disposition": disposition["disposition"],
        "reasons": disposition["reasons"],
        "direct_write_blocked": direct_blocked,
        "dedupe_key": normalized["dedupe_key"],
        "path": str(path),
    }


def background_review_learning_complete(result: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(str(result.get("path", ""))) if result.get("path") else None
    key = str(result.get("dedupe_key", ""))
    blockers: List[str] = []
    if result.get("disposition") not in {"accepted", "owner_gate_required", "duplicate"}:
        blockers.append("not_accepted")
    if result.get("disposition") != "duplicate" and not result.get("written"):
        blockers.append("ledger_entry_missing")
    if path and key and path.exists():
        try:
            found = any(json.loads(line).get("dedupe_key") == key for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except Exception:
            found = False
        if not found:
            blockers.append("ledger_entry_missing")
    elif result.get("disposition") != "duplicate":
        blockers.append("ledger_entry_missing")
    return {"complete": not blockers, "blockers": sorted(set(blockers))}


def extract_background_review_candidate_signals(review_messages: List[Dict], *, agent: Any, messages_snapshot: List[Dict]) -> List[Dict[str, Any]]:
    """Extract JSON candidate signals from the review agent's final text."""
    signals: List[Dict[str, Any]] = []
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        text = _msg_text(msg)
        if not text:
            continue
        candidates = [text]
        candidates.extend(match.group(1) for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S))
        for candidate_text in candidates:
            try:
                parsed = json.loads(candidate_text)
            except Exception:
                continue
            raw = parsed.get("candidate_signals") if isinstance(parsed, dict) else None
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        source_event = item.setdefault("source_event", {})
                        if isinstance(source_event, dict):
                            source_event.setdefault("session_id", getattr(agent, "session_id", ""))
                            source_event.setdefault("event_id", f"background_review:{len(messages_snapshot or [])}")
                        signals.append(item)
    return signals


def _message_text_for_learning(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return " ".join(parts).strip()
    return ""


def _eligible_session_learning_signal(text: str) -> bool:
    lower = text.lower()
    markers = (
        "you framed", "wrong task", "generic", "do not", "don't", "never ",
        "next time", "future", "scope boundary", "owner gate", "quality standard",
        "fake done", "evidence", "verification", "blocked", "rework", "i hate",
        "stop ", "must ", "should ", "angry", "correction",
    )
    return len(text) >= 40 and any(marker in lower for marker in markers)


def _session_message_to_signal(session_id: str, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = _message_text_for_learning(message)
    if not _eligible_session_learning_signal(text):
        return None
    message_id = str(message.get("id") or message.get("message_id") or message.get("turn_id") or hashlib.sha256(text.encode("utf-8")).hexdigest()[:12])
    timestamp = str(message.get("timestamp", ""))
    lower = text.lower()
    authority_tier = "T3" if any(marker in lower for marker in ("push", "publish", "deploy", "runtime", "owner gate", "config", "credential", "secret")) else "T1"
    candidate_class = "external_action" if any(marker in lower for marker in ("push", "publish", "deploy")) else "skill_patch"
    target = {"store": "skill", "path_or_name": "software-delivery-workflows"}
    if authority_tier == "T3" and "runtime" in lower:
        target = {"store": "hermes-runtime", "path_or_name": "agent/background_review.py"}
        candidate_class = "runtime_patch"
    return {
        "source_event": {"session_id": session_id, "event_id": message_id, "timestamp": timestamp},
        "signal_type": "session_end_user_correction",
        "claim": text[:500],
        "target": target,
        "candidate_class": candidate_class,
        "evidence": [{"path": f"sessiondb:{session_id}:{message_id}", "excerpt": text[:500]}],
        "confidence": 0.9,
        "recurrence_count": 1,
        "future_trigger": "When this class of user correction or workflow failure recurs.",
        "authority_tier": authority_tier,
    }


def run_session_end_learning_hook(
    session_db: Any,
    session_id: str,
    *,
    ledger_path: Optional[str | Path] = None,
    receipt_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Read a real SessionDB transcript and append eligible learning candidates.

    The hook is fail-closed: missing/unreadable SessionDB never reports success.
    It only writes candidate ledger entries through record_background_review_signal;
    it never promotes memory/skill changes directly.
    """
    path = Path(ledger_path) if ledger_path is not None else _background_review_default_ledger_path()
    receipt = Path(receipt_path) if receipt_path is not None else path.with_suffix(".receipt.json")
    result: Dict[str, Any] = {
        "status": "processed",
        "session_id": session_id,
        "ledger_path": str(path),
        "receipt_path": str(receipt),
        "created": 0,
        "duplicates": 0,
        "skipped": 0,
        "malformed": 0,
        "blocked": 0,
        "promoted": 0,
        "promotion_performed": False,
        "blockers": [],
        "decisions": [],
    }
    if session_db is None or not session_id:
        result["status"] = "blocked"
        result["blockers"].append("session_db_unavailable")
        return result
    try:
        messages = session_db.get_messages(session_id)
    except Exception as exc:
        result["status"] = "blocked"
        result["blockers"].append("session_db_unavailable")
        result["error"] = str(exc)
        return result
    if not isinstance(messages, list):
        result["status"] = "blocked"
        result["blockers"].append("session_db_malformed")
        return result
    for message in messages:
        if not isinstance(message, dict):
            result["malformed"] += 1
            continue
        if message.get("role") != "user" or not isinstance(message.get("content"), (str, list)):
            result["malformed"] += 1 if message.get("role") == "user" else 0
            result["skipped"] += 0 if message.get("role") == "user" else 1
            continue
        signal = _session_message_to_signal(session_id, message)
        if signal is None:
            result["skipped"] += 1
            continue
        decision = record_background_review_signal(signal, path)
        result["decisions"].append(decision)
        if decision.get("written"):
            result["created"] += 1
        elif decision.get("disposition") == "duplicate":
            result["duplicates"] += 1
        elif decision.get("disposition") == "rejected_weak":
            result["skipped"] += 1
        else:
            result["blocked"] += 1
    if result["malformed"] or result["created"] or result["duplicates"] or result["skipped"]:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def record_session_end_learning_hook_failure(session_id: str, error: Exception | str, *, path: Optional[str | Path] = None) -> Dict[str, Any]:
    receipt_path = Path(path) if path is not None else _background_review_default_ledger_path().with_name("session-end-hook-failures.jsonl")
    payload = {
        "source": "session_end_learning_hook",
        "session_id": session_id,
        "status": "blocked",
        "blocker": "session_end_learning_hook_failed",
        "error": str(error),
        "recorded_at": int(time.time()),
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("a", encoding="utf-8") as fh:
        fh.write(_stable_json(payload) + "\n")
    return {"written": True, "path": str(receipt_path), "blocker": payload["blocker"]}
