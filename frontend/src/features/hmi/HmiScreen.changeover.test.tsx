import { getCasepackerStatus } from '../engineering/casepackerApi'
vi.mock('../engineering/casepackerApi')
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HmiScreen } from './HmiScreen'
import * as hmiApi from './api'
import { ApiRequestError } from '../../api/client'
import { saveActiveRun } from './activeRunStorage'
import {
  HMI_CONFIG,
  HOURS_ONE_DUE,
  allLinesAvailable,
  availableLine,
  lineStateResponse,
  runState,
} from './hmiTestState'
import type { CompletionPreviewResponse, LineFault, LineStoppageResponse } from './types'

vi.mock('./api')

const MOCKED = [
  'getHmiConfig',
  'getRunState',
  'getLineState',
  'previewCompletion',
  'completeRun',
  'startLineStoppage',
  'endLineStoppage',
  'getOpenLineFaults',
  'acknowledgeFault',
  'changeTargetSpeed',
] as const

afterEach(() => {
  MOCKED.forEach((name) => vi.mocked(hmiApi[name]).mockReset())
  window.localStorage.clear()
})

beforeEach(() => {
  vi.mocked(getCasepackerStatus).mockResolvedValue({ request: null })
  vi.mocked(hmiApi.getHmiConfig).mockResolvedValue(HMI_CONFIG)
  vi.mocked(hmiApi.getLineState).mockResolvedValue(allLinesAvailable())
})

function renderHmi() {
  return render(
    <MemoryRouter initialEntries={['/hmi']}>
      <Routes>
        <Route path="/hmi" element={<HmiScreen />} />
      </Routes>
    </MemoryRouter>,
  )
}

async function renderActiveRun(state = runState()) {
  saveActiveRun({ runId: 99, productionLine: 'Rovema' })
  vi.mocked(hmiApi.getRunState).mockResolvedValue(state)
  renderHmi()
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Rovema' })).toBeInTheDocument())
}

function preview(): CompletionPreviewResponse {
  return {
    production_run_id: 99, production_line: 'Rovema', total_pallets_recorded: 10.5,
    final_pallets_produced: 0, palletised_pallets: 10.5, palletised_packs: 18480, count_available: true,
    xray_pack_count: 18500, post_xray_pack_difference: 20, estimated_post_xray_waste_percent: 0.1,
    waste_status: 'estimated', waste_unavailable_reason: null, data_quality_warning: null,
    calculation_status: 'estimated', method: 'Estimated from test data.', can_complete: true,
    blocking_reason: null, saved: false,
  }
}

async function endRun() {
  vi.mocked(hmiApi.previewCompletion).mockResolvedValue(preview())
  vi.mocked(hmiApi.completeRun).mockResolvedValue({
    status: 'success', message: 'Run completed', run_id: 99, production_line: 'Rovema',
    run_status: 'Completed', xray: {} as never,
  })
  await renderActiveRun()
  fireEvent.click(screen.getByRole('button', { name: /^end run$/i }))
  fireEvent.click(screen.getByRole('radio', { name: 'No' }))
  fireEvent.change(screen.getByLabelText(/x-ray pack count/i), { target: { value: '18500' } })
  fireEvent.click(screen.getByRole('button', { name: /review/i }))
  fireEvent.click(await screen.findByRole('button', { name: /confirm end run/i }))
  await screen.findByText('✓ Run ended')
}

function stoppage(overrides: Partial<LineStoppageResponse> = {}): LineStoppageResponse {
  return {
    status: 'success', stoppage_id: 11, production_line: 'Rovema', kind: 'changeover',
    downtime_type: 'planned', reason: null, started_by: 'Liam', started_at: new Date().toISOString(),
    ended_by: null, ended_at: null, duration_minutes: null, is_active: true, ...overrides,
  }
}

// ==========================================================
// END RUN -> END SHIFT / CHANGEOVER / OTHER
// ==========================================================

