"""Tests for system-prompt model-identity sync across provider failover.

The system prompt is session-stable and embeds ``Model:``/``Provider:``
identity lines.  When ``try_activate_fallback`` swaps the runtime, the
prompt must be rewritten in place (and synced into the in-flight
``api_messages``) or the agent reports the primary model's name while a
fallback model is answering — e.g. a local gemma fallback claiming to be
gpt-5.4-mini after a Codex usage-limit 429.
"""

from types import SimpleNamespace

from agent.chat_completion_helpers import rewrite_prompt_model_identity
from agent.conversation_loop import (
    _redecorate_prompt_cache_for_provider,
    _sync_failover_system_message,
)
from agent.prompt_caching import apply_anthropic_cache_control


_PROMPT = (
    "You are a helpful assistant.\n"
    "\n"
    "Memory note at line start:\n"
    "Model: decoy-from-memory\n"
    "\n"
    "Conversation started: Wednesday, June 10, 2026\n"
    "Model: gpt-5.4-mini\n"
    "Provider: openai-codex"
)


def _agent(prompt=_PROMPT, ephemeral=None):
    return SimpleNamespace(
        _cached_system_prompt=prompt,
        ephemeral_system_prompt=ephemeral,
    )


def _count_cache_markers(messages):
    count = 0
    for msg in messages:
        if "cache_control" in msg:
            count += 1
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "cache_control" in part:
                    count += 1
    return count


def _cache_agent(
    *,
    use_caching,
    native=False,
    prompt=_PROMPT,
    static=None,
    cache_ttl="5m",
    provider="openai",
    model="gpt-4o",
    tools=None,
    direct_tool_cache=False,
):
    return SimpleNamespace(
        _cached_system_prompt=prompt,
        _cached_system_prompt_static=static,
        ephemeral_system_prompt=None,
        _use_prompt_caching=use_caching,
        _use_native_cache_layout=native,
        _cache_ttl=cache_ttl,
        provider=provider,
        model=model,
        tools=tools or [],
        _direct_native_anthropic_tool_cache_capability=lambda: direct_tool_cache,
        client=None,
    )


class TestRewritePromptModelIdentity:

    def test_only_last_occurrence_is_rewritten(self):
        agent = _agent()
        rewrite_prompt_model_identity(agent, "gemma4:e2b-mlx", "custom")
        # Earlier matching lines may be user content (memory snapshots,
        # context files) and must survive untouched.
        assert "Model: decoy-from-memory" in agent._cached_system_prompt


    def test_noop_when_prompt_missing_or_empty(self):
        for prompt in (None, ""):
            agent = _agent(prompt=prompt)
            rewrite_prompt_model_identity(agent, "m", "p")
            assert agent._cached_system_prompt == prompt



class TestSyncFailoverSystemMessage:
    def test_patches_in_flight_system_message(self):
        agent = _agent()
        rewrite_prompt_model_identity(agent, "gemma4:e2b-mlx", "custom")
        api_messages = [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": "what model are you?"},
        ]
        result = _sync_failover_system_message(agent, api_messages, _PROMPT)
        assert "Model: gemma4:e2b-mlx" in api_messages[0]["content"]
        assert result == agent._cached_system_prompt


    def test_noop_without_cached_prompt(self):
        agent = _agent(prompt=None)
        api_messages = [{"role": "system", "content": "original"}]
        result = _sync_failover_system_message(agent, api_messages, "active")
        assert api_messages[0]["content"] == "original"
        assert result == "active"



