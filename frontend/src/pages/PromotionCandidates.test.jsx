import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { PromotionCandidatesTable } from './PromotionCandidates.jsx'

const CANDS = [
  { wallet: '0x1111111111111111111111111111111111111111', promotion_score: 82, status: 'strong',
    signals_seen: 30, average_edge: 0.21, average_confidence: 82, average_production_score: 0,
    roi: 0.25, profit_factor: 1.9, settled_trades: 40, win_rate: 0.6, last_active: '2026-06-27T00:00:00',
    reason_rejected: 'Outside production top-N' },
  { wallet: '0x2222222222222222222222222222222222222222', promotion_score: 55, status: 'near',
    signals_seen: 12, average_edge: 0.30, average_confidence: 70, average_production_score: 0,
    roi: 0.04, profit_factor: 1.15, settled_trades: 14, win_rate: 0.55, last_active: '2026-06-20T00:00:00',
    reason_rejected: 'PF 1.15 <= 1.20' },
  { wallet: '0x3333333333333333333333333333333333333333', promotion_score: 30, status: 'watch',
    signals_seen: 3, average_edge: 0.10, average_confidence: 65, average_production_score: 0,
    roi: -0.02, profit_factor: 0.9, settled_trades: 5, win_rate: 0.4, last_active: '2026-05-01T00:00:00',
    reason_rejected: 'settled 5 < 20' },
]

const rows = () => screen.getAllByTestId('promo-row')

describe('PromotionCandidatesTable', () => {
  it('renders the table with all candidates', () => {
    render(<PromotionCandidatesTable candidates={CANDS} />)
    expect(screen.getByTestId('promo-table')).toBeInTheDocument()
    expect(rows()).toHaveLength(3)
    expect(screen.getByText('Outside production top-N')).toBeInTheDocument()
  })

  it('filters by status (Strong only)', () => {
    render(<PromotionCandidatesTable candidates={CANDS} />)
    fireEvent.click(screen.getByText('Strong Candidates'))
    const r = rows()
    expect(r).toHaveLength(1)
    expect(within(r[0]).getByText('⭐ Strong')).toBeInTheDocument()
  })

  it('sorts by Avg Edge (descending)', () => {
    render(<PromotionCandidatesTable candidates={CANDS} />)
    fireEvent.change(screen.getByLabelText('sort by'), { target: { value: 'average_edge' } })
    const first = rows()[0]
    // candidate #2 has the highest avg edge (0.30)
    expect(within(first).getByText('0.300')).toBeInTheDocument()
  })

  it('searches by wallet address', () => {
    render(<PromotionCandidatesTable candidates={CANDS} />)
    fireEvent.change(screen.getByLabelText('search wallet'), { target: { value: '0x2222' } })
    expect(rows()).toHaveLength(1)
  })

  it('renders wallet addresses as Polymarket profile links opening in a new tab', () => {
    render(<PromotionCandidatesTable candidates={CANDS} />)
    const link = within(rows()[0]).getByRole('link')
    expect(link).toHaveAttribute('href', `https://polymarket.com/profile/${CANDS[0].wallet}`)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })
})
