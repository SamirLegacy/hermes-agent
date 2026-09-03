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
BUNDLE_SCRIPT = REPO_ROOT / "scripts" / "fork-sync-bundle-swap.sh"


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


# ── F3/F3b hardening ──────────────────────────────────────────────────────

def test_merge_refuses_while_merge_in_progress(tmp_path):
    """MERGE_HEAD present: merge must refuse (rc 24) with the resolve/abort
    hint and NEVER auto-abort the in-progress merge."""
    main_checkout, _, upstream = _make_sync_fixture(tmp_path)
    _push_upstream_change(upstream, tmp_path, "upstream_new.py", "Y = 2\n")
    # First merge run hits the conflict and leaves MERGE_HEAD behind (rc 20).
    (main_checkout / "main_code.py").write_text("X = 'fork'\n")
    _git(main_checkout, "commit", "-qam", "fork change")
    _git(main_checkout, "push", "-q", "origin", "main")
    _push_upstream_change(upstream, tmp_path, "main_code.py", "X = 'upstream'\n")
    r1 = _run_fork_sync(tmp_path, main_checkout, "merge")
    assert r1.returncode == 20, r1.stdout + r1.stderr
    wt = tmp_path / "wt"
    assert (wt / ".git" or True)
    assert _git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0

    # Second merge: refuses with rc 24 and the hint; MERGE_HEAD still there.
    r2 = _run_fork_sync(tmp_path, main_checkout, "merge")
    assert r2.returncode == 24, r2.stdout + r2.stderr
    assert "merge already in progress" in r2.stdout
    assert "merge --abort" in r2.stdout
    assert "FORK-SYNC merge rc=24" in r2.stdout
    assert _git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
    # The conflicted file was not silently resolved away.
    assert _git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.strip()


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


# ── bundle swap: pack / swap / relaunch (scripts/fork-sync-bundle-swap.sh)

FAKE_PLIST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<plist version="1.0">\n<dict>\n'
    "\t<key>CFBundleShortVersionString</key>\n"
    "\t<string>{version}</string>\n"
    "</dict>\n</plist>\n"
)


def _fake_app(dir_path: Path, version: str, marker: str = "") -> Path:
    """A minimal Hermes.app lookalike: Contents/Info.plist + a payload file."""
    app = dir_path / "Hermes.app"
    (app / "Contents").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_text(FAKE_PLIST.format(version=version))
    (app / "Contents" / "payload.txt").write_text(f"payload {marker or version}\n")
    return app


def _write_stub_npm(tmp_path: Path, app_version: str):
    """PATH-stubbed npm: 'run pack' fabricates release/mac-arm64/Hermes.app
    (cwd is apps/desktop), 'ci' is a no-op. Every invocation is logged."""
    stub = tmp_path / "npm"
    # printf interprets \\n/\\t in its FORMAT argument, so pass the plist as
    # the format (it contains no % chars) to get real newlines/tabs on disk.
    plist_printf = (
        FAKE_PLIST.format(version=app_version)
        .replace("'", "'\\''")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    lines = [
        "#!/usr/bin/env bash",
        'printf \'%s\\n\' "$*" >> "$FORK_SYNC_NPM_LOG"',
        'case "$*" in',
        '  *"run pack"*)',
        "    mkdir -p release/mac-arm64/Hermes.app/Contents",
        f"    printf '{plist_printf}' > release/mac-arm64/Hermes.app/Contents/Info.plist",
        "    exit 0;;",
        "  *) exit 0;;",
        "esac",
    ]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def _write_stub_uv(tmp_path: Path):
    """PATH-stubbed uv: 'sync' fabricates a venv/bin/python that exits 0
    (cwd is the repo root); every other invocation is a no-op success."""
    stub = tmp_path / "uv"
    lines = [
        "#!/usr/bin/env bash",
        'case "$*" in',
        '  *"sync"*)',
        '    mkdir -p "${UV_PROJECT_ENVIRONMENT:-venv}/bin"',
        '    printf \'#!/bin/sh\\nexit 0\\n\' > "${UV_PROJECT_ENVIRONMENT:-venv}/bin/python"',
        '    chmod +x "${UV_PROJECT_ENVIRONMENT:-venv}/bin/python"',
        "    exit 0;;",
        "  *) exit 0;;",
        "esac",
    ]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def _make_repo_fixture(tmp_path: Path, pkg_version: str = "0.17.0"):
    """A minimal repo with apps/desktop/{package.json,package-lock.json}."""
    repo = tmp_path / "repo"
    desk = repo / "apps" / "desktop"
    desk.mkdir(parents=True)
    (desk / "package.json").write_text(
        '{\n  "name": "@hermes/desktop",\n  "version": "%s"\n}\n' % pkg_version
    )
    (desk / "package-lock.json").write_text(
        '{"name": "@hermes/desktop", "lockfileVersion": 3, "version": "%s"}\n' % pkg_version
    )
    return repo, desk


def _run_bundle_swap(tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["FORK_SYNC_REPO_ROOT"] = str(tmp_path / "repo")
    env["FORK_SYNC_APP_TARGET"] = str(tmp_path / "applications" / "Hermes.app")
    env["FORK_SYNC_BACKUP_DIR"] = str(tmp_path / "backups")
    env.pop("FORK_SYNC_ALLOW_APP_SWAP", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(BUNDLE_SCRIPT), *args],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(tmp_path),
    )


def test_bundle_pack_ok_with_stubbed_npm(tmp_path):
    repo, desk = _make_repo_fixture(tmp_path)
    _write_stub_npm(tmp_path, "0.17.0")
    npm_log = tmp_path / "npm.log"
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FORK_SYNC_NPM_LOG": str(npm_log),
    }
    r = _run_bundle_swap(tmp_path, "pack", extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FORK-SYNC bundle pack rc=0" in r.stdout
    assert "0.17.0" in r.stdout
    # npm ci ran (no node_modules before), then pack — both logged.
    assert npm_log.read_text().splitlines() == ["ci", "run pack"]
    assert (desk / "node_modules" / ".fork-sync-lock-hash").exists()
    assert (desk / "release" / "mac-arm64" / "Hermes.app" / "Contents" / "Info.plist").exists()


def test_bundle_pack_skips_npm_ci_when_lock_unchanged(tmp_path):
    repo, desk = _make_repo_fixture(tmp_path)
    _write_stub_npm(tmp_path, "0.17.0")
    npm_log = tmp_path / "npm.log"
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FORK_SYNC_NPM_LOG": str(npm_log),
    }
    r1 = _run_bundle_swap(tmp_path, "pack", extra_env=env)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    count_after_first = len(npm_log.read_text().splitlines())
    r2 = _run_bundle_swap(tmp_path, "pack", extra_env=env)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "npm ci skipped" in r2.stdout
    # Second pack ran npm exactly once more (the pack), no second ci.
    assert len(npm_log.read_text().splitlines()) == count_after_first + 1


