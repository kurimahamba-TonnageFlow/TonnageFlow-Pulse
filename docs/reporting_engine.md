# Shift reporting engine - release notes

Status: implemented in the isolated `pulse-linetech-preview` worktree. Not committed, pushed or deployed. No live database changes were made for this feature.

## What staff can do

- HMI: open Generate Shift Report, or accept the optional prompt after a successful End Shift. Not Now leaves the successful handover unchanged.
- Engineering: generate a combined Engineering report or a LineTech report using the existing Engineering session. Older unresolved jobs appear in handover, even when raised before the selected shift.
- Management: Home > Reports or Management > Shift reports. Generate any template for a shift; Management also supports the production day (06:00 to 06:00) and rolling last 24 hours.
- Preview, download a detailed PDF, save a compact PNG, or share the image through native device sharing. Download is the fallback.
- Reopen saved history, load older pages, or generate a numbered revision. Originals never change.

Credentials and line scope are unchanged. Tablet sessions retain their existing cross-line HMI access. Management reports require Management credentials on every endpoint, including direct report IDs, previews, revisions and exports.

## Architecture and calculations

`src/shift_reports.py` reads existing records within one repeatable-read, read-only source transaction. It reuses factory-time boundaries, fixed-hour reporting and the existing dashboard OEE engine. No production, fault, shift or downtime writes are made by reports.

`report_models.py` validates the persisted contract. `report_store.py` atomically saves an immutable snapshot and its audit record. Request keys prevent duplicate reports; revision locks allocate a new version without replacing the original. `report_render.py` renders the same saved snapshot into HTML, PDF and PNG. HTML text is escaped, previews are sandboxed, exports cannot fetch external resources, and one export per API process is allowed at a time.

Fixed-shift output uses existing clock-hour packs and target calculations. Missing readings are not zero; Not Scheduled remains a separate state. Output vs Target is produced / expected packs, not OEE. Management OEE uses the established Availability x Performance x Quality implementation and its explicit provisional quality/output labels. It is not an average of shift percentages. When output cards and OEE have different source coverage, OEE is withheld with a review message rather than presented as a consolidated figure.

Rolling reports use the existing dashboard period-end membership. The chart and totals include the same whole recorded periods; counts are never invented by prorating a boundary reading. The report explains this distinction from a clock-hour shift report.

Downtime is clipped to the reporting window and allocated once per line: changeover first, planned second, then unplanned. Not Scheduled is excluded. Across multiple lines these are line-minutes. Individual event durations can overlap; allocated category totals do not. Running time is labelled as time not recorded as stopped, not a verified machine-running measurement.

Engineering completion comes from Engineering update evidence, not the production restart time. Current status and outstanding jobs are as of report generation. Fault-open time is the available raised timestamp; no separate Engineering request timestamp is fabricated. Response averages use timestamped casepacker requests only. Active repair/work duration is Not Available where true work timestamps do not exist. Detailed PDFs retain setting changes and related changeover/QA records.

## Files and API

- Backend: `report_auth.py`, `report_models.py`, `report_store.py`, `shift_reports.py`, `report_render.py`, `reports_api.py`; router registration in `api.py`.
- Existing database readers accept an optional shared connection; normal callers retain their existing behaviour.
- Frontend: shared `features/reports/ReportsPanel.tsx`, its stylesheet/tests, Management Reports page/route/navigation, HMI launcher and successful-handover event, Engineering launcher.
- `deploy/api.Dockerfile` installs Chromium matching the existing pinned Playwright dependency. No new application package or service.
- `migrations/20261012232758_shift_report_snapshots.sql`: one backend-only snapshot table, indexes, RLS/client revocations and an immutable-row trigger. Existing audit log reused. Readiness checks require this table.

