#!/usr/bin/env bash
# fork-sync-bundle-swap.sh — pack / swap / relaunch the desktop app bundle.
# Called by fork-sync.sh deploy; usable standalone for a manual UI-only swap.
#
# Subcommands: pack | swap | relaunch | all
# Exit codes:  0 ok                       2 usage or environment error
#             23 swap/relaunch refused: FORK_SYNC_ALLOW_APP_SWAP != 1
#             24 pack failed (npm ci / npm run pack / version assertion)
#             25 swap or relaunch failed
#
# swap and relaunch touch the Owner's running app and refuse to run unless
# FORK_SYNC_ALLOW_APP_SWAP=1 — the orchestrator sets that only after the
# Owner's GO. pack needs no gate (it only writes into the repo checkout).
#
# Every exit prints one receipt line: FORK-SYNC bundle <sub> rc=<n> <summary>
set -euo pipefail

REPO_ROOT="${FORK_SYNC_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
APP_TARGET="${FORK_SYNC_APP_TARGET:-/Applications/Hermes.app}"
BACKUP_DIR="${FORK_SYNC_BACKUP_DIR:-/Users/sd/Workspace/backups/hermes-app}"
BACKUP_KEEP="${FORK_SYNC_BACKUP_KEEP:-2}"
RELEASE_APP="$REPO_ROOT/apps/desktop/release/mac-arm64/Hermes.app"

SUB=""
SUMMARY="ok"
receipt() { printf 'FORK-SYNC bundle %s rc=%s %s\n' "$1" "$2" "$3"; }
trap 'rc=$?; [ -n "$SUB" ] || SUB="usage"; receipt "$SUB" "$rc" "$SUMMARY"' EXIT

usage() {
  sed -n '2,17p' "$0"
}

# ditto (mac, resource forks) > rsync -a > cp -R — same tree-copy semantics.
copy_tree() {
  if command -v ditto >/dev/null 2>&1; then ditto "$1" "$2"
  elif command -v rsync >/dev/null 2>&1; then rsync -a "$1" "$2"
  else cp -R "$1" "$2"; fi
}

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else cksum "$1" | awk '{print $1}'; fi
}

read_plist_version() { # $1 = .app dir; prints CFBundleShortVersionString
  local plist="$1/Contents/Info.plist"
  [ -f "$plist" ] || return 1
  if command -v PlistBuddy >/dev/null 2>&1; then
    PlistBuddy -c 'Print :CFBundleShortVersionString' "$plist" 2>/dev/null && return 0
    return 1
  fi
  # XML fallback where PlistBuddy is absent (Linux CI): value follows the key.
  sed -n '/CFBundleShortVersionString/{n;s/.*<string>\([^<]*\)<\/string>.*/\1/p;q;}' "$plist"
}

read_pkg_version() { # first "version" key = the top-level package version
  sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$1" | head -n 1
}

gate_check() {
  if [ "${FORK_SYNC_ALLOW_APP_SWAP:-0}" != "1" ]; then
    SUMMARY="refused: FORK_SYNC_ALLOW_APP_SWAP != 1 (touches the running app; orchestrator sets it after the Owner's GO) — nothing touched"
    return 1
  fi
}

# ---------------------------------------------------------------------------
cmd_pack() {
  local desk="$REPO_ROOT/apps/desktop"
  [ -d "$desk" ] || { SUMMARY="apps/desktop not found under $REPO_ROOT"; return 2; }
  command -v npm >/dev/null 2>&1 || { SUMMARY="required tool 'npm' not found in PATH"; return 2; }
  [ -f "$desk/package-lock.json" ] || { SUMMARY="apps/desktop/package-lock.json missing"; return 2; }

  # Conditional npm ci: only when package-lock.json changed since the last
  # successful ci (state hash kept inside node_modules) or node_modules is
  # absent — a merge that did not touch the lock must not pay a full reinstall.
  local lock_hash state="$desk/node_modules/.fork-sync-lock-hash" ci_needed=0
  lock_hash=$(hash_file "$desk/package-lock.json")
  if [ ! -d "$desk/node_modules" ] || [ ! -f "$state" ] \
     || [ "$(cat "$state" 2>/dev/null)" != "$lock_hash" ]; then
    ci_needed=1
  fi
  if [ "$ci_needed" -eq 1 ]; then
    printf '%s\n' 'npm ci (package-lock changed since last pack or no prior state)'
    ( cd "$desk" && nice -n 15 npm ci ) || { SUMMARY="npm ci failed"; return 24; }
    # npm ci owns node_modules, but our state file must not depend on it
    # having created the dir (stubbed npm in tests, partially failed ci).
    mkdir -p "$desk/node_modules"
    printf '%s\n' "$lock_hash" > "$state"
  else
    printf '%s\n' 'npm ci skipped (package-lock unchanged since last pack)'
  fi

  printf '%s\n' 'npm run pack (build + electron-builder --dir)'
  ( cd "$desk" && nice -n 15 npm run pack ) || { SUMMARY="npm run pack failed"; return 24; }

  [ -d "$RELEASE_APP" ] || { SUMMARY="release app missing after pack: $RELEASE_APP"; return 24; }
  local app_ver pkg_ver
  app_ver=$(read_plist_version "$RELEASE_APP") \
    || { SUMMARY="could not read CFBundleShortVersionString from packed app"; return 24; }
  pkg_ver=$(read_pkg_version "$desk/package.json")
  [ -n "$pkg_ver" ] || { SUMMARY="could not read version from apps/desktop/package.json"; return 24; }
  [ "$app_ver" = "$pkg_ver" ] \
    || { SUMMARY="version mismatch: packed app $app_ver != package.json $pkg_ver"; return 24; }

  SUMMARY="packed Hermes.app $app_ver (npm ci $( [ "$ci_needed" -eq 1 ] && echo ran || echo skipped ))"
}

