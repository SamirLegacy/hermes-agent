import { describe, expect, it } from 'vitest'

import { describeSessionOwner, isSessionNotOwnedError, sessionOwnerRefusalOf } from './gateway-rpc'

const HOLDER = {
  age_s: 120,
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

  it('sessionOwnerRefusalOf reads holder facts from every accepted shape', () => {
    expect(sessionOwnerRefusalOf({ code: '4090', data: DATA })).toEqual(HOLDER)
    expect(sessionOwnerRefusalOf({ error: { code: 4090, data: DATA } })).toBeNull()
    expect(describeSessionOwner({ code: 4090, data: DATA })).toContain('cli')
  })
})
