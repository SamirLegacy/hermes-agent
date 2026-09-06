"""Every key that cmd_chat forwards through _CHAT_PASSTHROUGH must be accepted by cli.main().

Regression: a7db91450d added ("takeover", False) to _CHAT_PASSTHROUGH but cli.main() had no
``takeover`` parameter, so every ``hermes chat`` invocation died with
``TypeError: main() got an unexpected keyword argument 'takeover'`` (observed 2026-09-06 in the
E1 sandbox runner against the fork candidate).
"""

import inspect


def test_chat_passthrough_keys_are_accepted_by_cli_main():
    from hermes_cli.main import _CHAT_PASSTHROUGH
    import cli

    params = inspect.signature(cli.main).parameters
    accepts_var_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    missing = [key for key, _default in _CHAT_PASSTHROUGH if key not in params]
    assert accepts_var_kwargs or not missing, (
        f"cmd_chat forwards {missing} to cli.main() but cli.main() does not accept them"
    )


def test_cli_main_forwards_takeover_to_hermes_cli(monkeypatch):
    import cli

    captured = {}

    def fake_build(*args, **kwargs):
        captured.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_build_cli_from_args", fake_build)
    monkeypatch.setattr(cli, "_start_worktree_setup", lambda *a, **k: None)
    try:
        cli.main(query="x", oneshot=True, takeover=True)
    except SystemExit:
        pass
    assert captured.get("takeover") is True
