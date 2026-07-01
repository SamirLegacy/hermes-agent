import { describe, expect, it } from 'vitest'

import type { ModelOptionProvider } from '@/types/hermes'

import { groupModels } from './model-menu-panel'

const provider = (slug: string, models: string[]): ModelOptionProvider => ({
  models,
  name: slug,
  slug
})

describe('groupModels', () => {
  it('keeps configured fallback models visible even when a stale visibility filter hides their provider', () => {
    const providers = [
      provider('copilot', ['gpt-5.4', 'gpt-5.4-mini']),
      provider('openai-codex', ['gpt-5.5', 'gpt-5.4']),
      provider('anthropic', ['claude-sonnet-5'])
    ]
    const visible = new Set(['copilot::gpt-5.4'])

    const groups = groupModels(
      providers,
      '',
      { model: 'claude-sonnet-5', provider: 'anthropic' },
      visible,
      [{ provider: 'openai-codex', model: 'gpt-5.5', reason: 'fallback' }]
    )

    const codex = groups.find(group => group.provider.slug === 'openai-codex')
    expect(codex?.families.map(family => family.id)).toContain('gpt-5.5')
  })
})
