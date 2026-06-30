import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DrewFindsReport, TargetCard, SimilarTable, OurSpecialists } from './Btc5mDrewFinds.jsx'

const report = (over = {}) => ({
  summary: '@std0 is a BTC-5m scalper (87% of flow, 250/day, avg entry 0.41, P&L 3818).',
  seed_wallet: '@std0',
  targets: [
    { address: '0xf3a6ef82d0904db48c0ad8016ca62c556fee8c6c', handle: '@std0', name: '', all_time_pnl: 3818,
      btc_5m_pct: 87, trades_per_day: 250, n_trades: 1000, buy_pct: 86, avg_price: 0.41, avg_size: 31,
      category_mix: { btc_5m_updown: 865, other: 127 }, strategy: 'BTC 5-minute up/down specialist — high-frequency scalper that buys the CHEAP side' },
    { address: '0x2c335066fe58fe9237c3d3dc7b275c2a034a0563', handle: '@0x2c33…', name: 'Substantial-Service',
      all_time_pnl: 71354, btc_5m_pct: 0, trades_per_day: 90, n_trades: 500, buy_pct: 92, avg_price: 0.6, avg_size: 75000,
      category_mix: { sports: 461 }, strategy: 'NOT a BTC-5m trader — concentrates in sports' },
  ],
  similar_btc5m_wallets: [
    { wallet: '0xc0trader0000000000000000000000000000aaaa', name: 'Scalper-X', similarity: 0.82, markets_shared: 7,
      trades: 40, buy_pct: 85, avg_price: 0.43, volume_usd: 500, all_time_pnl: 250 },
  ],
  our_indexed_specialists: [
    { wallet: '0xprof000000000000000000000000000000000001', realized_pnl: 3042, roi: 0.013, win_rate: 0.715,
      trade_count: 3400, profit_factor: 1.2, cluster: 'Momentum' },
  ],
  ...over,
})

describe('DrewFindsReport', () => {
  it('renders summary, both targets, similar + our specialists', () => {
    render(<DrewFindsReport report={report()} />)
    expect(screen.getByTestId('summary')).toHaveTextContent('BTC-5m scalper')
    expect(screen.getAllByTestId('target-card')).toHaveLength(2)
    expect(screen.getByTestId('similar-table')).toBeInTheDocument()
    expect(screen.getByTestId('our-table')).toBeInTheDocument()
  })

  it('renders empty state', () => {
    render(<DrewFindsReport report={null} />)
    expect(screen.getByText(/run the analysis/i)).toBeInTheDocument()
  })
})

describe('TargetCard', () => {
  it('shows the reverse-engineered strategy + P&L', () => {
    render(<TargetCard t={report().targets[0]} />)
    expect(screen.getByTestId('strategy')).toHaveTextContent('BTC 5-minute up/down specialist')
    expect(screen.getByTestId('target-pnl')).toHaveTextContent('$3,818')
  })
})

describe('SimilarTable', () => {
  it('renders co-trader rows with similarity + pnl', () => {
    render(<SimilarTable rows={report().similar_btc5m_wallets} />)
    expect(screen.getAllByTestId('similar-row')).toHaveLength(1)
    expect(screen.getByTestId('similar-table')).toHaveTextContent('0.82')
  })
  it('renders empty', () => {
    render(<SimilarTable rows={[]} />)
    expect(screen.getByText(/No similar BTC-5m wallets/)).toBeInTheDocument()
  })
})

describe('OurSpecialists', () => {
  it('renders indexed specialists', () => {
    render(<OurSpecialists rows={report().our_indexed_specialists} />)
    expect(screen.getAllByTestId('our-row')).toHaveLength(1)
    expect(screen.getByTestId('our-table')).toHaveTextContent('Momentum')
  })
})
