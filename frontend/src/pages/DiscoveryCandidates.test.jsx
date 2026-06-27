import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { DiscoveryCandidatesTable } from './DiscoveryCandidates.jsx'

const C = [
  { wallet: '0x1111111111111111111111111111111111111111', discovery_score: 100, discovery_sources: ['profit_leaderboard'],
    source_details: ['profit_30d'], source_rank: 1, backfill_priority: 100, needs_backfill: true,
    roi: null, profit_factor: null, production_eligible: false, reason_not_eligible: 'needs backfill (no stats yet)',
    first_seen: '2026-06-27T00:00:00', last_seen: '2026-06-27T00:00:00' },
  { wallet: '0x2222222222222222222222222222222222222222', discovery_score: 70, discovery_sources: ['top_holders'],
    source_details: ['holders:0xabc'], source_rank: 3, backfill_priority: 70, needs_backfill: false,
    roi: 0.12, profit_factor: 1.4, production_eligible: true, reason_not_eligible: '(production eligible)',
    first_seen: '2026-06-20T00:00:00', last_seen: '2026-06-26T00:00:00' },
  { wallet: '0x3333333333333333333333333333333333333333', discovery_score: 30, discovery_sources: ['recent_trades'],
    source_details: ['recent'], source_rank: null, backfill_priority: 30, needs_backfill: true,
    roi: -0.05, profit_factor: 0.8, production_eligible: false, reason_not_eligible: 'PF 0.80 <= 1.20',
    first_seen: '2026-06-25T00:00:00', last_seen: '2026-06-25T00:00:00' },
]
const rows = () => screen.getAllByTestId('discovery-row')

describe('DiscoveryCandidatesTable', () => {
  it('renders all discovered wallets', () => {
    render(<DiscoveryCandidatesTable candidates={C} />)
    expect(screen.getByTestId('discovery-table')).toBeInTheDocument()
    expect(rows()).toHaveLength(3)
  })

  it('sorts by backfill priority by default (highest first)', () => {
    render(<DiscoveryCandidatesTable candidates={C} />)
    expect(within(rows()[0]).getByText('Profit LB')).toBeInTheDocument()  // priority 100
  })

  it('filters to Top Holders', () => {
    render(<DiscoveryCandidatesTable candidates={C} />)
    fireEvent.click(screen.getByRole('button', { name: 'Top Holders' }))
    expect(rows()).toHaveLength(1)
    expect(within(rows()[0]).getByText('Top Holders')).toBeInTheDocument()
  })

  it('filters to Needs Backfill', () => {
    render(<DiscoveryCandidatesTable candidates={C} />)
    fireEvent.click(screen.getByRole('button', { name: 'Needs Backfill' }))
    expect(rows()).toHaveLength(2)
  })

  it('filters to Leaderboard', () => {
    render(<DiscoveryCandidatesTable candidates={C} />)
    fireEvent.click(screen.getByRole('button', { name: 'Leaderboard' }))
    expect(rows()).toHaveLength(1)
  })

  it('renders wallet links to Polymarket profiles (new tab)', () => {
    render(<DiscoveryCandidatesTable candidates={C} />)
    const link = within(rows()[0]).getByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
    expect(link).toHaveAttribute('target', '_blank')
  })
})
