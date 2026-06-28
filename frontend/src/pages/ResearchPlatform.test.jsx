import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { StrategyTable, ChampionCard, HypothesisList, NightlyReviewCard, StrategyDrilldown } from './ResearchPlatform.jsx'

const STRATS = [
  { id: 1, name: 'Momentum Baseline', archetype: 'momentum', status: 'Champion', is_champion: true,
    is_ensemble: false, robust_score: 72.5, trades: 30, metrics: { roi: 0.3, profit_factor: 2.1, win_rate: 0.6, max_drawdown: 0.12, sharpe: 0.5 } },
  { id: 2, name: 'Consensus-2', archetype: 'consensus', status: 'Candidate', is_champion: false,
    is_ensemble: false, robust_score: 55.0, trades: 25, metrics: { roi: 0.1, profit_factor: 1.4, win_rate: 0.55, max_drawdown: 0.2, sharpe: 0.2 } },
]

describe('Research Platform — pure components', () => {
  it('StrategyTable renders rows, marks champion, and drills down on click', () => {
    const onSelect = vi.fn()
    render(<StrategyTable rows={STRATS} onSelect={onSelect} />)
    expect(screen.getByTestId('strategy-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('strategy-row')).toHaveLength(2)
    expect(screen.getByText(/★ Momentum Baseline/)).toBeInTheDocument()
    fireEvent.click(screen.getAllByTestId('strategy-row')[0])
    expect(onSelect).toHaveBeenCalledWith(1)
  })

  it('ChampionCard shows the champion metrics', () => {
    render(<ChampionCard champion={{ ...STRATS[0], description: 'Trade momentum', equity_curve: [{ equity: 100 }, { equity: 110 }] }} />)
    expect(screen.getByTestId('champion-card')).toBeInTheDocument()
    expect(screen.getByText(/★ Momentum Baseline/)).toBeInTheDocument()
    expect(screen.getByText('Profit factor')).toBeInTheDocument()
  })

  it('HypothesisList renders hypotheses with status badges', () => {
    render(<HypothesisList rows={[
      { id: 1, text: 'Momentum wins after high ATR', status: 'Confirmed', evidence: { delta: 0.2 } },
      { id: 2, text: 'Consensus 3 beats 2', status: 'Rejected', evidence: {} },
    ]} />)
    expect(screen.getAllByTestId('hypothesis-row')).toHaveLength(2)
    expect(screen.getByText('Confirmed')).toBeInTheDocument()
    expect(screen.getByText('Rejected')).toBeInTheDocument()
  })

  it('NightlyReviewCard renders the report sections', () => {
    render(<NightlyReviewCard review={{ created_at: '2026-06-27T00:00:00', summary: 'all good',
      report: { '7_champion_strategy': 'Momentum Baseline', '5_strategies_created': 9, _snapshot: {} } }} />)
    expect(screen.getByTestId('nightly-review')).toBeInTheDocument()
    expect(screen.getByText('all good')).toBeInTheDocument()
    expect(screen.getByText(/champion strategy/)).toBeInTheDocument()
  })

  it('StrategyDrilldown shows paper trades with explanations and a clickable origin wallet', () => {
    const data = {
      strategy: { ...STRATS[0], description: 'd', version: 1, params: {}, equity_curve: [],
        origin_wallets: ['0x1111111111111111111111111111111111111111'], paper_bankroll: 130 },
      lineage: { parent_id: null, children: [] },
      paper_trades: [{ id: 1, market_id: 'm1', market: 'BTC 5m #1', action: 'BUY_YES', confidence: 0.7,
        edge: 0.1, realized_pnl: 4.2, won: true, explanation: { reasons: ['trend slope +0.01 (momentum)'] } }],
    }
    render(<StrategyDrilldown data={data} onClose={() => {}} />)
    expect(screen.getByTestId('strategy-drilldown')).toBeInTheDocument()
    expect(screen.getByTestId('drilldown-trades')).toBeInTheDocument()
    expect(screen.getByText(/trend slope/)).toBeInTheDocument()
    const link = within(screen.getByTestId('strategy-drilldown')).getByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
  })
})
