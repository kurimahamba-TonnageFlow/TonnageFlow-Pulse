"""One escaped HTML template for preview, compact PNG and paginated PDF."""

from html import escape
import json
import os
import threading
from datetime import datetime
from .factory_time import FACTORY_TZ

_EXPORT_SLOT = threading.BoundedSemaphore(1)
TITLES = {
    "linetech": "LineTech Shift Report",
    "engineering": "Engineering Shift Report",
    "management": "Management Shift Report",
}


def text(value):
    return escape(str(value if value is not None else "Not Available"))


def number(value, suffix=""):
    return (
        "Not Available"
        if value is None
        else f"{value:,.1f}{suffix}" if suffix else f"{value:,.0f}"
    )


def time(value):
    if not value:
        return "Not Available"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(FACTORY_TZ).strftime("%d %b %H:%M %Z")


def table(headers, rows):
    return (
        "<table><thead><tr>"
        + "".join("<th>" + text(h) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + (
            "".join(
                "<tr>" + "".join("<td>" + text(v) + "</td>" for v in row) + "</tr>"
                for row in rows
            )
            or f'<tr><td colspan="{len(headers)}">No recorded events</td></tr>'
        )
        + "</tbody></table>"
    )


def render_report(r, compact=False):
    kind = r["type"]
    accent = {"linetech": "#16bc65", "engineering": "#f1b02e", "management": "#a277ef"}[
        kind
    ]
    m = r["metrics"]
    eng = r.get("engineering")
    oee = r.get("oee") or {}
    limit = 6 if compact else 100000
    kpis = [
        ("Output vs Target", number(m["output_percent"], "%")),
        ("Actual Packs", number(m["actual_packs"])),
        ("Target Packs", number(m["target_packs"])),
        ("Running time*", number(m["running_minutes"], " min")),
    ]
    if kind == "engineering":
        kpis = [
            ("Jobs Raised", number(eng["raised"])),
            ("Jobs Completed", number(eng["completed"])),
            ("Jobs Still Open", number(eng["open"])),
            ("Engineering downtime", number(eng.get("downtime_minutes"), " min")),
        ]
    if kind == "management":
        kpis = [
            ("Estimated OEE", number(oee.get("estimated_oee_percent"), "%")),
            ("Actual Packs", number(m["actual_packs"])),
            ("Target Packs", number(m["target_packs"])),
            ("Total downtime", number(r["losses"]["total_minutes"], " min")),
        ]
    sections = []
    if kind != "engineering":
        hours = {}
        for line in r["hourly"]["lines"]:
            for h in line["hours"]:
                row = hours.setdefault(
                    h["hour_start"],
                    dict(
                        label=h["hour_label"],
                        actual=[],
                        target=[],
                        missing=False,
                        statuses=set(),
                    ),
                )
                row["statuses"].add(h["line"]["status"])
                if h["line"]["actual_packs"] is not None:
                    row["actual"].append(h["line"]["actual_packs"])
                elif h["line"]["status"] not in ("idle", "not_scheduled"):
                    row["missing"] = True
                if h["line"]["target_packs"] is not None:
                    row["target"].append(h["line"]["target_packs"])
        maxv = max(
            [1] + [sum(h[k]) for h in hours.values() for k in ("actual", "target")]
        )
        bars = []
        for h in hours.values():
            a = sum(h["actual"]) if h["actual"] else None
            t = sum(h["target"]) if h["target"] else None
            state = (
                "Missing"
                if h["missing"]
                else (
                    "Not scheduled"
                    if h["statuses"] == {"not_scheduled"}
                    else "Idle" if a is None else number(a)
                )
            )
            bars.append(
                f'<div class="bar"><div class="bar-pair"><i style="height:{(t or 0)/maxv*110}px" title="Target {text(number(t))}"></i><b style="height:{(a or 0)/maxv*110}px" title="Actual {text(number(a))}"></b></div><small>{text(h["label"][:5])}</small><small>{state}</small></div>'
            )
        sections.append(
            '<h2>Hourly output</h2><p class="muted">Grey: target &middot; Accent: actual &middot; Blank readings are not zero</p><div class="chart">'
            + "".join(bars)
            + "</div><p>"
            + text(r["hourly"].get("basis", m["coverage_note"]))
            + "</p>"
        )
    if kind == "management":
        sections.append(
            "<h2>Shift comparison</h2>"
            + table(
                ["Shift", "Actual packs", "Target packs", "Output vs Target"],
                [
                    (
                        s["label"],
                        number(s["actual_packs"]),
                        number(s["target_packs"]),
                        number(s["output_percent"], "%"),
                    )
                    for s in r["shifts"]
                ],
            )
        )
        sections.append(
            table(
                ["Shift", "Estimated OEE", "Downtime", "Status"],
                [
                    (
                        s["label"],
                        number(s.get("oee", {}).get("estimated_oee_percent"), "%"),
                        number(s.get("downtime_minutes"), " min"),
                        s.get("status", "Preview"),
                    )
                    for s in r["shifts"]
                ],
            )
        )
        sections.append(
            "<p>"
            + text(oee.get("unavailable_reason") or oee.get("calculation_method"))
            + "</p><p>"
            + text(oee.get("output_basis"))
            + "</p><p>Quality: "
            + number(oee.get("estimated_quality_percent"), "%")
            + " ("
            + text(oee.get("quality_basis", "unavailable"))
            + ")</p>"
        )
    if eng and (kind == "engineering" or not compact):
        statuses = eng["statuses"]
        total = sum(statuses.values())
        completed = statuses.get("Completed", 0)
        percentage = completed / total * 100 if total else 0
        donut = f'<div class="donut" style="background:conic-gradient(#18b078 0 {percentage}%,#f26464 {percentage}% 100%)"><span>{total}<small>Active in period</small></span></div>'
        status_html = "".join(
            f'<span class="status">{text(k)} <strong>{v}</strong></span>'
            for k, v in statuses.items()
        )
        sections.append(
            '<h2>Engineering job status</h2><div class="status-row">'
            + donut
            + status_html
            + "</div><p>Average response: "
            + number(eng["average_response_minutes"], " min")
            + " ("
            + str(eng["response_sample_count"])
            + " timestamped requests). Active work duration: Not Available.</p>"
        )
        sections.append(
            "<h2>Engineering jobs</h2>"
            + table(
                [
                    "Raised",
                    "Line / machine",
                    "Fault / task",
                    "Engineer",
                    "Work time",
                    "Status",
                ],
                [
                    (
                        time(j["raised_at"]),
                        j["line"] + " / " + j["machine"],
                        j["description"],
                        j["engineer"] or "Unassigned",
                        number(j["work_minutes"], " min"),
                        j["status"],
                    )
                    for j in eng["jobs"][:limit]
                ],
            )
        )
        sections.append(
            "<h2>Outstanding jobs &middot; handover</h2>"
            + table(
                [
                    "Raised / requested",
                    "Machine / status",
                    "Issue",
                    "Owner",
                    "Latest action",
                ],
                [
                    (
                        time(j["raised_at"]) + (" (older)" if j["older"] else ""),
                        j["machine"] + " / " + j["status"],
                        j["description"],
                        j["engineer"] or "Unassigned",
                        j["latest_action"],
                    )
                    for j in eng["outstanding"][:limit]
                ],
            )
        )
        if not compact:
            sections.append("<p>" + text(eng["timing_note"]) + "</p>")
            for j in eng["jobs"]:
                sections.append(
                    "<h3>"
                    + text(j["machine"])
                    + " &middot; "
                    + text(j["description"])
                    + "</h3>"
                    + table(
                        [
                            "Recorded",
                            "Engineer",
                            "Finding / note",
                            "Action / setting",
                            "Setting change / reason",
                        ],
                        [
                            (
                                time(u.get("created_at")),
                                u.get("engineer"),
                                u.get("finding") or u.get("note"),
                                u.get("action") or u.get("setting_name"),
                                (
                                    " / ".join(
                                        str(u.get(k))
                                        for k in (
                                            "setting_name",
                                            "previous_value",
                                            "new_value",
                                            "reason_for_change",
                                            "notes",
                                        )
                                        if u.get(k) is not None
                                    )
                                    or None
                                ),
                            )
                            for u in j["updates"]
                        ],
                    )
                )
                if j.get("changeover"):
                    c = j["changeover"]
                    sections.append(
                        "<h3>Changeover / QA reference "
                        + text(c.get("id"))
                        + "</h3>"
                        + table(
                            ["Field", "Recorded value"],
                            [
                                ("Previous format", c.get("previous_format")),
                                ("New format", c.get("new_format")),
                                (
                                    "Previous pack weight (kg)",
                                    c.get("previous_pack_weight_kg"),
                                ),
                                ("New pack weight (kg)", c.get("new_pack_weight_kg")),
                                ("Requested", time(j["request_at"])),
                                ("Accepted", time(j["accepted_at"])),
                                ("Engineering ready", time(j["completed_at"])),
                                ("Engineer", j["engineer"]),
                                ("Status", j["status"]),
                                (
                                    "Recorded workflow / QA",
                                    json.dumps(c.get("workflow"), default=str),
                                ),
                            ],
                        )
                    )
    sections.append(
        '<h2>Downtime summary</h2><div class="losses">'
        + "".join(
            "<div><strong>"
            + number(v["minutes"], " min")
            + "</strong><span>"
            + text(k.title())
            + " &middot; "
            + str(v["events"])
            + " events"
            + (" &middot; " + number(v["percent"], "%") if kind == "management" else "")
            + "</span></div>"
            for k, v in r["losses"]["breakdown"].items()
        )
        + "</div>"
    )
    if kind == "linetech" or (kind == "management" and not compact):
        sections.append(
            "<h2>Key events</h2>"
            + table(
                ["Time", "Machine / type", "Details", "Duration", "Status"],
                [
                    (
                        time(e["start"]),
                        e["machine"]
                        + " / "
                        + e.get("category", "task").replace("_", " ").title(),
                        e["description"],
                        number(e["duration_minutes"], " min"),
                        e["status"],
                    )
                    for e in r.get("key_events", r["losses"]["events"])[:limit]
                ],
            )
        )
        if not compact:
            sections.append(
                "<h2>Recorded checks and tasks</h2>"
                + table(
                    ["Time", "Task", "Technician", "Outcome", "Note"],
                    [
                        (
                            time(t.get("started_at")),
                            t.get("task"),
                            t.get("technician"),
                            (
                                "Passed"
                                if t.get("completed_successfully") is True
                                else (
                                    "Review required"
                                    if t.get("completed_successfully") is False
                                    else "Not recorded"
                                )
                            ),
                            t.get("notes"),
                        )
                        for t in r["tasks"]
                    ],
                )
            )
    if kind == "management":
        sections.append(
            "<h2>Top downtime reasons</h2>"
            + table(
                ["Line / machine", "Reason", "Allocated time"],
                [
                    (
                        t["line"] + " / " + t["machine"],
                        t["reason"],
                        number(t["minutes"], " min"),
                    )
                    for t in r["losses"]["top_reasons"]
                ],
            )
        )
    if kind == "linetech":
        sections.append(
            "<h2>Outstanding issues</h2>"
            + table(
                ["Machine", "Issue", "Raised"],
                [
                    (e["machine"], e["description"], time(e["start"]))
                    for e in r["losses"]["events"]
                    if e["status"] == "Open"
                ][:limit],
            )
        )
    notes = (
        r["warnings"]
        + r["insights"]
        + ([r["handover_note"]] if r.get("handover_note") else [])
    )
    sections.append(
        "<h2>Handover notes</h2>" + "".join("<p>" + text(n) + "</p>" for n in notes)
    )
    if compact and kind == "linetech":
        sections.append(
            '<p class="muted">*Time not recorded as stopped. Not a verified running-time measurement.</p>'
        )
    if compact:
        sections.append(
            '<p class="muted">Compact summary: up to six records per table. Download the detailed PDF for all events, checks and source references.</p>'
        )
    else:
        sections.append(
            "<h2>Calculation and traceability</h2><p>"
            + text(r["losses"]["method"])
            + "</p><p>*Running time is time not recorded as stopped; it is not a verified machine running-time measurement.</p><p>Source references: "
            + text(json.dumps(r["source_refs"]))
            + "</p>"
        )
    css = """*{box-sizing:border-box}body{margin:0;background:#0b1d2a;color:#ecf4fa;font:14px Arial,sans-serif}article{max-width:940px;margin:auto;border:1px solid var(--accent);border-radius:12px;overflow:hidden}header{padding:24px 28px;background:linear-gradient(110deg,#112b35,#1c2235)}h1{font-size:30px;border-left:8px solid var(--accent);padding-left:18px;margin:22px 0 10px}h2{font-size:18px;margin:24px 0 12px}h3{font-size:15px}header p{margin:6px 0}.brand{font-size:20px;font-weight:bold}.content{background:#f6f9fc;color:#102037;padding:20px 24px}p{line-height:1.5;overflow-wrap:anywhere}.kpis,.losses{display:flex;gap:10px}.kpis>div,.losses>div{flex:1;background:#e8eff6;border-radius:8px;padding:15px 10px;text-align:center}.kpis strong{display:block;font-size:25px}.kpis span,.losses span{display:block;font-size:12px;margin-top:7px}.kpis>div:first-child{background:color-mix(in srgb,var(--accent) 22%,white)}table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:12px}th{background:#e3edf4;text-align:left}td,th{padding:8px;border-bottom:1px solid #d5e1ed;overflow-wrap:anywhere;vertical-align:top}thead{display:table-header-group}tr{break-inside:avoid}.chart{display:flex;gap:5px;align-items:flex-end;border-bottom:1px solid #b0c1cf;padding-top:12px}.bar{flex:1;min-width:0;text-align:center}.bar-pair{height:110px;display:flex;align-items:flex-end;justify-content:center;gap:3px}.bar i,.bar b{display:block;width:35%;background:#8ba3b7}.bar b{background:var(--accent)}.bar small{display:block;font-size:9px;overflow-wrap:anywhere;margin-top:5px}.muted{color:#587083;font-size:11px}.donut{width:130px;height:130px;border-radius:50%;display:grid;place-items:center;flex-shrink:0}.donut>span{width:90px;height:90px;border-radius:50%;background:#f6f9fc;display:flex;flex-direction:column;justify-content:center;text-align:center;font-size:28px;font-weight:bold}.donut small{font-size:10px;font-weight:normal}.status-row{display:flex;align-items:center;flex-wrap:wrap;gap:10px}.status{background:#e8eff6;padding:12px;border-left:4px solid var(--accent)}footer{background:#112b35;padding:18px 24px;font-size:11px;overflow-wrap:anywhere}dialog{color:inherit}@media print{body{background:white}article{border:0;max-width:none}header{-webkit-print-color-adjust:exact}.content{padding:10px 0}h2,h3{break-after:avoid}}@media(max-width:540px){.chart{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px 5px}.bar small{font-size:10px}header,.content{padding:16px}h1{font-size:23px}.kpis{display:grid;grid-template-columns:1fr 1fr}.kpis strong{font-size:22px}td,th{padding:5px;font-size:10px}}"""
    if compact:
        css += ".content{font-size:16px}.chart{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:12px 5px}.bar small{font-size:11px}td,th{font-size:12px}footer{font-size:12px}.muted{font-size:12px}.kpis span,.losses span{font-size:13px}"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + TITLES[kind]
        + "</title><style>"
        + css
        + '</style></head><body style="--accent:'
        + accent
        + '"><article><header><div class="brand">Tonnage Flow | Pulse</div><h1>'
        + TITLES[kind]
        + "</h1><p>"
        + text(r["line"])
        + " &middot; "
        + text(r["window"]["label"])
        + "</p><p>"
        + time(r["window"]["start"])
        + " to "
        + time(r["window"]["end"])
        + "</p><p>"
        + text(
            ", ".join(r.get("technicians", []))
            if kind == "linetech"
            else "All engineers" if kind == "engineering" else "Management"
        )
        + "</p><p>"
        + ("In progress" if r["is_current"] else "Completed reporting period")
        + " &middot; Version "
        + str(r["version"])
        + (" &middot; Revised report" if r.get("revision_of") else "")
        + '</p></header><div class="content"><div class="kpis">'
        + "".join(
            "<div><strong>" + text(v) + "</strong><span>" + text(k) + "</span></div>"
            for k, v in kpis
        )
        + "</div>"
        + "".join(sections)
        + "</div><footer>Report "
        + text(r["id"])
        + " &middot; Generated "
        + time(r["generated_at"])
        + " by "
        + text(r["generated_by"]["name"])
        + "</footer></article></body></html>"
    )