class TestSyncFailoverPreservesCacheDecoration:
    """The sync must not flatten a cache-decorated system message.

    ``apply_anthropic_cache_control`` runs once per call block, before the retry
    loop, splitting the system prompt into ``[static prefix, volatile tail]``
    blocks that carry the cache_control breakpoints. A failover fires *inside*
    that retry loop, so overwriting the list with a bare string drops both
    breakpoints and the retried request re-bills the whole system prompt.
    """

    _STATIC = "You are a helpful assistant.\n\nStable brief.\n"

    def _decorated(self, prompt):
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "what model are you?"},
        ]
        return apply_anthropic_cache_control(
            messages,
            cache_ttl=None,
            native_anthropic=True,
            static_system_prefix=self._STATIC,
        )

    def test_keeps_breakpoints_and_static_prefix(self):
        prompt = self._STATIC + "Model: gpt-5.4-mini\nProvider: openai-codex"
        agent = _agent(prompt=prompt)
        api_messages = self._decorated(prompt)
        assert isinstance(api_messages[0]["content"], list)

        rewrite_prompt_model_identity(agent, "gemma4:e2b-mlx", "custom")
        _sync_failover_system_message(agent, api_messages, prompt)

        content = api_messages[0]["content"]
        assert isinstance(content, list), "cache decoration was flattened to a string"
        assert len(content) == 2
        assert all(part.get("cache_control") for part in content), (
            "the failover retry would ship zero system cache breakpoints"
        )
        # The static prefix must stay byte-identical or its cache entry misses.
        assert content[0]["text"] == self._STATIC
        # The identity refresh still lands, in the volatile tail.
        assert "Model: gemma4:e2b-mlx" in content[1]["text"]
        assert "Provider: custom" in content[1]["text"]

    def test_keeps_single_block_shape(self):
        prompt = "Model: gpt-5.4-mini\nProvider: openai-codex"
        agent = _agent(prompt=prompt)
        # No static prefix match -> the single-block fallback layout.
        api_messages = apply_anthropic_cache_control(
            [{"role": "system", "content": prompt}],
            cache_ttl=None,
            native_anthropic=True,
            static_system_prefix=None,
        )
        assert isinstance(api_messages[0]["content"], list)
        assert len(api_messages[0]["content"]) == 1

        rewrite_prompt_model_identity(agent, "gemma4:e2b-mlx", "custom")
        _sync_failover_system_message(agent, api_messages, prompt)

        content = api_messages[0]["content"]
        assert isinstance(content, list) and len(content) == 1
        assert content[0].get("cache_control")
        assert "Model: gemma4:e2b-mlx" in content[0]["text"]




class TestRedecoratePromptCacheOnPolicyChange:
    """#72626: failover must re-render breakpoints for the *new* cache policy.

    ``TestSyncFailoverPreservesCacheDecoration`` only covers same-policy
    identity sync (native→native). These cases cover the policy-change class.
    """

    _STATIC = "You are a helpful assistant.\n\nStable brief.\n"

    def test_cache_off_to_cache_on_adds_breakpoints(self):
        prompt = self._STATIC + "Model: gpt-5.4-mini\nProvider: openai"
        # Primary never decorated (cache-off).
        undecorated = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "again"},
        ]
        assert _count_cache_markers(undecorated) == 0

        agent = _cache_agent(
            use_caching=True,
            native=True,
            prompt=prompt,
            static=self._STATIC,
            provider="anthropic",
        )
        decorated, _ = _redecorate_prompt_cache_for_provider(agent, undecorated)
        assert _count_cache_markers(decorated) >= 2
        assert isinstance(decorated[0]["content"], list)
        assert decorated[0]["content"][0]["text"] == self._STATIC

    def test_replans_tools_for_the_active_destination(self):
        from agent.prompt_caching import build_prompt_cache_plan

        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]
        messages = [
            {"role": "system", "content": self._STATIC + "volatile"},
            {"role": "user", "content": "lookup"},
        ]
        source = build_prompt_cache_plan(
            messages,
            tools,
            native_anthropic=True,
            static_system_prefix=self._STATIC,
            direct_native_tool_cache=True,
        )
        agent = _cache_agent(
            use_caching=True,
            native=False,
            prompt=messages[0]["content"],
            static=self._STATIC,
            provider="openrouter",
            tools=tools,
            direct_tool_cache=False,
        )

        fallback_messages, _, fallback_tools = _redecorate_prompt_cache_for_provider(
            agent,
            source.messages,
            tools_for_api=source.tools,
        )

        assert "cache_control" in source.tools[-1]
        assert "cache_control" not in fallback_tools[-1]
        assert "cache_control" not in tools[-1]
        assert all(
            part.get("cache_control")
            for part in fallback_messages[0]["content"]
            if isinstance(part, dict)
        )




    def test_moa_guidance_stays_outside_last_breakpoint(self):
        prompt = "sys"
        guidance = (
            "[Mixture of Agents context — use this as private guidance for the "
            "normal Hermes agent loop.]\nAggregator: agg\n\nadvice"
        )
        base = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "task\n\n" + guidance},
        ]
        decorated = apply_anthropic_cache_control(base, native_anthropic=True)

        class _Completions:
            def rebase_prepared_request(self, prepared, messages):
                from agent.moa_loop import _attach_reference_guidance

                out = [dict(m) for m in messages]
                _attach_reference_guidance(out, prepared["guidance"])
                return {**prepared, "messages": out}

        agent = _cache_agent(use_caching=True, native=True, prompt=prompt, provider="moa")
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        prepared = {"guidance": guidance, "messages": decorated}
        out, new_prepared = _redecorate_prompt_cache_for_provider(
            agent, decorated, moa_prepared=prepared
        )
        assert new_prepared is not None
        # Guidance must be present, but the cache marker on the last user
        # turn's content parts must terminate *before* the guidance text.
        last = out[-1]
        assert last.get("role") == "user"
        content = last["content"]
        if isinstance(content, list):
            marked = [p for p in content if isinstance(p, dict) and "cache_control" in p]
            guidance_parts = [
                p for p in content
                if isinstance(p, dict) and guidance in (p.get("text") or "")
            ]
            assert marked, "base transcript should still carry breakpoints"
            assert guidance_parts, "guidance must be re-attached"
            # Marked part should not contain the guidance body.
            assert all(guidance not in (p.get("text") or "") for p in marked)
        else:
            assert guidance in content




