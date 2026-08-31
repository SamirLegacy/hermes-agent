#!/bin/bash
# upstream-sync.sh — one-command Nous→fork merge ritual (smart-routing v4, 2026-08-02).
#
# Chain: upstream (NousResearch/hermes-agent, fetch-only) → local merge → checks →
# report. Push stays a SEPARATE, Owner-gated step (protected-branch hook: branch + PR,
# never direct-to-main). Never auto-resolves conflicts: on conflict it stops, prints
# the state, and exits 1 — a human resolves, then `git merge --continue`.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
  echo "ABORT: on '$BRANCH' — run the merge ritual on main." >&2
  exit 64
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "ABORT: dirty worktree — commit or stash first (merge ritual needs a clean tree)." >&2
  git status --short | head -20
  exit 64
fi

echo "== fetch upstream =="
git fetch upstream --quiet --tags

INCOMING=$(git rev-list --count HEAD..upstream/main)
echo "== upstream/main is $INCOMING commit(s) ahead of local main =="
if [ "$INCOMING" = "0" ]; then
  echo "Nothing to merge — local main already contains upstream/main. Done."
  exit 0
fi
git log --oneline HEAD..upstream/main | head -30
[ "$INCOMING" -gt 30 ] && echo "… (and $((INCOMING - 30)) more)"

echo "== merge =="
if ! git merge upstream/main --no-edit; then
  echo "" >&2
  echo "CONFLICTS — resolve manually, then: git merge --continue" >&2
  git status --short | grep -E '^(UU|AA|DU|UD)' || git status --short | head -20
  exit 1
fi

echo "== smoke check =="
python3 -c "import hermes_cli" && echo "hermes_cli import OK"

echo ""
echo "== merged $INCOMING commit(s). Next steps (manual, in order): =="
echo "  1. npm run build --workspace apps/desktop   # rebuild the app from the merged tree"
echo "  2. restart the desktop app, smoke: slash menu, /help, a short chat round"
echo "  3. publish via branch + PR (protected-branch hook):"
echo "       git checkout -b samir/runtime-sync-\$(date +%F)"
echo "       git push -u origin samir/runtime-sync-\$(date +%F)"
echo "       gh pr create --base main --title 'upstream sync \$(date +%F)'"
