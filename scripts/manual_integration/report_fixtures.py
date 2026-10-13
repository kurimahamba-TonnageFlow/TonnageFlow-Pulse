"""Synthetic report visuals, never live factory figures."""

import runpy
from pathlib import Path
from datetime import datetime, timezone, timedelta
from fastapi.encoders import jsonable_encoder
from decimal import Decimal
from src import shift_reports as r, hourly_reports, hourly_performance as performance
from src.report_auth import ReportActor


def fixtures():
    t = runpy.run_path(str(Path("tests/test_shift_reports.py")))
    d = t["data"]()
    w = t["W"]
    now = t["NOW"]
    at = t["moment"]
    d["planned"] = [
        dict(
            id=1,
            production_run_id=1,
            reason="Film change",
            started_at=at(7),
            ended_at=at(7, 15),
        )
    ]
    d["faults"] = [
        dict(
            id=2,
            production_line="Rovema",
            machine="SBS BV2",
            reason="Top seal fault",
            opened_at=at(9),
            resolved_at=at(9, 25),
        )
    ]
    hourly = hourly_reports.build_hourly_report(d, w, now)
    eng = r.engineering_summary(
        [
            dict(
                downtime_event_id=2,
                production_line="Rovema",
                machine="SBS BV2",
                reason="Top seal fault",
                engineer="Sample engineer",
                opened_at=at(9),
                accepted_at=at(9, 5),
                repair_updates=[
                    dict(
                        id=1,
                        created_at=at(9, 20),
                        engineering_status="Resolved",
                        action="Recorded setting adjustment",
                    )
                ],
            ),
            dict(
                downtime_event_id=3,
                production_line="Rovema",
                machine="Casepacker",
                reason="Infeed jam",
                engineer=None,
                opened_at=at(4),
                repair_updates=[],
            ),
        ],
        [],
        w,
        now,
    )
    eng["downtime_minutes"] = 25
    result = {}
    for kind in ("linetech", "engineering", "management"):
        result[kind] = jsonable_encoder(
            dict(
                id={
                    "linetech": "00000000-0000-4000-8000-000000000001",
                    "engineering": "00000000-0000-4000-8000-000000000002",
                    "management": "00000000-0000-4000-8000-000000000003",
                }[kind],
                type=kind,
                line="Rovema",
                window=w.to_api(),
                generated_at=now,
                generated_by={"role": "management", "name": "Synthetic preview"},
                version=1,
                template_version=1,
                is_current=False,
                metrics=r.hourly_totals(hourly),
                hourly=hourly,
                losses=r.loss_summary(d, w, now),
                engineering=eng if kind != "linetech" else None,
                oee={
                    "estimated_oee_percent": None,
                    "unavailable_reason": "Awaiting downtime data",
                    "output_basis": "Palletised packs - provisional output basis",
                },
                shifts=[dict(label=w.label, **r.hourly_totals(hourly))],
                technicians=["Sample technician"],
                tasks=[],
                handover_note="Synthetic preview only. Check the outstanding casepacker job before restart.",
                source_refs=[{"table": "production_runs", "id": 1}],
                warnings=[
                    "Synthetic demonstration data - not a live production report."
                ],
                insights=["One older Engineering job remains outstanding."],
            ),
            custom_encoder={Decimal: float},
        )
    # A full factory day with three separate shift runs and actual hourly records.
    full = t["data"]()
    full["runs"] = []
    full["readings"] = []
    full["planned"] = d["planned"]
    full["faults"] = d["faults"]
    periods = []
    shifts = []
    day = r.resolve_period("production_day", w.start.date(), "Day", now)
    for index, (name, pallets) in enumerate(
        [("Day", 36), ("Afternoon", 30), ("Night", 42)]
    ):
        sw = r.resolve_period("shift", w.start.date(), name, day.end)
        run = {
            **d["runs"][0],
            "id": index + 1,
            "shift": name,
            "started_at": sw.start,
            "finished_at": sw.end,
        }
        full["runs"].append(run)
        for hour in range(8):
            start = sw.start + timedelta(hours=hour)
            full["readings"].append(
                dict(
                    id=index * 8 + hour + 1,
                    production_run_id=run["id"],
                    hour_start=start,
                    pallets_completed=Decimal(pallets),
                )
            )
            periods.append(
                performance.period_report(
                    100,
                    start,
                    start + timedelta(hours=1),
                    pallets * 100,
                    full["planned"],
                    full["faults"],
                )
            )
        part = hourly_reports.build_hourly_report(full, sw, day.end)
        shifts.append(
            dict(
                label=sw.label,
                **r.hourly_totals(part),
                oee=performance.aggregate_oee(periods[index * 8 :]),
                downtime_minutes=r.loss_summary(full, sw, day.end)["total_minutes"],
                status="Recorded"
            )
        )
    all_hours = hourly_reports.build_hourly_report(full, day, day.end)
    result["management"].update(
        window=day.to_api(),
        generated_at=day.end,
        metrics=r.hourly_totals(all_hours),
        hourly=all_hours,
        losses=r.loss_summary(full, day, day.end),
        shifts=shifts,
        oee=performance.aggregate_oee(periods),
    )
    return jsonable_encoder(result, custom_encoder={Decimal: float})