describe('End Run and what happens next', () => {
  it('offers End Shift, Changeover and Other once the run is closed', async () => {
    await endRun()

    expect(screen.getByRole('heading', { name: /what happens next on rovema/i })).toBeInTheDocument()
    for (const name of [/end shift/i, /changeover/i, /^other/i]) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument()
    }
    expect(window.localStorage.getItem('pulse.hmi.activeRun.v2')).toBeNull()
  })

  it('End Shift starts a Handover on the line that the incoming technician stops with Start Run', async () => {
    const reportPrompt = vi.fn()
    window.addEventListener('pulse-shift-complete', reportPrompt)
    vi.mocked(hmiApi.startLineStoppage).mockResolvedValue(stoppage({ kind: 'handover' }))
    await endRun()

    fireEvent.click(screen.getByRole('button', { name: /end shift/i }))

    expect(await screen.findByRole('heading', { name: /shift handover — rovema/i })).toBeInTheDocument()
    const [line, payload, key] = vi.mocked(hmiApi.startLineStoppage).mock.calls[0]
    expect(line).toBe('Rovema')
    expect(payload).toEqual({ kind: 'handover', started_by: 'Liam', reason: null })
    expect(key).toMatch(/^k-/)
    expect(reportPrompt).toHaveBeenCalledTimes(1)
    window.removeEventListener('pulse-shift-complete', reportPrompt)
    expect(screen.getByText(/handed over by liam/i)).toBeInTheDocument()
    expect(screen.getByText(/not charged to either run/i)).toBeInTheDocument()
    // Nothing to end here: only the incoming run stops it.
    expect(screen.queryByRole('button', { name: /end changeover|resolve/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /start run \(incoming technician\)/i }))
    // The incoming technician picks themselves - never pre-filled with the outgoing one.
    expect(await screen.findByLabelText(/line technician/i)).toHaveValue('')
  })

  it('a repeated End Shift tap starts one handover with one idempotency key', async () => {
    let resolve: (value: LineStoppageResponse) => void = () => {}
    vi.mocked(hmiApi.startLineStoppage).mockReturnValue(new Promise((r) => (resolve = r)))
    await endRun()

    const button = screen.getByRole('button', { name: /end shift/i })
    fireEvent.click(button)
    fireEvent.click(button)
    resolve(stoppage({ kind: 'handover' }))

    expect(await screen.findByRole('heading', { name: /shift handover/i })).toBeInTheDocument()
    expect(hmiApi.startLineStoppage).toHaveBeenCalledTimes(1)
  })

  it('Changeover starts its own timer; End Changeover then asks for the new run details', async () => {
    vi.mocked(hmiApi.startLineStoppage).mockResolvedValue(stoppage())
    vi.mocked(hmiApi.endLineStoppage).mockResolvedValue(
      stoppage({ physical_ended_at: new Date().toISOString(), physical_ended_by: 'Liam' }),
    )
    await endRun()

    fireEvent.click(screen.getByRole('button', { name: /changeover/i }))
    fireEvent.click(screen.getByRole('button', { name: 'No casepacker change' }))
    fireEvent.click(screen.getByRole('button', { name: 'Start Changeover' }))

    expect(await screen.findByRole('heading', { name: /changeover — rovema/i })).toBeInTheDocument()
    const [line, payload, key] = vi.mocked(hmiApi.startLineStoppage).mock.calls[0]
    expect(line).toBe('Rovema')
    expect(payload).toEqual({ kind: 'changeover', started_by: 'Liam', reason: null, casepacker_required: false, casepacker_details: null })
    expect(key).toMatch(/^k-/)
    expect(screen.getByText(/timer keeps running until the new run starts/i)).toBeInTheDocument()

    // After End Changeover the line reports the event still open, in setup.
    const physicalEnd = new Date().toISOString()
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      lineStateResponse([
        availableLine({
          open_stoppage: {
            stoppage_id: 11, kind: 'changeover', reason: null, started_at: physicalEnd,
            started_by: 'Liam', elapsed_minutes: 25, physical_ended_at: physicalEnd,
          },
        }),
      ]),
    )
    fireEvent.click(screen.getByRole('button', { name: /end changeover/i }))

    // End Changeover goes straight to the new-run form; the event keeps running.
    expect(await screen.findByLabelText(/line technician/i)).toHaveValue('Liam')
    expect(vi.mocked(hmiApi.endLineStoppage).mock.calls[0].slice(0, 2)).toEqual([11, { ended_by: 'Liam' }])
    expect(await screen.findByText(/changeover timer is still running/i)).toBeInTheDocument()
    expect(screen.getByText(/stops when you confirm this run/i)).toBeInTheDocument()
  })

  it('Other needs a written reason before its timer starts, and counts as unplanned', async () => {
    vi.mocked(hmiApi.startLineStoppage).mockResolvedValue(
      stoppage({ kind: 'other', downtime_type: 'unplanned', reason: 'Power cut' }),
    )
    await endRun()

    fireEvent.click(screen.getByRole('button', { name: /^other/i }))
    expect(screen.getByText(/counts as unplanned downtime/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /start stop timer/i }))
    expect(await screen.findByText(/write the reason/i)).toBeInTheDocument()
    expect(hmiApi.startLineStoppage).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/why is the line stopped/i), { target: { value: 'Power cut' } })
    fireEvent.click(screen.getByRole('button', { name: /start stop timer/i }))

    expect(await screen.findByRole('heading', { name: /line stopped — rovema/i })).toBeInTheDocument()
    expect(vi.mocked(hmiApi.startLineStoppage).mock.calls[0][1]).toEqual({
      kind: 'other', started_by: 'Liam', reason: 'Power cut',
    })
    expect(screen.getByRole('button', { name: /resolve/i })).toBeInTheDocument()
  })

  it('a stop that cannot start shows the server message and keeps the choice', async () => {
    vi.mocked(hmiApi.startLineStoppage).mockRejectedValue(
      new ApiRequestError(409, 'A stop is already running on Rovema.'),
    )
    await endRun()

    fireEvent.click(screen.getByRole('button', { name: /changeover/i }))
    fireEvent.click(screen.getByRole('button', { name: 'No casepacker change' }))
    fireEvent.click(screen.getByRole('button', { name: 'Start Changeover' }))

    expect(await screen.findByText('A stop is already running on Rovema.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByRole('button', { name: /end shift/i })).toBeInTheDocument()
  })
})

