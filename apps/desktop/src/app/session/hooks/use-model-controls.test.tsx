import { QueryClient } from '@tanstack/react-query'
import { cleanup, render, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getGlobalModelInfo } from '@/hermes'
import { $activeSessionId, $currentModel, $currentProvider, setCurrentModel, setCurrentProvider } from '@/store/session'

import { useModelControls } from './use-model-controls'

const setGlobalModel = vi.fn()
const notifyError = vi.fn()

vi.mock('@/hermes', () => ({
  getGlobalModelInfo: vi.fn(),
  setGlobalModel: (...args: Parameters<typeof setGlobalModel>) => setGlobalModel(...args)
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      desktop: {
        modelSwitchFailed: 'Model switch failed'
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({
  notifyError: (...args: Parameters<typeof notifyError>) => notifyError(...args)
}))

type Controls = ReturnType<typeof useModelControls>

function Harness({
  activeSessionId,
  onReady,
  requestGateway
}: {
  activeSessionId: string | null
  onReady: (controls: Controls) => void
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const controls = useModelControls({
    activeSessionId,
    queryClient: new QueryClient(),
    requestGateway
  })

  onReady(controls)

  return null
}

describe('useModelControls', () => {
  beforeEach(() => {
    $activeSessionId.set(null)
    setCurrentModel('')
    setCurrentProvider('')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $activeSessionId.set(null)
    setCurrentModel('')
    setCurrentProvider('')
  })

  it('applies the global model when there is no active runtime session', async () => {
    vi.mocked(getGlobalModelInfo).mockResolvedValue({
      model: 'openai/gpt-5.5',
      provider: 'openai-codex'
    })

    const { result } = renderHook(() =>
      useModelControls({
        activeSessionId: null,
        queryClient: new QueryClient(),
        requestGateway: vi.fn()
      })
    )

    await result.current.refreshCurrentModel()

    expect($currentModel.get()).toBe('openai/gpt-5.5')
    expect($currentProvider.get()).toBe('openai-codex')
  })

  it('does not clobber the active session footer state with global model info', async () => {
    setCurrentModel('deepseek/deepseek-v4-pro')
    setCurrentProvider('deepseek')
    $activeSessionId.set('runtime-1')
    vi.mocked(getGlobalModelInfo).mockResolvedValue({
      model: 'openai/gpt-5.5',
      provider: 'openai-codex'
    })

    const { result } = renderHook(() =>
      useModelControls({
        activeSessionId: 'runtime-1',
        queryClient: new QueryClient(),
        requestGateway: vi.fn()
      })
    )

    await result.current.refreshCurrentModel()

    expect($currentModel.get()).toBe('deepseek/deepseek-v4-pro')
    expect($currentProvider.get()).toBe('deepseek')
  })

  it('routes active-session picker changes through config.set with an explicit provider', async () => {
    const requestGateway = vi.fn(async () => ({ key: 'model', value: 'claude-sonnet-4.6' }) as never)
    let controls!: Controls

    render(
      <Harness activeSessionId="session-1" onReady={value => (controls = value)} requestGateway={requestGateway} />
    )

    await expect(
      controls.selectModel({
        model: 'claude-sonnet-4.6',
        provider: 'anthropic'
      })
    ).resolves.toBe(true)

    expect(requestGateway).toHaveBeenCalledWith('config.set', {
      session_id: 'session-1',
      key: 'model',
      value: 'claude-sonnet-4.6 --provider anthropic'
    })
    expect(requestGateway).not.toHaveBeenCalledWith('slash.exec', expect.anything())
  })

  it('persists a no-session pick as the profile default instead of sticky composer state', async () => {
    const requestGateway = vi.fn()
    setGlobalModel.mockResolvedValue({ ok: true, provider: 'anthropic', model: 'claude-sonnet-4.6' })
    let controls!: Controls

    render(<Harness activeSessionId={null} onReady={value => (controls = value)} requestGateway={requestGateway} />)

    await expect(
      controls.selectModel({
        model: 'claude-sonnet-4.6',
        provider: 'anthropic'
      })
    ).resolves.toBe(true)

    // A draft-session pick is not sticky localStorage state. It becomes the
    // visible profile default, so fresh chats and relaunches do not resurrect
    // an old composer override.
    expect($currentModel.get()).toBe('claude-sonnet-4.6')
    expect($currentProvider.get()).toBe('anthropic')
    expect(requestGateway).not.toHaveBeenCalled()
    expect(setGlobalModel).toHaveBeenCalledWith('anthropic', 'claude-sonnet-4.6')
  })

  it('rolls back a no-session pick when the profile-default write is rejected', async () => {
    const requestGateway = vi.fn()
    setCurrentModel('claude-sonnet-5')
    setCurrentProvider('anthropic')
    setGlobalModel.mockResolvedValue({
      confirm_message: 'Confirm expensive model first',
      confirm_required: true,
      ok: false,
      provider: 'openrouter',
      model: 'anthropic/claude-opus-4.7'
    })
    let controls!: Controls

    render(<Harness activeSessionId={null} onReady={value => (controls = value)} requestGateway={requestGateway} />)

    await expect(
      controls.selectModel({
        model: 'anthropic/claude-opus-4.7',
        provider: 'openrouter'
      })
    ).resolves.toBe(false)

    expect($currentModel.get()).toBe('claude-sonnet-5')
    expect($currentProvider.get()).toBe('anthropic')
    expect(requestGateway).not.toHaveBeenCalled()
    expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Model switch failed')
  })

  it('always reseeds a draft composer from the profile default', async () => {
    vi.mocked(getGlobalModelInfo).mockResolvedValue({ model: 'openai/gpt-5.5', provider: 'openai-codex' })

    const { result } = renderHook(() =>
      useModelControls({
        activeSessionId: null,
        queryClient: new QueryClient(),
        requestGateway: vi.fn()
      })
    )

    // Empty → seeds the default.
    await result.current.refreshCurrentModel()
    expect($currentModel.get()).toBe('openai/gpt-5.5')

    // Stale local/session metadata must not survive draft refreshes. With no
    // active runtime session, the statusbar should always show the real profile
    // default, not a sticky composer override.
    setCurrentModel('anthropic/claude-sonnet-4.6')
    setCurrentProvider('anthropic')
    await result.current.refreshCurrentModel()
    expect($currentModel.get()).toBe('openai/gpt-5.5')
    expect($currentProvider.get()).toBe('openai-codex')

    // A profile swap remains a reseed to the new profile's default.
    await result.current.refreshCurrentModel(true)
    expect($currentModel.get()).toBe('openai/gpt-5.5')
  })
})
