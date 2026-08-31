# Update Flow — Fork ↔ Nous (Owner reference)

## Topology

| Remote | Target | Mode |
|---|---|---|
| `upstream` | `github.com/NousResearch/hermes-agent` | fetch-only (push blocked: `no_push://blocked`) |
| `origin` | `github.com/SamirLegacy/hermes-agent` (public fork) | read/write via branch + PR |
| `fork` | same as origin (alias) | — |

## How updates actually flow

1. **Nous ships** → `git fetch upstream`.
2. **Merge ritual (one command):** `bin/upstream-sync.sh` — fetches, reports incoming
   count, merges `upstream/main` into local `main`, smoke-checks the import. On
   conflict it stops and prints state; resolve manually, `git merge --continue`.
3. **Rebuild:** `npm run build --workspace apps/desktop`, restart the app
   (stamp check: `apps/desktop/build/install-stamp.json`).
4. **Publish to the fork (Owner-gated):** protected-branch hook blocks direct
   pushes to `main` — use a branch + PR:
   `git checkout -b samir/runtime-sync-$(date +%F) && git push -u origin <branch> && gh pr create --base main`.

## What the in-app update indicator watches

`checkUpdates()` (electron/main.ts) fetches **origin** (this fork) and counts
commits behind `origin/<branch>`; it never diffs against Nous directly. On the
official-SSH-remote path it would compare against Nous HEAD — deliberately NOT
used here, because pulling Nous over 5000+ fork commits would clobber the fork.
Consequence: Nous updates reach the indicator only AFTER they are merged locally
and published to origin via step 4.

## Rules

- samirs-os is a different repo — never merge/push it from here.
- `upstream` never receives pushes (by design, enforced by remote config).
- Local `main` may run ahead of `origin/main` (normal); publish periodically via
  branch + PR so the GitHub copy is a real backup and other machines see updates.
