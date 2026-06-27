import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { ShadowPortfolioTable } from './ShadowPortfolio.jsx'

const WALLETS = [
  { wallet: '0x1111111111111111111111111111111111111111', status: 'strong', promotion_score: 80,
    total_pl: 4.5, return_pct: 0.30, shadow_trades: 15, win_rate: 0.6, max_drawdown: 1.2,
    avg_edge: 0.22, last_simulated_trade: '2026-06-27T00:00:00' },
  { wallet: '0x2222222222222222222222222222222222222222', status: 'near', promotion_score: 55,
    total_pl: -1.0, return_pct: -0.10, shadow_trades: 10, win_rate: 0.4, max_drawdown: 2.0,
    avg_edge: 0.18, last_simulated_trade: '2026-06-20T00:00:00' },
  { wallet: '0x3333333333333333333333333333333333333333', status: 'watch', promotion_score: 30,
    total_pl: 0.5, return_pct: 0.05, shadow_trades: 4, win_rate: 0.5, max_drawdown: 0.5,
    avg_edge: 0.10, last_simulated_trade: '2026-06-26T00:00:00' },
]
const rows = () => screen.getAllByTestId('shadow-row')

describe('ShadowPortfolioTable', () => {
  it('renders the simulated table for all wallets', () => {
    render(<ShadowPortfolioTable wallets={WALLETS} />)
    expect(screen.getByTestId('shadow-table')).toBeInTheDocument()
    expect(rows()).toHaveLength(3)
  })

  it('filters by status', () => {
    render(<ShadowPortfolioTable wallets={WALLETS} />)
    fireEvent.click(screen.getByText('Strong'))
    expect(rows()).toHaveLength(1)
    expect(within(rows()[0]).getByText('⭐ Strong')).toBeInTheDocument()
  })

  it('sorts by P/L descending by default (best first)', () => {
    render(<ShadowPortfolioTable wallets={WALLETS} />)
    expect(within(rows()[0]).getByText('+4.50')).toBeInTheDocument()
  })

  it('re-sorts by drawdown', () => {
    render(<ShadowPortfolioTable wallets={WALLETS} />)
    fireEvent.change(screen.getByLabelText('sort by'), { target: { value: 'max_drawdown' } })
    expect(within(rows()[0]).getByText('2.00')).toBeInTheDocument()  // highest drawdown first
  })

  it('renders wallet addresses as Polymarket profile links (new tab)', () => {
    render(<ShadowPortfolioTable wallets={WALLETS} />)
    const link = within(rows()[0]).getByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
    expect(link).toHaveAttribute('target', '_blank')
  })
})
