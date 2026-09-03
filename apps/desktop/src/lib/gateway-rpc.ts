/** True when a JSON-RPC call failed because the backend predates the method. */
export function isMissingRpcMethod(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /method not found|-32601|unknown method|no such method/i.test(message)
}

/** REST twin of isMissingRpcMethod: the route does not exist on this backend.
 *  Matches the backend catch-all ('404: {"detail":"No such API endpoint: …}'),
 *  FastAPI's bare 404 on headless serve — directly, or wrapped as "Error
 *  invoking remote method 'hermes:api': Error: 404: …" through the IPC bridge
 *  — and the Electron JSON-guard ("endpoint is likely missing"). Transient
 *  failures (timeouts, 5xx, connection refused) must NOT match: they are
 *  retryable, not a capability verdict. Only sound for calls where a 404 can
 *  mean nothing else — a route with path params can 404 on a bad id. */
export function isMissingRestEndpoint(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return (
    /no such api endpoint/i.test(message) ||
    /endpoint is likely missing/i.test(message) ||
    /(?:^\s*|error:\s*)404\b/i.test(message)
  )
}

/** True when a prompt response raced a backend-side timeout / completion. */
export function isMissingPendingPromptRequest(error: unknown, key: string): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return message.toLowerCase().includes(`no pending ${key.toLowerCase()} request`)
}

/** True when a pre-deferral backend refused a mid-turn model switch (4009).
 *  Current gateways park the pick and answer `scope: "pending"` instead. */
export function isBusySessionModelSwitch(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /session busy/i.test(message) && /switching models/i.test(message)
}

/** Holder facts the gateway ships on a SESSION_NOT_OWNED refusal (4090). */
export interface SessionOwnerRefusal {
  age_s?: null | number
  holder_live?: boolean
  pid?: null | number | string
  session_id?: string
  started_at?: null | number
  surface?: string
}

/** Walk every transport shape a 4090 refusal may arrive in and return the
 *  unwrapped candidate, or null when none carries the refusal payload.
 *  Shared by the boolean classifier and the holder-facts extractor so the
 *  two can never disagree about what counts as the refusal. */
function refusalCandidateOf(error: unknown): unknown | null {
  const candidates = [
    error,
    (error as { error?: unknown })?.error,
  ]
  for (const candidate of candidates) {
    const code = (candidate as { code?: unknown })?.code
    const codeMatches = code === 4090 || code === '4090'
    if (!codeMatches) continue
    const data = (candidate as { data?: unknown })?.data
    if (typeof data !== 'object' || data === null) continue
    if ((data as { reason?: unknown }).reason !== 'SESSION_NOT_OWNED') continue
    return candidate
  }
  return null
}

/** The machine-readable holder payload attached to a 4090 refusal's `data`. */
export function sessionOwnerRefusalOf(error: unknown): SessionOwnerRefusal | null {
  const candidate = refusalCandidateOf(error)
  if (candidate === null) return null

  const data = (candidate as { data?: unknown }).data as {
    holder?: unknown
  }
  const holder = data.holder

  return typeof holder === 'object' && holder !== null ? (holder as SessionOwnerRefusal) : {}
}

/** True when an RPC failed because the session already has a live owner.
 *
 *  This refusal is terminal-for-now: retrying can never succeed while the holder lives,
 *  so it must NOT ride the reconnect/resume retry ladders (which keep re-dialing at
 *  attempt-0 cadence because ws-open resets the backoff attempt — the measured 200ms
 *  storm). The reason travels as typed data, not prose; never match the message text. */
export function isSessionNotOwnedError(error: unknown): boolean {
  return refusalCandidateOf(error) !== null
}

/** One-line human summary of a SESSION_NOT_OWNED refusal, from its holder facts. */
export function describeSessionOwner(error: unknown): string {
  const holder = sessionOwnerRefusalOf(error)

  if (!holder) {
    return error instanceof Error ? error.message : String(error)
  }

  const surface = holder.surface || 'another surface'
  const pid = holder.pid ?? '?'
  const age =
    typeof holder.age_s === 'number' && holder.age_s >= 0
      ? ` for ${Math.max(0, Math.round(holder.age_s / 60))}m`
      : ''
  const since =
    typeof holder.started_at === 'number' && holder.started_at > 0
      ? ` since ${new Date(holder.started_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
      : ''
  const sessionId = holder.session_id ? ` ${holder.session_id}` : ''
  // --takeover only reclaims dead/stale leases (the CLI refuses live
  // holders), so advertising it for a live holder is a dead remedy. Match
  // the CLI refusal message: live (or unverifiable) → quit that surface.
  if (holder.holder_live !== false) {
    return (
      `Session${sessionId} is live-owned by ${surface} (pid ${pid})${age}${since}. ` +
      `Quit that surface first (or wait for it to exit), then resume again.`
    )
  }
  return (
    `Session${sessionId} is held by a dead/stale ${surface} (pid ${pid})${age}${since}.` +
    (sessionId ? ` Reclaim it with: hermes chat --resume${sessionId} --takeover` : '')
  )
}
