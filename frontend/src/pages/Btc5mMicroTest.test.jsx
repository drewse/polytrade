import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MicroTestPanel } from './Btc5mMicroTest.jsx'

const baseStatus = (over = {}) => ({
  enabled: true, armed: false, stopped: false, stop_reason: null, armed_by: null,
  config: {
    primary_wallet: '0x4c9497941333332d29f1c235dd23200f3623ffad',
    backup_wallets: ['0xd9013df863c1ba932780857b020dfdeacedf8e14'],
    fixed_shares: 5, max_entry_price: 0.6, max_concurrent: 1, daily_loss_stop: 10,
    total_loss_stop: 15, min_seconds_remaining: 30, allowed_regimes: ['Hybrid', 'Liquidity Spike'],
    require_confidence: false, min_confidence: 0.85, max_trades: 20, expected_max_loss_per_trade: 3.0,
  },
  active_position: null, test_trades: 0, open_positions: 0, win_rate: 0, realized_pnl: 0,
  unrealized_pnl: 0, paper_realized_pnl: 0, paper_vs_live_delta: 0, max_loss_remaining: 15,
  day_loss_remaining: 10, trades_remaining: 20, last_signal: null, last_rejection: null,
  recent_trades: [], safety: 'isolated micro-test', ...over,
})

describe('MicroTestPanel', () => {
  it('renders disabled state when not enabled', () => {
    render(<MicroTestPanel status={baseStatus({ enabled: false })} />)
    expect(screen.getAllByTestId('mt-state')[0]).toHaveTextContent('disabled')
  })

  it('shows armed badge and disarm button when armed', () => {
    render(<MicroTestPanel status={baseStatus({ armed: true })} />)
    expect(screen.getAllByTestId('mt-state')[0]).toHaveTextContent('armed')
    expect(screen.getByTestId('disarm-btn')).toBeInTheDocument()
  })

  it('shows a stop banner and requires re-arm when stopped', () => {
    render(<MicroTestPanel status={baseStatus({ stopped: true, stop_reason: 'total test loss stop ($15) hit' })} />)
    expect(screen.getByTestId('stop-banner')).toHaveTextContent('total test loss stop')
  })

  it('disables the Arm button when env-disabled', () => {
    render(<MicroTestPanel status={baseStatus({ enabled: false })} />)
    expect(screen.getByTestId('arm-btn')).toBeDisabled()
  })

  it('Run paper cycle calls onRunPaper', () => {
    const onRunPaper = vi.fn()
    render(<MicroTestPanel status={baseStatus()} onRunPaper={onRunPaper} />)
    fireEvent.click(screen.getByTestId('run-paper'))
    expect(onRunPaper).toHaveBeenCalled()
  })

  it('Arm calls onArm; Run LIVE is disabled until armed', () => {
    const onArm = vi.fn()
    render(<MicroTestPanel status={baseStatus()} onArm={onArm} />)
    fireEvent.click(screen.getByTestId('arm-btn'))
    expect(onArm).toHaveBeenCalled()
    expect(screen.getByTestId('run-live')).toBeDisabled()    // not armed
  })

  it('Run LIVE enabled and calls onRunLive when armed', () => {
    const onRunLive = vi.fn()
    render(<MicroTestPanel status={baseStatus({ armed: true })} onRunLive={onRunLive} />)
    const btn = screen.getByTestId('run-live')
    expect(btn).not.toBeDisabled()
    fireEvent.click(btn)
    expect(onRunLive).toHaveBeenCalled()
  })

  it('renders the active position and the configured primary wallet link', () => {
    const status = baseStatus({
      active_position: { id: 1, market: 'Bitcoin Up or Down 5m', market_id: '0xabc', direction: 'YES',
        wallet: '0x4c9497941333332d29f1c235dd23200f3623ffad', role: 'primary', reference_price: 0.5,
        fill_price: 0.5, shares: 5, size_usd: 2.5, status: 'open' },
    })
    render(<MicroTestPanel status={status} />)
    expect(screen.getByTestId('active-table')).toBeInTheDocument()
    const link = within(screen.getByTestId('active-table')).getByRole('link')
    expect(link).toHaveAttribute('href', `https://polymarket.com/profile/${status.active_position.wallet}`)
  })

  it('renders recent trades with paper-vs-live P/L', () => {
    const status = baseStatus({
      recent_trades: [{ id: 9, created_at: '2026-06-28T01:00:00', market: 'BTC 5m', market_id: '0xa',
        direction: 'YES', wallet: '0x4c94', executor: 'paper', reference_price: 0.5, fill_price: 0.5,
        size_usd: 2.5, status: 'closed', fill_outcome: 'paper', realized_pnl: 2.5, paper_realized_pnl: 2.5 }],
    })
    render(<MicroTestPanel status={status} />)
    expect(screen.getByTestId('trades-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('trade-row')).toHaveLength(1)
  })
})
