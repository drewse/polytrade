import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StateBanner, ExposureCards, MakerMetrics, EventLog, SessionSummary, OrdersTable } from './Btc5mLiveMaker.jsx'

const st = (over = {}) => ({
  enabled: false, has_key: false, armed: false, mode: 'shadow', kill: false, locked: false, lock_reason: null,
  live_path_reachable: false, open_exposure_usd: 0, session_realized_pnl: 0, open_orders: 0,
  caps: { per_order_usd: 3, max_exposure_usd: 8, session_loss_limit_usd: 10, queue_lifetime_s: 12 },
  experiment_budget: { max_experiment_capital_usd: 100, committed_capital_usd: 0, remaining_usd: 100,
    cumulative_realized_pnl: 0, cumulative_loss_stop_usd: 100, loss_remaining_to_lock_usd: 100 },
  metrics: { real_orders: 0, shadow_orders: 0, fills: 0, fill_probability: null,
    avg_submit_latency_ms: null, net_pnl_usd: 0 },
  ...over,
})

describe('StateBanner', () => {
  it('shows DISARMED + live path blocked by default', () => {
    render(<StateBanner s={st()} />)
    expect(screen.getByTestId('state-banner')).toHaveTextContent('DISARMED')
    expect(screen.getByTestId('state-banner')).toHaveTextContent('live path blocked')
  })
  it('shows ARMED LIVE-MONEY when live path reachable', () => {
    render(<StateBanner s={st({ armed: true, mode: 'live', enabled: true, has_key: true, live_path_reachable: true })} />)
    expect(screen.getByTestId('state-banner')).toHaveTextContent('ARMED')
    expect(screen.getByTestId('state-banner')).toHaveTextContent('LIVE-MONEY')
    expect(screen.getByTestId('state-banner')).toHaveTextContent('REACHABLE')
  })
  it('shows KILLED', () => {
    render(<StateBanner s={st({ kill: true })} />)
    expect(screen.getByTestId('state-banner')).toHaveTextContent('KILLED')
  })
  it('shows LOCKED with reason', () => {
    render(<StateBanner s={st({ locked: true, lock_reason: 'cumulative loss $100' })} />)
    expect(screen.getByTestId('state-banner')).toHaveTextContent('LOCKED')
    expect(screen.getByTestId('state-banner')).toHaveTextContent('cumulative loss $100')
  })
})

describe('ExposureCards + metrics + events', () => {
  it('renders budget + exposure', () => {
    render(<ExposureCards s={st({ open_exposure_usd: 2.8, open_orders: 1,
      experiment_budget: { max_experiment_capital_usd: 100, committed_capital_usd: 6, remaining_usd: 94, cumulative_realized_pnl: -1.2, cumulative_loss_stop_usd: 100, loss_remaining_to_lock_usd: 98.8 } })} />)
    expect(screen.getByTestId('exposure')).toHaveTextContent('$2.80')
    expect(screen.getByTestId('budget')).toHaveTextContent('$6.00')
    expect(screen.getByTestId('budget')).toHaveTextContent('$100.00')
  })
  it('renders metrics', () => {
    render(<MakerMetrics m={st({ metrics: { real_orders: 5, shadow_orders: 0, fills: 2, fill_probability: 0.4, avg_submit_latency_ms: 35, net_pnl_usd: -0.12 } }).metrics} />)
    expect(screen.getByTestId('maker-metrics')).toHaveTextContent('40%')
  })
  it('renders event log + empty', () => {
    render(<EventLog events={[{ ts: '2026-06-29T12:00:01', type: 'fill', order_client_id: 'abcd1234', payload: { price: 0.4 } }]} />)
    expect(screen.getAllByTestId('event-row')).toHaveLength(1)
    render(<EventLog events={[]} />)
    expect(screen.getByText(/No events yet/)).toBeInTheDocument()
  })
})

describe('SessionSummary + OrdersTable (decision analytics)', () => {
  const summary = {
    session_id: 3, orders_posted: 8, settled_fills: 2, fill_rate: 0.25, avg_queue_lifetime_ms: 11800,
    avg_submit_latency_ms: 42, avg_ack_latency_ms: 90, avg_realized_spread: 0.012, avg_adverse_5s: -0.004,
    net_pnl_usd: 0.35, best_quote_distance: '0.01-0.02', worst_quote_distance: '<0.005',
    counterfactual: { actual_best: 1, one_tick_higher_better: 1, one_tick_lower_better: 0, n: 2 },
    patterns: ['best quote-distance bucket: 0.01-0.02; worst: <0.005'],
    suggested_parameter_changes: ['test improve_bid (one tick higher)'],
    note: 'research dataset',
  }
  it('renders summary with patterns + suggestions', () => {
    render(<SessionSummary summary={summary} />)
    expect(screen.getByTestId('session-summary')).toHaveTextContent('25%')
    expect(screen.getByTestId('patterns')).toHaveTextContent('best quote-distance')
    expect(screen.getByTestId('suggestions')).toHaveTextContent('improve_bid')
  })
  it('renders summary empty', () => {
    render(<SessionSummary summary={null} />)
    expect(screen.getByText(/No session summary yet/)).toBeInTheDocument()
  })
  it('renders order decisions with counterfactual', () => {
    render(<OrdersTable orders={[{ client_id: 'o1', title: 'BTC Up or Down?', secs_to_resolution: 200,
      best_bid: 0.4, best_ask: 0.44, spread: 0.04, price: 0.4, estimated_edge: 0.02, status: 'filled',
      filled: true, adverse_5s: -0.005, realized_pnl: 3.6, counterfactual: { best_choice: 'one_tick_lower' } }]} />)
    expect(screen.getAllByTestId('order-row')).toHaveLength(1)
    expect(screen.getByTestId('orders-table')).toHaveTextContent('one tick lower')
  })
})
