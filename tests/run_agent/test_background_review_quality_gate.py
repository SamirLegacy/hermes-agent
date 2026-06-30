"""Quality-gate tests for native background review self-improvement.

These guard the full-spectrum self-improvement contract: background review may
emit evidence-backed candidate signals, but it must not directly mutate durable
memory/skills or mark learning complete without a ledger entry.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import background_review as bg


def _signal(**overrides):
    payload = {
        "source_event": {
            "session_id": "sess-1",
            "event_id": "turn-7",
            "timestamp": "2026-06-28T17:30:00Z",
        },
        "signal_type": "owner_correction",
        "claim": "Owner corrected final verification discipline: after any edit, run fresh checks before delivery and do not reuse stale green output.",
        "target": {"store": "skill", "path_or_name": "software-delivery-workflows"},
        "candidate_class": "skill_patch",
        "evidence": [{"path": "tests/output.txt", "excerpt": "stale green output is not fresh verification"}],
        "confidence": 0.93,
        "recurrence_count": 2,
        "future_trigger": "When final delivery follows any code or artifact edit.",
        "authority_tier": "T1",
    }
    payload.update(overrides)
    return payload


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_bg_001_accepts_valid_high_quality_evidence_signal(tmp_path):
    result = bg.record_background_review_signal(_signal(), tmp_path / "ledger.jsonl")

    rows = _read_jsonl(tmp_path / "ledger.jsonl")
    assert result["disposition"] == "accepted"
    assert result["written"] is True
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.93
    assert rows[0]["source_event"]["event_id"] == "turn-7"
    assert rows[0]["evidence"][0]["path"] == "tests/output.txt"


def test_bg_002_rejects_duplicate_signal(tmp_path):
    first = bg.record_background_review_signal(_signal(), tmp_path / "ledger.jsonl")
    second = bg.record_background_review_signal(_signal(), tmp_path / "ledger.jsonl")

    assert first["disposition"] == "accepted"
    assert second["disposition"] == "duplicate"
    assert second["written"] is False
    assert len(_read_jsonl(tmp_path / "ledger.jsonl")) == 1


def test_bg_003_rejects_weak_single_signal(tmp_path):
    result = bg.record_background_review_signal(
        _signal(
            claim="Maybe answer shorter.",
            evidence=[{"path": "chat", "excerpt": "ok"}],
            confidence=0.41,
            recurrence_count=0,
            future_trigger="",
        ),
        tmp_path / "ledger.jsonl",
    )

    assert result["disposition"] == "rejected_weak"
    assert not (tmp_path / "ledger.jsonl").exists()


def test_bg_004_blocks_direct_permanent_skill_write(tmp_path):
    result = bg.record_background_review_signal(
        _signal(
            requested_action={"tool": "skill_manage", "action": "patch", "name": "software-delivery-workflows"},
        ),
        tmp_path / "ledger.jsonl",
    )

    assert result["direct_write_blocked"] is True
    assert result["disposition"] == "accepted"
    assert _read_jsonl(tmp_path / "ledger.jsonl")[0]["requested_action"]["blocked"] is True


def test_bg_005_blocks_direct_memory_write_unless_promotion_authorized(tmp_path):
    blocked = bg.record_background_review_signal(
        _signal(
            candidate_class="memory",
            target={"store": "memory", "path_or_name": "user"},
            requested_action={"tool": "memory", "action": "add", "target": "user"},
        ),
        tmp_path / "ledger.jsonl",
    )
    authorized = bg.record_background_review_signal(
        _signal(
            source_event={"session_id": "sess-1", "event_id": "turn-8", "timestamp": "2026-06-28T17:31:00Z"},
            candidate_class="memory",
            target={"store": "memory", "path_or_name": "user"},
            requested_action={"tool": "memory", "action": "add", "target": "user"},
            promotion_authorized=True,
        ),
        tmp_path / "ledger.jsonl",
    )

    assert blocked["direct_write_blocked"] is True
    assert authorized["direct_write_blocked"] is False
    rows = _read_jsonl(tmp_path / "ledger.jsonl")
    assert rows[0]["requested_action"]["blocked"] is True
    assert rows[1]["requested_action"]["blocked"] is False


def test_bg_006_records_evidence_path_for_accepted_candidate(tmp_path):
    bg.record_background_review_signal(_signal(), tmp_path / "ledger.jsonl")

    row = _read_jsonl(tmp_path / "ledger.jsonl")[0]
    assert row["evidence"][0]["path"] == "tests/output.txt"
    assert row["evidence"][0]["excerpt"]
    assert row["dedupe_key"]


def test_bg_007_is_idempotent_on_repeated_same_input(tmp_path):
    for _ in range(5):
        bg.record_background_review_signal(_signal(), tmp_path / "ledger.jsonl")

    assert len(_read_jsonl(tmp_path / "ledger.jsonl")) == 1


def test_bg_008_cannot_mark_learning_complete_without_ledger_entry(tmp_path):
    result = bg.background_review_learning_complete(
        {"disposition": "accepted", "written": False, "path": str(tmp_path / "ledger.jsonl")}
    )

    assert result["complete"] is False
    assert "ledger_entry_missing" in result["blockers"]
