# Pulse HMI — Backend Integration

The operator HMI now lives in this repository as a React app under
`frontend/src/features/hmi/`, and Stage 6B2 wired it to every endpoint
below. This document is the contract between that app and the FastAPI
backend.

Where the frontend implements each part of the contract:

| Concern | File |
| --- | --- |
| Request/response types | `frontend/src/features/hmi/types.ts` |
| Endpoint calls | `frontend/src/features/hmi/api.ts` |
| Idempotency keys | `frontend/src/features/hmi/idempotency.ts` |
| Screen flow / orchestration | `frontend/src/features/hmi/HmiScreen.tsx` |
| Which run is in progress | `frontend/src/features/hmi/activeRunStorage.ts` |

Every Stage 6B1/6B2 endpoint below needs
`migrations/0003_pulse_phase1_foundation.sql`. That migration was
applied successfully during Stage 6B4. The backend and the frontend must
still be released together.

## Line / machine / button configuration (new)

```
GET {PULSE_API_BASE_URL}/api/v1/hmi/config
```

Requires the existing tablet session (`X-HMI-Session`). Returns only **active** lines → machines →
buttons - never disabled rows, Management data, or credentials:

```json
{
  "lines": [
    {
      "id": 1,
      "name": "Rovema",
      "machines": [
        {
          "id": 5,
          "name": "BV1",
          "buttons": [
            {"id": 20, "name": "Film Jam", "event_type": "unplanned_fault", "ownership": "Production", "fault_category": null},
            {"id": 21, "name": "Film Change", "event_type": "planned_downtime", "ownership": "Production", "fault_category": null}
          ]
        }
      ]
    }
  ]
}
```

`event_type` is `"planned_downtime"` or `"unplanned_fault"`;
`ownership` is `"Production"` or `"Engineering"` (matches the existing
Engineering-access split). The HMI should poll this on load and on
refresh so a button a manager just added in the new Management area
(see `docs/management_integration.md`) appears without republishing
the HMI.

The React HMI calls this on load (`getHmiConfig` in
`frontend/src/features/hmi/api.ts`) and holds no hard-coded machine or
fault-button list. `frontend/src/features/hmi/constants.ts` still holds
only the Start Run picklists (lines, technicians, shifts, products,
customers) and the default planned-downtime reasons — not machines or
buttons.

## Cross-device line state

```
GET {PULSE_API_BASE_URL}/api/v1/hmi/lines
```

Read-only, no authentication, no `Idempotency-Key`. The authoritative
answer to "is this line already running?", identical for every tablet
because it comes from the database rather than from any one device's
`localStorage`.

```json
{
  "generated_at": "2026-01-12T08:00:00+00:00",
  "stale_after_minutes": 75,
  "lines": [
    {
      "line_id": 1,
      "production_line": "Rovema",
      "has_active_run": true,
      "run_id": 4101,
      "line_technician": "Liam",
      "shift": "Night",
      "customer": "Asda",
      "product": "White Basmati",
      "started_at": "2026-01-12T06:05:00+00:00",
      "planned_downtime_active": false,
      "changeover_active": false,
      "engineering_fault_open": true,
      "open_fault_count": 1,
      "last_activity_at": "2026-01-12T07:40:00+00:00",
      "stale_status": "current",
      "stale_reason": null,
      "minutes_since_last_hourly_update": 22
    }
  ]
}
```

- Only lines configured **active** in Management appear, exactly as for
  `/api/v1/hmi/config`. A line with no active run returns `null` for
  every run field and `stale_status: "no_active_run"` — that is
  "available", not missing data.
- `stale_status` is `current`, `stale`, `unknown` or `no_active_run`,
  using the same 75-minute threshold as the dashboard
  (`src/pulse_calculations.py: freshness`). `last_activity_at` is the
  most recent event actually recorded; legacy hourly updates with no
  timestamp contribute nothing rather than a guessed time.
- **No Management-only information is exposed**: no tonnage,
  achievement, OEE, target, waste or cost figure appears here. The
  factory-floor HMI has no login, so this endpoint carries only what a
  tablet needs to decide whether a line can be started.
- `503` if the database cannot be read, with a safe message and no
  connection details.

