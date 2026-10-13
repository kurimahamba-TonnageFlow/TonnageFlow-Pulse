"""Authenticated report generation, immutable history and retryable exports."""

from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID
import hashlib, json
from fastapi import APIRouter, Depends, Header, HTTPException, Response, Query
from pydantic import BaseModel, Field
from .report_auth import ReportActor, ReportKind, require_report_actor
from . import report_store, shift_reports, report_render, database
from .factory_time import TimeWindow
from .api_logging import log_operation_failure


def prevent_caching(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    dependencies=[Depends(prevent_caching)], prefix="/api/v1/reports", tags=["reports"]
)


class GenerateReport(BaseModel):
    report_type: ReportKind
    line: str | None = Field(default=None, max_length=100)
    mode: Literal["current", "previous", "shift", "production_day", "rolling_24h"] = (
        "current"
    )
    day: date | None = None
    shift: Literal["Day", "Afternoon", "Night"] = "Day"
    note: str = Field(default="", max_length=500)
    revision_of: UUID | None = None


@router.get("/options")
def options(actor: ReportActor = Depends(require_report_actor)):
    with database.get_database_connection() as con:
        rows = con.execute(
            "SELECT name FROM public.production_lines WHERE active=TRUE ORDER BY display_order,name"
        ).fetchall()
    return {"report_types": sorted(actor.report_types), "lines": [r[0] for r in rows]}


@router.post("")
def generate(
    payload: GenerateReport,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    actor: ReportActor = Depends(require_report_actor),
):
    actor.require(payload.report_type)
    now = datetime.now(timezone.utc)
    if payload.report_type != "management" and payload.mode in (
        "production_day",
        "rolling_24h",
    ):
        raise HTTPException(422, "Select a shift for this report type.")
    fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()

    def build(prior):
        window = shift_reports.resolve_period(
            payload.mode, payload.day, payload.shift, now
        )
        line = payload.line
        if prior:
            if prior["type"] != payload.report_type:
                raise HTTPException(422, "A revision must retain its report type.")
            w = prior["window"]
            window = TimeWindow(
                w["kind"],
                w["label"],
                datetime.fromisoformat(w["start"]),
                datetime.fromisoformat(w["end"]),
            )
            line = None if prior["line"] == "All lines" else prior["line"]
        if window.start > now:
            raise HTTPException(422, "Select a shift that has started.")
        return shift_reports.load_report(
            payload.report_type, line, window, actor, payload.note, now
        )

    try:
        return report_store.generate_once(
            actor, idempotency_key, fingerprint, payload.revision_of, build
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except Exception as error:
        log_operation_failure("reports", "generate", error)
        raise HTTPException(
            503,
            "Report generation is unavailable. Your production records are unchanged. Retry the same request.",
        ) from None


@router.get("")
def history(
    offset: int = Query(default=0, ge=0, le=1000000),
    actor: ReportActor = Depends(require_report_actor),
):
    try:
        return {"reports": report_store.history(actor, offset)}
    except Exception as error:
        log_operation_failure("reports", "history", error)
        raise HTTPException(
            503, "Report history is unavailable. Please try again."
        ) from None


@router.get("/{report_id}")
def detail(report_id: UUID, actor: ReportActor = Depends(require_report_actor)):
    return report_store.get_report(report_id, actor)


@router.get("/{report_id}/preview")
def preview(report_id: UUID, actor: ReportActor = Depends(require_report_actor)):
    report = report_store.get_report(report_id, actor)
    return {"html": report_render.render_report(report)}


@router.get("/{report_id}/export/{format}")
def export(
    report_id: UUID,
    format: Literal["png", "pdf"],
    actor: ReportActor = Depends(require_report_actor),
):
    report = report_store.get_report(report_id, actor)
    try:
        data = report_render.export_report(report, format)
    except Exception as error:
        log_operation_failure("reports", "export", error)
        raise HTTPException(
            503, "Export failed or is busy. Your saved report is safe; please retry."
        ) from None
    return Response(
        data,
        media_type="image/png" if format == "png" else "application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="pulse-{report["type"]}-{report_id}.{format}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
