---
name: peekaboo
description: |
  Use the Peekaboo CLI for local macOS screenshots, UI inspection, and UI
  automation with explicit owner gates for screen capture, AI analysis,
  provider configuration, MCP exposure, and UI mutation.
version: 1.0.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [macos, desktop, automation, screenshots, owner-gate]
    category: desktop
    related_skills: [macos-computer-use]
---

# Peekaboo macOS Automation

Peekaboo is a macOS CLI for screenshots, UI element maps, and UI automation.
Use it directly only on a Mac host where `/opt/homebrew/bin/peekaboo` or
`peekaboo` is installed. On Linux/VPS hosts, this skill is relay-only: do
not try to install or run the macOS binary locally; call a Mac-side bridge or
SSH command that executes on the Mac.

## Safety contract

Before using Peekaboo, classify the next command:

- Local status/help only: allowed without owner gate.
- Screen capture or UI/app inspection: owner gate required.
- UI mutation such as click, type, paste, hotkey, window/app/menu changes:
  owner gate required.
- AI analysis, `peekaboo agent`, or any `--analyze` use: owner gate required
  and the user must approve the exact image/path or screen target that will
  be sent to the configured provider.
- Provider credentials/configuration and `peekaboo mcp`: owner gate required.
- Sensitive values: never type, paste, read aloud, or configure login values,
  provider credentials, OAuth credentials, 2FA codes, payment data, or private
  key material.

Hermes' terminal approval guard should ask for these gated commands. If a
critical Peekaboo action runs without an approval prompt, stop and report the
guard gap before continuing.

## macOS permission ownership

macOS grants Screen Recording and Accessibility to the responsible app, not
to the `peekaboo` binary alone. Treat these as separate permission targets:

- Terminal / iTerm: needed when Hermes CLI or Peekaboo is launched from that terminal.
- Hermes Desktop app: needed when Hermes Desktop launches Peekaboo directly.
- Codex Desktop app: needed only when this Codex thread runs Peekaboo directly.

If `peekaboo permissions` is green in Terminal but red in Codex or Hermes, do
not reinterpret that as a Peekaboo install failure. Grant the missing host app,
quit/reopen that app, then rerun `peekaboo permissions` from the same surface.

## Data locations

Default local locations:

- Binary: `/opt/homebrew/bin/peekaboo`
- Homebrew install: `/opt/homebrew/Cellar/peekaboo/<version>/`
- Peekaboo config: `~/.peekaboo/config.json`
- Peekaboo credentials: `~/.peekaboo/credentials/`
- Snapshot cache: `~/.peekaboo/snapshots/`
- Explicit screenshots: whatever `--path` names, commonly `/tmp/*.png`

Without `--analyze`, `peekaboo agent`, or provider config, the data flow is:

```text
macOS screen -> Peekaboo local runtime -> stdout / local file / local snapshot cache
```

With `--analyze` or `peekaboo agent`, captured content may be sent to the
configured AI provider. Do not use those modes unless the user approved that
exact action.

## Safe baseline checks

These are read-only or local housekeeping:

```bash
peekaboo --version
peekaboo permissions
peekaboo tools --json
peekaboo list permissions
peekaboo clean --older-than 1 --dry-run
```

Use this to verify install and macOS permissions:

```bash
peekaboo permissions
peekaboo list apps --json
```

`peekaboo list apps --json` requires Screen Recording and is owner-gated
because it exposes running app/window state.

## Local capture workflow

Ask for approval before capture. Prefer an explicit output path:

```bash
peekaboo see --mode screen --screen-index 0 --annotate --path /tmp/peekaboo-see.png --json
```

Then verify the file:

```bash
ls -lh /tmp/peekaboo-see.png
```

For screenshots without element maps:

```bash
peekaboo image --mode screen --path /tmp/peekaboo-screen.png --json
```

After use, clean generated artifacts when they are no longer needed:

```bash
peekaboo clean --all-snapshots --dry-run
peekaboo clean --all-snapshots
rm /tmp/peekaboo-see.png
```

Run the dry-run first. Only run the non-dry-run cleanup after owner approval,
and only remove files you created in this workflow.

## UI automation workflow

1. Capture with `see` and save the annotated screenshot.
2. Report what target you intend to interact with.
3. Wait for owner gate if the next action mutates UI state.
4. Use stable element IDs from the latest snapshot:

```bash
peekaboo click --on B1
peekaboo type "text" --app TextEdit
peekaboo hotkey cmd+s --app TextEdit
```

Never type sensitive values. If a login prompt, payment, 2FA, keychain,
permission dialog, or sensitive admin panel appears, stop and hand control back
to the user.

## AI analysis workflow

AI analysis is off by default. Use it only after explicit owner approval in
this shape:

```text
The image /tmp/peekaboo-see.png will be analyzed by the configured provider.
Approve this specific analysis?
```

Allowed only after approval:

```bash
peekaboo see --path /tmp/peekaboo-see.png --analyze "Question..."
peekaboo image --path /tmp/peekaboo-screen.png --analyze "Question..."
peekaboo agent "Task..."
```

Do not set `PEEKABOO_AI_PROVIDERS`, provider API keys, or run
`peekaboo config add/login/set-credential` without a separate owner gate.

## Failure modes

- `PERMISSION_ERROR_SCREEN_RECORDING` or TCC capture denial: grant Screen
  Recording to the responsible host app (Terminal/iTerm, Hermes Desktop,
  Codex Desktop, or the bridge host), quit/reopen that app, then retry
  `peekaboo permissions` from the same surface.
- `Accessibility: Not Granted`: grant Accessibility to the responsible host
  app (Terminal/iTerm, Hermes Desktop, Codex Desktop, or the bridge host)
  before clicking, typing, window, menu, or app control.
- Swift continuation warning before permission output: treat the final
  permission status as authoritative, then retest after permissions change.
- `see` on the default/frontmost target can time out or return an internal
  bridge error. Prefer `peekaboo see --mode screen --screen-index 0 ...` for
  the baseline health check, then narrow to a specific app/window only after
  the screen-mode check passes.
- VPS host: Peekaboo is macOS-only. A Linux VPS cannot run local Mac captures
  unless it calls a Mac-side bridge or SSH command that executes on the Mac.
