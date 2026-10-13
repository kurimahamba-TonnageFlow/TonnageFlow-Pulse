import './linetech.css'
import { LineTechStoppageScreen } from './screens/LineTechStoppageScreen'
import type { ChangeoverSelection } from './linetech'
import { parsePackFormat } from './packWeight'
import { getCasepackerStatus } from '../engineering/casepackerApi'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiRequestError } from '../../api/client'
import {
  acknowledgeFault,
  changeTargetSpeed,
  completeChangeover,
  completeRun,
  endLineStoppage,
  endPlannedDowntime,
  getHmiConfig,
  getOpenLineFaults,
  getRunState,
  previewCompletion,
  reportFault,
  startChangeover,
  startLineStoppage,
  startPlannedDowntime,
  startRun,
  submitHourlyUpdate,
} from './api'
import { clearActiveRun, loadActiveRun, saveActiveRun } from './activeRunStorage'
import {
  beginAction,
  clearPendingAction,
  loadPendingAction,
  newIdempotencyKey,
  type PendingAction,
  type PendingActionKind,
} from './idempotency'
import { DEFAULT_PLANNED_DOWNTIME_REASONS } from './constants'
import { parsePackWeightLabel } from './packWeight'
import { resolveCurrentShift } from './shift'
import { useLineStatePolling } from './useLineStatePolling'
import { HomeScreen, type HmiConfigState } from './screens/HomeScreen'
import { StartRunFormScreen } from './screens/StartRunFormScreen'
import { ReviewRunScreen } from './screens/ReviewRunScreen'
import { RunStartedScreen } from './screens/RunStartedScreen'
import { ActiveRunScreen } from './screens/ActiveRunScreen'
import { HourlyUpdateScreen } from './screens/HourlyUpdateScreen'
import { PlannedDowntimeScreen } from './screens/PlannedDowntimeScreen'
import { ChangeoverStartScreen } from './screens/ChangeoverStartScreen'
import { ChangeoverCompleteScreen } from './screens/ChangeoverCompleteScreen'
import { ReportToEngineerScreen } from './screens/ReportToEngineerScreen'
import { CompleteRunScreen } from './screens/CompleteRunScreen'
import { RunCompletedScreen } from './screens/RunCompletedScreen'
import { ExitRestartScreen } from './screens/ExitRestartScreen'
import { RecoverableErrorScreen } from './screens/RecoverableErrorScreen'
import { TargetSpeedScreen } from './screens/TargetSpeedScreen'
import { EndRunNextScreen } from './screens/EndRunNextScreen'
import { LineStoppageScreen, type ActiveLineStoppage } from './screens/LineStoppageScreen'
import { FaultHandoverScreen } from './screens/FaultHandoverScreen'
import {
  EMPTY_CHANGEOVER_FORM,
  EMPTY_COMPLETE_RUN_FORM,
  EMPTY_START_RUN_FORM,
  type ChangeoverFormValues,
  type ChangeoverStartPayload,
  type CompleteRunFormValues,
  type CompletionPayload,
  type CompletionPreviewResponse,
  type FaultReportResponse,
  type FaultReportPayload,
  type HmiLineState,
  type HourlyUpdateResponse,
  type LineFault,
  type LineStoppageKind,
  type LineStoppageResponse,
  type OpenLineStoppage,
  type RunHour,
  type RunState,
  type StartRunFormValues,
  type StoredActiveRun,
} from './types'
import {
  completeRunFormErrors,
  hasStartRunFormErrors,
  validateStartRunForm,
  type StartRunFormErrors,
} from './validation'
import './hmi.css'

type Screen =
  | 'home'
  | 'startRun'
  | 'reviewRun'
  | 'runStarted'
  | 'activeRun'
  | 'hourlyUpdate'
  | 'plannedDowntime'
  | 'changeoverStart'
  | 'changeoverComplete'
  | 'reportToEngineer'
  | 'completeRun'
  | 'runCompleted'
  | 'exitRestart'
  | 'recoverableError'
  | 'targetSpeed'
  | 'endRunNext'
  | 'lineStoppage'
  | 'faultHandover'

/** The run that End Run just closed, for the "what next?" choice. */
interface EndedRun {
  productionLine: string
  technician: string
  /** Set when the choice is made later from Home: the event covers the
   * time since this moment. */
  finishedAt?: string | null
}

interface HandoverState {
  productionLine: string
  faults: LineFault[] | null
  loadError: string | null
  technician: string
  acknowledgingId: number | null
  error: string | null
}

function stoppageFromResponse(response: LineStoppageResponse): ActiveLineStoppage {
  return {
    stoppageId: response.stoppage_id,
    linetech: !!response.changeover?.workflow,
    productionLine: response.production_line,
    kind: response.kind,
    reason: response.reason,
    startedAt: response.started_at,
    startedBy: response.started_by,
    physicalEndedAt: response.physical_ended_at ?? null,
    endedAt: response.ended_at,
    durationMinutes: response.duration_minutes,
  }
}

/** A handover, or a changeover past End Changeover, keeps running through
 * the start-run form and stops only when the new run is confirmed. */
function runsIntoNextRun(stop: OpenLineStoppage | null | undefined): stop is OpenLineStoppage {
  return (
    !!stop &&
    (stop.kind === 'handover' ||
      stop.kind === 'restart_delay' ||
      stop.kind === 'not_scheduled' ||
      (stop.kind === 'changeover' && !!stop.physical_ended_at))
  )
}

const TIMER_NOTICE: Record<'handover' | 'changeover' | 'restart_delay' | 'not_scheduled', string> = {
  handover: 'Shift handover timer is still running',
  changeover: 'Changeover timer is still running (new-run setup)',
  restart_delay: 'Restart delay timer is still running (unplanned)',
  not_scheduled: 'Not scheduled time is still being recorded',
}

const PLANNED_DOWNTIME_REASONS = DEFAULT_PLANNED_DOWNTIME_REASONS.filter(
  (reason) => reason !== 'Changeover',
)

