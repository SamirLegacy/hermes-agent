# Fork sync (SamirLegacy/hermes-agent ← NousResearch/hermes-agent)

`scripts/fork-sync.sh` runs the daily upstream sync deterministically. The LLM
cron job only calls the script and resolves real merge conflicts by hand.

## Subcommands

- `check` — fetch origin+upstream, print `BEHIND`/`AHEAD`. rc 0 = nothing to
  do, rc 10 = work available.
- `merge` — in the sync worktree (env `FORK_SYNC_WORKTREE`, default
  `/Users/sd/Workspace/repos/hermes-agent-worktrees/upstream-sync`):
  `checkout -B samir/post-update-sync-<date> origin/main`, then
  `merge upstream/main --no-edit`. On conflict it prints
  `git diff --name-only --diff-filter=U` and exits 20, after auto-resolving
  the mechanical `contributors/emails/*` add/add conflicts (takes upstream)
  and creating missing `contributors/emails/<email>` files from
  `git log origin/main..upstream/main --format='%ae|%an'`.
- `verify` — canonical dependency sync into the worktree `.venv`, then a hard
  import guard (`import mcp, httpx2`), then targeted tests only: every changed
  `.py` file under `hermes_cli/`, `agent/`, `tools/`, `gateway/`,
  `tui_gateway/` is mapped to `tests/` by name (the mapping is printed) and
  run with `HERMES_TEST_WORKERS=6 nice -n 15` via `scripts/run_tests.sh`.
- `deploy` — bundle swap, then the single allowed write in the main checkout
  (`git pull --ff-only`), then the same canonical sync against the runtime
  venv, then the detached restart (`scripts/fork-sync-restart.sh`, verbatim
  from the old cron job's STEP 8). Owner-gated.
- `probe` — mandatory post-deploy checks, each printed `PASS`/`FAIL` with the
  command: `mcp test heygen`, `hooks doctor`, a one-shot chat smoke
  (`chat -q "reply OK" --oneshot`; no `--no-tools` flag exists — `--oneshot`
  is the closest), a resume smoke (create a session, `chat --resume <id>`,
  assert the same id appears in the resume output), and a skills-preservation
  diff (deploy snapshots `hermes skills list` before any mutation; probe
  compares). Any FAIL → rc 30 and the cron job stops and reports.
- `all` — check → merge → verify, then prints the push + PR commands and
  stops. Merging the PR and deploy stay separate invocations because the PR
  needs CI.

Every subcommand is idempotent and prints a one-line receipt
`FORK-SYNC <sub> rc=<n> <summary>`.

## The canonical sync line (structural MCP guard)

Exactly one place defines it: `CANONICAL_SYNC=(uv sync --extra dev --extra mcp)`
at the top of the script. `verify` and `deploy` both expand it; no other
`uv sync` exists in the script (enforced by
`tests/scripts/test_fork_sync_sh.py::test_single_canonical_sync_line`), and the
script refuses to run without `uv` on PATH. The 2026-09-02 incident: a plain
`uv sync` pruned `mcp`/`httpx2` (both are optional extras, never in the
default sync set) and killed every MCP server profile-wide. `--extra mcp`
pins `mcp==2.0.0`, `httpx2==2.7.0`, `starlette==1.3.1`; `--extra dev` is needed
in the worktree for pytest and already carries the same pins, so the guard
holds in both venvs. `deploy` additionally falls back to the additive
`uv pip install 'mcp==2.0.0' 'httpx2==2.7.0'` restore if the import guard
still fails in the runtime venv.

## Conflict rules

Upstream structure + fork intent, union. Read both sides before editing.
Recurring fork surfaces:

1. **apps/desktop slash-alias** (`desktop-slash-commands.ts`,
   `use-slash-completions.ts` + tests): keep the fork's alias decoration /
   `includeAliases` opt-in layered on top of upstream refactors (adopt
   upstream helpers like `isAliasCommand`/`slashCompletionGroup`).
2. **`plugins/platforms/telegram/adapter.py` `_register_ingest_handlers`**:
   keep the fork's `self._register_ingest_handlers()` call AND any upstream
   handler wiring — union, fork ingest first, then upstream additions, then
   core handlers.
3. **`contributors/emails/*` add/add**: take upstream (real-name mapping).
   `merge` auto-resolves exactly this case.
4. **`.github/workflows` runs-on labels** (tests.yml, js-tests.yml,
   tests-os.yml, nix.yml, rust-tests.yml, e2e-desktop.yml, docker.yml) and
   tests.yml `HERMES_TEST_WORKERS` (fork: 4): KEEP the fork's standard
   runners — GitHub-hosted larger runners (`*-96-core`, `*-32-core`) are not
   schedulable on this personal account and queue forever (PR #32). Adopt
   upstream workflow-structure changes around them, never the labels.

After resolving: zero `<<<<<<<` markers must remain. A same-logic
incompatible rewrite is the only stop case — report files + hunks, Samir
decides.

## Why no full suite locally

The full pytest suite at 6 workers takes ~25+ minutes and heats the machine;
the PR's CI runs it on every push. Locally `verify` runs only the tests
mapped to files the merge actually touched. The old job's "full suite up to
twice" is gone; CI is the full-suite gate.

## Does the slow path preserve skills / slash commands?

Yes — verified, they are unreachable by a dependency sync or bundle swap:

- Profile skills live under `~/.hermes/profiles/<profile>/skills/` (observed:
  `~/.hermes/profiles/desktop-local/skills/` holds the local skills shown as
  `Source: local` by `hermes -p desktop-local skills list`).
- Repo skills live under `<repo>/skills/`; slash commands come from
  `agent/skill_commands` scanning those directories.
- `uv sync` only ever writes the project venv (`.venv` / `venv`); the bundle
  swap only touches `/Applications/Hermes.app`. Neither path overlaps a skills
  directory.
- Evidence: `hermes -p desktop-local skills list` captured 153 skill rows
  before and after this lane's real `verify` run (canonical sync + import
  guard + test mapping in the lane worktree) — identical output, diff empty.
  `probe` enforces this going forward: `deploy` snapshots the skills list
  before mutating anything and `probe` hard-fails on any diff.

## Deploy prerequisite (deliberately gated)

`deploy` requires an executable `scripts/fork-sync-bundle-swap.sh` in the main
checkout. It does not exist: the old cron job's STEP 8 contains no
npm-pack/bundle-swap procedure (it only ff-pulls, syncs the runtime venv,
guards MCP, and restarts), and this lane refuses to invent one. `deploy`
exits 22 with a TODO until that script is provided; the ff-pull, canonical
runtime-venv sync, import guard, and detached restart are already wired
behind the barrier.
