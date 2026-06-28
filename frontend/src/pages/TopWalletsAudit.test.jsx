import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { AuditTable, WarningChips, AuditDrilldown, HardeningSummary } from './TopWalletsAudit.jsx'

const W = '0x4f29e103339919c4baaea2a60195cf1c8bb27a7e'
const ROW = {
  rank: 1, address: W, display_name: '0x4f2', production_rank_score: 61.4,
  internal: { roi: 0.25, profit_factor: 1.8, win_rate: 0.6, num_settled: 30, realized_pnl: 120, volume: 1500, backfill_coverage: { level: 'low', volume_ratio: 0.0001 } },
  public: { pnl_all: -22088.48, position_value: 186181, predictions: 40, volume_all: 162141683 },
  rolling: { '1d': { pnl: 0 }, '7d': { pnl: 350 }, '30d': { pnl: 1500 }, '90d': { pnl: 1500 } },
  warning_count: 3,
  warnings: [
    { code: 'public_lifetime_loss', severity: 'high', message: 'Public all-time P/L is -22,088.' },
    { code: 'likely_market_maker_whale', severity: 'high', message: 'Whale/MM signature: public volume 162,141,683' },
    { code: 'low_coverage', severity: 'high', message: 'Internal data captures only a small slice.' },
  ],
  hardened_pass: false, would_be_excluded: true,
  hardened_exclusions: [
    { code: 'partial_history', message: 'partial' },
    { code: 'whale_volume', message: 'whale' },
  ],
}

describe('Top 20 Audit — pure components', () => {
  it('AuditTable renders rows, links wallets to Polymarket, drills down on click', () => {
    const onSelect = vi.fn()
    render(<AuditTable rows={[ROW]} onSelect={onSelect} />)
    expect(screen.getByTestId('audit-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('audit-row')).toHaveLength(1)
    const link = within(screen.getByTestId('audit-row')).getByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
    fireEvent.click(screen.getByTestId('audit-row'))
    expect(onSelect).toHaveBeenCalledWith(W)
  })

  it('AuditTable shows hardened exclusion chips for would-be-excluded wallets', () => {
    render(<AuditTable rows={[ROW]} />)
    const cell = screen.getByTestId('hardened-excluded')
    expect(within(cell).getByText('partial history')).toBeInTheDocument()
    expect(within(cell).getByText('whale volume')).toBeInTheDocument()
  })

  it('HardeningSummary renders mode, counts and audit-only label', () => {
    render(<HardeningSummary hardening={{
      audit_only: true, mode: 'AUDIT-ONLY (no eligibility change)', current_eligible_count: 20,
      would_pass_hardened_count: 3, excluded_by_public_pnl: ['0xa'], excluded_by_partial_history: ['0xa', '0xb'],
      excluded_by_coverage: ['0xa'], excluded_by_whale: ['0xa', '0xb', '0xc'],
      currently_copied_would_be_removed: ['0x4f29e103339919c4baaea2a60195cf1c8bb27a7e'],
      thresholds: { min_public_all_time_pnl: 0, allow_partial_history: false, min_coverage_ratio: 0.05, max_public_volume: 1000000, require_public_stats: false },
    }} />)
    expect(screen.getByTestId('hardening-summary')).toBeInTheDocument()
    expect(screen.getByText(/no eligibility change/)).toBeInTheDocument()           // mode badge
    expect(screen.getByText(/do NOT change which wallets are copied/)).toBeInTheDocument()
    expect(screen.getByText('Would pass')).toBeInTheDocument()
    expect(screen.getByText(/WOULD be removed/)).toBeInTheDocument()
  })

  it('WarningChips renders a chip per warning', () => {
    render(<WarningChips warnings={ROW.warnings} />)
    expect(screen.getByTestId('warning-chips')).toBeInTheDocument()
    expect(screen.getByText('public lifetime loss')).toBeInTheDocument()
    expect(screen.getByText('likely market maker whale')).toBeInTheDocument()
  })

  it('WarningChips shows "none" when there are no warnings', () => {
    render(<WarningChips warnings={[]} />)
    expect(screen.getByText('none')).toBeInTheDocument()
  })

  it('AuditDrilldown shows internal-vs-public side-by-side, score breakdown and eligibility', () => {
    render(<AuditDrilldown data={{
      address: W, display_name: '0x4f2', production_rank_score: 61.4, copy_rationale: 'Selected: ...',
      internal: ROW.internal, public: ROW.public, rolling: ROW.rolling,
      score_breakdown: { components: { reputation: { weight: 0.4, points: 30.4 }, profit_factor: { weight: 0.3, points: 12 }, roi: { weight: 0.2, points: 10 }, recency: { weight: 0.1, points: 9 } }, total: 61.4 },
      eligibility_rules: [{ rule: 'ROI > 0%', pass: true, detail: '25%' }, { rule: 'Profit factor > 1.20', pass: true, detail: '1.80' }],
      largest_wins: [{ pnl: 50 }], largest_losses: [{ pnl: -20 }],
      warnings: ROW.warnings,
    }} onClose={() => {}} />)
    expect(screen.getByTestId('audit-drilldown')).toBeInTheDocument()
    expect(screen.getByTestId('side-by-side')).toBeInTheDocument()
    expect(screen.getByText('ROI > 0%')).toBeInTheDocument()
    expect(within(screen.getByTestId('audit-drilldown')).getByRole('link')).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
  })
})
