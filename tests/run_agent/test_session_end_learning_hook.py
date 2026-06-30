"""Session-end learning hook tests.

The hook reads real SessionDB-style events, extracts eligible learning
candidates, and writes them through the background-review candidate ledger only.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import background_review as bg


class FakeSessionDB:
    def __init__(self, messages=None, error: Exception | None = None):
        self.messages = messages or []
        self.error = error
        self.calls = []

    def get_messages(self, session_id: str):
        self.calls.append(session_id)
        if self.error:
            raise self.error
        return self.messages


def _user_msg(message_id=1, content="You framed the task wrong. Next time map the exact owner goal before acting."):
    return {
        "id": message_id,
        "role": "user",
        "content": content,
        "timestamp": 1234.5 + message_id,
    }


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_hook_001_real_session_event_creates_candidate_ledger_entry(tmp_path):
    db = FakeSessionDB([_user_msg()])
    result = bg.run_session_end_learning_hook(db, "sess-1", ledger_path=tmp_path / "ledger.jsonl")

    rows = _read_jsonl(tmp_path / "ledger.jsonl")
    assert result["status"] == "processed"
    assert result["created"] == 1
    assert rows[0]["source_event"]["session_id"] == "sess-1"
    assert rows[0]["source_event"]["event_id"] == "1"


def test_hook_002_ineligible_event_creates_no_candidate(tmp_path):
    db = FakeSessionDB([_user_msg(content="Thanks, looks good.")])
    result = bg.run_session_end_learning_hook(db, "sess-1", ledger_path=tmp_path / "ledger.jsonl")

    assert result["created"] == 0
    assert result["skipped"] >= 1
    assert not (tmp_path / "ledger.jsonl").exists()


def test_hook_003_duplicate_run_creates_no_duplicate_candidate(tmp_path):
    db = FakeSessionDB([_user_msg()])
    first = bg.run_session_end_learning_hook(db, "sess-1", ledger_path=tmp_path / "ledger.jsonl")
    second = bg.run_session_end_learning_hook(db, "sess-1", ledger_path=tmp_path / "ledger.jsonl")

    assert first["created"] == 1
    assert second["duplicates"] == 1
    assert len(_read_jsonl(tmp_path / "ledger.jsonl")) == 1


def test_hook_004_malformed_event_is_skipped_with_receipt(tmp_path):
    db = FakeSessionDB([{"id": 1, "role": "user", "content": None}, "not-a-dict", _user_msg(2)])
    result = bg.run_session_end_learning_hook(db, "sess-1", ledger_path=tmp_path / "ledger.jsonl")

    assert result["created"] == 1
    assert result["malformed"] == 2
    assert result["receipt_path"]
    assert Path(result["receipt_path"]).exists()


def test_hook_005_missing_sessiondb_fails_closed_no_fake_success(tmp_path):
    result = bg.run_session_end_learning_hook(None, "sess-1", ledger_path=tmp_path / "ledger.jsonl")

    assert result["status"] == "blocked"
    assert result["created"] == 0
    assert "session_db_unavailable" in result["blockers"]
    assert not (tmp_path / "ledger.jsonl").exists()


def test_hook_006_multiple_valid_events_create_traceable_candidates(tmp_path):
    db = FakeSessionDB([
        _user_msg(1, "Your output was generic. Future writing must include concrete evidence paths and no filler."),
        _user_msg(2, "You crossed the scope boundary. Next time stop at the explicit owner gate before editing runtime."),
    ])
    result = bg.run_session_end_learning_hook(db, "sess-1", ledger_path=tmp_path / "ledger.jsonl")

    rows = _read_jsonl(tmp_path / "ledger.jsonl")
    assert result["created"] == 2
    assert {row["source_event"]["event_id"] for row in rows} == {"1", "2"}


def test_hook_007_preserves_source_session_ids(tmp_path):
    db = FakeSessionDB([_user_msg(9)])
    bg.run_session_end_learning_hook(db, "sess-xyz", ledger_path=tmp_path / "ledger.jsonl")

    row = _read_jsonl(tmp_path / "ledger.jsonl")[0]
    assert row["source_event"]["session_id"] == "sess-xyz"


def test_hook_008_hook_does_not_directly_promote_candidates(tmp_path):
    db = FakeSessionDB([_user_msg()])
    result = bg.run_session_end_learning_hook(db, "sess-1", ledger_path=tmp_path / "ledger.jsonl")

    assert result["promoted"] == 0
    assert result["promotion_performed"] is False


def test_hook_failure_on_agent_close_records_failure_receipt(monkeypatch):
    import run_agent
    import agent.background_review as bg_review

    events = []

    class FakeDB:
        def end_session(self, session_id, reason):
            events.append(("end_session", session_id, reason))

    def boom(session_db, session_id):
        events.append(("hook", session_id))
        raise RuntimeError("hook failed")

    def record_failure(session_id, error):
        events.append(("failure_receipt", session_id, str(error)))
        return {"written": True}

    agent = object.__new__(run_agent.AIAgent)
    agent.session_id = "sess-fail"
    agent._session_db = FakeDB()
    agent._end_session_on_close = True
    agent._session_messages = []
    agent._active_children = []
    import threading
    agent._active_children_lock = threading.Lock()
    agent.client = None

    monkeypatch.setattr(run_agent, "cleanup_vm", lambda task_id: None)
    monkeypatch.setattr(run_agent, "cleanup_browser", lambda task_id: None)
    monkeypatch.setattr(bg_review, "run_session_end_learning_hook", boom)
    monkeypatch.setattr(bg_review, "record_session_end_learning_hook_failure", record_failure)

    run_agent.AIAgent.close(agent)

    assert ("hook", "sess-fail") in events
    assert ("failure_receipt", "sess-fail", "hook failed") in events
    assert ("end_session", "sess-fail", "agent_close") in events

def test_hook_wired_into_agent_close_before_end_session(monkeypatch):
    import run_agent

    events = []

    class FakeDB:
        def get_messages(self, session_id):
            events.append(("get_messages", session_id))
            return [_user_msg(1)]

        def end_session(self, session_id, reason):
            events.append(("end_session", session_id, reason))

    agent = object.__new__(run_agent.AIAgent)
    agent.session_id = "sess-close"
    agent._session_db = FakeDB()
    agent._end_session_on_close = True
    agent._session_messages = []
    agent._active_children = []
    import threading
    agent._active_children_lock = threading.Lock()
    agent.client = None

    monkeypatch.setattr(run_agent, "cleanup_vm", lambda task_id: events.append(("cleanup_vm", task_id)))
    monkeypatch.setattr(run_agent, "cleanup_browser", lambda task_id: events.append(("cleanup_browser", task_id)))

    run_agent.AIAgent.close(agent)

    assert ("get_messages", "sess-close") in events
    assert events.index(("get_messages", "sess-close")) < events.index(("end_session", "sess-close", "agent_close"))