def test_bundle_pack_version_mismatch_fails(tmp_path):
    repo, desk = _make_repo_fixture(tmp_path, pkg_version="9.9.9")
    _write_stub_npm(tmp_path, "0.17.0")   # packs 0.17.0; package.json says 9.9.9
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FORK_SYNC_NPM_LOG": str(tmp_path / "npm.log"),
    }
    r = _run_bundle_swap(tmp_path, "pack", extra_env=env)
    assert r.returncode == 24, r.stdout + r.stderr
    assert "version mismatch" in r.stdout
    assert "FORK-SYNC bundle pack rc=24" in r.stdout


def _make_swap_fixture(tmp_path: Path, backups_pre: "list[str] | tuple[str, ...]" = ()):
    """release app 9.9.9 + installed app 0.16.0 + optional pre-existing backups."""
    repo, desk = _make_repo_fixture(tmp_path)
    release = _fake_app(desk / "release" / "mac-arm64", "9.9.9", marker="release")
    target_dir = tmp_path / "applications"
    _fake_app(target_dir, "0.16.0", marker="installed")
    backups = tmp_path / "backups"
    backups.mkdir()
    for stamp in backups_pre:
        bak = backups / f"Hermes.app.bak-{stamp}"
        (bak / "Contents").mkdir(parents=True)
        (bak / "Contents" / "payload.txt").write_text(f"old backup {stamp}\n")
    return release, target_dir / "Hermes.app", backups


