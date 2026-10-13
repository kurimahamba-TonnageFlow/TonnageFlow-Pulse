"""Shared, read-only shift report aggregation. No production writes or exports here."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from . import (
    database as db,
    hourly_reports,
    dashboard_reports,
    pulse_calculations as calc,
)
from .factory_time import (
    TimeWindow,
    FACTORY_TZ,
    shift_window_for,
    shift_window_offset,
    factory_day_start,
    factory_date_of,
    rolling_24h_window,
    clock_hours_between,
    clock_hour_start,
    CLOCK_HOUR,
)


def resolve_period(mode, day, shift, now):
    if mode == "current":
        return shift_window_offset(now, 0)
    if mode == "previous":
        return shift_window_offset(now, 1)
    if mode == "rolling_24h":
        return rolling_24h_window(now)
    day = day or factory_date_of(now)
    if mode == "shift":
        return shift_window_for(day, shift)
    if mode == "production_day":
        return TimeWindow(
            "factory_day",
            f"Production day {day}",
            factory_day_start(day),
            factory_day_start(day + timedelta(days=1)),
        )
    raise ValueError("Unknown reporting period")


def engineering_summary(faults, requests, window, now):
    jobs = []
    for f in faults:
        updates = [u for u in f.get("repair_updates", []) if u["created_at"] <= now]
        closed = next(
            (
                u["created_at"]
                for u in reversed(updates)
                if u.get("engineering_status") == "Resolved"
            ),
            None,
        )
        raised = f["opened_at"]
        if raised > now:
            continue
        # Fault-open time is not an Engineering-request timestamp. Do not fabricate one.
        status = (
            "Completed"
            if closed or f.get("engineering_status") == "Resolved"
            else ("In Progress" if f.get("engineer") else "Open")
        )
        jobs.append(
            dict(
                source="downtime_events",
                id=f["downtime_event_id"],
                line=f["production_line"],
                machine=f["machine"],
                description=f["reason"],
                raised_at=raised,
                request_at=None,
                accepted_at=f.get("accepted_at"),
                completed_at=closed,
                engineer=f.get("engineer"),
                status=status,
                response_minutes=None,
                work_minutes=None,
                latest_action=(
                    (updates[-1].get("action") or updates[-1].get("finding"))
                    if updates
                    else None
                ),
                updates=updates,
                source_refs=[
                    {"table": "engineering_updates", "id": u["id"]} for u in updates
                ],
            )
        )
    for q in requests:
        if q["requested_at"] > now:
            continue
        accepted = q.get("first_accepted_at") or q.get("accepted_at")
        completed = q.get("ready_at")
        cancelled = q.get("cancelled_at")
        status = (
            "Cancelled"
            if cancelled
            else (
                "Completed"
                if completed
                else "Accepted" if q.get("engineer") else "Open"
            )
        )
        jobs.append(
            dict(
                source="casepacker_requests",
                id=q["id"],
                line=q["production_line"],
                machine="Casepacker",
                description=q["details"],
                raised_at=q["requested_at"],
                request_at=q["requested_at"],
                accepted_at=accepted,
                completed_at=completed,
                engineer=q.get("engineer"),
                status=status,
                response_minutes=(
                    float(calc.duration_minutes(q["requested_at"], accepted))
                    if accepted and accepted >= q["requested_at"]
                    else None
                ),
                work_minutes=None,
                latest_action=(q.get("updates") or [{}])[-1].get("note"),
                updates=q.get("updates", []),
                changeover=q.get("changeover"),
                source_refs=[
                    {"table": "casepacker_updates", "id": u["id"]}
                    for u in q.get("updates", [])
                ],
            )
        )
    in_period = lambda t: t is not None and window.start <= t < min(window.end, now)
    outstanding = [
        {**j, "older": j["raised_at"] < window.start}
        for j in jobs
        if j["status"] not in ("Completed", "Cancelled")
    ]
    activity = [
        j
        for j in jobs
        if in_period(j["raised_at"])
        or in_period(j["completed_at"])
        or any(in_period(u.get("created_at")) for u in j["updates"])
    ]
    durations = [
        j["response_minutes"]
        for j in activity
        if j["response_minutes"] is not None and in_period(j["accepted_at"])
    ]
    return dict(
        jobs=activity,
        outstanding=outstanding,
        raised=sum(in_period(j["raised_at"]) for j in jobs),
        completed=sum(in_period(j["completed_at"]) for j in jobs),
        open=len(outstanding),
        statuses=dict(Counter(j["status"] for j in activity)),
        average_response_minutes=sum(durations) / len(durations) if durations else None,
        response_sample_count=len(durations),
        average_work_minutes=None,
        timing_note="Fault jobs raised use fault-open time; Engineering request time is not captured. Active work timestamps are not captured. Response averages use timestamped changeover requests only. Job status and handover are as of generation.",
    )


def loss_summary(data, window, now):
    end = min(window.end, now)
    events = []
    runs = {r["id"]: r for r in data["runs"]}
    for p in data["planned"]:
        run = runs.get(p["production_run_id"], {})
        events.append(
            dict(
                source="planned_downtime_events",
                id=p.get("id"),
                line=p.get("production_line") or run.get("production_line"),
                category="planned",
                machine=p.get("component") or "Line",
                description=p["reason"],
                start=p["started_at"],
                end=p.get("ended_at"),
            )
        )
    for f in data["faults"]:
        events.append(
            dict(
                source="downtime_events",
                id=f["id"],
                line=f["production_line"],
                category="unplanned",
                machine=f["machine"],
                description=f["reason"],
                start=f["opened_at"],
                end=f.get("resolved_at"),
            )
        )
    for s in data["stoppages"]:
        category = (
            "changeover"
            if s["kind"] == "changeover"
            else (
                "not_scheduled"
                if s["kind"] == "not_scheduled"
                else "planned" if s["kind"] == "handover" else "unplanned"
            )
        )
        events.append(
            dict(
                source="line_stoppages",
                id=s["id"],
                line=s["production_line"],
                category=category,
                machine="Line",
                description=s.get("reason") or s["kind"].replace("_", " ").title(),
                start=s["started_at"],
                end=s.get("ended_at"),
            )
        )
    totals = {
        k: dict(minutes=0.0, events=0) for k in ("planned", "unplanned", "changeover")
    }
    allocated = defaultdict(list)
    # Changeover first removes its duplicate planned-stop representation.
    rank = {"changeover": 0, "planned": 1, "unplanned": 2, "not_scheduled": 3}
    for e in sorted(
        events, key=lambda x: (rank[x["category"]], x["start"], str(x["id"]))
    ):
        bounds = (
            calc.intersect_intervals(
                [(e["start"], e["end"] or end)], [(window.start, end)]
            )
            if end > window.start
            else []
        )
        if not bounds:
            continue
        unscheduled = [
            (v["start"], v["end"] or end)
            for v in events
            if v["category"] == "not_scheduled" and v["line"] == e["line"]
        ]
        exclusive = calc.subtract_intervals(
            calc.subtract_intervals(bounds, unscheduled), allocated[e["line"]]
        )
        e["duration_minutes"] = float(calc.total_minutes(bounds))
        e["allocated_minutes"] = float(calc.total_minutes(exclusive))
        e["status"] = "Open" if e["end"] is None else "Completed"
        if e["category"] in totals:
            totals[e["category"]]["events"] += 1
            totals[e["category"]]["minutes"] += e["allocated_minutes"]
            allocated[e["line"]] += exclusive
    visible = sorted(
        (e for e in events if "duration_minutes" in e), key=lambda e: e["start"]
    )
    total = sum(t["minutes"] for t in totals.values())
    for t in totals.values():
        t["percent"] = t["minutes"] / total * 100 if total else 0
    drivers = defaultdict(lambda: dict(minutes=0.0, events=0))
    for e in visible:
        if e["category"] in totals:
            key = (e["line"], e["machine"], e["description"])
            drivers[key]["minutes"] += e["allocated_minutes"]
            drivers[key]["events"] += 1
    top = sorted(
        (dict(line=k[0], machine=k[1], reason=k[2], **v) for k, v in drivers.items()),
        key=lambda v: -v["minutes"],
    )[:5]
    return dict(
        total_minutes=total,
        breakdown=totals,
        events=visible,
        top_reasons=top,
        method="Recorded line-minutes, clipped to period; Not Scheduled excluded. Overlaps allocated once per line: changeover, planned, then unplanned. Event durations may overlap; allocated totals do not.",
    )


def hourly_totals(hourly):
    slots = [h for line in hourly["lines"] for h in line["hours"]]
    actual = [
        h["line"]["actual_packs"]
        for h in slots
        if h["line"]["actual_packs"] is not None
    ]
    targets = [
        h["line"]["target_packs"]
        for h in slots
        if h["line"]["target_packs"] is not None
    ]
    a = sum((Decimal(str(v)) for v in actual), Decimal(0)) if actual else None
    t = sum((Decimal(str(v)) for v in targets), Decimal(0)) if targets else None
    missing = sum(
        h["line"]["status"] in ("no_reading", "missing_reading", "invalid_config")
        or (
            h["line"]["actual_packs"] is None
            and h["line"]["status"] not in ("idle", "not_scheduled")
        )
        for h in slots
    )
    return dict(
        actual_packs=a,
        target_packs=t,
        output_percent=a / t * 100 if a is not None and t else None,
        missing_hours=missing,
        coverage_note=(
            "Recorded output only; missing readings are not zero."
            if missing
            else "Based on recorded line hours."
        ),
        running_minutes=sum(
            max(
                0,
                (h["line"].get("covered_minutes") or 0)
                - (h["line"].get("stopped_minutes") or 0),
            )
            for h in slots
        ),
        running_label="Time not recorded as stopped (line-minutes)",
    )


def aligned_oee(overview, metrics):
    """Withhold OEE when source coverage differs; never alter its formula."""
    oee = overview["estimated_oee"]
    output = overview["output"]
    mismatch = any(
        metrics[key] is not None
        and abs(float(metrics[key]) - float(output[source])) > 0.01
        for key, source in [
            ("actual_packs", "actual_packs"),
            ("target_packs", "expected_packs"),
        ]
    )
    if mismatch:
        return {
            **oee,
            "estimated_oee_percent": None,
            "calculation_status": "unavailable",
            "unavailable_reason": "Output and OEE source coverage differ. Review missing readings and recorded shift/period boundaries.",
        }
    return oee


def rolling_chart(source, window, now):
    """Group whole recorded periods by their end hour, matching dashboard membership.

    Boundary readings cannot be prorated without inventing counts. Between-run
    stops remain clipped to the actual window and are counted once.
    """
    hours = []
    for hour in clock_hours_between(window.start, min(window.end, now)):
        start, end = max(window.start, hour), min(window.end, now, hour + CLOCK_HOUR)
        part = TimeWindow("rolling_24h", "Recorded period end", start, end)
        rows = [
            row for row in source["hourly"] if start < row["period_ended_at"] <= end
        ]
        subset = {**source, "hourly": rows, "attribution_bounds": (start, end)}
        output = dashboard_reports.build_overview(subset, part, now)["output"]
        present = bool(output["hourly_update_count"])
        hours.append(
            dict(
                hour_start=hour,
                hour_label=hour.astimezone(FACTORY_TZ).strftime("%H:%M %Z"),
                line=dict(
                    actual_packs=output["actual_packs"] if present else None,
                    target_packs=output["expected_packs"] if present else None,
                    status="recorded" if present else "no_reading",
                ),
            )
        )
    return dict(
        lines=[dict(production_line="Selected lines", hours=hours)],
        basis="Whole recorded periods grouped by end hour. Boundary periods are not prorated.",
    )


def load_report(kind, line, window, actor, note, now):
    with db.get_database_connection() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute("SET LOCAL statement_timeout='20s'")
        connection.execute("SET LOCAL lock_timeout='2s'")
        data = db.get_hourly_report_data(
            window.start, window.end, line, connection=connection
        )
        if line and line not in data["lines"]:
            raise ValueError("Select a configured production line.")
        # Source IDs and detailed event fields for traceability, omitted by the dashboard reader.
        with connection.cursor(row_factory=dict_row) as cur:
            ids = [r["id"] for r in data["runs"]]
            cur.execute(
                "SELECT * FROM public.planned_downtime_events WHERE production_run_id=ANY(%s)",
                (ids,),
            )
            data["planned"] = cur.fetchall()
            cur.execute(
                "SELECT id,production_run_id,hour_start,period_started_at,period_ended_at FROM public.hourly_updates WHERE production_run_id=ANY(%s) AND hour_start >= %s AND hour_start < %s",
                (ids, window.start, window.end),
            )
            refs = cur.fetchall()
            cur.execute(
                "SELECT * FROM public.task_observations WHERE started_at >= %s AND started_at < %s AND (%s::text IS NULL OR production_line=%s) ORDER BY started_at",
                (window.start, min(window.end, now), line, line),
            )
            tasks = cur.fetchall()
        hourly = hourly_reports.build_hourly_report(data, window, now, line)
        overview_data = db.get_dashboard_window_data(
            window.start,
            window.end,
            line,
            window.kind != "rolling_24h",
            connection=connection,
        )
        overview = dashboard_reports.build_overview(overview_data, window, now)
        losses = loss_summary(data, window, now)
        eng = None
        if kind != "linetech":
            faults = db.get_engineering_faults(
                {"production_line": line} if line else {}, connection=connection
            )
            with connection.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT q.*,s.production_line,to_jsonb(c) AS changeover FROM public.casepacker_requests q
                    JOIN public.line_stoppages s ON s.id=q.line_stoppage_id
                    LEFT JOIN public.changeovers c ON c.line_stoppage_id=s.id
                    WHERE (%s::text IS NULL OR s.production_line=%s) AND s.production_line NOT ILIKE 'TEST-%%' AND q.requested_at<=%s""",
                    (line, line, now),
                )
                requests = cur.fetchall()
                for q in requests:
                    cur.execute(
                        "SELECT * FROM public.casepacker_updates WHERE request_id=%s ORDER BY id",
                        (q["id"],),
                    )
                    q["updates"] = cur.fetchall()
            eng = engineering_summary(faults, requests, window, now)
            fault_ids = {f["downtime_event_id"] for f in faults}
            stop_ids = {q["line_stoppage_id"] for q in requests}
            spans = defaultdict(list)
            for event in losses["events"]:
                if (
                    event["source"] == "downtime_events" and event["id"] in fault_ids
                ) or (event["source"] == "line_stoppages" and event["id"] in stop_ids):
                    spans[event["line"]] += calc.intersect_intervals(
                        [(event["start"], event["end"] or now)],
                        [(window.start, min(window.end, now))],
                    )
            eng["downtime_minutes"] = sum(
                float(
                    calc.total_minutes(
                        calc.subtract_intervals(
                            v,
                            [
                                (e["start"], e["end"] or now)
                                for e in losses["events"]
                                if e["category"] == "not_scheduled"
                                and e["line"] == name
                            ],
                        )
                    )
                )
                for name, v in spans.items()
            )
        shifts = []
        if kind == "management":
            d = window.start.astimezone(FACTORY_TZ).date() - timedelta(days=1)
            for offset in range(3):
                for name in ("Day", "Afternoon", "Night"):
                    sw = shift_window_for(d + timedelta(days=offset), name)
                    a, b = max(sw.start, window.start), min(sw.end, window.end, now)
                    if b <= a:
                        continue
                    part = TimeWindow("current_shift", sw.label, a, b)
                    shift_metrics = hourly_totals(
                        hourly_reports.build_hourly_report(data, part, now, line)
                    )
                    shift_source = db.get_dashboard_window_data(
                        part.start,
                        part.end,
                        line,
                        window.kind != "rolling_24h",
                        connection=connection,
                    )
                    shift_overview = dashboard_reports.build_overview(
                        shift_source, part, now
                    )
                    if window.kind == "rolling_24h":
                        shift_metrics = hourly_totals(
                            rolling_chart(shift_source, part, now)
                        )
                    shifts.append(
                        dict(
                            label=sw.label,
                            **shift_metrics,
                            oee=aligned_oee(shift_overview, shift_metrics),
                            downtime_minutes=loss_summary(data, part, now)[
                                "total_minutes"
                            ],
                            status=(
                                "Incomplete readings"
                                if shift_metrics["missing_hours"]
                                else (
                                    "Recorded"
                                    if shift_metrics["actual_packs"] is not None
                                    else "No readings / idle"
                                )
                            ),
                        )
                    )
    metrics = hourly_totals(hourly)
    if window.kind == "rolling_24h":
        running = metrics["running_minutes"]
        missing = metrics["missing_hours"]
        hourly = rolling_chart(overview_data, window, now)
        metrics = hourly_totals(hourly)
        metrics.update(
            running_minutes=running,
            missing_hours=missing,
            coverage_note=hourly["basis"],
        )
    warnings = []
    if metrics["actual_packs"] is None:
        warnings.append(
            "No production readings available for this period; this is not a zero-output result."
        )
    if metrics["output_percent"] is not None and metrics["output_percent"] > 100:
        warnings.append(
            "Output above target: review counts, conversions and standard speed."
        )
    if metrics["missing_hours"]:
        warnings.append("Missing hourly readings: production totals are incomplete.")
    if window.kind == "rolling_24h":
        warnings.append(
            "Rolling output and chart use whole recorded periods ending in the window; boundary periods are not prorated."
        )
    # The established dashboard OEE is the only A x P x Q calculation used.
    oee = overview["estimated_oee"] if kind == "management" else None
    warnings.extend(overview.get("data_issues", []))
    if oee:
        warnings.extend(oee.get("warnings", []))
        oee = aligned_oee(overview, metrics)
        if oee.get("unavailable_reason"):
            warnings.append(oee["unavailable_reason"])
    source_refs = [
        {"table": "production_runs", "id": r["id"]} for r in data["runs"]
    ] + [{"table": "hourly_updates", "id": r["id"]} for r in refs]
    source_refs += [
        {"table": e["source"], "id": e["id"]}
        for e in losses["events"]
        if e["id"] is not None
    ]
    source_refs += [{"table": "task_observations", "id": r["id"]} for r in tasks]
    if kind == "management" or window.kind == "rolling_24h":
        source_refs += [
            {"table": "production_runs", "id": r["id"]} for r in overview_data["runs"]
        ]
        source_refs += [
            {"table": "hourly_updates", "id": r["id"]} for r in overview_data["hourly"]
        ]
    source_refs += [
        {"table": "run_operating_speed_changes", "id": r["id"]}
        for r in data.get("operating_changes", [])
        if r.get("id") is not None
    ]
    if eng:
        for j in eng["jobs"] + eng["outstanding"]:
            source_refs += [{"table": j["source"], "id": j["id"]}] + j["source_refs"]
            if j.get("changeover"):
                source_refs.append(
                    {"table": "changeovers", "id": j["changeover"]["id"]}
                )
    source_refs = list({(r["table"], r["id"]): r for r in source_refs}.values())
    key_events = list(losses["events"])
    for task in tasks:
        key_events.append(
            dict(
                start=task["started_at"],
                machine="Recorded task",
                description=task["task"],
                duration_minutes=(
                    float(calc.duration_minutes(task["started_at"], task["ended_at"]))
                    if task.get("ended_at")
                    else None
                ),
                status=(
                    "Passed"
                    if task.get("completed_successfully") is True
                    else (
                        "Review required"
                        if task.get("completed_successfully") is False
                        else "Recorded"
                    )
                ),
            )
        )
    key_events.sort(key=lambda e: e["start"])
    payload = dict(
        id=str(uuid4()),
        type=kind,
        line=line or "All lines",
        window=window.to_api(),
        generated_at=now,
        generated_by=dict(role=actor.role, name=actor.name),
        version=1,
        template_version=1,
        is_current=now < window.end,
        metrics=metrics,
        hourly=hourly,
        losses=losses,
        engineering=eng,
        oee=oee,
        shifts=shifts,
        technicians=sorted(
            {r["line_technician"] for r in data["runs"] if r.get("line_technician")}
        ),
        tasks=tasks,
        key_events=key_events,
        handover_note=note,
        source_refs=source_refs,
        warnings=warnings,
        insights=[
            f"{len(losses['events'])} recorded stop events in the reporting period."
        ]
        + (
            [f"{eng['open']} Engineering jobs outstanding at generation."]
            if eng
            else []
        ),
    )
    return jsonable_encoder(payload, custom_encoder={Decimal: float})