def export_report(report, format):
    from playwright.sync_api import sync_playwright

    if not _EXPORT_SLOT.acquire(blocking=False):
        raise RuntimeError("Another export is running. Please retry shortly.")
    try:
        with sync_playwright() as p:
            options = {"headless": True}
            if os.getenv("PULSE_REPORT_BROWSER_CHANNEL"):
                options["channel"] = os.environ["PULSE_REPORT_BROWSER_CHANNEL"]
            browser = p.chromium.launch(**options)
            try:
                page = browser.new_page(
                    viewport={"width": 520 if format == "png" else 940, "height": 1200},
                    device_scale_factor=2,
                )
                page.route("**/*", lambda route: route.abort())
                page.set_default_timeout(20000)
                page.set_content(
                    render_report(report, format == "png"), wait_until="load"
                )
                if format == "png":
                    return page.locator("article").screenshot(type="png", timeout=20000)
                return page.pdf(
                    format="A4",
                    print_background=True,
                    display_header_footer=True,
                    header_template="<span></span>",
                    footer_template='<div style="font-size:8px;width:100%;text-align:center">'
                    + text(report["id"])
                    + ' &middot; <span class="pageNumber"></span> / <span class="totalPages"></span></div>',
                    margin={
                        "top": "12mm",
                        "bottom": "18mm",
                        "left": "12mm",
                        "right": "12mm",
                    },
                )
            finally:
                browser.close()
    finally:
        _EXPORT_SLOT.release()
