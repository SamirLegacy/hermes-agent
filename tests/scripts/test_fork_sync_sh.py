"""Tests for scripts/fork-sync.sh.

Same pattern as tests/scripts/test_case_collision_check.py: drive the real
script against throwaway repositories so a normal pytest run catches a
regression in the sync contract (check exit codes, contributors/emails
auto-resolution, conflict reporting, canonical-sync invariant, probe
output format).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "fork-sync.sh"


def _git(repo: Path, *args: str, check=True):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.t",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.t",
    )
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )
    if check:
        assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r


def _init_repo(path: Path, remote_urls: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text("seed\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "seed")
    for name, url in remote_urls.items():
        _git(path, "remote", "add", name, url)
    return path


def _make_sync_fixture(tmp_path: Path):
    """origin = fork, upstream = upstream; both backed by real bare repos."""
    origin = tmp_path / "origin-bare"
    upstream = tmp_path / "upstream-bare"
    for bare in (origin, upstream):
        bare.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("seed\n")
    (seed / "main_code.py").write_text("X = 1\n")
    (seed / "contributors").mkdir()
    (seed / "contributors" / "emails").mkdir()
    (seed / "contributors" / "emails" / "t@t.t").write_text("t\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "remote", "add", "upstream", str(upstream))
    _git(seed, "push", "-q", "upstream", "main")

    main_checkout = tmp_path / "main"
    subprocess.run(
        ["git", "clone", "-q", "-o", "origin", str(origin), str(main_checkout)],
        check=True,
    )
    _git(main_checkout, "remote", "add", "upstream", str(upstream))
    _git(main_checkout, "fetch", "-q", "upstream")
    return main_checkout, origin, upstream


def _push_upstream_change(upstream_bare: Path, tmp_path: Path, fname: str, content: str, author: tuple[str, str] = ("u", "u@u.u")):
    work = tmp_path / ("upwork-" + fname.replace("/", "_").replace(".", "_"))
    subprocess.run(["git", "clone", "-q", str(upstream_bare), str(work)], check=True)
    (work / fname).parent.mkdir(parents=True, exist_ok=True)
    (work / fname).write_text(content)
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"] = author
    env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"] = author
    subprocess.run(["git", "-C", str(work), "add", fname], check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", f"upstream {fname}"],
        check=True, env=env, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)


def _run_fork_sync(tmp_path: Path, main_checkout: Path, *args: str, extra_env: dict[str, str] | None = None):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["FORK_SYNC_MAIN_CHECKOUT"] = str(main_checkout)
    env["FORK_SYNC_WORKTREE"] = str(tmp_path / "wt")
    env["FORK_SYNC_HERMES_PROFILE"] = "desktop-local"
    env["FORK_SYNC_SKILLS_SNAPSHOT"] = str(tmp_path / "skills-before.txt")
    env["FORK_SYNC_BUNDLE_SWAP_SCRIPT"] = str(tmp_path / "no-swap.sh")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=180, env=env,
        cwd=str(tmp_path),
    )


# ── check ────────────────────────────────────────────────────────────────

def test_check_exit_0_when_nothing_to_do(tmp_path):
    main_checkout, _, _ = _make_sync_fixture(tmp_path)
    r = _run_fork_sync(tmp_path, main_checkout, "check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "BEHIND=0" in r.stdout
    assert "FORK-SYNC check rc=0" in r.stdout


def test_check_exit_10_when_work_available(tmp_path):
    main_checkout, _, upstream = _make_sync_fixture(tmp_path)
    _push_upstream_change(upstream, tmp_path, "upstream_new.py", "Y = 2\n")
    r = _run_fork_sync(tmp_path, main_checkout, "check")
    assert r.returncode == 10, r.stdout + r.stderr
    assert "BEHIND=1" in r.stdout
    assert "FORK-SYNC check rc=10" in r.stdout


# ── merge: contributors auto-resolution + conflict reporting ─────────────

def test_merge_auto_resolves_contributors_add_add(tmp_path):
    """contributors/emails add/add conflict must be auto-resolved to upstream,
    and new upstream author emails get mapping files."""
    main_checkout, _, upstream = _make_sync_fixture(tmp_path)
    # Fork side: add its own mapping file.
    (main_checkout / "contributors" / "emails" / "fork-dev@x.x").write_text("fork-dev\n")
    _git(main_checkout, "add", "-A")
    _git(main_checkout, "commit", "-q", "-m", "fork mapping")
    _git(main_checkout, "push", "-q", "origin", "main")

    # Upstream side: add/add conflict on the same file + a new author.
    _push_upstream_change(
        upstream, tmp_path, "contributors/emails/fork-dev@x.x", "UPSTREAM-WINS\n",
        author=("New Person", "newperson@example.org"),
    )
    r = _run_fork_sync(tmp_path, main_checkout, "merge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "auto-resolved (took upstream): contributors/emails/fork-dev@x.x" in r.stdout
    assert "new contributor mapping: newperson@example.org" in r.stdout
    wt = tmp_path / "wt"
    assert (wt / "contributors" / "emails" / "fork-dev@x.x").read_text() == "UPSTREAM-WINS\n"
    assert (wt / "contributors" / "emails" / "newperson@example.org").read_text() == "New Person\n"
    head = _git(wt, "log", "-1", "--format=%P").stdout.strip()
    assert len(head.split()) == 2, "merge commit must have two parents"


def test_merge_reports_real_conflicts_with_rc20(tmp_path):
    """A real code conflict is NOT auto-resolved: rc=20 plus the diff-filter=U list."""
    main_checkout, _, upstream = _make_sync_fixture(tmp_path)
    # Fork edits main_code.py one way, upstream another way -> real conflict.
    (main_checkout / "main_code.py").write_text("X = 'fork'\n")
    _git(main_checkout, "commit", "-qam", "fork change")
    _git(main_checkout, "push", "-q", "origin", "main")
    _push_upstream_change(upstream, tmp_path, "main_code.py", "X = 'upstream'\n")

    r = _run_fork_sync(tmp_path, main_checkout, "merge")
    assert r.returncode == 20, r.stdout + r.stderr
    assert "CONFLICT (manual resolution required" in r.stdout
    assert "main_code.py" in r.stdout
    assert "FORK-SYNC merge rc=20" in r.stdout


# ── structural invariant: exactly one canonical sync line ────────────────

def test_single_canonical_sync_line():
    text = SCRIPT.read_text()
    assert text.count("uv sync") == 1, (
        "the canonical dependency-sync line must be defined exactly once "
        "(CANONICAL_SYNC=...); any other occurrence re-opens the MCP-prune hole"
    )
    assert "CANONICAL_SYNC=(uv sync --extra dev --extra mcp)" in text
    assert 'command -v uv' in text, "script must refuse to run without uv"


def test_verify_and_deploy_use_the_canonical_line():
    text = SCRIPT.read_text()
    assert text.count('"${CANONICAL_SYNC[@]}"') >= 2, (
        "verify and deploy must both expand CANONICAL_SYNC"
    )


# ── probe output format with a stubbed hermes ────────────────────────────

def _write_stub_hermes(tmp_path: Path, behaviors: dict[str, int]):
    """behaviors: hermes-subcommand-keyword -> exit code (default 0)."""
    stub = tmp_path / "hermes"
    lines = [
        "#!/usr/bin/env bash",
        'args="$*"',
        'case "$args" in',
        f'  *"mcp test heygen"*) exit {behaviors.get("mcp", 0)};;',
        f'  *"hooks doctor"*) exit {behaviors.get("hooks", 0)};;',
        '  *"sessions list"*) echo "ID"; echo "probe-session-123"; exit 0;;',
        f'  *"chat --resume"*) echo "resumed probe-session-123 OK2"; exit {behaviors.get("resume", 0)};;',
        f'  *"chat -q"*) echo "OK"; exit {behaviors.get("chat", 0)};;',
        '  *"skills list"*) cat "$FORK_SYNC_STUB_SKILLS"; exit 0;;',
        "  *) exit 0;;",
        "esac",
    ]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def test_probe_all_pass_output_format(tmp_path):
    main_checkout, _, _ = _make_sync_fixture(tmp_path)
    skills = tmp_path / "skills.txt"
    skills.write_text("skill-a\nskill-b\n")
    (tmp_path / "skills-before.txt").write_text("skill-a\nskill-b\n")
    stub = _write_stub_hermes(tmp_path, {})
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FORK_SYNC_STUB_SKILLS": str(skills),
    }
    r = _run_fork_sync(tmp_path, main_checkout, "probe", extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    for needle in ("PASS mcp heygen", "PASS hooks doctor", "PASS chat smoke",
                   "PASS resume smoke", "PASS skills preservation"):
        assert needle in r.stdout, needle
    assert "CHECK mcp heygen :: hermes" in r.stdout
    assert "FORK-SYNC probe rc=0" in r.stdout


def test_probe_fails_when_check_fails(tmp_path):
    main_checkout, _, _ = _make_sync_fixture(tmp_path)
    skills = tmp_path / "skills.txt"
    skills.write_text("a\n")
    (tmp_path / "skills-before.txt").write_text("a\n")
    stub = _write_stub_hermes(tmp_path, {"mcp": 1})
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FORK_SYNC_STUB_SKILLS": str(skills),
    }
    r = _run_fork_sync(tmp_path, main_checkout, "probe", extra_env=env)
    assert r.returncode == 30, r.stdout + r.stderr
    assert "FAIL mcp heygen" in r.stdout
    assert "FORK-SYNC probe rc=30" in r.stdout


def test_probe_fails_on_skills_drift(tmp_path):
    main_checkout, _, _ = _make_sync_fixture(tmp_path)
    skills = tmp_path / "skills.txt"
    skills.write_text("a\nb\n")          # after
    (tmp_path / "skills-before.txt").write_text("a\n")   # before
    stub = _write_stub_hermes(tmp_path, {})
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FORK_SYNC_STUB_SKILLS": str(skills),
    }
    r = _run_fork_sync(tmp_path, main_checkout, "probe", extra_env=env)
    assert r.returncode == 30, r.stdout + r.stderr
    assert "FAIL skills preservation" in r.stdout
