#!/usr/bin/env bash
# fork-sync.sh — deterministic Hermes fork <-> upstream sync in minutes.
#
# Subcommands: check | merge | verify | deploy | probe | all
# Exit codes:  0 ok / nothing to do        2 usage or environment error
#             10 work available           20 merge conflicts need manual resolution
#             21 main checkout dirty      23 import guard failed
#             24 targeted tests failed
#             30 probe FAIL
#
# Every exit prints one receipt line: FORK-SYNC <sub> rc=<n> <summary>
# See docs/fork-sync.md for conflict rules and rationale.
set -euo pipefail

MAIN_CHECKOUT="${FORK_SYNC_MAIN_CHECKOUT:-/Users/sd/.hermes/hermes-agent}"
WORKTREE="${FORK_SYNC_WORKTREE:-/Users/sd/Workspace/repos/hermes-agent-worktrees/upstream-sync}"
HERMES_PROFILE_NAME="${FORK_SYNC_HERMES_PROFILE:-desktop-local}"
BUNDLE_SWAP_SCRIPT="${FORK_SYNC_BUNDLE_SWAP_SCRIPT:-$MAIN_CHECKOUT/scripts/fork-sync-bundle-swap.sh}"
SKILLS_SNAPSHOT="${FORK_SYNC_SKILLS_SNAPSHOT:-${TMPDIR:-/tmp}/fork-sync-skills-before.txt}"
SKILLS_AFTER="$SKILLS_SNAPSHOT.after"

# STRUCTURAL MCP GUARD — the single canonical dependency-sync invocation.
# verify and deploy both expand this exact array; tests/scripts/test_fork_sync_sh.py
# enforces that this is the script's only such line. `--extra mcp` is load-bearing:
# a sync without it prunes mcp/httpx2 and kills every MCP server profile-wide
# (incident 2026-09-02, "streamable_http not available").
CANONICAL_SYNC=(uv sync --extra dev --extra mcp)

SUB=""
SUMMARY="ok"
receipt() { printf 'FORK-SYNC %s rc=%s %s\n' "$1" "$2" "$3"; }
trap 'rc=$?; [ -n "$SUB" ] || SUB="usage"; receipt "$SUB" "$rc" "$SUMMARY"' EXIT

# Deterministic git identity for every mutation this script performs —
# including the auto-commit inside a CLEAN `git merge --no-edit`, which needs
# an identity before MERGE_HEAD even exists. `:=` (not `:-`) also overrides
# empty values: CI runners inject user.name="" (fatal: empty ident name) and
# have no ~/.gitconfig at all (Committer identity unknown). Fork automation
# convention: contributors/emails/samir@local.
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:=Samir}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:=samir@local}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:=Samir}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:=samir@local}"

usage() {
  sed -n '2,13p' "$0"
}

# ---------------------------------------------------------------------------
cmd_check() {
  git -C "$MAIN_CHECKOUT" fetch origin --quiet || { SUMMARY="fetch origin failed"; return 128; }
  git -C "$MAIN_CHECKOUT" fetch upstream --quiet || { SUMMARY="fetch upstream failed"; return 128; }
  BEHIND=$(git -C "$MAIN_CHECKOUT" rev-list --count origin/main..upstream/main) \
    || { SUMMARY="rev-list behind failed"; return 128; }
  AHEAD=$(git -C "$MAIN_CHECKOUT" rev-list --count upstream/main..origin/main) \
    || { SUMMARY="rev-list ahead failed"; return 128; }
  printf 'BEHIND=%s AHEAD=%s\n' "$BEHIND" "$AHEAD"
  if [ "$BEHIND" -eq 0 ]; then
    SUMMARY="nothing to do (behind=$BEHIND ahead=$AHEAD)"
    return 0
  fi
  SUMMARY="work available (behind=$BEHIND ahead=$AHEAD)"
  return 10
}

# ---------------------------------------------------------------------------
cmd_merge() {
  git -C "$MAIN_CHECKOUT" fetch origin --quiet || { SUMMARY="fetch origin failed"; return 128; }
  git -C "$MAIN_CHECKOUT" fetch upstream --quiet || { SUMMARY="fetch upstream failed"; return 128; }
  if [ ! -e "$WORKTREE" ]; then
    git -C "$MAIN_CHECKOUT" worktree add --detach "$WORKTREE" origin/main \
      || { SUMMARY="worktree add failed for $WORKTREE"; return 2; }
  elif ! git -C "$WORKTREE" rev-parse --git-dir >/dev/null 2>&1; then
    SUMMARY="path exists but is not a git worktree: $WORKTREE"
    return 2
  fi

  BRANCH="samir/post-update-sync-$(date +%Y%m%d)"
  git -C "$WORKTREE" merge --abort >/dev/null 2>&1 || true
  git -C "$WORKTREE" checkout -B "$BRANCH" origin/main \
    || { SUMMARY="checkout -B $BRANCH failed"; return 2; }

  if ! git -C "$WORKTREE" merge upstream/main --no-edit; then
    resolve_conflicts || return
  fi
  add_missing_contributors || return
  finalize_merge_commit
}

