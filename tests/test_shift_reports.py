from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from src import (
    shift_reports as r,
    hourly_reports,
    report_store,
    reports_api,
    report_render,
    management_auth,
    engineering_auth,
    hmi_auth,
)
from src.report_auth import ReportActor
from src.api import app

NOW = datetime(2026, 1, 12, 14, tzinfo=timezone.utc)
W = r.resolve_period("shift", date(2026, 1, 12), "Day", NOW)


def moment(hour, minute=0):
    return NOW.replace(hour=hour, minute=minute)


def data():
    return {
        "lines": ["Rovema"],
        "runs": [
            {
                "id": 1,
                "production_line": "Rovema",
                "line_technician": "Liam",
                "shift": "Day",
                "customer": "Sample customer",
                "product": "Rice",
                "format": "1 x 10",
                "pack_weight_kg": Decimal(1),
                "packs_per_case": 10,
                "cases_per_pallet": 10,
                "standard_speed_ppm": Decimal(100),
                "target_speed_ppm": Decimal(100),
                "status": "Completed",
                "started_at": moment(6),
                "finished_at": moment(14),
            }
        ],
        "readings": [
            {
                "id": i,
                "production_run_id": 1,
                "hour_start": moment(i),
                "pallets_completed": Decimal(30),
            }
            for i in range(6, 14)
        ],
        "speed_changes": [],
        "operating_changes": [],
        "planned": [],
        "faults": [],
        "stoppages": [],
    }


def test_pack_conversion_and_weighted_output():
    result = r.hourly_totals(hourly_reports.build_hourly_report(data(), W, NOW))
    assert result["actual_packs"] == 24000
    assert result["target_packs"] == 48000
    assert result["output_percent"] == 50


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 28), 7), (date(2026, 10, 24), 9)])
def test_dst_night(day, hours):
    w = r.resolve_period("shift", day, "Night", NOW)
    assert (w.end - w.start).total_seconds() == hours * 3600


def test_production_day_and_previous_shift():
    w = r.resolve_period("production_day", date(2026, 1, 12), "Day", NOW)
    assert w.start.hour == 6 and w.end.hour == 6 and w.end.day == 13
    assert r.resolve_period("previous", None, "Day", NOW).start == moment(6)


def test_missing_and_zero_are_distinct():
    d = data()
    d["readings"] = []
    missing = r.hourly_totals(hourly_reports.build_hourly_report(d, W, NOW))
    assert missing["actual_packs"] is None and missing["missing_hours"] == 8
    d["readings"] = [
        {
            "production_run_id": 1,
            "hour_start": moment(i),
            "pallets_completed": Decimal(0),
        }
        for i in range(6, 14)
    ]
    zero = r.hourly_totals(hourly_reports.build_hourly_report(d, W, NOW))
    assert (
        zero["actual_packs"] == 0
        and zero["output_percent"] == 0
        and zero["missing_hours"] == 0
    )


def test_partial_hour_and_above_target_not_capped():
    d = data()
    d["runs"][0]["started_at"] = moment(6, 30)
    d["runs"][0]["finished_at"] = moment(7)
    d["readings"] = [
        {
            "production_run_id": 1,
            "hour_start": moment(6),
            "pallets_completed": Decimal(110),
        }
    ]
    totals = r.hourly_totals(hourly_reports.build_hourly_report(d, W, moment(7)))
    assert totals["target_packs"] == 3000 and totals["actual_packs"] == 11000
    assert totals["output_percent"] > 366


def test_overlapping_loss_buckets_are_exclusive():
    d = data()
    d["planned"] = [
        {
            "id": 1,
            "production_run_id": 1,
            "reason": "Changeover",
            "started_at": moment(6),
            "ended_at": moment(7),
        }
    ]
    d["faults"] = [
        {
            "id": 2,
            "production_line": "Rovema",
            "machine": "SBS",
            "reason": "Seal",
            "opened_at": moment(6, 30),
            "resolved_at": moment(7, 30),
        }
    ]
    d["stoppages"] = [
        {
            "id": 3,
            "production_line": "Rovema",
            "kind": "changeover",
            "started_at": moment(6),
            "ended_at": moment(7),
            "reason": None,
        }
    ]
    losses = r.loss_summary(d, W, NOW)
    assert losses["total_minutes"] == 90
    assert losses["breakdown"]["changeover"]["minutes"] == 60
    assert losses["breakdown"]["planned"]["minutes"] == 0
    assert losses["breakdown"]["unplanned"]["minutes"] == 30


def test_not_scheduled_is_excluded():
    d = data()
    d["stoppages"] = [
        {
            "id": 1,
            "production_line": "Rovema",
            "kind": "not_scheduled",
            "reason": "No order",
            "started_at": moment(6),
            "ended_at": moment(7),
        }
    ]
    d["faults"] = [
        {
            "id": 2,
            "production_line": "Rovema",
            "machine": "SBS",
            "reason": "Seal",
            "opened_at": moment(6),
            "resolved_at": moment(7),
        }
    ]
    assert r.loss_summary(d, W, NOW)["total_minutes"] == 0