class TestPeelReferenceGuidanceRoundTrip:
    """peel must invert every attach shape — the two live adjacent in
    moa_loop.py precisely so this contract can't drift silently."""

    _GUIDANCE = "[Mixture of Agents reference context]\nAdvice body."

    def _round_trip(self, base):
        import copy

        from agent.moa_loop import _attach_reference_guidance, peel_reference_guidance

        attached = copy.deepcopy(base)
        _attach_reference_guidance(attached, self._GUIDANCE)
        assert attached != base, "attach must change the transcript"
        return peel_reference_guidance(attached, self._GUIDANCE)


    def test_list_part_shape(self):
        base = [
            {"role": "system", "content": "sys"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "task", "cache_control": {"type": "ephemeral"}}],
            },
        ]
        assert self._round_trip(base) == base


    def test_guidance_only_part_drops_message_not_empty_residue(self):
        # If the guidance part is the only content left after peeling, the
        # whole message goes — an empty-content user turn must never remain.
        from agent.moa_loop import peel_reference_guidance

        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "ok"},
            {
                "role": "user",
                "content": [{"type": "text", "text": self._GUIDANCE}],
            },
        ]
        peeled = peel_reference_guidance(messages, self._GUIDANCE)
        assert peeled == messages[:-1]


# ── Mid-turn provider fallback: durable record + loud surfaces ─────────────
#
# Incident 2026-09-02 (L2 forensics D4): try_activate_fallback swapped
# fable-5 → kimi-k3 mid-turn and wrote NOTHING to the session row or
# transcript — a later resume re-resolved the ambient default instead of the
# model that actually answered; the classifier's reason was DEBUG-only; and
# the user-facing notice died with the suppressed -q status channel.


class _MockFallbackClient:
    base_url = "https://api.moonshot.cn/v1"
    api_key = "fb-key"


def _fallback_agent_with_db(db, session_id):
    """Real AIAgent on a fallback chain, bound to a real session row."""
    from unittest.mock import MagicMock, patch

    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "kimi-coding", "model": "kimi-k3"}],
            session_db=db,
            session_id=session_id,
        )
    agent.client = MagicMock()
    # The pre-fallback identity, mirroring the incident's VibeProxy lane.
    agent.model = "claude-fable-5"
    agent.provider = "vibeproxy-claude"
    agent.base_url = "http://127.0.0.1:8317/v1"
    return agent


