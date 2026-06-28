import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { ApprovalQueueTable } from './WalletApprovalQueue.jsx'

const CANDS = [
  { address: '0x1111111111111111111111111111111111111111', recommendation_score: 78, public_all_time_pnl: 9000,
    roi: 0.22, profit_factor: 1.7, num_settled: 35, coverage_ratio: 0.55, coverage_grade: 'medium',
    why_recommended: 'internal ROI 22% / PF 1.7, 35 settled, public all-time +9,000',
    why_not_auto_approved: 'manual approval required (no wallet auto-promotes)', watchlisted: false },
  { address: '0x2222222222222222222222222222222222222222', recommendation_score: 40, public_all_time_pnl: null,
    roi: 0.05, profit_factor: 1.3, num_settled: 22, coverage_ratio: 0.1, coverage_grade: 'low',
    why_recommended: 'internal ROI 5% / PF 1.3, 22 settled',
    why_not_auto_approved: 'coverage below target — request deeper backfill; public stats not yet fetched; manual approval required (no wallet auto-promotes)',
    watchlisted: true },
]

const rows = () => screen.getAllByTestId('queue-row')

describe('ApprovalQueueTable', () => {
  it('renders all candidates with their warnings', () => {
    render(<ApprovalQueueTable candidates={CANDS} onAction={() => {}} />)
    expect(screen.getByTestId('queue-table')).toBeInTheDocument()
    expect(rows()).toHaveLength(2)
    expect(screen.getByText(/coverage below target/)).toBeInTheDocument()
  })

  it('shows the empty state when nothing qualifies', () => {
    render(<ApprovalQueueTable candidates={[]} onAction={() => {}} />)
    expect(screen.getByText(/No wallets currently meet/)).toBeInTheDocument()
  })

  it('Approve fires onAction(addr, "approve")', () => {
    const onAction = vi.fn()
    render(<ApprovalQueueTable candidates={CANDS} onAction={onAction} />)
    fireEvent.click(within(rows()[0]).getByText('Approve'))
    expect(onAction).toHaveBeenCalledWith(CANDS[0].address, 'approve')
  })

  it('Reject fires onAction(addr, "reject")', () => {
    const onAction = vi.fn()
    render(<ApprovalQueueTable candidates={CANDS} onAction={onAction} />)
    fireEvent.click(within(rows()[0]).getByText('Reject'))
    expect(onAction).toHaveBeenCalledWith(CANDS[0].address, 'reject')
  })

  it('Watchlist fires onAction(addr, "watchlist")', () => {
    const onAction = vi.fn()
    render(<ApprovalQueueTable candidates={CANDS} onAction={onAction} />)
    fireEvent.click(within(rows()[0]).getByText('Watchlist'))
    expect(onAction).toHaveBeenCalledWith(CANDS[0].address, 'watchlist')
  })

  it('Deeper backfill fires onAction(addr, "request_backfill")', () => {
    const onAction = vi.fn()
    render(<ApprovalQueueTable candidates={CANDS} onAction={onAction} />)
    fireEvent.click(within(rows()[0]).getByText('Deeper backfill'))
    expect(onAction).toHaveBeenCalledWith(CANDS[0].address, 'request_backfill')
  })

  it('marks watchlisted candidates', () => {
    render(<ApprovalQueueTable candidates={CANDS} onAction={() => {}} />)
    expect(within(rows()[1]).getByText(/watch/)).toBeInTheDocument()
  })

  it('renders wallet profile links', () => {
    render(<ApprovalQueueTable candidates={CANDS} onAction={() => {}} />)
    const link = within(rows()[0]).getByRole('link')
    expect(link).toHaveAttribute('href', `https://polymarket.com/profile/${CANDS[0].address}`)
  })
})
