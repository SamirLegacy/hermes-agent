import { describe, expect, it } from 'vitest'

import {
  describeSessionOwner,
  isMissingPendingPromptRequest,
  isMissingRpcMethod,
  isSessionNotOwnedError,
  sessionOwnerRefusalOf,
} from './gateway-rpc'

describe('isMissingRpcMethod', () => {
  it('detects JSON-RPC method-not-found errors', () => {
    expect(isMissingRpcMethod(new Error('unknown method: projects.create'))).toBe(true)
    expect(isMissingRpcMethod(new Error('Method not found'))).toBe(true)
    expect(isMissingRpcMethod(new Error('RPC failed: -32601'))).toBe(true)
  })

  it('ignores unrelated failures', () => {
    expect(isMissingRpcMethod(new Error('Hermes gateway is not connected'))).toBe(false)
    expect(isMissingRpcMethod(new Error('no such project'))).toBe(false)
  })
})

describe('isMissingPendingPromptRequest', () => {
  it('detects stale prompt response errors from the gateway', () => {
    expect(isMissingPendingPromptRequest(new Error('no pending password request'), 'password')).toBe(true)
    expect(isMissingPendingPromptRequest(new Error('RPC failed: no pending value request'), 'value')).toBe(true)
  })

  it('ignores unrelated gateway failures', () => {
    expect(isMissingPendingPromptRequest(new Error('gateway not connected'), 'password')).toBe(false)
    expect(isMissingPendingPromptRequest(new Error('no pending value request'), 'password')).toBe(false)
  })
})

const HOLDER = {
  age_s: 120,
  holder_live: true,
  pid: 4242,
  session_id: 'sess_1',
  started_at: 1788438000,
  surface: 'cli',
}

const DATA = { reason: 'SESSION_NOT_OWNED', holder: HOLDER }

/** R2 review: the transport wraps 4090 refusals inconsistently — code as
 * number OR string, payload at the top level OR nested under `.error`. Every
 * shape must classify, or the desktop retry ladder treats the refusal as a
 * transient connection error and re-dials the 200ms storm. */
describe('isSessionNotOwnedError', () => {
  it('accepts the canonical numeric top-level shape', () => {
    expect(isSessionNotOwnedError({ code: 4090, data: DATA })).toBe(true)
  })

  it('accepts a string code', () => {
    expect(isSessionNotOwnedError({ code: '4090', data: DATA })).toBe(true)
  })

  it('accepts a nested .error payload with numeric code', () => {
    expect(isSessionNotOwnedError({ error: { code: 4090, data: DATA } })).toBe(true)
  })

  it('accepts a nested .error payload with string code', () => {
    expect(isSessionNotOwnedError({ error: { code: '4090', data: DATA } })).toBe(true)
  })

  it('rejects other codes and missing payloads', () => {
    expect(isSessionNotOwnedError({ code: 4091, data: DATA })).toBe(false)
    expect(isSessionNotOwnedError({ code: 4090, data: { reason: 'OTHER' } })).toBe(false)
    expect(isSessionNotOwnedError({ code: 4090 })).toBe(false)
    expect(isSessionNotOwnedError(new Error('boom'))).toBe(false)
    expect(isSessionNotOwnedError(null)).toBe(false)
  })
})

describe('sessionOwnerRefusalOf', () => {
  it('reads holder facts from every accepted shape', () => {
    expect(sessionOwnerRefusalOf({ code: '4090', data: DATA })).toEqual(HOLDER)
    expect(sessionOwnerRefusalOf({ error: { code: 4090, data: DATA } })).toEqual(HOLDER)
    expect(sessionOwnerRefusalOf({ error: { code: '4090', data: DATA } })).toEqual(HOLDER)
  })

  it('returns null for non-refusals', () => {
    expect(sessionOwnerRefusalOf({ code: 4091, data: DATA })).toBeNull()
    expect(sessionOwnerRefusalOf(new Error('boom'))).toBeNull()
  })
})

/** R3 re-review: --takeover only reclaims dead/stale leases (the CLI refuses
 * live holders), so the desktop hint must never advertise it for a live
 * holder — the refusal payload's holder_live field carries that verdict. */
describe('describeSessionOwner', () => {
  it('live holder: names surface + pid + age and says quit/wait, never --takeover', () => {
    const text = describeSessionOwner({ code: 4090, data: DATA })
    expect(text).toContain('cli')
    expect(text).toContain('4242')
    expect(text).toContain('Quit that surface first')
    expect(text).not.toContain('--takeover')
  })

  it('live holder: unwraps nested .error payloads too', () => {
    const text = describeSessionOwner({ error: { code: 4090, data: DATA } })
    expect(text).toContain('live-owned by cli')
    expect(text).not.toContain('--takeover')
  })

  it('dead/stale holder: advertises --takeover as the reclaim remedy', () => {
    const dead = { ...DATA, holder: { ...HOLDER, holder_live: false } }
    const text = describeSessionOwner({ code: 4090, data: dead })
    expect(text).toContain('dead/stale')
    expect(text).toContain('hermes chat --resume sess_1 --takeover')
  })

  it('unverifiable liveness (field absent) is treated as live', () => {
    const unknown = {
      ...DATA,
      holder: { session_id: 'sess_1', surface: 'cli', pid: 4242 },
    }
    const text = describeSessionOwner({ code: 4090, data: unknown })
    expect(text).not.toContain('--takeover')
  })

  it('falls back to the error message for non-refusal errors', () => {
    expect(describeSessionOwner(new Error('boom'))).toBe('boom')
  })
})
