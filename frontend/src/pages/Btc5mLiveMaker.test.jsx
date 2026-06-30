import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StateBanner, ExposureCards, MakerMetrics, EventLog } from './Btc5mLiveMaker.jsx'

const st = (over = {}) => ({
  enabled: false, has_key: false, armed: false, mode: 'shadow', kill: false, live_path_reachable: false,
  open_exposure_usd: 0, deployed_usd: 0, session_realized_pnl: 0, open_orders: 0,
  caps: { per_order_usd: 3, max_exposure_usd: 8, total_cap_usd: 100, session_loss_limit_usd: 10, queue_lifetime_s: 12 },
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
})

describe('ExposureCards + metrics + events', () => {
  it('renders caps + exposure', () => {
    render(<ExposureCards s={st({ open_exposure_usd: 2.8, open_orders: 1 })} />)
    expect(screen.getByTestId('exposure')).toHaveTextContent('$2.80')
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