describe('A stop running between runs', () => {
  it('Home shows the running changeover and reopens its timer', async () => {
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      lineStateResponse([
        availableLine({
          open_stoppage: {
            stoppage_id: 11, kind: 'changeover', reason: null, started_at: new Date().toISOString(),
            started_by: 'Liam', elapsed_minutes: 12,
          },
        }),
      ]),
    )
    renderHmi()

    const button = await screen.findByRole('button', { name: /open changeover/i })
    expect(screen.getByText('Changeover in progress')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^start run$/i })).not.toBeInTheDocument()

    fireEvent.click(button)
    expect(await screen.findByRole('heading', { name: /changeover — rovema/i })).toBeInTheDocument()
  })

  it('a handover left running lets the incoming technician Start Run from Home', async () => {
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      lineStateResponse([
        availableLine({
          open_stoppage: {
            stoppage_id: 12, kind: 'handover', reason: null, started_at: new Date().toISOString(),
            started_by: 'Liam', elapsed_minutes: 9,
          },
        }),
      ]),
    )
    renderHmi()

    expect(await screen.findByText('Shift handover')).toBeInTheDocument()
    expect(screen.getByText(/handed over by liam/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^start run$/i }))

    expect(await screen.findByText(/shift handover timer is still running/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/line technician/i)).toHaveValue('')
  })

  it('an abandoned new-run form after End Changeover is picked up again from Home', async () => {
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      lineStateResponse([
        availableLine({
          open_stoppage: {
            stoppage_id: 11, kind: 'changeover', reason: null, started_at: new Date().toISOString(),
            started_by: 'Liam', elapsed_minutes: 30, physical_ended_at: new Date().toISOString(),
          },
        }),
      ]),
    )
    renderHmi()

    expect(await screen.findByText('Changeover — new-run setup')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /enter new run details/i }))
    expect(await screen.findByText(/changeover timer is still running/i)).toBeInTheDocument()
    expect(hmiApi.endLineStoppage).not.toHaveBeenCalled()
  })

  it('a run ended without a next-step choice is flagged so the gap is not lost', async () => {
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      lineStateResponse([
        availableLine({
          awaiting_next_step: { run_id: 99, line_technician: 'Liam', finished_at: new Date().toISOString() },
        }),
      ]),
    )
    vi.mocked(hmiApi.startLineStoppage).mockResolvedValue(stoppage({ kind: 'handover' }))
    renderHmi()

    expect(await screen.findByText(/next step not chosen/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /choose next step/i }))
    fireEvent.click(await screen.findByRole('button', { name: /end shift/i }))

    await screen.findByRole('heading', { name: /shift handover/i })
    expect(vi.mocked(hmiApi.startLineStoppage).mock.calls[0][1]).toEqual({
      kind: 'handover', started_by: 'Liam', reason: null,
    })
  })
})

