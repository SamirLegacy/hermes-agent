import { describe, expect, it } from 'vitest'

import {
  describeSessionOwner,
  isMissingPendingPromptRequest,
  isMissingRpcMethod,
  isSessionNotOwnedError
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

describe('session ownership refusal', () => {
  it.each([4090, '4090'])('recognizes typed and wrapped code %s without matching prose', code => {
    const error = { code, data: { reason: 'SESSION_NOT_OWNED', holder: { surface: 'cli', pid: 42, age_s: 120 } } }
    expect(isSessionNotOwnedError(error)).toBe(true)
    expect(isSessionNotOwnedError({ error })).toBe(true)
    expect(describeSessionOwner({ error })).toContain('cli (pid 42, 2m)')
    expect(describeSessionOwner(error)).not.toContain('--takeover')
    expect(isSessionNotOwnedError({ code, data: { reason: 'SESSION_LIMIT' } })).toBe(false)
    expect(isSessionNotOwnedError(new Error('SESSION_NOT_OWNED'))).toBe(false)
  })

  it('renders the continuation resume command with live holder facts', () => {
    const error = {
      code: 4090,
      data: { reason: 'SESSION_NOT_OWNED', holder: {
        surface: 'desktop', pid: 42, age_s: 120, session_id: 'child-session', holder_live: true
      } }
    }
    expect(describeSessionOwner(error)).toContain('desktop (pid 42, 2m)')
    expect(describeSessionOwner(error)).toContain('hermes chat --resume child-session')
    expect(describeSessionOwner(error)).not.toContain('--takeover')
  })

  it('preserves the server recovery message when holder facts are partial', () => {
    const message = 'Lease re-anchor failed. Re-attach with: hermes chat --resume child-session'
    const error = { code: 4090, message, data: { reason: 'SESSION_NOT_OWNED' } }
    expect(describeSessionOwner({ error })).toBe(message)
    expect(describeSessionOwner(Object.assign(new Error(message), error))).toBe(message)
  })

  it('offers takeover only for an explicitly stale holder', () => {
    const error = {
      code: 4090,
      data: { reason: 'SESSION_NOT_OWNED', holder: { holder_live: false, session_id: 'test-session' } }
    }
    expect(describeSessionOwner(error)).toContain('hermes chat --resume test-session --takeover')
    expect(isSessionNotOwnedError(null)).toBe(false)
  })
})
