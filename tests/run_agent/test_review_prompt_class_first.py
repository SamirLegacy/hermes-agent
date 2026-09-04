"""Behavior tests for background-review candidate-only prompts.

Background review must not directly write memory or skills. It may only emit
structured candidate signals for the score-gated ledger/promotion lane.

Sync note (2026-09-04): upstream's old-doctrine tests (active-update bias,
memory section, unresolved-failures, read-before-write, lesson-layer on
_MEMORY/_SKILL/_COMBINED review prompts) were dropped — they assert the
direct-write review doctrine the fork deliberately replaced with the
candidate-only lane. Upstream's lesson-layer doctrine survives in
``agent/curator.py`` (``CURATOR_REVIEW_PROMPT``), covered by the curator
test below.
"""

from run_agent import AIAgent


def _all_prompts():
    return [
        AIAgent._MEMORY_REVIEW_PROMPT,
        AIAgent._SKILL_REVIEW_PROMPT,
        AIAgent._COMBINED_REVIEW_PROMPT,
    ]


def test_background_review_prompts_are_candidate_only():
    for prompt in _all_prompts():
        lower = prompt.lower()
        assert "candidate_signals" in prompt
        assert "must not write memory" in lower or "must not directly call memory" in lower or "must not write" in lower
        assert "must not" in lower and "patch" in lower
        assert "promotion lane" in lower


def test_background_review_prompts_require_evidence_and_source_event():
    for prompt in _all_prompts():
        lower = prompt.lower()
        assert "evidence" in lower
        assert "source_event" in prompt
        assert "confidence" in lower
        assert "future_trigger" in prompt
        assert "authority_tier" in prompt


def test_background_review_prompts_reject_weak_or_duplicate_signals():
    for prompt in _all_prompts():
        lower = prompt.lower()
        assert "weak" in lower
        assert "duplicate" in lower
        assert "empty candidate_signals" in lower or '"candidate_signals":[]' in prompt


def test_background_review_prompts_encode_owner_gate_targets():
    for prompt in _all_prompts():
        lower = prompt.lower()
        assert "runtime" in lower
        assert "governance" in lower
        assert "push" in lower
        assert "deploy" in lower
        assert "t3" in lower


def test_background_review_prompts_do_not_bias_toward_most_sessions_writing():
    for prompt in _all_prompts():
        lower = prompt.lower()
        assert "most sessions produce" not in lower
        assert "missed learning opportunity" not in lower
        assert "be active" not in lower


# ---------------------------------------------------------------------------
# Upstream's curator prompt (agent/curator.py) — the consolidation lane where
# lesson-layer doctrine lives in the merged tree. Kept from upstream.
# ---------------------------------------------------------------------------

def test_curator_prompt_consolidates_by_distilling():
    from agent.curator import CURATOR_REVIEW_PROMPT
    lower = CURATOR_REVIEW_PROMPT.lower()
    assert "distill" in lower, "curator must distill absorbed content, not file it"
    assert "verbatim" in lower and "per-incident" in lower, "curator must not copy siblings verbatim into references/"