// ==========================================================
// CARRIED FAULTS
// ==========================================================

function fault(id: number, acknowledged: boolean): LineFault {
  return {
    downtime_event_id: id, production_run_id: 3, fault_id: 1, machine: 'Casepacker', reason: `Open cases ${id}`,
    reported_by: 'Liam', engineer: 'Aaron', engineering_status: 'Ongoing', opened_at: '2026-01-10T10:00:00Z',
    escalation_count: acknowledged ? 1 : 0, last_escalated_at: null, last_escalated_by: null, acknowledged,
  }
}

describe('Faults carried to the incoming technician', () => {
  beforeEach(() => {
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      lineStateResponse([availableLine({ line_open_fault_count: 2 })]),
    )
  })

  it('must be acknowledged and escalated, one by one, before Start Run', async () => {
    vi.mocked(hmiApi.getOpenLineFaults)
      .mockResolvedValueOnce({ production_line: 'Rovema', handover_at: null, faults: [fault(2, false), fault(3, false)], unacknowledged_count: 2 })
      .mockResolvedValueOnce({ production_line: 'Rovema', handover_at: null, faults: [fault(2, true), fault(3, false)], unacknowledged_count: 1 })
      .mockResolvedValue({ production_line: 'Rovema', handover_at: null, faults: [fault(2, true), fault(3, true)], unacknowledged_count: 0 })
    vi.mocked(hmiApi.acknowledgeFault).mockResolvedValue({ status: 'success', escalated: true, downtime_event_id: 2, escalation_count: 1 })
    renderHmi()

    expect(await screen.findByText('2 open faults to acknowledge')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^start run$/i }))

    expect(await screen.findByRole('heading', { name: /open faults on rovema/i })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /2 still to acknowledge/i })).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/your name/i), { target: { value: 'Ben' } })
    const [first] = screen.getAllByRole('button', { name: /acknowledge and escalate/i })
    fireEvent.click(first)

    await waitFor(() => expect(hmiApi.acknowledgeFault).toHaveBeenCalledTimes(1))
    const [id, payload] = vi.mocked(hmiApi.acknowledgeFault).mock.calls[0]
    expect(id).toBe(2)
    expect(payload).toEqual({ production_line: 'Rovema', acknowledged_by: 'Ben', note: null })

    await screen.findByRole('button', { name: /1 still to acknowledge/i })
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and escalate/i }))

    const proceed = await screen.findByRole('button', { name: /continue to start run/i })
    fireEvent.click(proceed)
    expect(await screen.findByLabelText(/line technician/i)).toHaveValue('Ben')
    // Escalation updates the existing faults - no fault is ever reported here.
    expect(hmiApi.reportFault).not.toHaveBeenCalled()
  })

  it('cannot acknowledge without choosing who is acknowledging', async () => {
    vi.mocked(hmiApi.getOpenLineFaults).mockResolvedValue({
      production_line: 'Rovema', handover_at: null, faults: [fault(2, false)], unacknowledged_count: 1,
    })
    renderHmi()
    fireEvent.click(await screen.findByRole('button', { name: /^start run$/i }))

    const button = await screen.findByRole('button', { name: /acknowledge and escalate/i })
    expect(button).toBeDisabled()
  })
})

