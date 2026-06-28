import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ChallengerTable, ComparisonTable, ExperimentFeed, ExperimentDrilldown, RegimeHeatmap, RecommendationList } from './ChallengerLab.jsx'

describe('Paper Challenger Lab — pure components', () => {
  it('ChallengerTable renders rows, marks champion, drills down', () => {
    const onSelect = vi.fn()
    render(<ChallengerTable rows={[
      { key: 'size_double', name: 'Double position', kind: 'sizing', is_champion: true, is_production: false, trades: 54,
        metrics: { roi: 0.3, profit_factor: 1.8, win_rate: 0.6 }, vs_production: { mean_improvement: 6.6, significance: 'Significant' }, robust_score: 100 },
      { key: 'production', name: 'Production', kind: 'production', is_production: true, trades: 54,
        metrics: { roi: 0.1, profit_factor: 1.2, win_rate: 0.55 }, vs_production: {}, robust_score: 60 },
    ]} onSelect={onSelect} />)
    expect(screen.getByTestId('challenger-table')).toBeInTheDocument()
    expect(screen.getByText(/★ Double position/)).toBeInTheDocument()
    expect(screen.getByText('Significant')).toBeInTheDocument()
    fireEvent.click(screen.getAllByTestId('challenger-row')[0])
    expect(onSelect).toHaveBeenCalledWith('size_double')
  })

  it('ComparisonTable shows variant metrics', () => {
    render(<ComparisonTable rows={[
      { key: 'timing_+5', name: 'Entry +5s', trades: 40, roi: 0.2, profit_factor: 1.5, sharpe: 0.4, max_drawdown: 0.2, vs_production: { mean_improvement: 0.3, significance: 'Promising' } },
    ]} />)
    expect(screen.getByTestId('comparison-table')).toBeInTheDocument()
    expect(screen.getByText('Entry +5s')).toBeInTheDocument()
  })

  it('ExperimentFeed renders + drills down', () => {
    const onSelect = vi.fn()
    render(<ExperimentFeed rows={[
      { id: 7, market: 'Bitcoin Up or Down 5m #7', market_id: 'm7', regime: 'Liquidity Spike', outcome: 'YES', winner: 'size_double', improvement: 4.2, created_at: '2026-06-28T00:00:00' },
    ]} onSelect={onSelect} />)
    expect(screen.getByTestId('experiment-table')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('experiment-row'))
    expect(onSelect).toHaveBeenCalledWith(7)
  })

  it('ExperimentDrilldown ranks challenger decisions with the winner highlighted', () => {
    render(<ExperimentDrilldown data={{
      id: 7, market: 'm7', regime: 'Trend', outcome: 'YES', winner: 'size_double',
      challenger_decisions: { production: { action: 'BUY_YES', pnl: 1.0, won: true, entry_price: 0.4, size: 5 },
        size_double: { action: 'BUY_YES', pnl: 2.0, won: true, entry_price: 0.4, size: 10 } },
    }} onClose={() => {}} />)
    expect(screen.getByTestId('experiment-drilldown')).toBeInTheDocument()
    expect(screen.getByTestId('decisions-table')).toBeInTheDocument()
    expect(screen.getByText(/★ size_double/)).toBeInTheDocument()
  })

  it('RegimeHeatmap renders a grid', () => {
    render(<RegimeHeatmap data={{ regimes: ['Trend', 'Whipsaw'], challengers: [{ key: 'timing_+5', name: 'Entry +5s', by_regime: { Trend: 4.1, Whipsaw: -2.7 } }] }} />)
    expect(screen.getByTestId('regime-heatmap')).toBeInTheDocument()
    expect(screen.getByTestId('regime-row')).toBeInTheDocument()
  })

  it('RecommendationList renders recommendations with significance', () => {
    render(<RecommendationList rows={[
      { category: 'timing', text: "'Entry +5s' has outperformed production by 6.2% over 824 trades.", significance: 'Significant' },
    ]} />)
    expect(screen.getByTestId('rec-list')).toBeInTheDocument()
    expect(screen.getByText(/outperformed production/)).toBeInTheDocument()
    expect(screen.getByText('Significant')).toBeInTheDocument()
  })
})
