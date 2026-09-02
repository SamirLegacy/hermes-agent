import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    exclude: ['dist/**', 'node_modules/**'],
    // FORK: several ui-tui tests assert on real-time effects after short
    // fixed delays (delay(40), wall-clock re-syncs). On the fork's standard
    // 4-vCPU ubuntu-latest runners the scheduler routinely starves those
    // windows, failing one different timing test per CI run (2026-09-02:
    // appChromeBlockedTimers, virtualHistoryOffsetCache). Retry once so a
    // starved window does not fail the whole JS gate; deterministic breaks
    // still fail on retry.
    retry: 2
  }
})