The HMI polls this every 20 seconds, pausing while the tab is hidden and
refreshing immediately when it becomes visible again, with at most one
request in flight
(`frontend/src/features/hmi/useLineStatePolling.ts`). When it cannot be
read, Home says the status is unavailable and **disables Start Run** —
it never treats an empty `localStorage` as proof that a line is free.

This endpoint is an early warning, not the guard. The database
uniqueness constraint on one active run per line remains the final
authority: if two tablets still race, the loser gets `409` from
`POST /api/v1/runs`, the HMI re-reads this endpoint and tells the
operator that another device started the run first.

## Endpoint

```
POST {PULSE_API_BASE_URL}/api/v1/runs
Content-Type: application/json
```

Required environment variable on the HMI side:

```
PULSE_API_BASE_URL=https://<wherever-src/api.py-is-deployed>
```

## Request body

```json
{
  "production_line": "Rovema",
  "line_technician": "Liam",
  "shift": "Night",
  "customer": "Asda",
  "product": "Basmati",
  "pack_weight": "1kg",
  "pack_weight_kg": 1.0,
  "packs_per_case": 8,
  "pack_type": "Pillow Pack",
  "target_speed_ppm": 120,
  "cases_per_pallet": 220,
  "pallets_remaining": 38,
  "previous_run_completed": 0
}
```

Field rules:

- `production_line`, `line_technician`, `shift`, `customer`, `product`,
  `pack_weight`, `pack_type` — required, non-blank text.
- `production_line` must be one of the configured lines (currently
  `Rovema`, `GIC`, `Guill`); `line_technician` must be one of the
  technicians configured for that specific line.
  - Valid on `Rovema`, `GIC` and `Guill`: `Marina`, `Mariusz`, `Liam`,
    `Ben`, `Tomasz`, `Sumit`, `Gurpreet`, `Baljeet`, `Pali`, `Diego`,
    `Seb`, `Bupreet`. There are no line-specific restrictions - every
    technician is valid on every line.
  - Names must match this spelling and capitalisation exactly.
- `pack_weight_kg` — number, must be greater than 0.
- `packs_per_case` — integer, must be at least 1.
- `target_speed_ppm` — number, must be greater than 0.
- `cases_per_pallet` — integer, must be at least 1.
- `pallets_remaining`, `previous_run_completed` — integers, cannot be
  negative.

## Success response

`201 Created`

```json
{
  "status": "success",
  "message": "Run started",
  "run_id": 123,
  "production_line": "Rovema",
  "line_technician": "Marina",
  "pallets_remaining": 38
}
```

## Error responses

No response body ever includes database connection strings, credentials,
or raw exception text.

