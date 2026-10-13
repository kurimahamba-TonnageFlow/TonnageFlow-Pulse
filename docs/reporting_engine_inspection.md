# Reporting engine: pre-implementation inspection

Inspection date: 12 October 2026. Worktree: pulse-linetech-preview. No live database changes, production writes, commits or deployment.

## Existing functionality to reuse

- src/factory_time.py: Europe/London shift, factory-day and rolling windows. Night shifts across DST are correctly seven or nine elapsed hours. Canonical evening shift name is Afternoon.
- src/hourly_reports.py / database.get_hourly_report_data: fixed-hour line/run reports, explicit no-reading and Not Scheduled states, line stop coverage and missing standards. Keep these rules intact.
- src/dashboard_reports.py / pulse_calculations.py: shared Decimal production arithmetic and consolidated totals. Do not average shift percentages or introduce a second OEE formula.
- src/engineering_api.py / engineering_auth.py: separate Engineering sessions, accepted faults, updates, fixes, setting adjustments and handover.
- src/database.py: Engineering repair update history, changeovers, casepacker requests and updates. Existing casepacker list defaults to unfinished line transitions; it must NOT be used unchanged for historical report retrieval.
- src/hmi_auth.py: tablet PIN session (device_name and expiry), or validated Management session. No server-side line or shift assignment on tablet sessions.
- frontend HmiScreen.startStoppage: End Shift begins an existing handover stoppage AFTER End Run. Reporting prompt must occur only after this succeeds, without changing timer writes or requiring report generation.
- Existing API client carries X-HMI-Session; Engineering and Management use distinct server-issued bearer sessions.

## Required scope decision

The brief asks for reports restricted to authorised lines/shifts, but current tablet sessions authorise the HMI as a whole. A selected line is a filter, not an access-control boundary. Two valid implementations exist:

1. Retain current scope: authenticated tablets can generate/retrieve LineTech summaries across the same configured lines they can already operate. Management and Engineering permissions stay separate. Report payloads are role-specific and unnecessary Engineering details are omitted from LineTech summaries.
2. Introduce assignment: persist tablet/user-to-line permissions and enforce them during report generation, history, revision and download. This adds a permissions/configuration workflow beyond the current HMI boundary.

Confirmed by the user: option 1. Keep the existing credentials and cross-line HMI scope; Engineering can also view LineTech reports; Management reports require Management credentials.

## Data gaps and reporting rules

- Fault opened_at is not necessarily the time Engineering was requested. Do not substitute it for a missing request timestamp.
- Acceptance is not a verified work-start time. Do not present accepted-to-resolved as active repair duration.
- Derive completion from the appropriate recorded Engineering completion/update evidence, not production restart alone.
- Older unresolved jobs must be fetched separately from period events. Current status and historical-period activity must be labelled distinctly.
- Actual casepacker request/acceptance/ready timestamps are available; preserve related changeover and QA references.
- Planned, unplanned and changeover intervals need a documented mutually exclusive allocation per line, with union/subtraction of overlaps and explicit exclusion of Not Scheduled time. Multiple lines produce line-minutes, not factory elapsed minutes.
- Preserve missing output vs zero vs Not Scheduled. No defaults for missing speeds or counts.

## Proposed implementation boundaries

One report service builds a validated, versioned snapshot and source references. All templates and exports consume that snapshot. Separate read-only source aggregation from a short report-save transaction. Use UUID report identifiers, authenticated retrieval and download, request idempotency and append-only revisions linked to the original report. Store report-generation/revision audit metadata atomically with each snapshot. New report tables use backend-only access and RLS; no public report URLs or production-table mutation.

Management entry point: /management/reports inside existing Management session boundary; connect Home Reports to it. Engineering and HMI open the shared report panel inside their existing session owner, avoiding loss of their in-memory authentication. End Shift remains successful if reports fail or are declined.

## Export/deployment considerations

Playwright is already pinned in requirements.txt. deploy/api.Dockerfile currently installs no browser. Server-side PNG/PDF export needs a pinned-compatible Chromium install plus system dependencies in that image; no additional PDF package is necessary. Limit concurrent export work and timeouts, prevent external resource fetching, escape all source text, and keep export failures retryable from saved snapshots. Do not install browsers or change the live container during implementation.

Detailed PDF can paginate tables with repeated headings, page numbers and report ID. Single-page PNG should explicitly identify a compact summary and point to the detailed report when records exceed its readable capacity; do not silently crop records.

## Visual references

The user subsequently supplied Tonnage Flow Shift Report Dashboard.png. The implemented reports follow its dark header/footer, light content, green LineTech, amber Engineering and purple Management accents.

## Validation plan

Focused engine tests: partial shifts, night boundary, both DST changes, weighted totals, missing/zero/Not Scheduled, overlapping stops, previous-shift completions, older outstanding jobs, changeover job durations and source references.

Storage/API tests: permissions and cross-role denials, line scope, idempotency races, immutable revisions, audit writes, export failure retaining report, missing migration handling.

UI tests: generate/history/revision, optional End Shift prompt, failure recovery, download/share fallback, protected routes and existing workflow regression suites. Local synthetic-data PNG/PDF rendering at mobile/tablet/desktop, PDF multi-page inspection and screenshot evidence for each template.

Implementation and validation are documented in [reporting_engine.md](reporting_engine.md). This file preserves the pre-implementation findings and confirmed scope decisions.
