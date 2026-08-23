/**
 * Pure helpers for choosing a remote URL during passive update checks.
 *
 * A public install can end up with `origin=git@github.com:NousResearch/hermes-agent.git`.
 * If the user's GitHub SSH key is FIDO2/passkey-backed, a background `git fetch
 * origin` triggers an unexplained hardware-touch prompt. For passive checks
 * against the official repo we substitute the public HTTPS `ls-remote` path,
 * which needs no auth and cannot prompt. Active update/apply flows are left
 * unchanged.
 *
 * Extracted from main.ts so the security-critical remote detection is unit
 * testable without booting Electron (main.ts requires('electron') at load).
 */

const OFFICIAL_REPO_HTTPS_URL = 'https://github.com/NousResearch/hermes-agent.git'
const OFFICIAL_REPO_CANONICAL = 'github.com/nousresearch/hermes-agent'

// Normalize common GitHub remote URL forms to `host/owner/repo` (lowercased,
// no trailing slash, no .git suffix) so SSH and HTTPS forms of the same repo
// compare equal.
function canonicalGitHubRemote(url) {
  if (!url) {
    return ''
  }

  let value = String(url).trim()

  if (value.startsWith('git@github.com:')) {
    value = `github.com/${value.slice('git@github.com:'.length)}`
  } else if (value.startsWith('ssh://git@github.com/')) {
    value = `github.com/${value.slice('ssh://git@github.com/'.length)}`
  } else {
    try {
      const parsed = new URL(value)

      if (parsed.hostname && parsed.pathname) {
        value = `${parsed.hostname}${parsed.pathname}`
      }
    } catch {
      // Leave non-URL forms unchanged.
    }
  }

  value = value.trim().replace(/\/+$/, '')

  if (value.endsWith('.git')) {
    value = value.slice(0, -4)
  }

  return value.toLowerCase()
}

function isSshRemote(url) {
  const value = String(url || '')
    .trim()
    .toLowerCase()

  return value.startsWith('git@') || value.startsWith('ssh://')
}

function isOfficialSshRemote(url) {
  return isSshRemote(url) && canonicalGitHubRemote(url) === OFFICIAL_REPO_CANONICAL
}

function resolvePassiveUpdateSource({ originUrl, upstreamUrl }) {
  const originCanonical = canonicalGitHubRemote(originUrl)
  const upstreamCanonical = canonicalGitHubRemote(upstreamUrl)

  if (originCanonical === OFFICIAL_REPO_CANONICAL) {
    const ssh = isOfficialSshRemote(originUrl)

    return {
      compareUrl: OFFICIAL_REPO_HTTPS_URL,
      explicitRefspec: ssh,
      fetchRemote: ssh ? OFFICIAL_REPO_HTTPS_URL : 'origin',
      trackingRemote: ssh ? 'hermes-official' : 'origin'
    }
  }

  if (upstreamCanonical === OFFICIAL_REPO_CANONICAL) {
    const ssh = isSshRemote(upstreamUrl)

    return {
      compareUrl: OFFICIAL_REPO_HTTPS_URL,
      explicitRefspec: ssh,
      fetchRemote: ssh ? OFFICIAL_REPO_HTTPS_URL : 'upstream',
      trackingRemote: 'upstream'
    }
  }

  return {
    compareUrl: OFFICIAL_REPO_HTTPS_URL,
    explicitRefspec: true,
    fetchRemote: OFFICIAL_REPO_HTTPS_URL,
    trackingRemote: 'hermes-official'
  }
}

// Build the exact git-fetch argv for a passive check. explicitRefspec fetches
// against a raw URL (the anonymous official HTTPS path) write FETCH_HEAD, not
// a tracking ref — so they must name the destination refspec explicitly, or
// the checker would rev-parse a stale tracking ref afterwards.
function buildPassiveFetchArgs({ fetchRemote, trackingRemote, explicitRefspec }, branch) {
  if (explicitRefspec) {
    return [
      'fetch',
      '--quiet',
      fetchRemote,
      `+refs/heads/${branch}:refs/remotes/${trackingRemote}/${branch}`
    ]
  }

  return ['fetch', '--quiet', fetchRemote, branch]
}

export {
  buildPassiveFetchArgs,
  canonicalGitHubRemote,
  isOfficialSshRemote,
  isSshRemote,
  OFFICIAL_REPO_CANONICAL,
  OFFICIAL_REPO_HTTPS_URL,
  resolvePassiveUpdateSource
}
