import { useEffect, useRef, useState } from 'react'
import { apiClient } from '../../api/client'
import { joinUrl } from '../../utils/joinUrl'
import './reports.css'

type Kind = 'linetech' | 'engineering' | 'management'
type Saved = { id: string; type: Kind; version: number; line: string; window: { label: string }; generated_at: string }
type History = { id: string; report_type: Kind; production_line: string; version: number; generated_at: string }
const NAMES: Record<Kind, string> = { linetech: 'LineTech Shift Report', engineering: 'Engineering Shift Report', management: 'Management Shift Report' }
export type ShiftContext = { line: string; finishedAt: string }
function shiftContext(context?: ShiftContext) {
  if (!context) return { mode: 'current', day: '', shift: 'Day', line: '' }
  const moment = new Date(Date.parse(context.finishedAt) - 1)
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', hourCycle: 'h23' }).formatToParts(moment)
  const get = (key: string) => parts.find((p) => p.type === key)?.value ?? ''
  const hour = Number(get('hour')); let day = `${get('year')}-${get('month')}-${get('day')}`
  if (hour < 6) day = new Date(Date.parse(day + 'T12:00:00Z') - 86400000).toISOString().slice(0, 10)
  return { mode: 'shift', day, shift: hour >= 6 && hour < 14 ? 'Day' : hour >= 14 && hour < 22 ? 'Afternoon' : 'Night', line: context.line }
}
export function ReportsPanel({ token, initialType = 'linetech', context, onClose }: { token?: string; initialType?: Kind; context?: ShiftContext; onClose?: () => void }) {
  const initial = shiftContext(context)
  const [options, setOptions] = useState<{ report_types: Kind[]; lines: string[] } | null>(null)
  const [kind, setKind] = useState<Kind>(initialType)
  const [mode, setMode] = useState(initial.mode); const [day, setDay] = useState(initial.day)
  const [shift, setShift] = useState(initial.shift); const [line, setLine] = useState(initial.line)
  const [note, setNote] = useState(''); const [history, setHistory] = useState<History[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [saved, setSaved] = useState<Saved | null>(null); const [html, setHtml] = useState('')
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('')
  const request = useRef<{ body: string; key: string } | null>(null)
  const inFlight = useRef(false)
  useEffect(() => {
    const controller = new AbortController()
    apiClient.get<{ report_types: Kind[]; lines: string[] }>('/api/v1/reports/options', { token, signal: controller.signal }).then(setOptions).catch(() => { if (!controller.signal.aborted) setError('Could not load reporting access. Check your sign-in and try again.') })
    apiClient.get<{ reports: History[] }>('/api/v1/reports', { token, signal: controller.signal }).then((r) => { setHistory(r.reports); setHasMore(r.reports.length === 100) }).catch(() => { if (!controller.signal.aborted) setNotice('Report history is unavailable. You can retry by reopening Reports.') })
    return () => controller.abort()
  }, [token])
  async function preview(report: Saved) {
    setSaved(report); setHtml('')
    const result = await apiClient.get<{ html: string }>(`/api/v1/reports/${report.id}/preview`, { token })
    setHtml(result.html)
  }
  async function generate(revision = false) {
    if (inFlight.current) return
    inFlight.current = true; setBusy(true); setError(''); setNotice('')
    const body = JSON.stringify({ report_type: revision && saved ? saved.type : kind, line: line || null, mode, day: day || null, shift, note, revision_of: revision ? saved?.id : null })
    if (!request.current || request.current.body !== body) request.current = { body, key: crypto.randomUUID() }
    try {
      const report = await apiClient.post<Saved>('/api/v1/reports', JSON.parse(body), { token, idempotencyKey: request.current.key })
      // Preserve the saved result even if rendering fails. Retrying replays it.
      setSaved(report); await preview(report)
      const list = await apiClient.get<{ reports: History[] }>('/api/v1/reports', { token }); setHistory(list.reports); setHasMore(list.reports.length === 100)
      setNotice('Report saved. Exports use this version of the records.')
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not generate report. Production is unchanged.') }
    finally { inFlight.current = false; setBusy(false) }
  }
  async function loadMore() {
    setBusy(true); setError('')
    try {
      const list = await apiClient.get<{ reports: History[] }>(`/api/v1/reports?offset=${history.length}`, { token })
      setHistory((previous) => [...previous, ...list.reports.filter((r) => !previous.some((p) => p.id === r.id))]); setHasMore(list.reports.length === 100)
    } catch { setError('Could not load older reports. Please retry.') }
    finally { setBusy(false) }
  }
  async function retrieve(id: string) {
    setBusy(true); setError('')
    try { await preview(await apiClient.get<Saved>(`/api/v1/reports/${id}`, { token })) }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not open report.') }
    finally { setBusy(false) }
  }
  async function download(format: 'png' | 'pdf', share = false) {
    if (!saved || busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const headers: Record<string, string> = {}
      if (token) headers.Authorization = `Bearer ${token}`
      const tablet = sessionStorage.getItem('pulse.tablet.session'); if (tablet) headers['X-HMI-Session'] = tablet
      const response = await fetch(joinUrl(import.meta.env.VITE_API_BASE_URL ?? '', `/api/v1/reports/${saved.id}/export/${format}`), { headers })
      if (!response.ok) throw new Error('Export unavailable. Your saved report is safe. Please retry.')
      const blob = await response.blob(); const file = new File([blob], `pulse-${saved.type}-${saved.id}.${format}`, { type: blob.type })
      if (share && navigator.share && navigator.canShare?.({ files: [file] })) {
        try { await navigator.share({ files: [file], title: NAMES[saved.type] }); return }
        catch (e) { if (e instanceof DOMException && e.name === 'AbortError') return }
      }
      const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = file.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 60000)
      setNotice(share ? 'Sharing unavailable on this device. The file has been downloaded for you to attach.' : 'Report downloaded.')
    } catch (e) { setError(e instanceof Error ? e.message : 'Export failed. The saved report remains available.') }
    finally { setBusy(false) }
  }
  return <section className="pulse-reports" aria-label="Shift reports">
    <header><h1>Shift reports</h1>{onClose && <button onClick={onClose}>Back to workspace</button>}</header>
    <p>Reports use saved Pulse records. No production information needs to be entered again.</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <fieldset disabled={busy || !options}><legend>Generate report</legend><div className="pulse-reports__fields">
      <label>Report type<select value={kind} onChange={(e) => { setKind(e.target.value as Kind); setMode('current') }}>{(options?.report_types ?? [initialType]).map((t) => <option key={t} value={t}>{NAMES[t]}</option>)}</select></label>
      <label>Production line<select value={line} onChange={(e) => setLine(e.target.value)}><option value="">All lines</option>{options?.lines.map((l) => <option key={l}>{l}</option>)}</select></label>
      <label>Reporting period<select value={mode} onChange={(e) => setMode(e.target.value)}><option value="current">Current shift</option><option value="previous">Previous shift</option><option value="shift">Specific shift</option>{kind === 'management' && <><option value="production_day">Production day: 06:00 to 06:00</option><option value="rolling_24h">Rolling last 24 hours</option></>}</select></label>
      {(mode === 'shift' || mode === 'production_day') && <label>Shift start date<input type="date" value={day} onChange={(e) => setDay(e.target.value)} /></label>}
      {mode === 'shift' && <label>Shift<select value={shift} onChange={(e) => setShift(e.target.value)}><option value="Day">Day 06:00 - 14:00</option><option value="Afternoon">Evening 14:00 - 22:00</option><option value="Night">Night 22:00 - 06:00</option></select></label>}
      <label>Optional handover note<textarea maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} /></label>
    </div><button onClick={() => void generate()}>{busy ? 'Generating...' : 'Generate Report'}</button></fieldset>
    <div className="pulse-reports__actions" aria-live="polite">{busy && <span>Working...</span>}{saved && <><button disabled={busy} onClick={() => void download('pdf')}>Download PDF</button><button disabled={busy} onClick={() => void download('png')}>Save as Image</button><button className="pulse-reports__share" disabled={busy} onClick={() => void download('png', true)}>Share report</button><button disabled={busy} onClick={() => void generate(true)}>Generate revised report</button><button disabled={busy} onClick={() => void retrieve(saved.id)}>Reload preview</button><span>Version {saved.version} | {saved.id}</span></>}</div>
    {html && <iframe className="pulse-reports__preview" title="Shift report preview" sandbox="" srcDoc={html} />}
    {saved && html && <div className="pulse-reports__actions" aria-label="Share generated report"><button className="pulse-reports__share" disabled={busy} onClick={() => void download('png', true)}>Share report</button><p>Share the report as an image using your device's sharing options, or download it to attach.</p></div>}
    <h2>Saved report history</h2><p>Saved reports available to your credentials, newest first.</p><ul>{history.map((r) => <li key={r.id}><button disabled={busy} onClick={() => void retrieve(r.id)}>{NAMES[r.report_type]} | {r.production_line} | {new Date(r.generated_at).toLocaleString('en-GB')} | v{r.version}</button></li>)}</ul>{hasMore && <button disabled={busy} onClick={() => void loadMore()}>Load older reports</button>}
  </section>
}

export function ReportLauncher({ token, initialType = 'linetech', listenForShift = false }: { token?: string; initialType?: Kind; listenForShift?: boolean }) {
  const [open, setOpen] = useState(false); const [context, setContext] = useState<ShiftContext>(); const [prompt, setPrompt] = useState(false)
  useEffect(() => {
    if (!listenForShift) return
    const handler = (event: Event) => { setContext((event as CustomEvent<ShiftContext>).detail); setPrompt(true) }
    window.addEventListener('pulse-shift-complete', handler)
    return () => window.removeEventListener('pulse-shift-complete', handler)
  }, [listenForShift])
  if (open) return <ReportsPanel token={token} initialType={initialType} context={context} onClose={() => setOpen(false)} />
  return <div className="pulse-reports__launcher"><button onClick={() => { setContext(undefined); setOpen(true) }}>Generate Shift Report</button>{prompt && <section role="region" aria-label="Shift Complete"><h2>Shift Complete</h2><p>Would you like to generate your shift report?</p><button onClick={() => { setPrompt(false); setOpen(true) }}>Generate Report</button><button onClick={() => setPrompt(false)}>Not Now</button></section>}</div>
}