| Status | Meaning | Body |
| --- | --- | --- |
| `422` | A field failed validation (blank, out of range, unknown line/technician). | `{"detail": [...] }` (FastAPI's standard Pydantic validation-error shape) |
| `409` | This line already has an active run. Enforced by a database constraint, so two tablets racing each other cannot both win. | `{"detail": "..."}` |
| `503` | The database could not be checked or the run could not be saved. Safe to retry. | `{"detail": "..."}` |

## Example `fetch()` call

```javascript
async function startRun(payload) {
  const response = await fetch(`${PULSE_API_BASE_URL}/api/v1/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const body = await response.json();

  if (!response.ok) {
    // body.detail describes what went wrong; never overwrite the
    // technician's entered form data on failure - let them retry.
    throw new Error(
      typeof body.detail === "string" ? body.detail : "Run could not be started."
    );
  }

  // Only now - after Supabase has confirmed the save - show
  // "✓ RUN STARTED" and store body.run_id.
  return body;
}
```

Recommended HMI button behaviour (per the connection requirements):
show a submitting state and disable the Start Run button for the
duration of the request; keep the entered form values on screen if the
request fails and offer a Retry button; only show "✓ RUN STARTED" after
this call resolves successfully; store the returned `run_id`.

## CORS

The API allows exactly two origins (no wildcard): the deployed Pulse HMI
and the deployed Pulse Dashboard, overridable with the `HMI_ORIGIN` and
`DASHBOARD_ORIGIN` environment variables. If either is redeployed to a
different origin, set the matching variable on the API side or the
browser will block the request.

Allowed request headers are `Content-Type`, `Authorization` and
`Idempotency-Key`. A proxy or CDN in front of the API must forward
`Idempotency-Key` unchanged, or every write will be rejected as missing
its key.

## Capture endpoints

Same access model as `POST /api/v1/runs` (no login on the factory-floor
HMI); every technician name is validated against the run's line, and
every event time comes from the server clock — the tablet never sends a
timestamp. All figures in responses are calculated by the backend
(`src/pulse_calculations.py`); the HMI only displays them. Source:
`src/pulse_capture_api.py`.

| Method | Path | Body | Idempotency-Key | Success |
| --- | --- | --- | --- | --- |
| POST | `/api/v1/runs/{run_id}/hourly-updates` | `line_technician`, `pallets_produced` (decimal string, 0-1000, max 4 dp, e.g. `"3.75"`), optional `other_loss_reason` | required | `201` |
| POST | `/api/v1/runs/{run_id}/planned-downtime` | `reason`, `started_by` | required | `201` |
| POST | `/api/v1/planned-downtime/{id}/end` | `ended_by` | required | `200` |
| POST | `/api/v1/runs/{run_id}/faults` | `reported_by`, `machine`, `reason`, optional `machine_id`, `button_id` (requires `machine_id`), `note` | required | `201` |
| POST | `/api/v1/runs/{run_id}/xray-counts` | X-ray body (below) | required | `201` |
| POST | `/api/v1/runs/{run_id}/completion-preview` | same body as Complete Run | not used (read-only) | `200` |
| POST | `/api/v1/runs/{run_id}/changeovers` | `line_technician`, `new_customer`, `new_product`, `new_pack_weight_kg` (decimal string), `new_format`, optional `note` | required | `201` |
| POST | `/api/v1/changeovers/{id}/complete` | `completed_by`, `first_acceptable_packs_confirmed: true`, optional `note` | required | `200` |
| GET | `/api/v1/changeovers/open?production_line=GIC` | - | - | `200` |
| GET | `/api/v1/runs/{run_id}/hmi-state` | - | - | `200` |

### Idempotency (every write)

Each write endpoint above requires an `Idempotency-Key` request header:

```
Idempotency-Key: <16-100 chars, [A-Za-z0-9_-]>
```

The backend claims the key and writes the business rows in **one
transaction**, then stores the response it returned. This is a real
idempotency guarantee, not a time-window duplicate guard:

- **Retrying with the same key** replays the stored response, with the
  same status code and an `Idempotent-Replayed: true` response header.
  Nothing is written a second time.
- **Reusing a key with a different body** is `409` — the key identifies
  one specific request, not a slot.
- **A new key** is a genuinely new action.

The HMI generates a key when the operator commits an action and keeps it
for the lifetime of that action, so a lost response, a tapped-twice
button, a dropped connection or a tablet refresh mid-request can all be
retried safely (`frontend/src/features/hmi/idempotency.ts`). A zero-length
hourly period is still rejected with `409` on its own merits.

Behaviour worth knowing:
- **Hourly update**: the reported period runs from the end of the run's
  previous hourly period (or the run start) to now; expected output =
  target speed x those minutes. Pallets reduce `pallets_remaining` until
  zero, then count as potential overrun (same rule as the CLI), with
  exact decimal arithmetic. The update is attributed to the run's own
  recorded shift, and stores the shift instance (`shift_window_start`)
  the period **ended** in — output is never silently moved into the next
  shift when a period crosses a shift boundary.
- **Planned downtime**: one open planned stop per run (`409` otherwise).
  A planned stop that belongs to a changeover cannot be ended directly
  (`409`) — complete the changeover instead.
- **Report to Engineer**: creates an `Ongoing` / `Not Started` fault that
  immediately appears in the Engineering workflow, with the next
  per-run `fault_id`. `machine_id`/`button_id`, when sent, must belong
  to the run's line (`422` otherwise).
- **Changeover**: Start Changeover opens the planned stop **and** the
  changeover record in one transaction, so a changeover can never exist
  without its downtime (or the reverse). Completing it ends both, and is
  confirmed by the operator only when the first acceptable packs of the
  new run are produced — not when the machines restart. One open
  changeover per line (`409`). Previous customer/product/pack
  weight/format are copied from the line's most recent run; the new
  values are captured at the start.

### Idempotency-key retention (90 days)

Each row in `hmi_idempotency_keys` carries `created_at` and `expires_at`,
both set by database column DEFAULTs (`now()` and
`now() + interval '90 days'`). The HMI never sends either, so a client
cannot shorten its own duplicate protection by claiming an early expiry,
and `CHECK (expires_at > created_at)` makes an inverted pair impossible.

While a key is inside its 90 days it behaves exactly as described above:
a repeat replays the stored response, and a different body is `409`.
**Once a key has expired**, the next request using it takes the row over
in the same transaction (`ON CONFLICT … DO UPDATE … WHERE expires_at <=
now()`) and is treated as a brand-new logical action, with the stored
response cleared. A key still inside its period never matches that
condition, so it can never be silently overwritten by a race.

**Cleanup is a separate maintenance operation**, never part of an HMI
write:

```python
from src.database import delete_expired_idempotency_keys

