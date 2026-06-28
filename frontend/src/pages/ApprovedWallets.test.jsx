import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { ApprovedWalletsTable, DeepBackfillPanel } from './ApprovedWallets.jsx'

const WALLETS = [
  { address: '0x1111111111111111111111111111111111111111', production_rank: 1, production_rank_score: 88,
    roi: 0.25, profit_factor: 1.9, num_settled: 40, public_all_time_pnl: 12000, coverage_ratio: 0.92,
    coverage_grade: 'complete', manually_approved: true, manually_disabled: false, approval_status: 'approved',
    enabled: true, copyable: true, why_not_copyable: null },
  { address: '0x2222222222222222222222222222222222222222', production_rank: null, production_rank_score: 60,
    roi: 0.1, profit_factor: 1.4, num_settled: 25, public_all_time_pnl: -500, coverage_ratio: 0.3,
    coverage_grade: 'low', manually_approved: false, manually_disabled: true, approval_status: 'none',
    enabled: false, copyable: false, why_not_copyable: 'manually disabled (hard override)', note: 'looks like a MM' },
]

const rows = () => screen.getAllByTestId('approved-row')

describe('ApprovedWalletsTable', () => {
  it('renders all wallets', () => {
    render(<ApprovedWalletsTable wallets={WALLETS} onAction={() => {}} />)
    expect(screen.getByTestId('approved-table')).toBeInTheDocument()
    expect(rows()).toHaveLength(2)
  })

  it('shows disabled wallets clearly and as not copyable', () => {
    render(<ApprovedWalletsTable wallets={WALLETS} onAction={() => {}} />)
    const disabledRow = rows()[1]
    expect(within(disabledRow).getByTestId('disabled-badge')).toBeInTheDocument()
    expect(within(disabledRow).getByText(/manually disabled \(hard override\)/)).toBeInTheDocument()
  })

  it('Disable button fires onAction(addr, "disable") for an enabled wallet', () => {
    const onAction = vi.fn()
    render(<ApprovedWalletsTable wallets={WALLETS} onAction={onAction} />)
    fireEvent.click(within(rows()[0]).getByText('Disable'))
    expect(onAction).toHaveBeenCalledWith(WALLETS[0].address, 'disable')
  })

  it('Enable button fires onAction(addr, "enable") for a disabled wallet', () => {
    const onAction = vi.fn()
    render(<ApprovedWalletsTable wallets={WALLETS} onAction={onAction} />)
    fireEvent.click(within(rows()[1]).getByText('Enable'))
    expect(onAction).toHaveBeenCalledWith(WALLETS[1].address, 'enable')
  })

  it('Unapprove/Approve toggle reflects manual approval state', () => {
    const onAction = vi.fn()
    render(<ApprovedWalletsTable wallets={WALLETS} onAction={onAction} />)
    fireEvent.click(within(rows()[0]).getByText('Unapprove'))   // approved wallet -> remove_approval
    expect(onAction).toHaveBeenCalledWith(WALLETS[0].address, 'remove_approval')
    fireEvent.click(within(rows()[1]).getByText('Approve'))     // unapproved -> approve
    expect(onAction).toHaveBeenCalledWith(WALLETS[1].address, 'approve')
  })

  it('renders wallet profile links', () => {
    render(<ApprovedWalletsTable wallets={WALLETS} onAction={() => {}} />)
    const link = within(rows()[0]).getByRole('link')
    expect(link).toHaveAttribute('href', `https://polymarket.com/profile/${WALLETS[0].address}`)
  })
})

describe('DeepBackfillPanel', () => {
  const STATUS = {
    queued: 3, running: ['0xabc'], completed: 5, failed: 1, tracked: 12, average_coverage: 0.61,
    coverage_target: 0.85, page_size: 500, max_pages_per_run: 8,
    top_low_coverage_production: [{ address: '0x3333333333333333333333333333333333333333', coverage_ratio: 0.2, grade: 'low' }],
  }

  it('renders backfill metrics and low-coverage table', () => {
    render(<DeepBackfillPanel status={STATUS} onRun={() => {}} running={false} />)
    expect(screen.getByTestId('deep-backfill-panel')).toBeInTheDocument()
    expect(screen.getByTestId('low-coverage-table')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()  // completed
  })

  it('Run button fires onRun and disables while running', () => {
    const onRun = vi.fn()
    const { rerender } = render(<DeepBackfillPanel status={STATUS} onRun={onRun} running={false} />)
    fireEvent.click(screen.getByTestId('run-backfill'))
    expect(onRun).toHaveBeenCalled()
    rerender(<DeepBackfillPanel status={STATUS} onRun={onRun} running={true} />)
    expect(screen.getByTestId('run-backfill')).toBeDisabled()
  })
})
