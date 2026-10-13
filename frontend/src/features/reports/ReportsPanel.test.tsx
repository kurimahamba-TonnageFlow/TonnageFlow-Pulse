import { cleanup, fireEvent, render, screen, waitFor, act } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ReportsPanel, ReportLauncher } from './ReportsPanel'
import { apiClient } from '../../api/client'
vi.mock('../../api/client', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }))
const saved = { id: '00000000-0000-4000-8000-000000000001', type: 'linetech', line: 'Rovema', version: 1, window: { label: 'Day' }, generated_at: '2026-01-12T14:00:00Z' }
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(apiClient.get).mockImplementation(async (path) => {
    if (path.endsWith('/options')) return { report_types: ['linetech'], lines: ['Rovema'] }
    if (path.endsWith('/preview')) return { html: '<h1>Saved report preview</h1>' }
    if (path === '/api/v1/reports') return { reports: [] }
    return saved
  })
  vi.mocked(apiClient.post).mockResolvedValue(saved)
})
afterEach(cleanup)
it('offers only server-authorised templates and keeps the preview sandboxed', async () => {
  render(<ReportsPanel />)
  await waitFor(() => expect(screen.getByText('Generate Report')).not.toBeDisabled())
  expect(screen.queryByRole('option', { name: 'Management Shift Report' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('Generate Report'))
  const preview = await screen.findByTitle('Shift report preview')
  expect(preview).toHaveAttribute('sandbox', '')
  expect(apiClient.post).toHaveBeenCalledTimes(1)
})
it('reuses the original request key after a generation failure', async () => {
  vi.mocked(apiClient.post).mockRejectedValueOnce(new Error('Temporary failure'))
  render(<ReportsPanel />)
  await waitFor(() => expect(screen.getByText('Generate Report')).not.toBeDisabled())
  fireEvent.click(screen.getByText('Generate Report'))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByText('Generate Report'))
  await screen.findByTitle('Shift report preview')
  expect(vi.mocked(apiClient.post).mock.calls[0][2]?.idempotencyKey).toBe(vi.mocked(apiClient.post).mock.calls[1][2]?.idempotencyKey)
})
it('preserves a saved report when preview rendering fails', async () => {
  vi.mocked(apiClient.get).mockImplementation(async (path) => {
    if (path.endsWith('/options')) return { report_types: ['linetech'], lines: ['Rovema'] }
    if (path.endsWith('/preview')) throw new Error('Preview unavailable')
    return { reports: [] }
  })
  render(<ReportsPanel />)
  await waitFor(() => expect(screen.getByText('Generate Report')).not.toBeDisabled())
  fireEvent.click(screen.getByText('Generate Report'))
  await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: 'Reload preview' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Download PDF' })).toBeInTheDocument()
})
it('End Shift prompt is optional and Not Now makes no report request', () => {
  render(<ReportLauncher listenForShift />)
  act(() => { window.dispatchEvent(new CustomEvent('pulse-shift-complete', { detail: { line: 'Rovema', finishedAt: '2026-01-12T06:00:00Z' } })) })
  expect(screen.getByRole('region', { name: 'Shift Complete' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Not Now' }))
  expect(apiClient.post).not.toHaveBeenCalled()
  expect(screen.queryByRole('region', { name: 'Shift Complete' })).not.toBeInTheDocument()
})
it('End Shift uses the completed overnight shift at the 06:00 boundary', async () => {
  render(<ReportLauncher listenForShift />)
  act(() => { window.dispatchEvent(new CustomEvent('pulse-shift-complete', { detail: { line: 'Rovema', finishedAt: '2026-01-12T06:00:00Z' } })) })
  fireEvent.click(screen.getByRole('button', { name: 'Generate Report' }))
  await waitFor(() => expect(screen.getByText('Generate Report')).not.toBeDisabled())
  expect(screen.getByLabelText('Shift')).toHaveValue('Night')
  expect(screen.getByLabelText('Shift start date')).toHaveValue('2026-01-11')
  expect(screen.getByLabelText('Production line')).toHaveValue('Rovema')
})