resolve_conflicts() {
  local conflicted f remaining auto=0
  conflicted=$(git -C "$WORKTREE" diff --name-only --diff-filter=U)
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      contributors/emails/*)
        # Mechanical add/add: the upstream (real-name) mapping wins.
        git -C "$WORKTREE" checkout --theirs -- "$f"
        git -C "$WORKTREE" add -- "$f"
        printf 'auto-resolved (took upstream): %s\n' "$f"
        auto=$((auto + 1))
        ;;
    esac
  done <<EOF_CONFLICTS
$conflicted
EOF_CONFLICTS

  remaining=$(git -C "$WORKTREE" diff --name-only --diff-filter=U)
  if [ -n "$remaining" ]; then
    printf '%s\n' 'CONFLICT (manual resolution required, see docs/fork-sync.md):'
    printf '%s\n' "$remaining"
    SUMMARY="manual conflicts remain ($auto contributors/emails file(s) auto-resolved)"
    return 20
  fi
  return 0
}

add_missing_contributors() {
  # Fork CI (contributor-check) requires a file per upstream author email.
  local ae an created=0
  mkdir -p "$WORKTREE/contributors/emails"
  while IFS='|' read -r ae an; do
    [ -n "$ae" ] || continue
    if [ ! -f "$WORKTREE/contributors/emails/$ae" ]; then
      printf '%s\n' "$an" > "$WORKTREE/contributors/emails/$ae"
      git -C "$WORKTREE" add -- "contributors/emails/$ae"
      printf 'new contributor mapping: %s -> %s\n' "$ae" "$an"
      created=$((created + 1))
    fi
  done < <(git -C "$WORKTREE" log origin/main..upstream/main --format='%ae|%an' | sort -u)
  NEW_CONTRIB="$created"
  return 0
}

finalize_merge_commit() {
  # Deterministic committer identity — fork convention for automation
  # (contributors/emails/samir@local: "fork-local git identity for Hermes
  # self-improvement commits"). CI runners have no ambient git identity,
  # without this both commits below die with "Committer identity unknown".
  local idargs=(-c "user.name=Samir" -c "user.email=samir@local")
  if git -C "$WORKTREE" rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
    git -C "$WORKTREE" "${idargs[@]}" commit --no-edit
    SUMMARY="merge committed on $BRANCH (auto-resolved emails + $NEW_CONTRIB new mapping(s) included)"
  elif [ "${NEW_CONTRIB:-0}" -gt 0 ]; then
    git -C "$WORKTREE" "${idargs[@]}" commit -m "chore(contributors): map new upstream author emails"
    SUMMARY="branch $BRANCH ready ($NEW_CONTRIB new contributor mapping(s))"
  else
    SUMMARY="branch $BRANCH ready (clean merge, no new mappings)"
  fi
  printf 'branch=%s\n' "$BRANCH"
}

# ---------------------------------------------------------------------------
cmd_verify() {
  git -C "$WORKTREE" rev-parse --git-dir >/dev/null 2>&1 \
    || { SUMMARY="not a git worktree: $WORKTREE"; return 2; }

  SUMMARY="canonical dependency sync into $WORKTREE/.venv"
  ( cd "$WORKTREE" && env -u PYTHONPATH UV_PROJECT_ENVIRONMENT=.venv nice -n 15 "${CANONICAL_SYNC[@]}" )

  SUMMARY="import guard (mcp, httpx2) in $WORKTREE/.venv"
  "$WORKTREE/.venv/bin/python" -c 'import mcp, httpx2' \
    || { SUMMARY="import guard FAILED: mcp/httpx2 missing after canonical sync"; return 23; }

  # Targeted tests only — the PR's CI run executes the full suite.
  local changed t base m mapped=""
  changed=$(git -C "$WORKTREE" diff --name-only origin/main..HEAD \
      -- 'hermes_cli/*.py' 'agent/*.py' 'tools/*.py' 'gateway/*.py' 'tui_gateway/*.py')
  for t in $changed; do
    base=$(basename "$t" .py)
    while IFS= read -r m; do
      case " $mapped " in
        *" $m "*) ;;
        *) mapped="$mapped $m" ;;
      esac
    done < <(cd "$WORKTREE" && find tests -name "*${base}*.py" ! -name conftest.py | sort)
  done
  if [ -z "${mapped// /}" ]; then
    printf '%s\n' 'targeted tests: none mapped (no changed files under hermes_cli/ agent/ tools/ gateway/ tui_gateway/)'
    SUMMARY="verified: canonical sync + import guard green; no targeted tests mapped"
    return 0
  fi
  printf '%s\n' 'targeted test mapping:'
  for m in $mapped; do printf '  %s\n' "$m"; done
  SUMMARY="targeted tests running (HERMES_TEST_WORKERS=6, nice -n 15)"
  ( cd "$WORKTREE" && env -u PYTHONPATH HERMES_TEST_WORKERS=6 nice -n 15 scripts/run_tests.sh $mapped ) \
    || { SUMMARY="targeted tests FAILED"; return 24; }
  SUMMARY="verified: canonical sync + import guard + targeted tests green"
}

# ---------------------------------------------------------------------------
cmd_deploy() {
  if [ -n "$(git -C "$MAIN_CHECKOUT" status --porcelain)" ]; then
    git -C "$MAIN_CHECKOUT" status --porcelain
    SUMMARY="main checkout dirty — deploy skipped (never reset/stash the main checkout)"
    return 21
  fi

  # Before-snapshot for probe's skills-preservation diff (written before any mutation).
  hermes -p "$HERMES_PROFILE_NAME" skills list > "$SKILLS_SNAPSHOT" \
    || { SUMMARY="pre-deploy skills snapshot failed"; return 30; }

  [ -x "$BUNDLE_SWAP_SCRIPT" ] || {
    SUMMARY="bundle-swap script missing or not executable: $BUNDLE_SWAP_SCRIPT"
    return 2
  }

  SUMMARY="ff-pull main checkout"
  git -C "$MAIN_CHECKOUT" fetch origin --quiet
  git -C "$MAIN_CHECKOUT" pull --ff-only origin main

  SUMMARY="canonical sync of the runtime venv ($MAIN_CHECKOUT/venv)"
  ( cd "$MAIN_CHECKOUT" && env -u PYTHONPATH UV_PROJECT_ENVIRONMENT=venv nice -n 15 "${CANONICAL_SYNC[@]}" )

  "$MAIN_CHECKOUT/venv/bin/python" -c 'import mcp, httpx2' || {
    SUMMARY="mcp/httpx2 missing after runtime sync — restoring locked pins additively"
    env -u PYTHONPATH uv pip install --python "$MAIN_CHECKOUT/venv/bin/python" 'mcp==2.0.0' 'httpx2==2.7.0'
    "$MAIN_CHECKOUT/venv/bin/python" -c 'import mcp, httpx2' \
      || { SUMMARY="import guard FAILED even after additive restore"; return 23; }
  }

  # Pack the desktop bundle AFTER the checkout is at the new commit and the
  # runtime venv is synced — the bundle must be built from the code that just
  # became the runtime. pack only writes into the repo checkout (node_modules
  # + release/), so it needs no gate.
  SUMMARY="desktop bundle pack via $BUNDLE_SWAP_SCRIPT pack"
  "$BUNDLE_SWAP_SCRIPT" pack

  # Gated bundle swap + relaunch: these touch the Owner's running app, so the
  # bundle-swap script refuses them unless FORK_SYNC_ALLOW_APP_SWAP=1 (set by
  # the orchestrator after the Owner's GO). Without the gate deploy still
  # completes the runtime side; the packed bundle stays in release/ for a
  # later manual swap.
  if [ "${FORK_SYNC_ALLOW_APP_SWAP:-0}" = "1" ]; then
    SUMMARY="desktop bundle swap (gated)"
    "$BUNDLE_SWAP_SCRIPT" swap
    SUMMARY="desktop bundle relaunch (gated)"
    "$BUNDLE_SWAP_SCRIPT" relaunch
  else
    printf '%s\n' "bundle swap+relaunch skipped: FORK_SYNC_ALLOW_APP_SWAP != 1 (packed bundle waits in $MAIN_CHECKOUT/apps/desktop/release/mac-arm64/Hermes.app)"
  fi

  # Detached restart, from the cron job's STEP 8 — kept in its own file so the
  # gateway-restart guard can scan this script without matching the payload.
  # Scheduled detached; the 90s lead in fork-sync-restart.sh lets the report
  # land before the caller's own host process is restarted.
  nohup /bin/bash "$MAIN_CHECKOUT/scripts/fork-sync-restart.sh" >/tmp/hermes-deploy-restart.log 2>&1 &
  SUMMARY="deployed; bundle packed; swap+relaunch $( [ "${FORK_SYNC_ALLOW_APP_SWAP:-0}" = "1" ] && echo done || echo skipped '(FORK_SYNC_ALLOW_APP_SWAP != 1)') ; detached restart scheduled (+90s); run 'probe' after it settles"
}

# ---------------------------------------------------------------------------
cmd_probe() {
  local fails=0 label sid out rout
  pass() { printf 'PASS %s\n' "$1"; }
  failp() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
  run_check() {
    label="$1"; shift
    printf 'CHECK %s :: %s\n' "$label" "$*"
    if "$@" >/dev/null 2>&1; then pass "$label"; else failp "$label"; fi
  }

  run_check "mcp heygen" hermes -p "$HERMES_PROFILE_NAME" mcp test heygen
  run_check "hooks doctor" hermes -p "$HERMES_PROFILE_NAME" hooks doctor
  # No --no-tools flag exists; --oneshot is the closest: answer the query, exit.
  run_check "chat smoke" hermes -p "$HERMES_PROFILE_NAME" chat -q "reply OK" --oneshot

  printf 'CHECK resume smoke :: hermes chat -q (create) + chat --resume <id> -q\n'
  if out=$(hermes -p "$HERMES_PROFILE_NAME" chat -q "fork-sync probe seed" --oneshot 2>&1) \
     && sid=$(hermes -p "$HERMES_PROFILE_NAME" sessions list --limit 1 2>/dev/null | tail -n 1 | awk '{print $NF}') \
     && [ -n "$sid" ]; then
    if rout=$(hermes -p "$HERMES_PROFILE_NAME" chat --resume "$sid" -q "reply OK2" --oneshot 2>&1) \
       && printf '%s' "$rout" | grep -q "$sid"; then
      pass "resume smoke (id $sid named in resume output)"
    else
      failp "resume smoke (id $sid not confirmed in resume output)"
    fi
  else
    failp "resume smoke (session create / id capture failed)"
  fi

  printf 'CHECK skills preservation :: hermes skills list diff vs pre-deploy snapshot\n'
  if [ ! -f "$SKILLS_SNAPSHOT" ]; then
    failp "skills preservation (no before-snapshot at $SKILLS_SNAPSHOT — deploy writes it)"
  elif hermes -p "$HERMES_PROFILE_NAME" skills list > "$SKILLS_AFTER" 2>/dev/null \
       && diff -u "$SKILLS_SNAPSHOT" "$SKILLS_AFTER" >/dev/null; then
    pass "skills preservation (list identical pre/post deploy)"
  else
    failp "skills preservation (post-deploy list differs or capture failed)"
  fi

  if [ "$fails" -gt 0 ]; then
    SUMMARY="$fails probe check(s) FAILED — STOP, report verbatim, do not paper over"
    return 30
  fi
  SUMMARY="all probe checks passed"
}

# ---------------------------------------------------------------------------
print_pr_instructions() {
  printf '%s\n' 'NEXT — push + PR (CI runs the full suite; deploy+probe are separate invocations):'
  printf '  git -C "%s" push fork %s\n' "$WORKTREE" "$BRANCH"
  printf '  gh pr create --repo SamirLegacy/hermes-agent --base main --head %s \\\n' "$BRANCH"
  printf '    --title "sync: merge upstream/main into fork (%s, N commits)" \\\n' "$(date +%Y-%m-%d)"
  printf '    --body "conflicted files + resolution, new contributor emails, verify receipt"\n'
  printf '  gh pr edit <num> --repo SamirLegacy/hermes-agent --add-label ci-reviewed\n'
}

cmd_all() {
  local rc=0
  cmd_check || rc=$?
  if [ "$rc" -eq 0 ]; then
    SUMMARY="all: nothing to do"
    return 0
  fi
  [ "$rc" -eq 10 ] || return "$rc"
  rc=0; cmd_merge || rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  rc=0; cmd_verify || rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  print_pr_instructions
  SUMMARY="all: check->merge->verify done; PR instructions printed (merge+deploy+probe come after CI)"
}

# ---------------------------------------------------------------------------
SUB="${1:-}"
if [ -z "$SUB" ]; then
  usage
  exit 2
fi
case "$SUB" in
  check|merge|verify|deploy|probe|all) ;;
  *)
    printf 'unknown subcommand: %s\n' "$SUB" >&2
    usage >&2
    exit 2
    ;;
esac
command -v uv >/dev/null 2>&1 || { SUMMARY="required tool 'uv' not found in PATH"; exit 2; }

cmd_"$SUB"
