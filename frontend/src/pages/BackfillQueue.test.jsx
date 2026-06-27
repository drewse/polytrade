import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { BackfillQueue } from './DiscoveryCandidates.jsx'

const STATUS = {
  pending: 7, running: 1, completed: 12, failed: 2, skipped: 3,
  currently_running: ['0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'],
  last_run: '2026-06-27T00:00:00',
  latest_errors: [{ wallet: '0xbbbbbbbb', error: 'data-api 500', at: '2026-06-27T00:00:00' }],
}
const QUEUE = [
  { wallet: '0x1111111111111111111111111111111111111111', discovery_sources: ['profit_leaderboard'],
    discovery_score: 100, backfill_priority: 100, backfill_status: 'pending', trades_imported: 0,
    stats_updated: false, backfill_error: null, last_backfill_attempt_at: null },
  { wallet: '0x2222222222222222222222222222222222222222', discovery_sources: ['top_holders'],
    discovery_score: 70, backfill_priority: 70, backfill_status: 'failed', trades_imported: 0,
    stats_updated: false, backfill_error: 'rate limited', last_backfill_attempt_at: '2026-06-27T00:00:00' },
]

describe('BackfillQueue', () => {
  it('renders status counts and the queue table', () => {
    render(<BackfillQueue status={STATUS} queue={QUEUE} onRun={() => {}} running={false} />)
    expect(screen.getByText('Pending')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()           // completed count
    expect(screen.getByTestId('backfill-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('backfill-row')).toHaveLength(2)
    expect(screen.getByText(/data-api 500/)).toBeInTheDocument() // latest error shown
  })

  it('Run Backfill Batch button triggers onRun', () => {
    const onRun = vi.fn()
    render(<BackfillQueue status={STATUS} queue={QUEUE} onRun={onRun} running={false} />)
    fireEvent.click(screen.getByTestId('run-backfill'))
    expect(onRun).toHaveBeenCalledTimes(1)
  })

  it('disables the run button while a batch is running', () => {
    render(<BackfillQueue status={STATUS} queue={QUEUE} onRun={() => {}} running={true} />)
    expect(screen.getByTestId('run-backfill')).toBeDisabled()
  })

  it('renders wallet links to Polymarket profiles', () => {
    render(<BackfillQueue status={STATUS} queue={QUEUE} onRun={() => {}} running={false} />)
    const link = within(screen.getAllByTestId('backfill-row')[0]).getByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
    expect(link).toHaveAttribute('target', '_blank')
  })
})