// ==========================================================
// TARGET SPEED AND MISSED HOURS
// ==========================================================

describe('Target speed change', () => {
  it('needs a reason, then saves forward from now', async () => {
    vi.mocked(hmiApi.changeTargetSpeed).mockResolvedValue({
      status: 'success', change_id: 1, previous_speed_ppm: 120, new_speed_ppm: 100, reason: 'New film',
      changed_by: 'Liam', effective_at: '2026-01-12T08:20:00Z',
    })
    await renderActiveRun()

    fireEvent.click(screen.getByRole('button', { name: /record operating speed/i }))
    fireEvent.change(screen.getByLabelText(/operating speed \(packs/i), { target: { value: '100' } })
    fireEvent.click(screen.getByRole('button', { name: /save operating speed/i }))

    expect(await screen.findByText(/write why the operating setting is changing/i)).toBeInTheDocument()
    expect(hmiApi.changeTargetSpeed).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/reason for the change/i), { target: { value: 'New film' } })
    fireEvent.click(screen.getByRole('button', { name: /save operating speed/i }))

    await waitFor(() => expect(hmiApi.changeTargetSpeed).toHaveBeenCalledTimes(1))
    expect(vi.mocked(hmiApi.changeTargetSpeed).mock.calls[0].slice(0, 2)).toEqual([
      99, { line_technician: 'Liam', new_operating_speed_ppm: '100', reason: 'New film' },
    ])
  })
})

describe('End Run with a missed hour', () => {
  it('asks for the missed hour first, on its own', async () => {
    await renderActiveRun(runState({ hours: HOURS_ONE_DUE }))

    expect(screen.getByText('Hourly update due: 07:00–08:00')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^end run$/i }))

    expect(screen.getByText(/report this hour before ending the run: 07:00–08:00/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /report missed hours/i }))

    expect(screen.getByLabelText(/pallets produced 07:00–08:00/i)).toBeInTheDocument()
    expect(within(screen.getByRole('button', { name: /save 07:00–08:00/i })).getByText(/07:00–08:00/)).toBeInTheDocument()
  })
})

// ==========================================================
// OTHER -> RESTART DELAY, AND A LATE NEXT-STEP CHOICE
// ==========================================================

function otherLine(overrides = {}) {
  return lineStateResponse([
    availableLine({
      open_stoppage: {
        stoppage_id: 21, kind: 'other', reason: 'Power cut', started_at: new Date().toISOString(),
        started_by: 'Liam', elapsed_minutes: 40,
      },
      ...overrides,
    }),
  ])
}