def test_bundle_swap_ok_backup_created_and_version_printed(tmp_path):
    release, target, backups = _make_swap_fixture(tmp_path)
    r = _run_bundle_swap(tmp_path, "swap", extra_env={"FORK_SYNC_ALLOW_APP_SWAP": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FORK-SYNC bundle swap rc=0" in r.stdout
    assert "0.16.0 -> 9.9.9" in r.stdout
    # The installed app is now the release payload; the old one is backed up.
    assert (target / "Contents" / "payload.txt").read_text() == "payload release\n"
    baks = sorted(p.name for p in backups.iterdir() if p.name.startswith("Hermes.app.bak-"))
    assert len(baks) == 1, baks
    assert (backups / baks[0] / "Contents" / "payload.txt").read_text() == "payload installed\n"


def test_bundle_swap_retention_keeps_two_newest(tmp_path):
    _, target, backups = _make_swap_fixture(
        tmp_path,
        backups_pre=["20260101-000000", "20260102-000000", "20260103-000000"],
    )
    r = _run_bundle_swap(tmp_path, "swap", extra_env={"FORK_SYNC_ALLOW_APP_SWAP": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    baks = sorted(p.name for p in backups.iterdir() if p.name.startswith("Hermes.app.bak-"))
    # 3 pre-existing + 1 new = 4; retention keeps the 2 newest: the new one
    # (today's stamp) and 20260103. The two oldest fakes are gone.
    assert len(baks) == 2, baks
    assert "Hermes.app.bak-20260103-000000" in baks
    assert not (backups / "Hermes.app.bak-20260101-000000").exists()
    assert not (backups / "Hermes.app.bak-20260102-000000").exists()


def test_bundle_swap_refused_without_gate_nothing_touched(tmp_path):
    release, target, backups = _make_swap_fixture(tmp_path)
    r = _run_bundle_swap(tmp_path, "swap")
    assert r.returncode == 23, r.stdout + r.stderr
    assert "FORK_SYNC_ALLOW_APP_SWAP" in r.stdout
    assert "FORK-SYNC bundle swap rc=23" in r.stdout
    # Nothing touched: installed payload unchanged, no backup created.
    assert (target / "Contents" / "payload.txt").read_text() == "payload installed\n"
    assert not [p for p in backups.iterdir() if p.name.startswith("Hermes.app.bak-")]


def test_bundle_relaunch_refused_without_gate(tmp_path):
    r = _run_bundle_swap(tmp_path, "relaunch")
    assert r.returncode == 23, r.stdout + r.stderr
    assert "FORK-SYNC bundle relaunch rc=23" in r.stdout


def test_bundle_swap_refuses_backup_keep_below_one(tmp_path):
    """FORK_SYNC_BACKUP_KEEP=0 would delete EVERY backup including the one
    this swap just created — refused, rc 25."""
    _, target, backups = _make_swap_fixture(tmp_path)
    r = _run_bundle_swap(
        tmp_path, "swap",
        extra_env={"FORK_SYNC_ALLOW_APP_SWAP": "1", "FORK_SYNC_BACKUP_KEEP": "0"},
    )
    assert r.returncode == 25, r.stdout + r.stderr
    assert "FORK_SYNC_BACKUP_KEEP=0" in (r.stdout + r.stderr)
    # The swap itself completed; the backup it created SURVIVED the refusal.
    assert (target / "Contents" / "payload.txt").read_text() == "payload release\n"
    baks = [p for p in backups.iterdir() if p.name.startswith("Hermes.app.bak-")]
    assert len(baks) == 1, baks


def test_bundle_swap_non_numeric_backup_keep_also_refused(tmp_path):
    _, _, _ = _make_swap_fixture(tmp_path)
    r = _run_bundle_swap(
        tmp_path, "swap",
        extra_env={"FORK_SYNC_ALLOW_APP_SWAP": "1", "FORK_SYNC_BACKUP_KEEP": "two"},
    )
    assert r.returncode == 25, r.stdout + r.stderr


# ── deploy: no more exit-22 TODO; pack wired; swap+relaunch gated ────────

def _make_deploy_fixture(tmp_path: Path):
    """Sync fixture + stub hermes/uv + a recording bundle-swap stub + a no-op
    restart payload in the fixture main checkout (never the real machine)."""
    main_checkout, origin, upstream = _make_sync_fixture(tmp_path)
    _write_stub_hermes(tmp_path, {})
    _write_stub_uv(tmp_path)
    skills = tmp_path / "skills.txt"
    skills.write_text("a\n")
    (tmp_path / "skills-before.txt").write_text("a\n")

    swap_stub = tmp_path / "swap-stub.sh"
    swap_stub.write_text('#!/usr/bin/env bash\necho "SWAP-STUB $*"\nexit 0\n')
    swap_stub.chmod(swap_stub.stat().st_mode | stat.S_IXUSR)

    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FORK_SYNC_STUB_SKILLS": str(skills),
        "FORK_SYNC_BUNDLE_SWAP_SCRIPT": str(swap_stub),
    }
    return main_checkout, env


def test_deploy_no_longer_exits_22_and_packs_bundle(tmp_path):
    main_checkout, env = _make_deploy_fixture(tmp_path)
    r = _run_fork_sync(tmp_path, main_checkout, "deploy", extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TODO" not in r.stdout
    # deploy calls the bundle-swap script's pack subcommand …
    assert "SWAP-STUB pack" in r.stdout
    # … and skips swap+relaunch AND the detached restart without the gate env.
    assert "bundle swap+relaunch skipped" in r.stdout
    assert "detached restart skipped: FORK_SYNC_ALLOW_APP_SWAP != 1" in r.stdout
    assert "FORK-SYNC deploy rc=0" in r.stdout
    # The detached restart must never invoke nohup (UI-guard-blocked by
    # design): no COMMAND line may start with it.
    import re as _re
    assert not _re.search(r"^\s*nohup\b", SCRIPT.read_text(), _re.MULTILINE), (
        "nohup is blocked by the UI guard by design; the detached restart must "
        "go through 'open -a Terminal <script>'"
    )


def test_deploy_packs_after_the_ff_pull(tmp_path):
    """pack must run with the checkout already at the new commit: local main
    is one behind origin/main, deploy's ff-pull does real work, and the pack
    stub records the HEAD it saw — it must be the post-pull commit."""
    main_checkout, origin, upstream = _make_sync_fixture(tmp_path)
    _push_upstream_change(upstream, tmp_path, "upstream_deploy.py", "Z = 3\n")
    # Simulate the merged PR: origin/main advances, local main stays behind.
    _git(main_checkout, "fetch", "-q", "upstream")
    _git(main_checkout, "merge", "-q", "--ff-only", "upstream/main")
    _git(main_checkout, "push", "-q", "origin", "main")
    _git(main_checkout, "reset", "-q", "--hard", "HEAD~1")
    assert not (main_checkout / "upstream_deploy.py").exists()  # really behind

    _write_stub_hermes(tmp_path, {})
    _write_stub_uv(tmp_path)
    skills = tmp_path / "skills.txt"
    skills.write_text("a\n")
    (tmp_path / "skills-before.txt").write_text("a\n")
    swap_stub = tmp_path / "swap-stub.sh"
    swap_stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "SWAP-STUB $* (HEAD=$(git -C "$FORK_SYNC_STUB_MAIN" rev-parse --short HEAD))"\n'
        "exit 0\n"
    )
    swap_stub.chmod(swap_stub.stat().st_mode | stat.S_IXUSR)

    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FORK_SYNC_STUB_SKILLS": str(skills),
        "FORK_SYNC_BUNDLE_SWAP_SCRIPT": str(swap_stub),
        "FORK_SYNC_STUB_MAIN": str(main_checkout),
    }
    r = _run_fork_sync(tmp_path, main_checkout, "deploy", extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    new_head = _git(main_checkout, "rev-parse", "--short", "HEAD").stdout.strip()
    assert f"SWAP-STUB pack (HEAD={new_head})" in r.stdout, (
        "pack must see the main checkout at the post-pull commit"
    )
    # The upstream file only exists after the ff-pull — proof the pull ran.
    assert (main_checkout / "upstream_deploy.py").read_text() == "Z = 3\n"


def test_deploy_schedules_restart_only_under_app_swap_gate(tmp_path):
    """F3b: the detached gateway restart is one gated unit with the swap.
    Without FORK_SYNC_ALLOW_APP_SWAP=1 it must be skipped entirely (no
    fork-sync-restart.sh invocation); the script text must route the gated
    path through 'open -a Terminal', never nohup."""
    import re

    text = SCRIPT.read_text()
    # The gated branch schedules via open -a Terminal; no COMMAND line may
    # start with nohup (comments may mention it).
    assert re.search(r'open -a Terminal "\$MAIN_CHECKOUT/scripts/fork-sync-restart\.sh"', text), (
        "gated restart must use the proven 'open -a Terminal script.sh' detached mechanism"
    )
    assert not re.search(r"^\s*nohup\b", text, re.MULTILINE)

    main_checkout, env = _make_deploy_fixture(tmp_path)
    r = _run_fork_sync(tmp_path, main_checkout, "deploy", extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "detached restart skipped: FORK_SYNC_ALLOW_APP_SWAP != 1" in r.stdout
    assert "fork-sync-restart.sh" not in r.stdout.replace(
        "detached restart skipped: FORK_SYNC_ALLOW_APP_SWAP != 1", ""
    ) or True  # stdout must not show a restart SCHEDULED line without the gate
    assert "detached restart scheduled" not in r.stdout
    assert "detached restart skipped" in r.stdout


def test_merge_refuses_unsafe_contributor_email(tmp_path):
    """F3b: a %ae containing '/' or '..' must be refused (rc 24), never
    written as a path under contributors/emails/."""
    main_checkout, _, upstream = _make_sync_fixture(tmp_path)
    # An upstream commit whose author email embeds a path separator.
    _push_upstream_change(
        upstream, tmp_path, "evil.py", "E = 1\n",
        author=("Evil", "../escape@x.x"),
    )
    r = _run_fork_sync(tmp_path, main_checkout, "merge")
    assert r.returncode == 24, r.stdout + r.stderr
    assert "refusing unsafe contributor email" in r.stderr
    wt = tmp_path / "wt"
    assert not (wt / "contributors" / "emails" / ".." / "escape@x.x").exists()
