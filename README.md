# TonnageFlow Pulse

**See the flow. Understand the loss. Improve the journey of the tonne.**

Pulse is a food-manufacturing application that connects production runs, hourly output, downtime, Engineering response and shift handovers. It turns factory-floor updates into a shared operational record: what was expected, what was produced, where time was lost and what needs attention next.

A line can hit its output and still carry unnecessary cost. Pulse starts with reliable production evidence; the wider Tonnage Flow direction connects that evidence to cost per dispatched tonne.

> **Current application branch:** [`feature/linetech-preview`](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/tree/feature/linetech-preview). GitHub's default `main` branch is an older code baseline. This guide describes the current application branch, reviewed on 21 October 2026. Deployment is manual through Dokploy; documentation is not proof of a live deployment.

## What is implemented

| Area | Available workflow |
| --- | --- |
| LineTech HMI | Tablet sign-in, line selection, run setup, hourly pallet capture, progress, planned stops, faults, production restoration and handover. |
| LineTech equipment setup | Management-controlled groups, machines, fault buttons and planned reasons. Rovema has a configurable BV1/BV2 preset. |
| Engineering workspace | Fault investigation and repair updates, ownership, casepacker readiness and handover. Production restoration and Engineering closure remain separate events. |
| Changeovers | Product, size and format changes, physical completion, Engineering readiness and QA verification where required. |
| Management | Lines, machines, buttons, active runs, technician task observations, weekly tonnage targets and versioned production standards by line and pack weight. |
| Live Operations | Latest recorded line state, last completed-hour output, current-shift charts, weekly tonnes, open issues, downtime and changeover readiness. |
| Operational Intelligence | Production evidence, recorded loss drivers, priorities, line snapshots and downtime comparisons. Financial metrics are explicitly unavailable Phase 2 placeholders. |
| Shift reports | LineTech, Engineering and Management templates; saved history, immutable revisions, HTML preview, detailed PDF, compact PNG and device sharing with a download fallback. |
| Home navigation | Connected destinations with existing sign-in controls. Planned modules are clearly identified rather than presented as working screens. |

### Report preview

The image below uses **synthetic demonstration data**, not live factory results.

[![LineTech shift report preview](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/report-preview/linetech.png)](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/report-preview/linetech.pdf)

[Engineering example](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/report-preview/engineering.png) | [Management example](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/report-preview/management.png) | [Reporting engine guide](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/reporting_engine.md)

## How production is recorded

The production run connects the line, technician, customer, product, pack configuration and the standard effective when the run starts. Hourly updates and events build its operational history.

```text
Run setup + fixed standard
          |
          +-- Hourly production updates
          +-- Planned stops and unplanned faults
          +-- Engineering response and repair evidence
          +-- Changeover, verification and handover
          |
          v
Shared calculations -> Dashboards -> Saved shift reports
```

Technicians record the event. Pulse handles conversions, timestamps and calculations. Existing runs retain their standard snapshots when Management changes a standard for future runs.

### Calculation boundaries

- **Actual packs:** reported pallets x the run's cases per pallet x packs per case. Pack weight converts output into tonnes.
- **Expected packs:** the fixed standard in packs/minute x the applicable scheduled minutes. Partial periods use their actual duration.
- **Output vs target:** actual packs / expected packs x 100. The Live Operations label "Last-Hour OEE" uses this output-achievement definition and explains it on screen. It is not classical Availability x Performance x Quality OEE.
- **Estimated OEE:** report paths using the existing OEE engine retain its separate availability, performance and quality definitions, provisional assumptions and input-validation rules. Do not compare differently defined percentages as if they were the same measure.
- Missing readings are not zero production. Not Scheduled is separate from a breakdown. Above-target percentages remain visible and are flagged for review.
- Recorded downtime is reconciled to avoid counting overlapping stops twice. Missing packs are not automatically attributed to downtime or assigned a financial value.
- Factory boundaries use **Europe/London**: Day 06:00-14:00, Afternoon 14:00-22:00 and Night 22:00-06:00. Charts follow the current shift; unfinished hours remain blank. Clock-change nights retain their actual seven or nine hours.
- "Running at last report" is recorded HMI evidence, not continuous machine telemetry. Produced tonnes are not dispatched tonnes.