const SCREENS_NEEDING_RUN_STATE: Screen[] = [
  'activeRun',
  'hourlyUpdate',
  'plannedDowntime',
  'changeoverStart',
  'changeoverComplete',
  'reportToEngineer',
  'completeRun',
  'targetSpeed',
]

function mapError(error: unknown, fallback: string): string {
  if (error instanceof ApiRequestError) {
    if (error.status >= 500 || error.status === 0) {
      return 'Pulse could not confirm the result. It may already have saved. Retry the same action to check safely.'
    }
    // 409 and 422 details are pre-written, operator-safe sentences.
    return error.message
  }
  return fallback
}

export function HmiScreen() {
  const navigate = useNavigate()

  const [screen, setScreen] = useState<Screen>('home')
  const [configState, setConfigState] = useState<HmiConfigState>({ status: 'loading' })
  // Authoritative, cross-device: which lines are already running. Never
  // inferred from this device's localStorage.
  const { lineState, refresh: refreshLineState } = useLineStatePolling()
  const [openingLine, setOpeningLine] = useState<string | null>(null)
  const [storedRun, setStoredRun] = useState<StoredActiveRun | null>(null)
  const [runState, setRunState] = useState<RunState | null>(null)
  const [isRestoring, setIsRestoring] = useState(false)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState<string | null>(null)

  const [formValues, setFormValues] = useState<StartRunFormValues>(EMPTY_START_RUN_FORM)
  const [formErrors, setFormErrors] = useState<StartRunFormErrors>({})
  const [isStartingRun, setIsStartingRun] = useState(false)
  const [startRunError, setStartRunError] = useState<string | null>(null)
  const startRunKeyRef = useRef<string | null>(null)

  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const [hourlyResult, setHourlyResult] = useState<HourlyUpdateResponse | null>(null)
  const [hourlyError, setHourlyError] = useState<string | null>(null)

  const [plannedDowntimeError, setPlannedDowntimeError] = useState<string | null>(null)

  const [changeoverForm, setChangeoverForm] = useState<ChangeoverFormValues>(EMPTY_CHANGEOVER_FORM)
  const [changeoverError, setChangeoverError] = useState<string | null>(null)

  const [faultResult, setFaultResult] = useState<FaultReportResponse | null>(null)
  const [faultError, setFaultError] = useState<string | null>(null)

  const [completeForm, setCompleteForm] = useState<CompleteRunFormValues>(EMPTY_COMPLETE_RUN_FORM)
  const [completePreview, setCompletePreview] = useState<CompletionPreviewResponse | null>(null)
  const [showCompleteErrors, setShowCompleteErrors] = useState(false)
  const [isPreviewing, setIsPreviewing] = useState(false)
  const [completeError, setCompleteError] = useState<string | null>(null)

  const [recoverableErrorMessage, setRecoverableErrorMessage] = useState<string | null>(null)

  const [targetSpeedError, setTargetSpeedError] = useState<string | null>(null)
  const [endedRun, setEndedRun] = useState<EndedRun | null>(null)
  const [endRunError, setEndRunError] = useState<string | null>(null)
  const [activeStoppage, setActiveStoppage] = useState<ActiveLineStoppage | null>(null)
  const [stoppageError, setStoppageError] = useState<string | null>(null)
  const [handover, setHandover] = useState<HandoverState | null>(null)
  // Line-level writes (stops, acknowledgements) have no active run, so
  // their idempotency keys are held here: one key per logical action,
  // reused on every retry until it succeeds.
  const lineKeysRef = useRef<Record<string, string>>({})

  function loadConfig(signal?: AbortSignal) {
    setConfigState({ status: 'loading' })
    getHmiConfig(signal)
      .then((data) => setConfigState({ status: 'ready', lines: data.lines }))
      .catch(() => setConfigState({ status: 'error' }))
  }

  /** Reads the authoritative state of the stored run. Never creates a
   * run, and never falls back to locally cached progress. */
  const refreshRunState = useCallback(
    async (runId: number, options: { background?: boolean } = {}) => {
      if (options.background) setIsRefreshing(true)
      try {
        const state = await getRunState(runId)
        setRunState(state)
        setRefreshError(null)

        if (state.run.status !== 'Active') {
          clearActiveRun()
          clearPendingAction()
          setStoredRun(null)
          setPendingAction(null)
          setRunState(null)
          setScreen('home')
        }
        return state
      } catch (error) {
        if (error instanceof ApiRequestError && error.status === 404) {
          clearActiveRun()
          clearPendingAction()
          setStoredRun(null)
          setPendingAction(null)
          setRunState(null)
          setRecoverableErrorMessage('That run no longer exists. Start a new run when ready.')
          setScreen('recoverableError')
          return null
        }
        if (options.background) {
          setRefreshError('Could not refresh from Pulse. The figures below may be out of date.')
          return null
        }
        throw error
      } finally {
        if (options.background) setIsRefreshing(false)
      }
    },
    [],
  )

  const restoreActiveRun = useCallback(
    async (stored: StoredActiveRun) => {
      setIsRestoring(true)
      try {
        const state = await refreshRunState(stored.runId)
        if (state && state.run.status === 'Active') setScreen('activeRun')
      } catch {
        setRecoverableErrorMessage(
          'Could not reach Pulse to restore the active run. The run is safe - try again.',
        )
        setScreen('recoverableError')
      } finally {
        setIsRestoring(false)
      }
    },
    [refreshRunState],
  )

  /** Opens a run this device may never have seen - the full state comes
   * from the backend, and the run is adopted locally only once that
   * read succeeds. */
  const openActiveRun = useCallback(
    async (runId: number, productionLine: string) => {
      setOpeningLine(productionLine)
      try {
        const state = await refreshRunState(runId)
        if (!state || state.run.status !== 'Active') {
          await refreshLineState()
          return
        }
        const stored: StoredActiveRun = { runId, productionLine }
        saveActiveRun(stored)
        setStoredRun(stored)
        setPendingAction(loadPendingAction())
        setScreen('activeRun')
      } catch {
        setRecoverableErrorMessage(
          'Could not reach Pulse to open that run. The run is safe - try again.',
        )
        setScreen('recoverableError')
      } finally {
        setOpeningLine(null)
      }
    },
    [refreshRunState, refreshLineState],
  )

  useEffect(() => {
    const controller = new AbortController()
    loadConfig(controller.signal)

    const stored = loadActiveRun()
    if (stored) {
      setStoredRun(stored)
      setPendingAction(loadPendingAction())
      void restoreActiveRun(stored)
    }

    return () => controller.abort()
  }, [restoreActiveRun])

  // ------------------------------------------------------------
  // Idempotent writes
  // ------------------------------------------------------------

  async function runWrite<T>(
    kind: PendingActionKind,
    label: string,
    payload: unknown,
    call: (key: string) => Promise<T>,
  ): Promise<T> {
    if (!storedRun) throw new Error('No active run')

    const action = beginAction(pendingAction, kind, storedRun.runId, label, payload)
    setPendingAction(action)
    setIsSubmitting(true)

    try {
      const result = await call(action.key)
      clearPendingAction()
      setPendingAction(null)
      return result
    } finally {
      setIsSubmitting(false)
    }
  }

  function cancelPending() {
    clearPendingAction()
    setPendingAction(null)
  }

  // ------------------------------------------------------------
  // Home / Start Run
  // ------------------------------------------------------------

  function openStartRunForm(lineName: string, technician = '') {
    setFormValues({
      ...EMPTY_START_RUN_FORM,
      productionLine: lineName,
      lineTechnician: technician,
      shift: resolveCurrentShift().name,
    })
    setFormErrors({})
    setStartRunError(null)
    startRunKeyRef.current = null
    setScreen('startRun')
  }

  /** Every new run starts here. Faults still open on the line from an
   * earlier run must be acknowledged and escalated first. */
  function handleStartRunFromHome(lineName: string, technician = '') {
    const state =
      lineState.status === 'ready'
        ? lineState.lines.find((line) => line.production_line === lineName)
        : undefined
    if ((state?.line_open_fault_count ?? 0) > 0) {
      setHandover({
        productionLine: lineName,
        faults: null,
        loadError: null,
        technician,
        acknowledgingId: null,
        error: null,
      })
      setScreen('faultHandover')
      loadHandoverFaults(lineName)
      return
    }
    openStartRunForm(lineName, technician)
  }

  // ------------------------------------------------------------
  // Line-level writes (no active run)
  // ------------------------------------------------------------

  async function lineWrite<T>(actionId: string, call: (key: string) => Promise<T>): Promise<T> {
    const key = lineKeysRef.current[actionId] ?? newIdempotencyKey()
    lineKeysRef.current[actionId] = key
    setIsSubmitting(true)
    try {
      const result = await call(key)
      delete lineKeysRef.current[actionId]
      return result
    } finally {
      setIsSubmitting(false)
    }
  }

  // ------------------------------------------------------------
  // Carried faults: acknowledge and escalate before a new run
  // ------------------------------------------------------------

  function loadHandoverFaults(lineName: string) {
    getOpenLineFaults(lineName)
      .then((response) =>
        setHandover((current) =>
          current && current.productionLine === lineName
            ? { ...current, faults: response.faults, loadError: null }
            : current,
        ),
      )
      .catch((error: unknown) =>
        setHandover((current) =>
          current ? { ...current, loadError: mapError(error, 'Could not load the open faults.') } : current,
        ),
      )
  }

  function acknowledgeHandoverFault(fault: LineFault, note: string) {
    if (!handover || isSubmitting) return
    const { productionLine, technician } = handover
    setHandover({ ...handover, acknowledgingId: fault.downtime_event_id, error: null })

    lineWrite(`ack:${fault.downtime_event_id}:${technician}`, (key) =>
      acknowledgeFault(
        fault.downtime_event_id,
        { production_line: productionLine, acknowledged_by: technician, note: note.trim() || null },
        key,
      ),
    )
      .then(() => loadHandoverFaults(productionLine))
      .catch((error: unknown) =>
        setHandover((current) =>
          current ? { ...current, error: mapError(error, 'Could not escalate the fault. Please try again.') } : current,
        ),
      )
      .finally(() =>
        setHandover((current) => (current ? { ...current, acknowledgingId: null } : current)),
      )
  }

  // ------------------------------------------------------------
  // End Run -> End Shift / Changeover / Other
  // ------------------------------------------------------------

  function startStoppage(kind: Exclude<LineStoppageKind, 'restart_delay'>, reason: string | null = null, casepackerRequired = false, casepackerDetails = '', changeoverSelection?: ChangeoverSelection) {
    if (!endedRun || isSubmitting) return
    setEndRunError(null)
    lineWrite(`stoppage:${endedRun.productionLine}:${kind}`, (key) =>
      startLineStoppage(
        endedRun.productionLine,
        { kind, started_by: endedRun.technician, reason, ...(changeoverSelection ? {changeover_selection:changeoverSelection} : {}), ...(kind === 'changeover' ? { casepacker_required: casepackerRequired, casepacker_details: casepackerRequired ? casepackerDetails : null } : {}) },
        key,
      ),
    )
      .then((response) => {
        setActiveStoppage(stoppageFromResponse(response))
        setStoppageError(null)
        setScreen('lineStoppage')
        if (kind === 'handover') window.dispatchEvent(new CustomEvent('pulse-shift-complete', { detail: { line: endedRun.productionLine, finishedAt: endedRun.finishedAt } }))
        void refreshLineState()
      })
      .catch((error: unknown) =>
        setEndRunError(mapError(error, 'Could not start the timer. Please try again.')),
      )
  }

  function openStoppageFromHome(line: HmiLineState) {
    const stop = line.open_stoppage
    if (!stop) return
    setActiveStoppage({
      stoppageId: stop.stoppage_id,
      linetech: stop.linetech,
      productionLine: line.production_line,
      kind: stop.kind,
      reason: stop.reason,
      startedAt: stop.started_at,
      startedBy: stop.started_by,
      physicalEndedAt: stop.physical_ended_at ?? null,
      endedAt: null,
      durationMinutes: null,
    })
    setStoppageError(null)
    setScreen('lineStoppage')
  }

  /** End Run was confirmed but nobody chose what happens next (screen
   * closed, device lost). Offer the choice again so no gap goes untimed. */
  function chooseNextStepFromHome(line: HmiLineState) {
    const ended = line.awaiting_next_step
    if (!ended) return
    setEndedRun({
      productionLine: line.production_line,
      technician: ended.line_technician ?? '',
      finishedAt: ended.finished_at,
    })
    setEndRunError(null)
    setScreen('endRunNext')
  }

  function finishStoppage(endedBy: string) {
    if (!activeStoppage || isSubmitting) return
    setStoppageError(null)
    const stoppageId = activeStoppage.stoppageId
    lineWrite(`stoppage-end:${stoppageId}`, (key) => endLineStoppage(stoppageId, { ended_by: endedBy }, key))
      .then((response) => {
        // Resolve on an Other stop: the Restart delay takes over at once.
        const stop = stoppageFromResponse(response.restart_delay ?? response)
        stop.linetech = activeStoppage.linetech
        setActiveStoppage(stop)
        void refreshLineState()
        // End Changeover: the physical work is done. The event keeps
        // running through the new-run form and stops when the run starts.
        if (stop.kind === 'changeover' && !stop.linetech) {
          void getCasepackerStatus(stop.stoppageId).then(({ request }) => {
            if (!request || request.ready_at) handleStartRunFromHome(stop.productionLine, endedBy)
          }).catch(() => setStoppageError('Changeover work recorded. Could not check Engineering readiness; refresh before starting.'))
        }
      })
      .catch((error: unknown) =>
        setStoppageError(mapError(error, 'Could not end the stop. Please try again.')),
      )
  }

  // ------------------------------------------------------------
  // Target speed (mid-run, forward only)
  // ------------------------------------------------------------

  function submitTargetSpeed(newSpeed: string, reason: string, effectiveAt?: string, supersedesId?: number) {
    if (!storedRun || !runState || isSubmitting) return
    setTargetSpeedError(null)

    runWrite('targetSpeed', `Recording operating speed ${newSpeed} packs/min`, { newSpeed, reason, effectiveAt, supersedesId, semantics: "operating" }, (key) =>
      changeTargetSpeed(
        storedRun.runId,
        { line_technician: runState.run.line_technician, new_operating_speed_ppm: newSpeed, reason, effective_at: effectiveAt, supersedes_id: supersedesId },
        key,
      ),
    )
      .then(async () => {
        await refreshRunState(storedRun.runId, { background: true })
        setScreen('activeRun')
      })
      .catch((error: unknown) =>
        setTargetSpeedError(mapError(error, 'Could not change the target speed. Please try again.')),
      )
  }

  function handleFormChange(field: keyof StartRunFormValues, value: string) {
    setFormValues((prev) => {
      const next = { ...prev, [field]: value }

      if (field === 'packWeightLabel') {
        const convertedKg = parsePackWeightLabel(value)
        next.packWeightKg = convertedKg === null ? '' : String(convertedKg)
      }
      if (field === 'packWeightLabel' || field === 'packType') {
        next.packsPerCase = parsePackFormat(next.packType, next.packWeightLabel).packsPerCase
      }

      return next
    })
  }

  function handleReview() {
    const errors = validateStartRunForm(formValues)
    const parsed = parsePackFormat(formValues.packType, formValues.packWeightLabel)
    if (parsed.error) errors.packType = parsed.error
    else if (parsed.packsPerCase !== formValues.packsPerCase) errors.packType = 'Re-enter the format to confirm packs per case.'
    setFormErrors(errors)
    if (!hasStartRunFormErrors(errors)) {
      setStartRunError(null)
      setScreen('reviewRun')
    }
  }

  function handleClearForm() {
    setFormValues({
      ...EMPTY_START_RUN_FORM,
      productionLine: formValues.productionLine,
      shift: resolveCurrentShift().name,
    })
    setFormErrors({})
  }

  function handleConfirmStartRun() {
    if (isStartingRun) return
    setIsStartingRun(true)
    setStartRunError(null)

    // The same key for every retry of THIS Start Run, so a timeout
    // retry returns the original run instead of a duplicate or a
    // misleading "line already active".
    startRunKeyRef.current = startRunKeyRef.current ?? newIdempotencyKey()

    startRun(
      {
        production_line: formValues.productionLine,
        line_technician: formValues.lineTechnician,
        shift: formValues.shift,
        customer: formValues.customer,
        product: formValues.product,
        pack_weight: formValues.packWeightLabel,
        pack_weight_kg: Number(formValues.packWeightKg),
        packs_per_case: Number(formValues.packsPerCase),
        pack_type: formValues.packType,
        ...((configState.status === "ready" && configState.lines.find(l=>l.name===formValues.productionLine)?.linetech?.enabled) || formLineState?.open_stoppage?.linetech ? {format: formValues.packType} : {}),
        cases_per_pallet: Number(formValues.casesPerPallet),
        pallets_remaining: Number(formValues.palletsRemaining),
        previous_run_completed: Number(formValues.previousRunCompleted || '0'),
      },
      startRunKeyRef.current,
    )
      .then(async (response) => {
        const stored: StoredActiveRun = {
          runId: response.run_id,
          productionLine: response.production_line,
        }
        saveActiveRun(stored)
        setStoredRun(stored)
        setFormValues(previous => ({...previous, targetSpeedPpm: response.standard_speed_ppm == null ? 'Unknown' : String(response.standard_speed_ppm)}))
        startRunKeyRef.current = null
        setActiveStoppage(null)
        void refreshLineState()
        await refreshRunState(stored.runId).catch(() => null)
        setScreen('runStarted')
      })
      .catch((error: unknown) => {
        if (
          error instanceof ApiRequestError &&
          error.status === 409 &&
          error.message.includes('already has an active')
        ) {
          // The database constraint is the final authority: Home's line
          // state can only ever be a moment out of date, so a race is
          // still possible and is settled here. Re-read the real state
          // so Home immediately shows the line as running.
          setStartRunError(
            'Another device started a run on this line first. Nothing was saved here. ' +
              'Go back to Home and open the active run.',
          )
          void refreshLineState()
          return
        }
        setStartRunError(mapError(error, 'Could not start the run. Please try again.'))
      })
      .finally(() => setIsStartingRun(false))
  }

  // ------------------------------------------------------------
  // Hourly update
  // ------------------------------------------------------------

  function submitHourly(hour: RunHour, palletsProduced: string, lossReason: string) {
    if (!storedRun || !runState || isSubmitting) return
    setHourlyError(null)

    runWrite(
      'hourlyUpdate',
      `${palletsProduced} pallets for ${hour.hour_label}`,
      { hourStart: hour.hour_start, palletsProduced, lossReason },
      (key) =>
        submitHourlyUpdate(
          storedRun.runId,
          {
            line_technician: runState.run.line_technician,
            hour_start: hour.hour_start,
            pallets_produced: palletsProduced,
            ...(lossReason ? { other_loss_reason: lossReason } : {}),
          },
          key,
        ),
    )
      .then(async (result) => {
        // Only now does the progress display change - it is refreshed
        // from the server, never incremented locally.
        setHourlyResult(result)
        await refreshRunState(storedRun.runId, { background: true })
      })
      .catch((error: unknown) =>
        setHourlyError(mapError(error, 'Could not save the update. Please try again.')),
      )
  }

  // ------------------------------------------------------------
  // Planned downtime
  // ------------------------------------------------------------

  function beginPlannedDowntime(reason: string, component?: string) {
    if (!storedRun || !runState || isSubmitting) return
    setPlannedDowntimeError(null)

    runWrite('plannedDowntimeStart', `Starting ${reason} planned downtime`, { reason, ...(component ? {component} : {}) }, (key) =>
      startPlannedDowntime(
        storedRun.runId,
        { reason, started_by: runState.run.line_technician, ...(component ? {component} : {}) },
        key,
      ),
    )
      .then(() => refreshRunState(storedRun.runId, { background: true }))
      .catch((error: unknown) =>
        setPlannedDowntimeError(
          mapError(error, 'Could not start planned downtime. Please try again.'),
        ),
      )
  }

  function finishPlannedDowntime() {
    if (!storedRun || !runState?.open_planned_downtime || isSubmitting) return
    const event = runState.open_planned_downtime
    setPlannedDowntimeError(null)

    runWrite(
      'plannedDowntimeEnd',
      `Ending ${event.reason} planned downtime`,
      { id: event.planned_downtime_id },
      (key) =>
        endPlannedDowntime(
          event.planned_downtime_id,
          { ended_by: runState.run.line_technician },
          key,
        ),
    )
      .then(async () => {
        await refreshRunState(storedRun.runId, { background: true })
        setScreen('activeRun')
      })
      .catch((error: unknown) =>
        setPlannedDowntimeError(
          mapError(error, 'Could not end planned downtime. Please try again.'),
        ),
      )
  }

  // ------------------------------------------------------------
  // Changeover
  // ------------------------------------------------------------

  function beginChangeover() {
    if (!storedRun || !runState || isSubmitting) return
    setChangeoverError(null)

    const payload = {
      line_technician: runState.run.line_technician,
      new_customer: changeoverForm.newCustomer.trim(),
      new_product: changeoverForm.newProduct.trim(),
      new_pack_weight_kg: changeoverForm.newPackWeightKg.trim(),
      new_format: changeoverForm.newFormat.trim(),
      note: changeoverForm.note.trim() || null,
    }

    runWrite('changeoverStart', 'Starting the changeover', payload, (key) =>
      startChangeover(storedRun.runId, payload, key),
    )
      .then(async () => {
        setChangeoverForm(EMPTY_CHANGEOVER_FORM)
        await refreshRunState(storedRun.runId, { background: true })
        setScreen('activeRun')
      })
      .catch((error: unknown) =>
        setChangeoverError(mapError(error, 'Could not start the changeover. Please try again.')),
      )
  }

  function finishChangeover() {
    if (!storedRun || !runState?.open_changeover || isSubmitting) return
    const changeover = runState.open_changeover
    setChangeoverError(null)

    runWrite(
      'changeoverComplete',
      'Completing the changeover',
      { id: changeover.changeover_id },
      (key) =>
        completeChangeover(
          changeover.changeover_id,
          {
            completed_by: runState.run.line_technician,
            first_acceptable_packs_confirmed: true,
          },
          key,
        ),
    )
      .then(async () => {
        await refreshRunState(storedRun.runId, { background: true })
        setScreen('activeRun')
      })
      .catch((error: unknown) =>
        setChangeoverError(mapError(error, 'Could not complete the changeover. Please try again.')),
      )
  }

  // ------------------------------------------------------------
  // Report to Engineer
  // ------------------------------------------------------------

  function submitFaultReport(input: {
    machine: string
    machineId: number | null
    buttonId: number | null
    reason: string
    note: string
    startedAt?: string
    awaitingRestart?: boolean
    restoredAt?: string
  }) {
    if (!storedRun || !runState || isSubmitting) return
    setFaultError(null)

    const payload = {
      reported_by: runState.run.line_technician,
      machine: input.machine,
      reason: input.reason,
      machine_id: input.machineId,
      button_id: input.buttonId,
      note: input.note || null,
      ...(input.awaitingRestart ? {outcome: 'resolved_waiting_restart' as const, started_at: input.startedAt} : {}),
      ...(input.restoredAt ? { outcome: 'resolved' as const, started_at: input.startedAt, restored_at: input.restoredAt } : {}),
    }

    saveFaultPayload(payload)
  }

  function saveFaultPayload(payload: FaultReportPayload) {
    if (!storedRun || isSubmitting) return
    setFaultError(null)
    runWrite('faultReport', `Reporting ${payload.reason} on ${payload.machine}`, payload, (key) =>
      reportFault(storedRun.runId, payload, key),
    )
      .then(async (result) => {
        setFaultResult(result)
        await refreshRunState(storedRun.runId, { background: true })
      })
      .catch((error: unknown) => {
        if (error instanceof ApiRequestError && error.status >= 400 && error.status < 500) {
          clearPendingAction()
          setPendingAction(null)
        }
        setFaultError(mapError(error, 'Could not report the fault. Please try again.'))
      })
  }

  // ------------------------------------------------------------
  // Complete Run
  // ------------------------------------------------------------

  function completionPayload(): CompletionPayload | null {
    if (!runState) return null

    const productionMade = completeForm.productionSinceLastUpdate === 'yes'

    return {
      line_technician: runState.run.line_technician,
      production_since_last_update: productionMade,
      other_loss_reason: completeForm.lossReason?.trim() || undefined,
      final_pallets_produced: productionMade ? completeForm.finalPallets.trim() : null,
      count_unavailable: completeForm.countUnavailable,
      xray_pack_count: completeForm.countUnavailable
        ? null
        : Number(completeForm.xrayPackCount.trim()),
      unavailable_reason: completeForm.countUnavailable
        ? completeForm.unavailableReason.trim()
        : null,
    }
  }

  function reviewCompletion() {
    if (!storedRun || isPreviewing) return

    setShowCompleteErrors(true)
    if (Object.keys(completeRunFormErrors(completeForm)).length > 0) return

    const payload = completionPayload()
    if (!payload) return

    setIsPreviewing(true)
    setCompleteError(null)

    previewCompletion(storedRun.runId, payload)
      .then(setCompletePreview)
      .catch((error: unknown) =>
        setCompleteError(mapError(error, 'Could not check these figures. Please try again.')),
      )
      .finally(() => setIsPreviewing(false))
  }

  function confirmCompleteRun() {
    if (!storedRun || isSubmitting) return
    const payload = completionPayload()
    if (!payload) return

    setCompleteError(null)

    const ended: EndedRun = {
      productionLine: storedRun.productionLine,
      technician: payload.line_technician,
    }

    runWrite('completeRun', 'Ending the run', payload, (key) =>
      completeRun(storedRun.runId, payload, key),
    )
      .then(() => {
        clearActiveRun()
        setStoredRun(null)
        setRunState(null)
        setCompleteForm(EMPTY_COMPLETE_RUN_FORM)
        setCompletePreview(null)
        setEndedRun(ended)
        setEndRunError(null)
        setScreen('endRunNext')
        void refreshLineState()
      })
      .catch((error: unknown) =>
        setCompleteError(mapError(error, 'Could not complete the run. Please try again.')),
      )
  }

  // ------------------------------------------------------------
  // Unconfirmed action recovery
  // ------------------------------------------------------------

  async function resolvePendingAction() {
    if (!pendingAction || !storedRun || !runState || isSubmitting) return
    if (pendingAction.runId !== storedRun.runId) {
      setRefreshError('This reminder belongs to another run. Open that run to check its result.')
      return
    }
    if (pendingAction.kind === 'faultReport') {
      setFaultResult(null)
      setFaultError(null)
      setScreen('reportToEngineer')
      return
    }
    const action = pendingAction
    const technician = runState.run.line_technician
    const payload = action.payload as Record<string, unknown>
    setRefreshError(null)
    try {
      await runWrite(action.kind, action.label, action.payload, async (key) => {
        switch (action.kind) {
          case 'hourlyUpdate':
            return submitHourlyUpdate(action.runId, {
              line_technician: technician,
              hour_start: payload.hourStart as string,
              pallets_produced: payload.palletsProduced as string,
              ...(payload.lossReason ? { other_loss_reason: payload.lossReason as string } : {}),
            }, key)
          case 'plannedDowntimeStart':
            return startPlannedDowntime(action.runId, { reason: payload.reason as string, started_by: technician }, key)
          case 'plannedDowntimeEnd':
            return endPlannedDowntime(payload.id as number, { ended_by: technician }, key)
          case 'changeoverStart':
            return startChangeover(action.runId, action.payload as ChangeoverStartPayload, key)
          case 'changeoverComplete':
            return completeChangeover(payload.id as number, { completed_by: technician, first_acceptable_packs_confirmed: true }, key)
          case 'targetSpeed':
            if (payload.semantics !== 'operating') throw new Error('Old target-speed request cannot be replayed as an operating setting. Review and discard it.')
            return changeTargetSpeed(action.runId, { line_technician: technician, new_operating_speed_ppm: payload.newSpeed as string, reason: payload.reason as string, effective_at: payload.effectiveAt as string | undefined, supersedes_id: payload.supersedesId as number | undefined }, key)
          case 'completeRun':
            return completeRun(action.runId, action.payload as CompletionPayload, key)
          default:
            throw new Error('This saved action cannot be retried.')
        }
      })
      if (action.kind === 'completeRun') {
        clearActiveRun()
        setStoredRun(null)
        setRunState(null)
        setEndedRun({ productionLine: storedRun.productionLine, technician })
        setEndRunError(null)
        setScreen('endRunNext')
        void refreshLineState()
      } else {
        await refreshRunState(action.runId, { background: true })
      }
    } catch (error) {
      setRefreshError(mapError(error, 'Could not confirm the saved action. Keep this reminder and retry.'))
    }
  }

  // ------------------------------------------------------------
  // Exit
  // ------------------------------------------------------------

  function handleConfirmExit() {
    clearActiveRun()
    clearPendingAction()
    setStoredRun(null)
    setRunState(null)
    setPendingAction(null)
    setScreen('home')
  }

  // ------------------------------------------------------------
  // Render
  // ------------------------------------------------------------

  const formLineState =
    lineState.status === 'ready'
      ? lineState.lines.find((line) => line.production_line === formValues.productionLine)
      : undefined
  const runningStop = formLineState?.open_stoppage
  const timerNotice = !runsIntoNextRun(runningStop)
    ? null
    : `${TIMER_NOTICE[runningStop.kind as keyof typeof TIMER_NOTICE]} on ${formValues.productionLine}. It stops when you confirm this run.`

  if (SCREENS_NEEDING_RUN_STATE.includes(screen) && !runState) {
    return (
      <div className="hmi-screen" role="status">
        <p>{isRestoring ? 'Restoring the active run…' : 'Loading…'}</p>
      </div>
    )
  }

  switch (screen) {
    case 'home':
      return (
        <HomeScreen
          configState={configState}
          lineState={lineState}
          activeRun={storedRun}
          isRestoring={isRestoring}
          openingLine={openingLine}
          onRetry={() => loadConfig()}
          onRetryLineState={() => void refreshLineState()}
          onStartRun={(lineName) => handleStartRunFromHome(lineName)}
          onOpenActiveRun={(runId, lineName) => void openActiveRun(runId, lineName)}
          onOpenStoppage={openStoppageFromHome}
          onChooseNextStep={chooseNextStepFromHome}
          onEngineering={() => navigate('/engineering')}
          onManagement={() => navigate('/management')}
        />
      )

    case 'startRun':
      return (
        <StartRunFormScreen
          linetech={configState.status === "ready" ? configState.lines.find(l=>l.name===formValues.productionLine)?.linetech : undefined}
          timerNotice={timerNotice}
          values={formValues}
          errors={formErrors}
          onChange={handleFormChange}
          onBack={() => setScreen('home')}
          onClear={handleClearForm}
          onReview={handleReview}
        />
      )

    case 'reviewRun':
      return (
        <ReviewRunScreen
          timerNotice={timerNotice}
          values={formValues}
          isSubmitting={isStartingRun}
          errorMessage={startRunError}
          onBack={() => setScreen('startRun')}
          onConfirm={handleConfirmStartRun}
        />
      )

    case 'runStarted':
      return <RunStartedScreen values={formValues} onContinue={() => setScreen('activeRun')} />

    case 'activeRun':
      return (
        <ActiveRunScreen
          linetechEnabled={configState.status === "ready" && configState.lines.find(l=>l.name===runState!.run.production_line)?.linetech?.enabled}
          state={runState!}
          isRefreshing={isRefreshing}
          refreshError={refreshError}
          pendingAction={pendingAction}
          isRetryingPending={isSubmitting}
          onRefresh={() => storedRun && refreshRunState(storedRun.runId, { background: true })}
          onResolvePending={resolvePendingAction}
          onDiscardPending={cancelPending}
          onHourlyUpdate={() => {
            setHourlyResult(null)
            setHourlyError(null)
            setScreen('hourlyUpdate')
          }}
          onPlannedDowntime={() => {
            setPlannedDowntimeError(null)
            setScreen(runState!.open_changeover ? 'changeoverComplete' : 'plannedDowntime')
          }}
          onReportToEngineer={() => {
            setFaultResult(null)
            setFaultError(null)
            setScreen('reportToEngineer')
          }}
          onChangeTargetSpeed={() => {
            setTargetSpeedError(null)
            setScreen('targetSpeed')
          }}
          onCompleteRun={() => {
            setCompleteError(null)
            setCompletePreview(null)
            setShowCompleteErrors(false)
            setScreen('completeRun')
          }}
          onExitRestart={() => setScreen('exitRestart')}
        />
      )

    case 'targetSpeed':
      return (
        <TargetSpeedScreen
          state={runState!}
          isSubmitting={isSubmitting}
          errorMessage={targetSpeedError}
          onCancel={() => {
            cancelPending()
            setScreen('activeRun')
          }}
          onSubmit={submitTargetSpeed}
        />
      )

    case 'endRunNext':
      return (
        <EndRunNextScreen
          linetechEnabled={configState.status === "ready" && configState.lines.find(l=>l.name===endedRun?.productionLine)?.linetech?.enabled}
          onLineTechChangeover={selection=>startStoppage("changeover",null,false,"",selection)}
          productionLine={endedRun?.productionLine ?? ''}
          technician={endedRun?.technician ?? ''}
          endedAt={endedRun?.finishedAt ?? null}
          isSubmitting={isSubmitting}
          errorMessage={endRunError}
          onEndShift={() => startStoppage('handover')}
          onChangeover={(required, details) => startStoppage('changeover', null, required, details)}
          onOther={(reason) => startStoppage('other', reason)}
          onNotScheduled={() => startStoppage('not_scheduled')}
        />
      )

    case 'lineStoppage':
      if (!activeStoppage) {
        setScreen('home')
        return null
      }
      if (activeStoppage.kind === 'changeover' && activeStoppage.linetech) return <LineTechStoppageScreen
        stoppage={activeStoppage} isSubmitting={isSubmitting} errorMessage={stoppageError} onEnd={finishStoppage}
        onStartNewRun={()=>{const {productionLine,startedBy}=activeStoppage;setActiveStoppage(null);handleStartRunFromHome(productionLine,startedBy??'')}}
        onHome={()=>{setActiveStoppage(null);void refreshLineState();setScreen('home')}}/>
      return (
        <LineStoppageScreen
          stoppage={activeStoppage}
          isSubmitting={isSubmitting}
          errorMessage={stoppageError}
          onEnd={finishStoppage}
          onStartNewRun={() => {
            const { productionLine, startedBy } = activeStoppage
            setActiveStoppage(null)
            // The incoming technician picks themselves after a handover.
            handleStartRunFromHome(productionLine, activeStoppage.kind === 'handover' ? '' : (startedBy ?? ''))
          }}
          onHome={() => {
            setActiveStoppage(null)
            void refreshLineState()
            setScreen('home')
          }}
        />
      )

    case 'faultHandover':
      if (!handover) {
        setScreen('home')
        return null
      }
      return (
        <FaultHandoverScreen
          productionLine={handover.productionLine}
          faults={handover.faults}
          loadError={handover.loadError}
          technician={handover.technician}
          onTechnicianChange={(name) => setHandover({ ...handover, technician: name })}
          acknowledgingId={handover.acknowledgingId}
          errorMessage={handover.error}
          onAcknowledge={acknowledgeHandoverFault}
          onRetryLoad={() => loadHandoverFaults(handover.productionLine)}
          onContinue={() => {
            const { productionLine, technician } = handover
            setHandover(null)
            void refreshLineState()
            openStartRunForm(productionLine, technician)
          }}
          onBack={() => {
            setHandover(null)
            setScreen('home')
          }}
        />
      )

    case 'hourlyUpdate':
      return (
        <HourlyUpdateScreen
          state={runState!}
          isSubmitting={isSubmitting}
          result={hourlyResult}
          errorMessage={hourlyError}
          onCancel={() => {
            cancelPending()
            setScreen('activeRun')
          }}
          onSubmit={submitHourly}
          onFaultRestored={() => storedRun && void refreshRunState(storedRun.runId, { background: true })}
          onDone={() => {
            setHourlyResult(null)
            // Stay here while another missed hour is waiting.
            const stillDue = (runState?.hours?.hours ?? []).some((hour) => hour.status === 'due')
            if (!stillDue) setScreen('activeRun')
          }}
        />
      )

    case 'plannedDowntime':
      return (
        <PlannedDowntimeScreen
          configuredReasons={configState.status === "ready" ? configState.lines.find(l=>l.name===runState!.run.production_line)?.linetech : undefined}
          reasons={[...PLANNED_DOWNTIME_REASONS]}
          activeEvent={runState!.open_planned_downtime}
          isSubmitting={isSubmitting}
          errorMessage={plannedDowntimeError}
          onStart={beginPlannedDowntime}
          onEnd={finishPlannedDowntime}
          onCancel={() => setScreen('activeRun')}
        />
      )

    case 'changeoverStart':
      return (
        <ChangeoverStartScreen
          state={runState!}
          values={changeoverForm}
          isSubmitting={isSubmitting}
          errorMessage={changeoverError}
          onChange={(field, value) => setChangeoverForm((prev) => ({ ...prev, [field]: value }))}
          onCancel={() => setScreen('plannedDowntime')}
          onStart={beginChangeover}
        />
      )

    case 'changeoverComplete':
      if (!runState!.open_changeover) {
        setScreen('activeRun')
        return null
      }
      return (
        <ChangeoverCompleteScreen
          changeover={runState!.open_changeover}
          isSubmitting={isSubmitting}
          errorMessage={changeoverError}
          onCancel={() => setScreen('activeRun')}
          onComplete={finishChangeover}
        />
      )

    case 'reportToEngineer': {
      const configLine =
        configState.status === 'ready'
          ? configState.lines.find((line) => line.name === runState!.run.production_line)
          : undefined
      return (
        <ReportToEngineerScreen
          linetech={configLine?.linetech}
          productionLine={runState!.run.production_line}
          machines={configLine?.machines ?? []}
          isSubmitting={isSubmitting}
          result={faultResult}
          errorMessage={faultError}
          onSubmit={submitFaultReport}
          pendingReport={pendingAction?.kind === 'faultReport' && pendingAction.runId === storedRun?.runId ? pendingAction.payload as FaultReportPayload : null}
          onRetry={() => {
            if (pendingAction?.kind === 'faultReport') saveFaultPayload(pendingAction.payload as FaultReportPayload)
          }}
          onCancel={() => {
            if (pendingAction?.kind !== 'faultReport') cancelPending()
            setScreen('activeRun')
          }}
          onDone={() => {
            setFaultResult(null)
            setScreen('activeRun')
          }}
        />
      )
    }

    case 'completeRun':
      return (
        <CompleteRunScreen
          state={runState!}
          values={completeForm}
          preview={completePreview}
          isPreviewing={isPreviewing}
          isSubmitting={isSubmitting}
          errorMessage={completeError}
          showErrors={showCompleteErrors}
          onChange={(field, value) => setCompleteForm((prev) => ({ ...prev, [field]: value }))}
          onCancel={() => {
            cancelPending()
            setScreen('activeRun')
          }}
          onReview={reviewCompletion}
          onBackToEdit={() => setCompletePreview(null)}
          onConfirm={confirmCompleteRun}
          onReportMissed={() => {
            setHourlyResult(null)
            setHourlyError(null)
            setScreen('hourlyUpdate')
          }}
        />
      )

    case 'runCompleted':
      return (
        <RunCompletedScreen
          productionLine={endedRun?.productionLine || formValues.productionLine || ''}
          onBackToHome={() => setScreen('home')}
        />
      )

    case 'exitRestart':
      return (
        <ExitRestartScreen
          productionLine={storedRun?.productionLine ?? ''}
          onCancel={() => setScreen('activeRun')}
          onConfirmExit={handleConfirmExit}
        />
      )

    case 'recoverableError':
      return (
        <RecoverableErrorScreen
          message={recoverableErrorMessage ?? 'Something went wrong.'}
          onRetry={() => {
            setRecoverableErrorMessage(null)
            const stored = loadActiveRun()
            if (stored) {
              setStoredRun(stored)
              void restoreActiveRun(stored)
            } else {
              setScreen('home')
              loadConfig()
            }
          }}
          onBackToHome={() => {
            setRecoverableErrorMessage(null)
            setScreen('home')
          }}
        />
      )

    default:
      return null
  }
}