# ---------------------------------------------------------------------------
enforce_backup_retention() {
  # Owner rule: at most BACKUP_KEEP (default 2) newest backups remain; older
  # ones are deleted in the same swap run. Timestamps sort chronologically.
  # BACKUP_KEEP < 1 is refused: keep=0 would delete EVERY backup including
  # the one this swap just created, leaving no rollback path.
  if ! [ "$BACKUP_KEEP" -ge 1 ] 2>/dev/null; then
    echo "refusing FORK_SYNC_BACKUP_KEEP=$BACKUP_KEEP: must be >= 1 (keep=0 deletes the just-created backup)" >&2
    return 25
  fi
  local base old
  base="$(basename "$APP_TARGET")"
  while IFS= read -r old; do
    [ -n "$old" ] || continue
    rm -rf "$old"
    printf 'retention: removed old backup %s\n' "$(basename "$old")"
  done < <(ls -1d "$BACKUP_DIR/$base.bak-"* 2>/dev/null | sort -r | tail -n +"$((BACKUP_KEEP + 1))")
}

cmd_swap() {
  gate_check || return 23
  [ -d "$RELEASE_APP" ] || { SUMMARY="no packed app at $RELEASE_APP (run pack first)"; return 25; }
  [ -d "$APP_TARGET" ] || { SUMMARY="app target missing: $APP_TARGET"; return 25; }

  local old_ver new_ver stamp backup
  old_ver=$(read_plist_version "$APP_TARGET") || old_ver="unknown"
  new_ver=$(read_plist_version "$RELEASE_APP") || new_ver="unknown"
  stamp=$(date +%Y%m%d-%H%M%S)
  backup="$BACKUP_DIR/$(basename "$APP_TARGET").bak-$stamp"
  mkdir -p "$BACKUP_DIR"

  # Transactional swap mirroring scripts/desktop-update/posix.sh mac_swap:
  # stage a full copy, back the old bundle up, move it aside, move the copy
  # in. Every step checked; a failed final move ROLLS BACK so a launchable
  # app always remains.
  rm -rf "${APP_TARGET}.new" "${APP_TARGET}.old" 2>/dev/null || true
  if ! copy_tree "$RELEASE_APP" "${APP_TARGET}.new"; then
    rm -rf "${APP_TARGET}.new" 2>/dev/null || true
    SUMMARY="staging copy failed; nothing touched"
    return 25
  fi
  if ! copy_tree "$APP_TARGET" "$backup"; then
    rm -rf "${APP_TARGET}.new" "$backup" 2>/dev/null || true
    SUMMARY="backup to $backup failed; nothing touched"
    return 25
  fi
  if ! mv "$APP_TARGET" "${APP_TARGET}.old"; then
    rm -rf "${APP_TARGET}.new" "$backup" 2>/dev/null || true
    SUMMARY="could not move old bundle aside; nothing touched"
    return 25
  fi
  if ! mv "${APP_TARGET}.new" "$APP_TARGET"; then
    if mv "${APP_TARGET}.old" "$APP_TARGET"; then
      rm -rf "${APP_TARGET}.new" "$backup" 2>/dev/null || true
      SUMMARY="install failed; rolled back to the previous app (backup discarded)"
    else
      SUMMARY="install failed AND rollback failed; previous app at ${APP_TARGET}.old, backup at $backup"
    fi
    return 25
  fi
  rm -rf "${APP_TARGET}.old" 2>/dev/null || true

  enforce_backup_retention \
    || { SUMMARY="backup retention refused (FORK_SYNC_BACKUP_KEEP=$BACKUP_KEEP < 1) — swap DONE, fix the env"; return 25; }
  SUMMARY="swapped app bundle: $old_ver -> $new_ver (backup $(basename "$backup"), keep $BACKUP_KEEP)"
}

# ---------------------------------------------------------------------------
cmd_relaunch() {
  gate_check || return 23
  [ -d "$APP_TARGET" ] || { SUMMARY="app target missing: $APP_TARGET"; return 25; }
  # Same primitives as scripts/fork-sync-restart.sh and posix.sh launch_app:
  # quit if running, then open — open's exit code is launch acceptance.
  if command -v osascript >/dev/null 2>&1; then
    osascript -e 'tell application "Hermes" to quit' >/dev/null 2>&1 || true
    sleep 8
  fi
  command -v xattr >/dev/null 2>&1 && xattr -dr com.apple.quarantine "$APP_TARGET" 2>/dev/null || true
  command -v open >/dev/null 2>&1 || { SUMMARY="required tool 'open' not found (macOS only)"; return 2; }
  open "$APP_TARGET" || { SUMMARY="open rejected $APP_TARGET"; return 25; }
  SUMMARY="relaunched $APP_TARGET"
}

cmd_all() {
  cmd_pack || return
  cmd_swap || return
  cmd_relaunch
}

# ---------------------------------------------------------------------------
SUB="${1:-}"
if [ -z "$SUB" ]; then
  usage
  exit 2
fi
case "$SUB" in
  pack|swap|relaunch|all) ;;
  *)
    printf 'unknown subcommand: %s\n' "$SUB" >&2
    usage >&2
    exit 2
    ;;
esac

cmd_"$SUB"