See [standard reconciliation](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/production_standard_reconciliation.md), [line/weight standards](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/line_weight_standards.md) and [operational intelligence boundaries](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/operational_intelligence_redesign.md).

## Access and navigation

Pulse uses separate server-configured tablet, Engineering and Management PINs with session-based API access. Showing a link does not grant permission.

| Route | Purpose | Access |
| --- | --- | --- |
| `/` | Home and destination navigation | Public navigation |
| `/hmi` | LineTech production capture and reports | Tablet session |
| `/engineering` | Engineering workspace and reports | Engineering session |
| `/management` | Factory configuration | Management session |
| `/management/linetech` | Equipment groups and reasons | Management session |
| `/management/production-standards` | Versioned standards | Management session |
| `/management/weekly-targets` | Weekly tonnage targets | Management session |
| `/management/performance` | Task and production performance | Management session |
| `/management/active-runs` | Active run oversight | Management session |
| `/management/reports` | Saved shift reports | Management session |
| `/dashboard` | Live Operations and detailed production views | Management session |
| `/dashboard/engineering` | Engineering dashboard | Management session |
| `/dashboard/qa` | QA dashboard | Management session |
| `/dashboard/operational-intelligence` | Production intelligence | Management session |

Report permissions are checked on generation, history, retrieval, revision and export:

| Signed-in role | LineTech report | Engineering report | Management report |
| --- | --- | --- | --- |
| Tablet / LineTech | Yes | No | No |
| Engineering | Yes | Yes | No |
| Management | Yes | Yes | Yes |

Saved reports are immutable snapshots. A revision creates another version; it does not overwrite the original. Sharing exports a file through the user's device; it does not create a public report URL or automatically send a WhatsApp message.

## Architecture

```text
React + TypeScript + Vite
          |
       Nginx (web)
          | /api, /health, /webhooks
       FastAPI (one API worker)
          |
       PostgreSQL (external; pilot uses Supabase)

Saved report -> HTML -> Playwright Chromium -> PDF / PNG
```

- **Backend:** Python, FastAPI, psycopg and shared production/time calculations.
- **Frontend:** React, TypeScript, React Router and Vite.
- **Database:** PostgreSQL, incremental SQL migrations and backend-only credentials.
- **Exports:** Playwright Chromium installed in the API image; no separate export service.
- **Delivery:** Docker Compose on Hostinger through Dokploy, HTTPS routing and same-origin API requests.
- **Tests:** pytest, Vitest, Testing Library and isolated browser/database verification scripts.

`src/main.py` is the earlier CLI entry point. The web application uses `src.api:app`. The WhatsApp webhook currently provides verification and acknowledgement; it is not a complete inbound/outbound messaging integration. Browser-observer tools are separate from the Compose services.

## Local development

Use Python 3.13 and Node.js 22.12+ (Node 24 is also suitable), with PostgreSQL available. The Dockerfiles use Python 3.13 and Node 22.

```powershell
git clone --branch feature/linetech-preview https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard.git
cd whatsapp-oee-dashboard
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm --prefix frontend ci
```

Create a local `.env` from [.env.example](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/.env.example) only if you do not already have one. Configure a **development database**, distinct tablet/Engineering/Management secrets and these local origins:

```dotenv
HMI_ORIGIN=http://localhost:5173
DASHBOARD_ORIGIN=http://localhost:5173
```

Keep the frontend API base URL unset for the Vite same-origin proxy, which forwards `/api` and `/health` to `127.0.0.1:8000`. If using a different frontend origin, configure the exact matching backend origins.

**Database prerequisite:** migrations are incremental and some depend on pre-existing application tables and PostgreSQL roles. They are not a one-command empty-database bootstrap. Use a prepared development schema or an authorised isolated restore, review the migration prerequisites, then apply only missing migrations in order. No migration or data reset runs at app startup.

Start the backend from the repository root:

```powershell
python -m src.serve
```

In a second terminal, start the frontend:

```powershell
npm --prefix frontend run dev
```

Open `http://localhost:5173`. The validated backend launcher requires three distinct secrets of at least 12 characters. Never use production data or credentials for casual demos.

For local PDF/PNG exports, install the matching browser once:

```powershell
python -m playwright install chromium
```

Linux hosts also need Chromium system libraries; the API Dockerfile installs them with `--with-deps`.

### Synthetic previews

With Vite running, these local-only entry points use sample data:

