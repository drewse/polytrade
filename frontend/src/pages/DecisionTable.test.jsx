import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DecisionTable } from './LiveTrading.jsx'

const ROWS = [
  { id: 1, signal_id: 101, status: 'filled', category: 'filled', market: 'Yankees vs Red Sox',
    wallet: '0x1111111111111111111111111111111111111111', edge: 0.12, confidence: 83, production_score: 77,
    reason: 'order placed $4.00 @ 0.43', gates: { trading_enabled: true, filled: true } },
  { id: 2, signal_id: 102, status: 'skipped', category: 'low_edge', market: 'Mariners vs Guardians',
    wallet: '0x2222222222222222222222222222222222222222', edge: 0.01, confidence: 70, production_score: 60,
    reason: 'edge 0.01 < min', gates: { trading_enabled: true, edge_ok: false } },
]

describe('DecisionTable (shared decision/placed-orders feed)', () => {
  it('renders a row per decision in the decision-feed format', () => {
    render(<DecisionTable rows={ROWS} />)
    expect(screen.getByTestId('decision-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('decision-row')).toHaveLength(2)
    expect(screen.getByText('★ order placed')).toBeInTheDocument()   // the filled row's gate trail
  })

  it('placed-orders view = rows filtered to status "filled" (shows the ★ order placed trail)', () => {
    const placed = ROWS.filter((d) => d.status === 'filled')
    render(<DecisionTable rows={placed} />)
    expect(screen.getAllByTestId('decision-row')).toHaveLength(1)
    expect(screen.getByText('★ order placed')).toBeInTheDocument()
  })

  it('shows the empty text when there are no rows', () => {
    render(<DecisionTable rows={[]} emptyText="No orders placed yet." />)
    expect(screen.getByText('No orders placed yet.')).toBeInTheDocument()
  })
})