class TestFallbackPersistsRoute:
    def test_marker_row_and_session_route_recorded(self, tmp_path):
        import json
        from unittest.mock import patch

        from agent.error_classifier import FailoverReason
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session(
                session_id="FB1", source="cli", model="claude-fable-5"
            )
            agent = _fallback_agent_with_db(db, "FB1")
            with patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_MockFallbackClient(), "kimi-k3"),
            ):
                assert agent._try_activate_fallback(
                    FailoverReason.server_error
                ) is True

            assert agent.model == "kimi-k3"

            # (a) Durable transcript marker, same family as the compaction
            # handoff rows: a session_meta row recording old→new + reason.
            meta_rows = [
                row
                for row in db.get_messages("FB1")
                if row.get("role") == "session_meta"
            ]
            assert len(meta_rows) == 1, db.get_messages("FB1")
            row = meta_rows[0]
            assert "claude-fable-5" in (row.get("content") or "")
            assert "kimi-k3" in (row.get("content") or "")
            dm = row.get("display_metadata") or {}
            assert dm.get("kind") == "model_fallback"
            assert dm.get("old_model") == "claude-fable-5"
            assert dm.get("new_model") == "kimi-k3"
            assert dm.get("new_provider") == "kimi-coding"
            assert dm.get("reason") == "server_error"
            assert dm.get("timestamp")

            # (b) The session row carries the fallback route so a later
            # resume (with the D1 restore) runs the model that actually
            # answered — plus provenance of the switch.
            meta = db.get_session("FB1")
            assert meta["model"] == "kimi-k3"
            config = json.loads(meta["model_config"])
            assert config["gateway_runtime"]["provider"] == "kimi-coding"
            assert config["gateway_runtime"]["base_url"] == (
                "https://api.moonshot.cn/v1"
            )
            assert config["fallback_from"] == "claude-fable-5"
            assert config["fallback_reason"] == "server_error"
        finally:
            db.close()

    def test_fallback_trigger_reason_is_info_level_and_precedes_activation(
        self, tmp_path, caplog
    ):
        """The classifier result was DEBUG-only — an undiagnosable switch.
        The trigger must log at INFO right before 'Fallback activated'."""
        import logging
        from unittest.mock import patch

        from agent.error_classifier import FailoverReason
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session(
                session_id="FB2", source="cli", model="claude-fable-5"
            )
            agent = _fallback_agent_with_db(db, "FB2")
            with caplog.at_level(
                logging.INFO, logger="agent.chat_completion_helpers"
            ):
                with patch(
                    "agent.auxiliary_client.resolve_provider_client",
                    return_value=(_MockFallbackClient(), "kimi-k3"),
                ):
                    assert agent._try_activate_fallback(
                        FailoverReason.server_error
                    ) is True

            messages = [
                r.getMessage()
                for r in caplog.records
                if r.name == "agent.chat_completion_helpers"
            ]
            trigger = next(
                (m for m in messages if m.startswith("Fallback trigger:")), None
            )
            activated = next(
                (m for m in messages if m.startswith("Fallback activated:")),
                None,
            )
            assert trigger is not None, messages
            assert activated is not None, messages
            assert "server_error" in trigger
            assert messages.index(trigger) < messages.index(activated)
        finally:
            db.close()


class TestPendingFallbackNoticeChannel:
    """-q keeps stdout machine-readable: the mid-turn switch notice must go
    to stderr when the status channel is suppressed, not die with the
    process."""

    def _stub(self, suppress):
        from types import SimpleNamespace

        emitted = []
        return SimpleNamespace(
            _pending_fallback_notice=(
                "Model fallback: claude-fable-5 via vibeproxy-claude "
                "unavailable; using kimi-k3 via kimi-coding."
            ),
            suppress_status_output=suppress,
            _emit_status=lambda s: emitted.append(s),
            _retry_status_buffer=None,
        ), emitted

    def test_notice_goes_to_stderr_when_status_suppressed(self, capsys):
        from run_agent import AIAgent

        stub, emitted = self._stub(suppress=True)
        AIAgent._emit_pending_fallback_notice(stub)
        out = capsys.readouterr()
        assert "Model fallback" in out.err
        assert "Model fallback" not in out.out  # stdout stays clean
        assert emitted, "status channel must still be notified"
        assert stub._pending_fallback_notice is None

    def test_notice_not_duplicated_to_stderr_in_normal_mode(self, capsys):
        from run_agent import AIAgent

        stub, emitted = self._stub(suppress=False)
        AIAgent._emit_pending_fallback_notice(stub)
        out = capsys.readouterr()
        assert "Model fallback" not in out.err
        assert emitted