def test_engineering_older_open_and_completed_in_period():
    base = {
        "downtime_event_id": 1,
        "production_line": "Rovema",
        "machine": "SBS",
        "reason": "Seal",
        "engineer": "Engineer",
        "opened_at": moment(4),
        "accepted_at": moment(5),
        "resolved_at": moment(13),
        "repair_updates": [],
    }
    closed = {
        **base,
        "downtime_event_id": 2,
        "repair_updates": [
            {
                "id": 3,
                "created_at": moment(8),
                "engineering_status": "Resolved",
                "action": "Recorded repair",
            }
        ],
    }
    result = r.engineering_summary([base, closed], [], W, NOW)
    assert result["raised"] == 0 and result["completed"] == 1 and result["open"] == 1
    assert result["outstanding"][0]["older"]
    assert result["jobs"][0]["completed_at"] == moment(
        8
    )  # not production restart at 13:00
    assert (
        result["average_response_minutes"] is None
        and result["average_work_minutes"] is None
    )


def test_changeover_response_and_qa_reference():
    q = {
        "id": 1,
        "production_line": "Rovema",
        "details": "Size change",
        "requested_at": moment(7),
        "first_accepted_at": moment(7, 12),
        "ready_at": moment(8),
        "engineer": "Lee",
        "updates": [],
        "changeover": {"id": 4, "workflow": {"qa": "recorded"}},
    }
    result = r.engineering_summary([], [q], W, NOW)
    assert result["average_response_minutes"] == 12
    assert result["jobs"][0]["changeover"]["id"] == 4


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        report_store,
        "history",
        lambda actor, offset=0: [
            {"report_type": k} for k in sorted(actor.report_types)
        ],
    )
    yield TestClient(app)


def test_engineering_history_includes_lintech_not_management(client):
    token = engineering_auth.create_session("Test")["token"]
    try:
        response = client.get(
            "/api/v1/reports", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 200
        assert {v["report_type"] for v in response.json()["reports"]} == {
            "engineering",
            "linetech",
        }
        response = client.post(
            "/api/v1/reports",
            headers={
                "Authorization": "Bearer " + token,
                "Idempotency-Key": str(uuid4()),
            },
            json={"report_type": "management"},
        )
        assert response.status_code == 403
    finally:
        engineering_auth.revoke_session(token)


def test_missing_credentials_cannot_use_history(client):
    assert client.get("/api/v1/reports").status_code == 401


def test_export_failure_keeps_saved_snapshot(client, monkeypatch):
    token = management_auth.create_session("Test")["token"]
    saved = {"id": str(uuid4()), "type": "management"}
    monkeypatch.setattr(report_store, "get_report", lambda *args: saved)
    monkeypatch.setattr(
        report_render,
        "export_report",
        lambda *args: (_ for _ in ()).throw(RuntimeError("secret internal path")),
    )
    try:
        response = client.get(
            "/api/v1/reports/" + saved["id"] + "/export/pdf",
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 503 and "secret" not in response.text
        assert (
            client.get(
                "/api/v1/reports/" + saved["id"],
                headers={"Authorization": "Bearer " + token},
            ).json()
            == saved
        )
    finally:
        management_auth.revoke_session(token)


def test_rolling_chart_and_headline_share_boundary_records():
    import runpy

    window_data = runpy.run_path("tests/test_dashboard_reports.py")["window_data"]
    from src import dashboard_reports

    source = window_data()
    window = r.TimeWindow("rolling_24h", "Boundary test", moment(6, 30), moment(9, 30))
    source["attribution_bounds"] = (window.start, window.end)
    chart = r.rolling_chart(source, window, NOW)
    expected = dashboard_reports.build_overview(source, window, NOW)["output"]
    totals = r.hourly_totals(chart)
    assert float(totals["actual_packs"]) == pytest.approx(expected["actual_packs"])
    assert float(totals["target_packs"]) == pytest.approx(expected["expected_packs"])
    assert chart["lines"][0]["hours"][0]["hour_start"] == moment(6)


def test_reports_escape_source_notes_and_show_explicit_quality_basis():
    from scripts.manual_integration.report_fixtures import fixtures

    reports = fixtures()
    reports["management"]["handover_note"] = '<script>alert("unsafe")</script>'
    html = report_render.render_report(reports["management"])
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "(provisional)" in html
    assert len(reports["management"]["shifts"]) == 3
    assert reports["management"]["metrics"]["actual_packs"] == 86400
    assert reports["management"]["metrics"]["target_packs"] == 144000


def test_oee_withheld_when_source_membership_differs():
    overview = {
        "output": {"actual_packs": 3000, "expected_packs": 6000},
        "estimated_oee": {"estimated_oee_percent": 49.0},
    }
    assert (
        r.aligned_oee(overview, {"actual_packs": 3000, "target_packs": 6000})[
            "estimated_oee_percent"
        ]
        == 49.0
    )
    result = r.aligned_oee(overview, {"actual_packs": 6000, "target_packs": 12000})
    assert result["estimated_oee_percent"] is None
    assert "coverage differ" in result["unavailable_reason"]
    assert overview["estimated_oee"]["estimated_oee_percent"] == 49.0
