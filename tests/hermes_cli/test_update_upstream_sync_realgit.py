"""Real-git integration tests for fork→upstream sync.

These run against disposable on-disk repositories (no mocks for git itself)
so they prove the actual merge/abort/reachability contract, not the argv list.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import update_cmd


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, content: str) -> str:
    (repo / "file.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def fork_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build upstream (bare), fork (bare clone of upstream), and local clone of fork."""
    upstream_src = tmp_path / "upstream-src"
    upstream_src.mkdir()
    _git(upstream_src, "init", "-b", "main")
    _commit(upstream_src, "upstream root", "upstream base\n")
    upstream = tmp_path / "upstream.git"
    _git(upstream_src, "init", "--bare", str(upstream))
    _git(upstream_src, "push", str(upstream), "main")

    fork = tmp_path / "fork.git"
    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(fork)],
        check=True,
        capture_output=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(fork), str(local)],
        check=True,
        capture_output=True,
    )
    _git(local, "remote", "add", "upstream", str(upstream))
    return local, upstream, fork


@pytest.fixture(autouse=True)
def _official_upstream(monkeypatch):
    """The on-disk fixture remote is local; the production guard checks URLs.
    Stub the URL read so the fixture exercises the real git machinery while
    the guard sees the official identity."""

    def fake_remote_url(git_cmd, cwd, remote):
        if remote == "upstream":
            return update_cmd.OFFICIAL_REPO_URL
        return "https://github.com/SamirLegacy/hermes-agent.git"

    monkeypatch.setattr(update_cmd, "_get_remote_url", fake_remote_url)


def test_ff_only_sync_advances_head_and_pushes_fork(fork_repo):
    local, upstream, fork = fork_repo

    upstream_src = local.parent / "upstream-src"
    _commit(upstream_src, "upstream new", "upstream new\n")
    _git(upstream_src, "push", str(upstream), "main")

    result = update_cmd._sync_with_upstream_if_needed(["git"], local)

    assert result["status"] == "advanced"
    assert result["pre_sha"] != result["post_sha"]
    assert _git(local, "rev-parse", "HEAD") == result["post_sha"]
    # Fork (origin) was pushed back
    fork_head = _git(fork, "rev-parse", "main")
    assert fork_head == result["post_sha"]


def test_diverged_merge_preserves_fork_commits(fork_repo):
    local, upstream, fork = fork_repo

    # Fork patch: a NEW file upstream doesn't touch → clean merge, no conflict.
    (local / "fork-patch.txt").write_text("fork patch\n", encoding="utf-8")
    _git(local, "add", "fork-patch.txt")
    _git(local, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "fork patch")
    fork_sha = _git(local, "rev-parse", "HEAD")
    _git(local, "push", "origin", "main")

    upstream_src = local.parent / "upstream-src"
    _commit(upstream_src, "upstream new", "upstream new\n")
    _git(upstream_src, "push", str(upstream), "main")

    result = update_cmd._sync_with_upstream_if_needed(["git"], local)

    assert result["status"] == "advanced"
    # Fork commit must remain reachable from the merged HEAD.
    assert _git(local, "merge-base", "--is-ancestor", fork_sha, "HEAD") == ""


def test_conflict_aborts_and_reports_restored(fork_repo):
    local, upstream, fork = fork_repo

    # Fork changes file.txt
    (local / "file.txt").write_text("fork version\n", encoding="utf-8")
    _git(local, "add", "file.txt")
    _git(local, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "fork edit")
    fork_sha = _git(local, "rev-parse", "HEAD")
    _git(local, "push", "origin", "main")

    # Upstream changes the same file
    upstream_src = local.parent / "upstream-src"
    (upstream_src / "file.txt").write_text("upstream version\n", encoding="utf-8")
    _git(upstream_src, "add", "file.txt")
    _git(upstream_src, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "upstream edit")
    _git(upstream_src, "push", str(upstream), "main")

    result = update_cmd._sync_with_upstream_if_needed(["git"], local)

    assert result["status"] == "conflict"
    assert result["restored"] is True
    assert _git(local, "rev-parse", "HEAD") == result["pre_sha"]
    # No merge in progress
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "MERGE_HEAD"],
        cwd=local,
        capture_output=True,
    ).returncode != 0


def test_non_official_upstream_refused(fork_repo, monkeypatch):
    local, upstream, fork = fork_repo

    other = local.parent / "other.git"
    subprocess.run(["git", "init", "--bare", str(other)], check=True, capture_output=True)
    _git(local, "remote", "set-url", "upstream", str(other))
    # Make the URL reader report the real (non-official) upstream URL so the
    # guard fires before any fetch. The autouse fixture stubs it to official;
    # here we want the honest failure path.
    monkeypatch.setattr(
        update_cmd,
        "_get_remote_url",
        lambda *_a, **_k: "https://github.com/not-nous/hermes-agent.git",
    )

    result = update_cmd._sync_with_upstream_if_needed(["git"], local)

    assert result["status"] == "failed"
    assert "non-official" in str(result["reason"])


def test_official_url_is_positive_match_not_negated_fork():
    assert update_cmd._is_official_repo_url(
        "https://github.com/NousResearch/hermes-agent.git"
    )
    assert update_cmd._is_official_repo_url(
        "git@github.com:nousresearch/hermes-agent"
    )
    assert not update_cmd._is_official_repo_url(
        "https://github.com/SamirLegacy/hermes-agent.git"
    )
    assert not update_cmd._is_official_repo_url("")
    assert not update_cmd._is_official_repo_url(None)


def test_missing_upstream_is_skipped_not_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(update_cmd, "_has_upstream_remote", lambda *_: False)
    monkeypatch.setattr(update_cmd, "_should_skip_upstream_prompt", lambda: True)

    result = update_cmd._sync_with_upstream_if_needed(["git"], tmp_path)

    assert result["status"] == "skipped"


def test_ff_only_failure_with_no_merge_head_is_restored(monkeypatch, tmp_path):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[-3:] == ["merge", "--ff-only", "upstream/main"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not possible")
        if cmd[-2:] == ["merge", "--abort"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no merge")
        if cmd[-3:] == ["rev-parse", "--verify", "MERGE_HEAD"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd, "_has_upstream_remote", lambda *_: True)
    monkeypatch.setattr(update_cmd, "_get_remote_url", lambda *_: update_cmd.OFFICIAL_REPO_URL)
    monkeypatch.setattr(
        update_cmd,
        "_count_commits_between",
        lambda *_a, **_k: 0 if _a[-1] == "HEAD" else 2,
    )
    monkeypatch.setattr(update_cmd, "_capture_head_sha", lambda *_: "aaaaaaa")
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

    result = update_cmd._sync_with_upstream_if_needed(["git"], tmp_path)

    assert result["status"] == "conflict"
    assert result["restored"] is True
    assert ["git", "merge", "--ff-only", "upstream/main"] in commands