delete_expired_idempotency_keys()                 # every expired row
delete_expired_idempotency_keys(batch_size=5000)  # in steps
```

It deletes only rows where `expires_at <= now()`, judged by the database
clock, and has no "delete everything" mode and no key argument. Leaving
expired rows in place is harmless - they simply keep protecting their key
until the sweep runs. No extension is required and `pg_cron` is
deliberately not used, so applying migration 0003 enables nothing on its
own; schedule the call from outside the database when convenient.

### Active-run recovery

`GET /api/v1/runs/{run_id}/hmi-state` is the authoritative state for a
tablet that was refreshed, closed or swapped. It returns the run, the
saved progress totals, the open planned downtime and the open changeover,
all read from the database. The HMI stores only `{runId, productionLine}`
locally and re-reads everything else from this endpoint, so no total
shown on the tablet is ever local-only state. A run id that no longer
exists returns `404`, and the HMI clears its stored run and returns the
operator to the home screen.

### X-ray count (end of shift and Complete Run)

```json
{"line_technician": "Liam", "production_since_last_update": false, "xray_pack_count": 9000}
{"line_technician": "Liam", "production_since_last_update": true, "final_pallets_produced": "2.5", "xray_pack_count": 9000}
{"line_technician": "Liam", "production_since_last_update": false, "count_unavailable": true, "unavailable_reason": "Counter reset mid-shift"}
```

`production_since_last_update` is required and has no default — the
operator must answer it. When it is `true`, `final_pallets_produced` is
required (decimal string, greater than zero); when it is `false`, sending
that field is `422`. The backend records that final production as a real
hourly update **before** it calculates palletised packs, so any output
made since the last hourly update is included in the X-ray comparison
rather than being counted as waste.

For the count itself, exactly one of: a non-negative `xray_pack_count`,
or `count_unavailable: true` with a non-blank reason — anything else is
`422`. One end-of-shift count per run per shift (`409`). The response
includes the estimated post-X-ray waste (see
`docs/dashboard_integration.md`); when palletised packs exceed the X-ray
count it is still saved, but returned with
`waste_status: "data_quality_warning"`, a warning message and no waste
percentage.

### Complete Run

`POST /api/v1/runs/{run_id}/complete` **requires** the X-ray body above
and an `Idempotency-Key` header. The final hourly update (if any), the
X-ray record and the run's `Completed` status are written in **one
transaction** — a Complete Run that fails leaves the run open with
nothing half-written, and a retry with the same key replays the original
result rather than closing the run twice.

`POST /api/v1/runs/{run_id}/completion-preview` takes the same body and
returns the same calculated figures with `"saved": false`. It writes
nothing. The HMI calls it so the operator reviews the waste figure before
committing, then sends the identical body to `/complete`.

This is a breaking change against any client that sends no body to
`/complete`. The React HMI sends the full body as of Stage 6B2; deploy
the backend and frontend together, after migration 0003.

`POST /api/v1/runs` additionally accepts an optional `format` string
(backward compatible).


### Residual output-loss review (local MVP snagging)

`POST /api/v1/runs/{run_id}/hourly-loss-review` accepts the hourly input
(`line_technician`, `hour_start`, decimal-string `pallets_produced`) and is read-only.
It returns `target_packs`, `remaining_gap_packs`, `equivalent_minutes`, and
`prompt_required`. No idempotency key is needed for this review.

The calculation clips all planned stops and line faults (including faults from
previous runs) to the hour's applicable run interval and merges them. It values
that union at the run's preserved management standard, subtracts it from the measured output
gap, and floors the residual at zero. Equivalent minutes are the residual packs
divided by that same standard. The unrounded value
triggers the prompt at 10 minutes; display rounding does not change the threshold.
This is an estimated output equivalent, not additional recorded downtime.

The tablet reviews before its hourly write. If prompted, the operator can supply
`other_loss_reason` or explicitly choose Cause unknown, which leaves the reason
empty. The existing hourly write remains idempotent. A failed review does not
submit a new reading. Final partial-hour End Run capture uses the same reconciliation. Operating reports are displayed as context and never subtract from the gap.


## Casepacker format change and next-run gate

After End Run > Changeover, the technician explicitly chooses whether casepacker work is needed. Yes requires details (customer, pack size, box format/program; max 500 characters). `POST /api/v1/lines/{line}/stoppages` accepts `casepacker_required` and `casepacker_details`; the request is inserted in the same transaction as its line stoppage and QA changeover. Existing clients omitting these optional fields retain the no-request behaviour.

`GET /api/v1/line-stoppages/{id}/casepacker` returns the linked request or null. The timer polls every five seconds and blocks new-run entry while readiness is pending or unknown. After End Changeover it stays on the timer while Engineering is working. Home or stale form attempts are also rejected by the backend; a database trigger protects other start paths. The technician still ends physical changeover normally. Engineering readiness alone does not end the overall changeover timer: the next run ends it using the existing timing model.

Engineering's authenticated `GET /api/v1/engineering/casepacker-requests` lists requests awaiting their next run. `POST /api/v1/engineering/casepacker-requests/{id}/actions` accepts `accept`, `update` or `ready`, a note and an Idempotency-Key. Only the accepting engineer can update or mark ready; the actor comes from the session. Update/ready require notes; readiness has an explicit UI confirmation. Request row locking and persisted idempotency prevent duplicate history/timestamps. The request is not a downtime fault and adds no second interval.

Deployment dependency: apply `migrations/20261002234341_casepacker_readiness.sql` after `0004_fixed_hour_reporting.sql`, before deploying this backend/UI. New tables are backend-only with RLS enabled and no public, anon or authenticated grants. No historical records are changed by the migration. Rehearsed only on synthetic local PostgreSQL. Factory tablet validation remains outstanding.


## Line-tech reconnection recovery (2026-10-03)

Network errors and 5xx responses no longer say "nothing was saved". The HMI retains an unconfirmed run action and its original key in localStorage. From the active-run reminder, Check and retry submits the saved action without asking the operator to reconstruct it. Hourly reasons, target-change reasons, stop IDs and End Run readings are retained. Fault reports show their stored details before Retry original report, including retrospective times and the original reporter. No run or stop clock is changed merely by refreshing a screen.

Configuration rehearsal: `scripts/production_config/machine_dropdowns.sql` defaults to Rovema, applies to one named line at a time and remains dry-run by default. It refuses to hide active machines with active preset buttons. An approved preset mapping is still required; this safeguard is not a substitute for reviewing presets already attached to inactive machines.


## How SBS fault buttons work

1. Select SBS, then BV1, BV2 or Shared SBS equipment.
2. Select a section, then a visible preset or Other. Changing equipment clears section and fault; changing section clears the fault.
3. Choose Resolved (actual stop/restart times and repair details) or Call Engineer.
4. The existing API receives the real machine ID and preset button ID, plus the selected section in the note. It checks that the button belongs to that machine. No fault is attributed to a bagger just because it belongs to SBS.

`faultSections.ts` groups known paper-sheet reasons for navigation. `rovema_sbs_presets.sql` configures the actual records: eight bagger presets each, three shared presets. It follows `machine_dropdowns.sql`, which now includes the approved shared SBS record. Both scripts stay dry-run by default and have only been rehearsed against synthetic local PostgreSQL. Other configured machines also use visible fault buttons. Legacy machine records are not renamed to infer a hierarchy.


## X-ray and Robot palletiser presets - 2026-10-04

`scripts/production_config/rovema_xray_robot_presets.sql` adds the supplied paper-sheet fault presets on Rovema only. X-ray / Checkweigher: Machine jam; CCP check fail. Robot Palletiser: Pallet stacker fault; Pallet outfeed transfer error; Infeed conveyor not moving; Incorrect stacking; Dropping cases; Safety sensor alarm; Pallet position; Wrapper.

Uses the existing visible fault buttons and Other fault reason with required details. Routine test-kit/hourly/end-of-shift checks remain in the planned check workflow, not fault presets. Apply machine_dropdowns.sql first. Dry run by default; explicit commit and named audit actor required. Existing buttons and history are preserved; inactive or differently classified matching presets stop the transaction for review.

Verified with a fresh synthetic PostgreSQL database: 57 checks passed. All ten presets saved through the actual fault-report API against their correct machines; mismatched Casepacker identity was rejected. Dry run rolled back, existing buttons were unchanged and repeat application added no buttons or audit rows. This verifies local configuration and API behavior, not a physical tablet or live deployment. The configuration has not been applied to live or the existing running preview.


### Management production standards (local change)

Management owns versioned production speed standards. Changes apply only to new runs from their effective time. Start Run no longer accepts a technician-defined baseline: any legacy speed field is ignored, and the database selects the applicable configuration version. A missing management standard prevents starting a new run. Existing run snapshots and operating-speed reports are unchanged. See `production_standard_reconciliation.md` for configuration matching, API contracts and the required local migration.


## Tablet access and task evidence ? current release

HMI routes now require `X-HMI-Session`, obtained from POST `/api/v1/hmi-access/login` with device_name and the server-configured HMI_DEVICE_PIN. The tablet UI handles session expiry and keeps pending submissions. Operator names remain reported identities, not personal authentication. Use one API worker/replica.

POST `/api/v1/task-observations` requires tablet access and Idempotency-Key. The completed-task form covers all seven Rovema tasks independently of operational stop timers. Task evidence is immutable, and incomplete/waiting/shared/legacy evidence is excluded from benchmarking. See deployment_release_gate.md for rollout requirements.


## Phone layouts and hourly image sharing

After a confirmed hourly save, Share this hour shows a branded screenshot-ready card and prepares a PNG locally in the browser. It uses the saved response, including exact UK reporting period, hourly pallets, cumulative run pallets including overrun, actual/target packs and output achievement. Output achievement is explicitly not OEE. Reported speed remains context only; no loss calculations change.

Optional share notes support handover/checks/actions. They are labelled as image-only notes, are not written to production history and are cleared on leaving the screen. Fault and quality records must still use their normal workflows. The card does not automatically infer that open faults occurred in the reported hour or invent check outcomes.

Share image opens the device share sheet when PNG file sharing is supported. The technician chooses WhatsApp and the recipient; the app never sends automatically. Otherwise Download PNG and a screenshot-ready card remain available. Export uses browser canvas with no remote rendering service or new dependency. The saved hour is unaffected by export failure or cancelled sharing. Native sharing needs a supporting secure browser context (HTTPS in deployment); see https://developer.mozilla.org/en-US/docs/Web/API/Navigator/share. Actual WhatsApp delivery is a physical-device acceptance check.

Responsive changes cover phone navigation, forms, action buttons, safe-area spacing and contained wide tables. Local Chromium checks: entry screens at 320/390/430 px and synthetic share-card layout at 320/390/430/768 px had no horizontal page overflow. A real PNG was downloaded and visually inspected. This is viewport emulation, not blanket certification of every phone or authenticated screen. Validation: 491 frontend tests, TypeScript, ESLint and production build passed. Existing React test warnings and bundle-size advisory remain. No backend/schema changes or deployment were required.
