import { ReportLauncher } from '../reports/ReportsPanel'
import { useEngineeringWrites } from './recovery'
import { RecoveryPanel } from './RecoveryPanel'
import { CasepackerQueue } from './CasepackerQueue'
import { useMemo, useState } from 'react'
import * as engineeringApi from './api'
import { ApiRequestError } from '../../api/client'
import { ConfirmDialog } from './ConfirmDialog'
import { FaultDetailPanel } from './FaultDetailPanel'
import { FaultList } from './FaultList'
import { useFaultPolling } from './useFaultPolling'
import type { CloseFaultPayload, EngineeringFault, RepairUpdatePayload } from './types'

type TabKey = 'open' | 'mine' | 'resolved'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'open', label: 'Open Production Faults' },
  { key: 'mine', label: 'My Engineering Jobs' },
  { key: 'resolved', label: 'Resolved Jobs' },
]

interface WorkspaceProps {
  token: string
  engineerName: string
  onLogout: () => void
  onSessionExpired: () => void
}

function safeActionError(error: unknown, fallback: string): string {
  return error instanceof ApiRequestError && (error.status === 0 || error.status >= 500) ? 'Save not confirmed. Use Retry original action above.' : error instanceof Error ? error.message : fallback
}

export function Workspace({ token, engineerName, onLogout, onSessionExpired }: WorkspaceProps) {
  const writes = useEngineeringWrites(engineerName, token)
  const { faults, isLoading, isRefreshing, loadError, refreshError, lastRefreshedAt, refresh } =
    useFaultPolling(token, onSessionExpired)

  const [activeTab, setActiveTab] = useState<TabKey>('open')
  const [selectedFaultId, setSelectedFaultId] = useState<number | null>(null)

  const [pendingAcceptFault, setPendingAcceptFault] = useState<EngineeringFault | null>(null)
  const [isAccepting, setIsAccepting] = useState(false)
  const [acceptError, setAcceptError] = useState<string | null>(null)

  const [isSubmittingUpdate, setIsSubmittingUpdate] = useState(false)
  const [updateError, setUpdateError] = useState<string | null>(null)
  const [updateSuccessMessage, setUpdateSuccessMessage] = useState<string | null>(null)
  const [updateResetSignal, setUpdateResetSignal] = useState(0)

  const [isSubmittingClose, setIsSubmittingClose] = useState(false)
  const [closeError, setCloseError] = useState<string | null>(null)
  const [closeSuccessMessage, setCloseSuccessMessage] = useState<string | null>(null)
  /** The close was confirmed by the backend but the follow-up read
   * failed, so the lists below are stale. Never presented as a failed
   * close. */
  const [closeRefreshFailed, setCloseRefreshFailed] = useState(false)

  const [isSubmittingHandover, setIsSubmittingHandover] = useState(false)
  const [handoverError, setHandoverError] = useState<string | null>(null)
  const [handoverSuccessMessage, setHandoverSuccessMessage] = useState<string | null>(null)

  const openFaults = useMemo(() => faults.filter((f) => f.engineering_status !== 'Resolved'), [faults])
  const myFaults = useMemo(() => openFaults.filter((f) => f.engineer === engineerName), [openFaults, engineerName])
  const resolvedFaults = useMemo(() => faults.filter((f) => f.engineering_status === 'Resolved'), [faults])

  const selectedFault = faults.find((f) => f.downtime_event_id === selectedFaultId) ?? null

  function handleLogoutClick() {
    // Best-effort server-side revocation - the in-memory token is
    // cleared by onLogout() regardless of whether this call succeeds.
    engineeringApi.logout(token).catch(() => {})
    onLogout()
  }

  function requestAccept(fault: EngineeringFault) {
    setAcceptError(null)
    setPendingAcceptFault(fault)
  }

  function confirmAccept() {
    if (!pendingAcceptFault || isAccepting) return
    setIsAccepting(true)
    setAcceptError(null)

    writes.perform('fault', pendingAcceptFault.downtime_event_id, 'accept', {}, key => engineeringApi.acceptFault(token, pendingAcceptFault.downtime_event_id, undefined, key))
      .then((response) => {
        setPendingAcceptFault(null)
        setSelectedFaultId(response.downtime_event_id)
        refresh()
      })
      .catch((error: unknown) => {
        if (error instanceof ApiRequestError && error.status === 401) {
          onSessionExpired()
          return
        }
        setAcceptError(safeActionError(error, 'Could not accept this job. Please try again.'))
      })
      .finally(() => setIsAccepting(false))
  }

  function handleSubmitUpdate(payload: RepairUpdatePayload) {
    if (!selectedFault || isSubmittingUpdate) return
    setIsSubmittingUpdate(true)
    setUpdateError(null)
    setUpdateSuccessMessage(null)

    writes.perform('fault', selectedFault.downtime_event_id, 'update', payload, key => engineeringApi.addRepairUpdate(token, selectedFault.downtime_event_id, payload, undefined, key))
      .then(() => {
        setUpdateSuccessMessage('Repair update saved.')
        setUpdateResetSignal((n) => n + 1)
        refresh()
      })
      .catch((error: unknown) => {
        if (error instanceof ApiRequestError && error.status === 401) {
          onSessionExpired()
          return
        }
        setUpdateError(safeActionError(error, 'Could not save this update. Please try again.'))
      })
      .finally(() => setIsSubmittingUpdate(false))
  }

  function handleConfirmClose(payload: CloseFaultPayload) {
    if (!selectedFault || isSubmittingClose) return
    const closing = selectedFault
    setIsSubmittingClose(true)
    setCloseError(null)
    setCloseRefreshFailed(false)

    writes.perform('fault', closing.downtime_event_id, 'close', payload, key => engineeringApi.closeFault(token, closing.downtime_event_id, payload, undefined, key))
      .then(async (response) => {
        // The backend has confirmed it. Dismiss the panel, say so, and
        // move to where the fault now lives - then take the authoritative
        // read as the source of every list and count. Nothing here edits
        // a local counter.
        setSelectedFaultId(null)
        setCloseSuccessMessage(
          `${closing.machine} — ${closing.reason} is now ${response.engineering_status}.`,
        )
        setActiveTab('resolved')

        const refreshed = await refresh()
        // A failed read after a confirmed close does not undo the close.
        setCloseRefreshFailed(!refreshed)
      })
      .catch((error: unknown) => {
        if (error instanceof ApiRequestError && error.status === 401) {
          onSessionExpired()
          return
        }
        setCloseError(safeActionError(error, 'Could not close this fault. Please try again.'))
      })
      .finally(() => setIsSubmittingClose(false))
  }

  function handleConfirmHandover(note: string) {
    if (!selectedFault || isSubmittingHandover) return
    setIsSubmittingHandover(true)
    setHandoverError(null)
    setHandoverSuccessMessage(null)

    writes.perform('fault', selectedFault.downtime_event_id, 'handover', { note }, key => engineeringApi.handOverFault(token, selectedFault.downtime_event_id, { note }, undefined, key))
      .then(() => {
        setSelectedFaultId(null)
        setActiveTab('open')
        setHandoverSuccessMessage('Job handed over. It is now unassigned in Open Production Faults.')
        refresh()
      })
      .catch((error: unknown) => {
        if (error instanceof ApiRequestError && error.status === 401) {
          onSessionExpired()
          return
        }
        setHandoverError(safeActionError(error, 'Could not hand over this job. Please try again.'))
      })
      .finally(() => setIsSubmittingHandover(false))
  }

  function closeDetailPanel() {
    setSelectedFaultId(null)
    setUpdateError(null)
    setUpdateSuccessMessage(null)
    setCloseError(null)
    setHandoverError(null)
  }

  /** Opening another fault clears the previous close confirmation, so a
   * stale "closed" note can never sit above a different fault. */
  function selectFault(downtimeEventId: number) {
    setCloseSuccessMessage(null)
    setCloseRefreshFailed(false)
    setSelectedFaultId(downtimeEventId)
  }

  const activeFaults = activeTab === 'open' ? openFaults : activeTab === 'mine' ? myFaults : resolvedFaults
  const emptyMessage =
    activeTab === 'open'
      ? 'No open production faults right now.'
      : activeTab === 'mine'
        ? 'No jobs are currently assigned to you.'
        : 'No resolved jobs yet.'

  return (
    <div className="engineering-screen engineering-workspace">
      <ReportLauncher token={token} initialType="engineering" />
      <header className="engineering-workspace__header">
        <div>
          <p className="engineering-workspace__brand">Tonnage Flow Pulse — Engineering</p>
          <p className="engineering-workspace__engineer">Signed in as {engineerName}</p>
        </div>
        <div className="engineering-workspace__header-actions">
          <span className="engineering-workspace__refreshed">
            {lastRefreshedAt
              ? `Last refreshed ${lastRefreshedAt.toLocaleTimeString()}`
              : 'Not refreshed yet'}
            {isRefreshing && ' · Refreshing…'}
          </span>
          <button type="button" className="engineering-secondary-button" onClick={refresh} disabled={isRefreshing}>
            Refresh
          </button>
          <button type="button" className="engineering-secondary-button" onClick={handleLogoutClick}>
            Log Out
          </button>
        </div>
      </header>

      <RecoveryPanel actor={engineerName} token={token} scope="fault" onSaved={() => { setSelectedFaultId(null); return refresh() }} onSessionExpired={onSessionExpired} />
      <CasepackerQueue token={token} engineerName={engineerName} onSessionExpired={onSessionExpired} />

      {refreshError && (
        <p className="engineering-inline-error" role="alert">
          {refreshError} (showing the last data loaded successfully)
        </p>
      )}

      {handoverSuccessMessage && (
        <p className="engineering-success-note" role="status">
          {handoverSuccessMessage}
        </p>
      )}

      {closeSuccessMessage && (
        <p className="engineering-success-note" role="status">
          ✓ Fault closed. {closeSuccessMessage}
        </p>
      )}

      {/* The close itself was confirmed; only the follow-up read failed,
          so this says the lists are stale rather than implying the close
          did not happen. */}
      {closeRefreshFailed && (
        <p className="engineering-inline-error" role="alert">
          The fault was closed, but the list below could not be refreshed. Press Refresh to see
          the current state.
        </p>
      )}

      <div className="engineering-summary-counts">
        <span>Open: {openFaults.length}</span>
        <span>Mine: {myFaults.length}</span>
        <span>Resolved: {resolvedFaults.length}</span>
      </div>

      <nav className="engineering-tabs" aria-label="Engineering fault sections">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            className={
              activeTab === tab.key
                ? 'engineering-tab engineering-tab--active'
                : 'engineering-tab'
            }
            aria-pressed={activeTab === tab.key}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {isLoading ? (
        <p className="engineering-empty-state">Loading Engineering faults…</p>
      ) : loadError ? (
        <div className="engineering-empty-state" role="alert">
          <p>{loadError}</p>
          <button type="button" className="engineering-secondary-button" onClick={refresh}>
            Retry
          </button>
        </div>
      ) : (
        <FaultList
          faults={activeFaults}
          emptyMessage={emptyMessage}
          canAccept={(fault) => activeTab === 'open' && fault.engineer === null}
          onSelect={(fault) => selectFault(fault.downtime_event_id)}
          onAccept={requestAccept}
        />
      )}

      {selectedFault && (
        <FaultDetailPanel
          key={selectedFault.downtime_event_id}
          fault={selectedFault}
          currentEngineer={engineerName}
          onClose={closeDetailPanel}
          onRequestAccept={() => requestAccept(selectedFault)}
          isAccepting={isAccepting}
          isSubmittingUpdate={isSubmittingUpdate}
          updateError={updateError}
          updateSuccessMessage={updateSuccessMessage}
          updateResetSignal={updateResetSignal}
          onSubmitUpdate={handleSubmitUpdate}
          isSubmittingClose={isSubmittingClose}
          closeError={closeError}
          onConfirmClose={handleConfirmClose}
          isSubmittingHandover={isSubmittingHandover}
          handoverError={handoverError}
          onConfirmHandover={handleConfirmHandover}
        />
      )}

      {pendingAcceptFault && (
        <ConfirmDialog
          title="Accept this job?"
          message={`This assigns ${pendingAcceptFault.machine} — ${pendingAcceptFault.reason} on ${pendingAcceptFault.production_line} to you (${engineerName}).`}
          confirmLabel="Accept Job"
          isBusy={isAccepting}
          onCancel={() => setPendingAcceptFault(null)}
          onConfirm={confirmAccept}
        />
      )}

      {acceptError && (
        <p className="engineering-inline-error" role="alert">
          {acceptError}
        </p>
      )}
    </div>
  )
}