Authenticated endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/reports/options` | Permitted templates and configured lines |
| POST | `/api/v1/reports` | Generate/revise; UUID `Idempotency-Key` required |
| GET | `/api/v1/reports?offset=0` | History in pages of 100 |
| GET | `/api/v1/reports/{id}` | Saved authorised snapshot |
| GET | `/api/v1/reports/{id}/preview` | Escaped HTML preview |
| GET | `/api/v1/reports/{id}/export/pdf` | Detailed paginated PDF |
| GET | `/api/v1/reports/{id}/export/png` | Compact high-resolution image |

Report responses are no-store. There are no public report URLs. Downloaded files can contain operational information; the selected template determines their content.

## Preview evidence

All preview figures are synthetic, clearly labelled, and not live factory results.

- [LineTech image](report-preview/linetech.png) / [PDF](report-preview/linetech.pdf)
- [Engineering image](report-preview/engineering.png) / [PDF](report-preview/engineering.pdf)
- [Management 24-hour image](report-preview/management.png) / [PDF](report-preview/management.pdf)
- Actual React screen screenshots at desktop, tablet and mobile sizes are under `docs/report-preview/`.
- [Browser/export verification](report-preview/verification.json)
- [Optional End Shift prompt](report-preview/end-shift-prompt.png) and [Engineering workspace](report-preview/engineering-workspace-report.png)
- [Entry-point verification](report-preview/entrypoints-verification.json): real HMI/Engineering components with synthetic API responses; the End Shift integration test separately verifies the event only follows a successful handover.

PNG is a compact summary, with up to six records per table and a clear pointer to the complete PDF. It does not silently crop a long report. PDF pagination was checked using an 80-record table; all records and repeated table headers survive. PDF pages carry the report ID and page numbers. The renderers require no external assets.

## Validation and reproduction

The complete regression suites passed: 1,177 backend tests and 582 frontend tests at the final backend/full frontend checkpoints. Frontend lint and TypeScript/Vite production build passed. Existing non-failing React act/deprecation warnings and Vite's bundle-size warning remain.

Run from the repository root with the existing Python/Node environments:

```powershell
python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

The local database acceptance script refuses any host except localhost/127.0.0.1 and any database except `pulse_reports_preview`. Apply the migration to that isolated database before running:

```powershell
$env:DATABASE_URL='postgresql://postgres@127.0.0.1:5432/pulse_reports_preview'
python scripts/manual_integration/verify_shift_reports.py
```

Verified with real local PostgreSQL: simultaneous duplicate requests save one snapshot, changed payload reuse returns 409, revisions retain the original, audit records are saved, UPDATE/DELETE are rejected, RLS/client revocations are present, source table counts remain unchanged, and 27 direct cross-role endpoint checks pass. All three real SQL aggregation paths also execute against the isolated database. The script leaves synthetic report/audit records only in that local database.

For screenshots, run the local frontend on port 5183, then `python scripts/manual_integration/report_screenshots.py`. The browser intercepts API requests with synthetic fixtures. Windows verification uses installed Edge; set `PULSE_REPORT_BROWSER_CHANNEL=msedge` for local exports only. Production uses the bundled Chromium default. PDF raster review tools were installed outside the app in a temporary QA tools directory and are not application dependencies.

## Deployment order after approval

1. Review and commit the reporting changes. Exclude `.env`, database backups and local test logs.
2. Take and verify a current live database backup using the established deployment procedure.
3. Apply **only** `20261012232758_shift_report_snapshots.sql` to the intended database, once. It adds report storage and does not rewrite production records. Verify the table, RLS and `immutable_shift_report` trigger.
4. Push the reviewed commit to the branch used by Dokploy and rebuild both services. The API image must rebuild because Chromium and its system libraries are new image contents. Do not change PINs, database URL or existing origins. Do not set the Windows Edge channel in Dokploy.
5. Confirm `/health/ready` reports ready with no missing migrations. An absent report table deliberately fails readiness.
6. Sign in again after the API restart. Verify the three access levels, generate a report for a known completed shift, download both PDF and PNG, reopen history and create a revision. Check the successful End Shift prompt on the next genuine handover; do not create a live trial run just for this check.
7. Check API logs if an export is busy or fails; the snapshot remains saved and can be retried.

The Linux Docker image has not been built/run in this Windows verification environment. Its Chromium launch and the first deployed PDF/PNG export remain deployment smoke checks. Local Edge rendering, browser downloads and PDF pagination have been verified. The API still runs one worker because existing sessions are process-local.

Rollback: redeploy the previously approved application commit and retain the additive report table/snapshots. Do not delete report history or restore the entire database merely to roll back this UI/API feature.
