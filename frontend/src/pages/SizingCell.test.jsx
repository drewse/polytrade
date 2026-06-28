import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SizingCell } from './LiveTrading.jsx'

describe('SizingCell (dynamic sizing breakdown)', () => {
  it('renders the limiting constraint as a chip', () => {
    render(<SizingCell sizing={{
      method: 'dynamic', market_price: 0.15, confidence: 88, confidence_multiplier: 1.0,
      edge: 0.05, edge_factor: 1.025, raw_target_stake: 51.25, share_cap: 3.0,
      max_shares_per_trade: 20, final_stake: 3.0, final_shares: 20,
      limiting_constraint: 'share_cap',
      constraints: { dynamic_target: 51.25, share_cap: 3.0, remaining_per_market: 6 },
    }} />)
    expect(screen.getByText('share cap')).toBeInTheDocument()
  })

  it('renders a dash when there is no sizing detail', () => {
    render(<SizingCell sizing={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