- `/linetech-preview.html` - HMI scenarios, with some actions represented by placeholders.
- `/live-preview.html` - Live Operations examples.
- `/intelligence-preview.html` - Operational Intelligence examples.

Preview entries are separate from the production build entry point. Screenshots under `docs/*-preview/` are review artifacts, not operational evidence.

## Validation

Run from the repository root:

```powershell
python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

Selected integration scripts under `scripts/manual_integration/` require an explicitly prepared disposable local database or a running local preview server. Read each script before running it; do not aim integration tests at production. See [report verification](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/reporting_engine.md) for export, role and snapshot checks.

Automated tests do not replace tablet/network acceptance or a deployed PDF/PNG smoke check. Historical test counts in feature documents describe those releases, not a fresh run of the current checkout.

## Manual deployment

Keep **Dokploy Autodeploy off**. Use the reviewed `feature/linetech-preview` commit and `./docker-compose.yml`.

1. Review the commit and check whether it introduces migrations.
2. Before database changes, take a fresh backup and prove recovery in an isolated database.
3. Apply only the required, unapplied migrations to the intended database.
4. Configure server-only `DATABASE_URL`, `MANAGEMENT_PIN`, `ENGINEERING_PIN`, `HMI_DEVICE_PIN` and `PULSE_ORIGIN` in Dokploy.
5. Deploy API and web from the same revision. Rebuild the API image when browser/system dependencies change.
6. Require `/health/ready` to return HTTP 200 with `status: ready`, `database: ok` and an empty `missing_migrations` list. `/health` is liveness only.
7. Sign in again after restart. Check role access, production views and report PDF/PNG downloads on the intended devices.

The reporting engine requires `20261012232758_shift_report_snapshots.sql`. Consult the full [migrations directory](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/tree/feature/linetech-preview/migrations), the [release gate](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/deployment_release_gate.md) and [Dokploy guide](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/dokploy_deployment.md); do not assume a historical migration list is exhaustive.

Keep **one API worker and one replica** while sessions and rate limits are process-local. The Compose image invokes Uvicorn directly; `python -m src.serve` provides additional environment validation for direct hosting. Do not expose database credentials or PINs through `VITE_*` variables. Keep the database external and the API behind the web proxy.

Retain the previous known-good image/commit for rollback. Do not use **Fresh Volumes**, delete report history or restore an entire database as a routine UI rollback.

## Repository map

| Path | Contents |
| --- | --- |
| `src/` | API routes, authentication, calculations, persistence, reporting and CLI/integration code |
| `frontend/src/features/` | HMI, Engineering, Management, Home and dashboard/report UI |
| `frontend/preview/` | Local synthetic preview fixtures |
| `migrations/` | Reviewed incremental SQL changes |
| `deploy/`, `docker-compose.yml` | API/web images, reverse proxy and services |
| `tests/` | Backend regression tests |
| `scripts/manual_integration/` | Database/browser acceptance and preview tools |
| `scripts/production_config/` | Controlled configuration utilities; inspect their dry-run options |
| `docs/` | Feature contracts, release procedures and synthetic visual evidence |

## Further documentation

- [HMI integration](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/hmi_integration.md) and [LineTech workflow](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/linetech_upgrade.md)
- [Rovema equipment preset](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/rovema_sheet_setup.md)
- [Engineering integration](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/engineering_integration.md)
- [Management integration](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/management_integration.md)
- [Live Operations](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/live_operations_upgrade.md)
- [Operational Intelligence](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/operational_intelligence_redesign.md)
- [Home navigation](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/home_navigation.md)
- [Hourly production card](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/hourly_card_upgrade.md)
- [Shift reporting and deployment checks](https://github.com/kurimahamba-TonnageFlow/whatsapp-oee-dashboard/blob/feature/linetech-preview/docs/reporting_engine.md)

Some feature documents preserve their original review-stage notes. Use the current code and release verification to establish what is deployed.

## Scope and direction

Warehouse, Cleaning Plant and general Settings are not complete operational modules. Financial intelligence, confirmed recoverable loss, cost per dispatched tonne and upstream/downstream material tracking require additional evidence and implementation. Current packaging output is not a substitute for intake or dispatch measurements.

Pulse provides production visibility. RunSafe is the wider verification-and-control direction, not a module implemented here. The aim is one connected view of the tonne from intake to dispatch, built by proving narrow operational use cases first.
