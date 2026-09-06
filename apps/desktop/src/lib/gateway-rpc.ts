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

interface SessionOwner {
  surface?: string
  pid?: number | string
  age_s?: number
  session_id?: string
  holder_live?: boolean
}

function sessionOwnerOf(error: unknown): SessionOwner | null {
  for (const candidate of [error, (error as { error?: unknown } | null)?.error]) {
    const rpc = candidate as { code?: unknown; data?: { reason?: unknown; holder?: SessionOwner } } | null
    if ((rpc?.code === 4090 || rpc?.code === '4090') && rpc.data?.reason === 'SESSION_NOT_OWNED') {
      return rpc.data.holder ?? {}
    }
  }
  return null
}

export function isSessionNotOwnedError(error: unknown): boolean {
  return sessionOwnerOf(error) !== null
}

export function describeSessionOwner(error: unknown): string {
  const owner = sessionOwnerOf(error)
  if (!owner) {
    return error instanceof Error ? error.message : String(error)
  }
  if (!owner.surface || owner.pid == null) {
    const rpc = (error as { error?: { message?: unknown }; message?: unknown } | null)
    const message = rpc?.error?.message ?? rpc?.message
    if (typeof message === 'string' && message) {
      return message
    }
  }
  const age = typeof owner.age_s === 'number' ? `, ${Math.max(0, Math.round(owner.age_s / 60))}m` : ''
  const facts = `${owner.surface || 'another surface'} (pid ${owner.pid ?? '?'}${age})`
  return owner.holder_live === false && owner.session_id
    ? `Session held by stale ${facts}. Reclaim with: hermes chat --resume ${owner.session_id} --takeover`
    : `Session has a live owner: ${facts}. Quit that surface first, then ${owner.session_id ? `resume with: hermes chat --resume ${owner.session_id}` : 'resume again.'}`
}
