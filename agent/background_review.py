"""Background memory/skill review — fork the agent to evaluate the turn.

After every turn, ``AIAgent.run_conversation`` may call
:func:`spawn_background_review` to fire off a daemon thread that replays
the conversation snapshot in a forked :class:`AIAgent` and asks itself
"should any skill/memory be saved or updated?".  Writes go straight to
the memory + skill stores.  Main conversation and prompt cache are never
touched.

The fork inherits the parent's live runtime (provider, model, base_url,
credentials, cached system prompt) so it hits the same prefix cache and
uses the same auth.  It runs with a tool whitelist limited to memory and
skill management tools; everything else is denied at runtime.

See the ``hermes-agent-dev`` skill (``references/self-improvement-loop.md``)
for invariants and PR review criteria.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background-review aux-model selector + routed digest.
#
# The review fork runs on the MAIN model by default ("auto"), replaying the
# full conversation — already warm in the prompt cache, so cheap cache reads.
# Optimal and unchanged. A user can route the review to a different, cheaper
# model via auxiliary.background_review.{provider,model}. A different model
# cannot reuse the parent's cache (different key), so the fork is cold
# regardless — replaying the full transcript would just cold-write it. So when
# (and only when) routed to a different model, we replay a compact DIGEST to
# minimise cold-written tokens. Same model -> full replay; different model ->
# digest. That's the whole policy.
# ---------------------------------------------------------------------------


def _resolve_review_runtime(agent: Any) -> Dict[str, Any]:
    """Resolve provider/model/credentials for the review fork.

    Default (auto / unset / same as parent): inherit the parent's live runtime
    (with codex_app_server -> codex_responses downgrade). ``routed`` is False —
    the fork uses the main model and the warm cache, exactly as before. When
    ``auxiliary.background_review.{provider,model}`` names a concrete model
    different from the parent's, resolve that runtime and set ``routed=True``.
    """
    parent_runtime = agent._current_main_runtime()
    parent_api_mode = parent_runtime.get("api_mode") or None
    if parent_api_mode == "codex_app_server":
        parent_api_mode = "codex_responses"
    parent = {
        "provider": agent.provider,
        "model": agent.model,
        "api_key": parent_runtime.get("api_key") or None,
        "base_url": parent_runtime.get("base_url") or None,
        "api_mode": parent_api_mode,
        "routed": False,
    }
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        return parent
    aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
    task = aux.get("background_review", {}) if isinstance(aux.get("background_review"), dict) else {}
    task_provider = (str(task.get("provider", "")).strip() or None)
    task_model = (str(task.get("model", "")).strip() or None)
    task_base_url = (str(task.get("base_url", "")).strip() or None)
    task_api_key = (str(task.get("api_key", "")).strip() or None)
    if not (task_provider and task_provider != "auto" and task_model):
        return parent
    if task_provider == (agent.provider or "") and task_model == (agent.model or ""):
        return parent  # same model/provider as parent -> not routed
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        rp = resolve_runtime_provider(
            requested=task_provider,
            target_model=task_model,
            explicit_api_key=task_api_key,
            explicit_base_url=task_base_url,
        )
        return {
            "provider": rp.get("provider") or task_provider,
            "model": task_model,
            "api_key": rp.get("api_key"),
            "base_url": rp.get("base_url"),
            "api_mode": rp.get("api_mode"),
            "routed": True,
        }
    except Exception as e:
        logger.debug("background-review aux routing failed (%s); using main model", e)
        return parent


def _msg_text(m: Dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict)).strip()
    return ""


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


def _digest_history(messages_snapshot: List[Dict], tail: int = 24) -> List[Dict]:
    """Compact replay for the routed (different-model) path only.

    Keeps the recent ``tail`` messages verbatim, collapses older turns into one
    synthetic user-role digest, preserving role alternation. Used ONLY when
    routed to a different model (cache cold regardless, so fewer cold-written
    tokens is a pure win). Never on the main-model path (full replay stays warm).
    """
    msgs = list(messages_snapshot or [])
    if len(msgs) <= tail:
        return msgs
    keep = msgs[-tail:]
    while keep and isinstance(keep[0], dict) and keep[0].get("role") == "tool":
        tail += 1
        if len(msgs) <= tail:
            return msgs
        keep = msgs[-tail:]
    old = msgs[:-len(keep)]
    lines: List[str] = []
    for m in old:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _msg_text(m).replace("\n", " ")
        if role == "user" and text:
            lines.append(f"USER: {text[:300]}")
        elif role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                names = [(tc.get("function") or {}).get("name", "?") for tc in tcs if isinstance(tc, dict)]
                lines.append(f"ASSISTANT[tools: {', '.join(names)}]")
            if text:
                lines.append(f"ASSISTANT: {text[:200]}")
    digest = {
        "role": "user",
        "content": (
            "[Earlier conversation digest — older turns summarised to bound the "
            "review's cold-write cost on the routed aux model. Recent turns "
            "follow verbatim below.]\n" + "\n".join(lines)
        ),
    }
    return [digest] + keep


# Review-prompt strings — used by ``spawn_background_review_thread`` to build
# the user-message that the forked review agent receives. Background review is
# candidate-only: it must not directly call memory or skill tools.
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


def summarize_background_review_actions(
    review_messages: List[Dict],
    prior_snapshot: List[Dict],
    notification_mode: str = "on",
) -> List[str]:
    """Build the human-facing action summary for a background review pass.

    Walks the review agent's session messages and collects successful memory
    and skill-management actions to surface to the user. Tool messages already
    present in ``prior_snapshot`` are skipped so stale inherited results are
    not re-surfaced as fresh background work (issue #14944).

    ``notification_mode`` controls display detail:
    - ``off``: return no actions.
    - ``on``: generic "Memory updated"/tool messages.
    - ``verbose``: include compact content previews from tool-call arguments.
    """
    mode = str(notification_mode or "on").lower()
    if mode == "off":
        return []
    verbose = mode == "verbose"

    existing_tool_call_ids = set()
    existing_tool_contents = set()
    for prior in prior_snapshot or []:
        if not isinstance(prior, dict) or prior.get("role") != "tool":
            continue
        tcid = prior.get("tool_call_id")
        if tcid:
            existing_tool_call_ids.add(tcid)
        else:
            content = prior.get("content")
            if isinstance(content, str):
                existing_tool_contents.add(content)

    # Map review-agent tool results back to the calls that produced them.  The
    # result JSON only says "Entry added"; the call arguments contain action,
    # target, and content previews.  Restricting to notify_tools also prevents
    # helper tools from surfacing as memory work just because they succeeded.
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
            if fn_name not in notify_tools:
                continue
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            if tcid:
                call_details[tcid] = {
                    "tool": fn_name,
                    "action": args.get("action", "?"),
                    "target": args.get("target", "memory"),
                    "content": args.get("content", ""),
                    "old_text": args.get("old_text", ""),
                    "operations": args.get("operations") or [],
                    "name": args.get("name", ""),
                    "old_string": args.get("old_string", ""),
                    "new_string": args.get("new_string", ""),
                }

    actions: List[str] = []
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id")
        if tcid and tcid in existing_tool_call_ids:
            continue
        if not tcid:
            content_str = msg.get("content")
            if isinstance(content_str, str) and content_str in existing_tool_contents:
                continue
        if tcid and all_tool_call_ids and tcid not in call_details:
            continue
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get("success"):
            continue
        message = data.get("message", "")
        detail = call_details.get(tcid, {})
        target = data.get("target", "") or detail.get("target", "")
        is_skill = detail.get("tool") == "skill_manage"

        message_lower = message.lower()
        if not verbose:
            if "created" in message_lower:
                actions.append(message)
                continue
            if "updated" in message_lower:
                actions.append(message)
                continue
            if is_skill and "patched" in message_lower:
                actions.append(message)
                continue

        if is_skill:
            label = "Skill"
        elif target:
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
        else:
            continue

        if verbose:
            action = detail.get("action", "")
            content = detail.get("content", "")
            old_text = detail.get("old_text", "")
            skill_name = detail.get("name", "")
            operations = detail.get("operations") or []
            max_preview = 120
            if is_skill:
                change = data.get("_change", {})
                old_string = change.get("old", "") or detail.get("old_string", "")
                new_string = change.get("new", "") or detail.get("new_string", "")
                description = change.get("description", "")
                if action == "patch" and (old_string or new_string):
                    old_preview = old_string[:80].replace("\n", " ") + (
                        "…" if len(old_string) > 80 else ""
                    )
                    new_preview = new_string[:80].replace("\n", " ") + (
                        "…" if len(new_string) > 80 else ""
                    )
                    actions.append(
                        f"📝 Skill '{skill_name}' patched: "
                        f"\"{old_preview}\" → \"{new_preview}\""
                    )
                elif action == "create" and description:
                    actions.append(f"📝 Skill '{skill_name}' created: {description}")
                elif action == "edit" and description:
                    actions.append(f"📝 Skill '{skill_name}' rewritten: {description}")
                else:
                    actions.append(f"📝 {message}" if message else f"Skill {action}")
            elif operations:
                for op in operations:
                    op = op or {}
                    op_act = op.get("action", "")
                    op_content = (op.get("content") or "")
                    op_old = (op.get("old_text") or "")
                    if op_act == "add" and op_content:
                        preview = op_content[:max_preview] + ("…" if len(op_content) > max_preview else "")
                        actions.append(f"{label} ➕ {preview}")
                    elif op_act == "replace" and op_content:
                        preview = op_content[:max_preview] + ("…" if len(op_content) > max_preview else "")
                        actions.append(f"{label} ✏️ {preview}")
                    elif op_act == "remove" and op_old:
                        preview = op_old[:60] + ("…" if len(op_old) > 60 else "")
                        actions.append(f"{label} ➖ {preview}")
            elif action == "add" and content:
                preview = content[:max_preview] + ("…" if len(content) > max_preview else "")
                actions.append(f"{label} ➕ {preview}")
            elif action == "replace" and content:
                preview = content[:max_preview] + ("…" if len(content) > max_preview else "")
                actions.append(f"{label} ✏️ {preview}")
            elif action == "remove" and old_text:
                preview = old_text[:60] + ("…" if len(old_text) > 60 else "")
                actions.append(f"{label} ➖ {preview}")
            else:
                actions.append(f"{label} updated")
        elif (
            "added" in message_lower
            or "replaced" in message_lower
            or "removed" in message_lower
            or "applied" in message_lower
            or (target and "add" in message.lower())
            or "Entry added" in message
        ):
            actions.append(f"{label} updated")
    return actions


def build_memory_write_metadata(
    agent: Any,
    *,
    write_origin: Optional[str] = None,
    execution_context: Optional[str] = None,
    task_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build provenance metadata for external memory-provider mirrors."""
    metadata: Dict[str, Any] = {
        "write_origin": write_origin or getattr(agent, "_memory_write_origin", "assistant_tool"),
        "execution_context": (
            execution_context
            or getattr(agent, "_memory_write_context", "foreground")
        ),
        "session_id": agent.session_id or "",
        "parent_session_id": agent._parent_session_id or "",
        "platform": agent.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
        "tool_name": "memory",
    }
    if task_id:
        metadata["task_id"] = task_id
    if tool_call_id:
        metadata["tool_call_id"] = tool_call_id
    return {k: v for k, v in metadata.items() if v not in {None, ""}}


def _run_review_in_thread(
    agent: Any,
    messages_snapshot: List[Dict],
    prompt: str,
) -> None:
    """Worker function executed in the background-review daemon thread.

    Spawns a forked ``AIAgent`` inheriting the parent's runtime, runs the
    review prompt, and surfaces a compact action summary back to the user
    via ``agent._safe_print`` and ``agent.background_review_callback``.
    """
    # Local import to avoid a hard circular dep at module load.
    from run_agent import AIAgent
    from tools.terminal_tool import set_approval_callback as _set_approval_callback

    # Install a non-interactive approval callback on this worker
    # thread so any dangerous-command guard the review agent trips
    # resolves to "deny" instead of falling back to input() -- which
    # deadlocks against the parent's prompt_toolkit TUI (#15216).
    # Same pattern as _subagent_auto_deny in tools/delegate_tool.py.
    def _bg_review_auto_deny(command, description, **kwargs):
        logger.warning(
            "Background review auto-denied dangerous command: %s (%s)",
            command, description,
        )
        return "deny"
    try:
        _set_approval_callback(_bg_review_auto_deny)
    except Exception:
        pass

    review_agent = None
    review_messages: List[Dict] = []
    try:
        with open(os.devnull, "w", encoding="utf-8") as _devnull, \
             contextlib.redirect_stdout(_devnull), \
             contextlib.redirect_stderr(_devnull):
            # Inherit the parent agent's live runtime (provider, model,
            # base_url, api_key, api_mode) so the fork uses the exact
            # same credentials the main turn is using.  Without this,
            # AIAgent.__init__ re-runs auto-resolution from env vars,
            # which fails for OAuth-only providers, session-scoped
            # creds, or credential-pool setups where the resolver can't
            # reconstruct auth from scratch -- producing the spurious
            # "No LLM provider configured" warning at end of turn.
            # _resolve_review_runtime() returns the parent's live runtime by
            # default (routed=False; main model, warm cache), or — when the user
            # set auxiliary.background_review.{provider,model} to a different
            # model — that model's runtime (routed=True). The codex_app_server
            # -> codex_responses downgrade is applied inside the resolver.
            _rt = _resolve_review_runtime(agent)
            _routed = bool(_rt.get("routed"))
            # skip_memory=True keeps the review fork from
            # touching external memory plugins (honcho, mem0,
            # supermemory, etc.).  Without it, the fork's
            # __init__ rebuilds its own _memory_manager from
            # config, scoped to the parent's session_id, and
            # run_conversation() then leaks the harness prompt
            # into the user's real memory namespace via three
            # ingestion sites: on_turn_start (cadence + turn
            # message), prefetch_all (recall query), and
            # sync_all (harness prompt + review output recorded
            # as a (user, assistant) turn pair).  Built-in
            # MEMORY.md / USER.md state is re-bound from the
            # parent below so memory(action="add") writes from
            # the review still land on disk; the review just
            # has zero side effects on external providers.
            # Match parent's toolset config so ``tools[]`` is byte-identical
            # in the request body — Anthropic's cache key includes it.
            # (The runtime whitelist below still restricts dispatch.)
            review_agent = AIAgent(
                model=_rt.get("model") or agent.model,
                max_iterations=16,
                quiet_mode=True,
                platform=agent.platform,
                provider=_rt.get("provider") or agent.provider,
                api_mode=_rt.get("api_mode"),
                base_url=_rt.get("base_url") or None,
                api_key=_rt.get("api_key") or None,
                credential_pool=getattr(agent, "_credential_pool", None),
                parent_session_id=agent.session_id,
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                skip_memory=True,
            )
            review_agent._memory_write_origin = "background_review"
            review_agent._memory_write_context = "background_review"
            # The review fork pins the parent's cached system prompt and keeps
            # ``tools[]`` byte-identical to the parent so its outbound request
            # hits the same provider cache prefix (see the toolset-parity note
            # above). The between-turns MCP refresh in build_turn_context would
            # add late-connecting MCP tools to this fork and break that parity,
            # so opt the review fork out of it.
            review_agent._skip_mcp_refresh = True
            review_agent._memory_store = agent._memory_store
            review_agent._memory_enabled = agent._memory_enabled
            review_agent._user_profile_enabled = agent._user_profile_enabled
            review_agent._memory_nudge_interval = 0
            review_agent._skill_nudge_interval = 0
            # Suppress all status/warning emits from the fork so the
            # user only sees the final successful-action summary.
            # Without this, mid-review "Iteration budget exhausted",
            # rate-limit retries, compression warnings, and other
            # lifecycle messages bubble up through _emit_status ->
            # _vprint and leak past the stdout redirect (they go via
            # _print_fn/status_callback, which bypass sys.stdout).
            review_agent.suppress_status_output = True
            # Inherit the parent's cached system prompt verbatim so
            # the review fork's outbound HTTP request hits the same
            # Anthropic/OpenRouter prefix cache the parent warmed.
            # Without this, the fork rebuilds the system prompt from
            # scratch (fresh _hermes_now() timestamp, fresh
            # session_id, narrower toolset → different skills_prompt)
            # and the byte-exact prefix-cache key misses. See
            # issue #25322 and PR #17276 for the full analysis +
            # measured impact (~26% end-to-end cost reduction on
            # Sonnet 4.5).
            # Share the parent's warm cached system prompt ONLY when the review
            # runs on the SAME model (not routed). When routed to a different
            # model the parent's cached prompt is for the wrong model/cache key
            # and would miss anyway, so let the routed fork build its own.
            if not _routed:
                review_agent._cached_system_prompt = agent._cached_system_prompt
                # Defensive: pin session_start + session_id to the
                # parent's so any code path that re-renders parts of
                # the system prompt (compression, plugin hooks) still
                # produces byte-identical output. The cached-prompt
                # assignment above already short-circuits the normal
                # rebuild path, but these pins guarantee parity even
                # if a future code path bypasses the cache.
                review_agent.session_start = agent.session_start
            review_agent.session_id = agent.session_id
            # The fork shares the parent's live session_id (pinned above for
            # prefix-cache parity). It is single-lifecycle and calls close()
            # right after this run_conversation(); without opting out, close()
            # would finalize the parent's still-active session row mid
            # conversation (the review fires every ~10 turns). Leave session
            # finalization to the real owner (CLI close / gateway reset / cron).
            review_agent._end_session_on_close = False
            # Never let the review fork compress. It shares the parent's
            # session_id, so if it won a compression race it would rotate the
            # parent into a NEW child that the gateway never adopts (the fork
            # is single-lifecycle and dies right after this run_conversation).
            # The foreground turn would then start from the stale parent and
            # compress it again, leaving the same parent with two sibling
            # children (issue #38727). Review also needs full context to
            # produce a good memory/skill summary — compressing would strip
            # detail. Both compression triggers in conversation_loop.py gate on
            # agent.compression_enabled, so this short-circuits both paths.
            review_agent.compression_enabled = False

            from hermes_cli.plugins import (
                set_thread_tool_whitelist,
                clear_thread_tool_whitelist,
            )

            review_whitelist: set[str] = set()
            set_thread_tool_whitelist(
                review_whitelist,
                deny_msg_fmt=(
                    "Background review denied tool call: {tool_name}. "
                    "Background review is candidate-ledger only and cannot "
                    "write memory, skills, config, or external side effects."
                ),
            )
            try:
                # Routed to a different model -> replay a digest (cache is cold
                # on that model anyway, so minimise cold-written tokens). Same
                # model -> replay the full snapshot (warm cache reads).
                _review_history = (
                    _digest_history(messages_snapshot) if _routed
                    else messages_snapshot
                )
                review_agent.run_conversation(
                    user_message=(
                        prompt
                        + "\n\nDo not call tools. Tool calls are denied. "
                        "Return only the candidate_signals JSON object described above."
                    ),
                    conversation_history=_review_history,
                )
            finally:
                clear_thread_tool_whitelist()

            # Snapshot review actions before teardown. close() is allowed to
            # clean per-session state, but the user-visible self-improvement
            # summary still needs the completed review agent's tool results.
            review_messages = list(getattr(review_agent, "_session_messages", []))

            # Tear down memory providers while stdout is still
            # redirected so background thread teardown (Honcho flush,
            # Hindsight sync, etc.) stays silent.  The finally block
            # below is a safety net for the exception path.
            try:
                review_agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                review_agent.close()
            except Exception:
                pass
            review_agent = None

        # Candidate-only background review: parse JSON candidate_signals and
        # append accepted/owner-gated records into the controlled ledger. Direct
        # memory/skill writes are no longer summarized because the review fork
        # cannot call those tools.
        candidate_actions: List[str] = []
        for signal in extract_background_review_candidate_signals(
            review_messages,
            agent=agent,
            messages_snapshot=messages_snapshot,
        ):
            result = record_background_review_signal(signal)
            if result.get("disposition") in {"accepted", "owner_gate_required", "duplicate"}:
                candidate_actions.append(
                    f"candidate {result.get('disposition')}:{result.get('dedupe_key')}"
                )

        if candidate_actions:
            summary = " · ".join(dict.fromkeys(candidate_actions))
            agent._safe_print(
                f"  💾 Self-improvement review: {summary}"
            )
            _bg_cb = agent.background_review_callback
            if _bg_cb:
                try:
                    _bg_cb(
                        f"💾 Self-improvement review: {summary}"
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.warning("Background memory/skill review failed: %s", e)
        agent._emit_auxiliary_failure("background review", e)
    finally:
        # Safety-net cleanup for the exception path.  Normal
        # completion already shut down inside redirect_stdout above.
        # Re-open devnull here so any teardown output (Honcho flush,
        # Hindsight sync, background thread joins) stays silent even
        # on the exception path where redirect_stdout already exited.
        if review_agent is not None:
            try:
                with open(os.devnull, "w", encoding="utf-8") as _fn, \
                     contextlib.redirect_stdout(_fn), \
                     contextlib.redirect_stderr(_fn):
                    try:
                        review_agent.shutdown_memory_provider()
                    except Exception:
                        pass
                    try:
                        review_agent.close()
                    except Exception:
                        pass
            except Exception:
                pass
        # Clear the approval callback on this bg-review thread so a
        # recycled thread-id doesn't inherit a stale reference.
        try:
            _set_approval_callback(None)
        except Exception:
            pass


def spawn_background_review_thread(
    agent: Any,
    messages_snapshot: List[Dict],
    review_memory: bool = False,
    review_skills: bool = False,
):
    """Build the review thread target and prompt for a background review.

    Returns a ``(target, prompt)`` tuple.  The caller (``AIAgent._spawn_background_review``)
    owns the actual ``threading.Thread`` construction so test-level patches
    of ``run_agent.threading.Thread`` keep working.
    """
    # Pick the right prompt based on which triggers fired.  Allow per-agent
    # override (the prompts moved to module-level constants but old code paths
    # that set agent._MEMORY_REVIEW_PROMPT etc. directly keep working).
    if review_memory and review_skills:
        prompt = getattr(agent, "_COMBINED_REVIEW_PROMPT", _COMBINED_REVIEW_PROMPT)
    elif review_memory:
        prompt = getattr(agent, "_MEMORY_REVIEW_PROMPT", _MEMORY_REVIEW_PROMPT)
    else:
        prompt = getattr(agent, "_SKILL_REVIEW_PROMPT", _SKILL_REVIEW_PROMPT)

    def _target() -> None:
        _run_review_in_thread(agent, messages_snapshot, prompt)

    return _target, prompt


__all__ = [
    "_MEMORY_REVIEW_PROMPT",
    "_SKILL_REVIEW_PROMPT",
    "_COMBINED_REVIEW_PROMPT",
    "record_background_review_signal",
    "background_review_learning_complete",
    "extract_background_review_candidate_signals",
    "run_session_end_learning_hook",
    "spawn_background_review_thread",
    "summarize_background_review_actions",
    "build_memory_write_metadata",
]