describe('Other stop and the restart delay', () => {
  it('Resolve ends the Other stop and the Restart delay takes over until the next run', async () => {
    const resolvedAt = new Date().toISOString()
    vi.mocked(hmiApi.getLineState).mockResolvedValue(otherLine())
    vi.mocked(hmiApi.endLineStoppage).mockResolvedValue(
      stoppage({
        stoppage_id: 21, kind: 'other', downtime_type: 'unplanned', reason: 'Power cut',
        ended_at: resolvedAt, ended_by: 'Ben', duration_minutes: 40, is_active: false,
        restart_delay: stoppage({
          stoppage_id: 22, kind: 'restart_delay', downtime_type: 'unplanned', reason: 'Power cut',
          started_by: 'Ben', started_at: resolvedAt,
        }),
      }),
    )
    renderHmi()

    fireEvent.click(await screen.findByRole('button', { name: /open line stop/i }))
    expect(screen.getByText(/resolve starts the restart delay/i)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(/resolved by/i), { target: { value: 'Ben' } })
    fireEvent.click(screen.getByRole('button', { name: /^resolve$/i }))

    expect(await screen.findByRole('heading', { name: /restart delay — rovema/i })).toBeInTheDocument()
    expect(screen.getByText(/“power cut” is resolved \(by ben\)/i)).toBeInTheDocument()
    expect(screen.getByText(/not charged to any run/i)).toBeInTheDocument()
    // Nothing on this screen can end it: only the next run's start does.
    expect(screen.queryByRole('button', { name: /^resolve$/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^start run$/i })).toBeInTheDocument()
  })

  it('a repeated Resolve tap sends one request with one key', async () => {
    let resolve: (value: LineStoppageResponse) => void = () => {}
    vi.mocked(hmiApi.getLineState).mockResolvedValue(otherLine())
    vi.mocked(hmiApi.endLineStoppage).mockReturnValue(new Promise((r) => (resolve = r)))
    renderHmi()

    fireEvent.click(await screen.findByRole('button', { name: /open line stop/i }))
    fireEvent.change(screen.getByLabelText(/resolved by/i), { target: { value: 'Ben' } })
    const button = screen.getByRole('button', { name: /^resolve$/i })
    fireEvent.click(button)
    fireEvent.click(button)
    resolve(
      stoppage({
        stoppage_id: 21, kind: 'other', ended_at: new Date().toISOString(), ended_by: 'Ben', is_active: false,
        restart_delay: stoppage({ stoppage_id: 22, kind: 'restart_delay', reason: 'Power cut' }),
      }),
    )

    await screen.findByRole('heading', { name: /restart delay/i })
    expect(hmiApi.endLineStoppage).toHaveBeenCalledTimes(1)
  })

  it('an abandoned restart delay stays on Home and the start form says it is still running', async () => {
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      otherLine({
        open_stoppage: {
          stoppage_id: 22, kind: 'restart_delay', reason: 'Power cut', started_at: new Date().toISOString(),
          started_by: 'Ben', elapsed_minutes: 95,
        },
      }),
    )
    renderHmi()

    expect(await screen.findByText('Restart delay')).toBeInTheDocument()
    expect(screen.getByText(/after: power cut/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^start run$/i }))

    expect(await screen.findByText(/restart delay timer is still running \(unplanned\)/i)).toBeInTheDocument()
    expect(hmiApi.endLineStoppage).not.toHaveBeenCalled()
  })

  it('a next step left unchosen since yesterday says the choice covers all the time since', async () => {
    const yesterday = new Date(Date.now() - 26 * 60 * 60 * 1000).toISOString()
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      lineStateResponse([
        availableLine({ awaiting_next_step: { run_id: 99, line_technician: 'Liam', finished_at: yesterday } }),
      ]),
    )
    renderHmi()

    // Start Run is not offered: the decision must be made first.
    expect(await screen.findByText(/next step not chosen/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^start run$/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /choose next step/i }))

    expect(await screen.findByText(/this run ended at \d\d:\d\d on /i)).toBeInTheDocument()
    expect(screen.getByText(/covers all the time since then/i)).toBeInTheDocument()
  })
})

// ==========================================================
// NOT SCHEDULED
// ==========================================================

describe('Not scheduled', () => {
  it('is its own End Run choice: recorded until the next run, not downtime', async () => {
    vi.mocked(hmiApi.startLineStoppage).mockResolvedValue(
      stoppage({ kind: 'not_scheduled', downtime_type: 'not_scheduled' }),
    )
    await endRun()

    fireEvent.click(screen.getByRole('button', { name: /^not scheduled/i }))

    expect(await screen.findByRole('heading', { name: /not scheduled — rovema/i })).toBeInTheDocument()
    expect(vi.mocked(hmiApi.startLineStoppage).mock.calls[0][1]).toEqual({
      kind: 'not_scheduled', started_by: 'Liam', reason: null,
    })
    expect(screen.getByText(/is not planned or unplanned\s+downtime/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^resolve$/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^start run$/i })).toBeInTheDocument()
  })

  it('stays on Home and the start form says it is still being recorded', async () => {
    vi.mocked(hmiApi.getLineState).mockResolvedValue(
      lineStateResponse([
        availableLine({
          open_stoppage: {
            stoppage_id: 30, kind: 'not_scheduled', reason: null, started_at: new Date().toISOString(),
            started_by: 'Liam', elapsed_minutes: 480,
          },
        }),
      ]),
    )
    renderHmi()

    expect(await screen.findByText('Not scheduled')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^start run$/i }))
    expect(await screen.findByText(/not scheduled time is still being recorded/i)).toBeInTheDocument()
  })
})
