"""Entry-point preview using synthetic API responses; no live production writes."""

import sys, json
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright
from scripts.manual_integration.report_fixtures import fixtures
from src.report_render import render_report

OUT = Path("docs/report-preview")


def main():
    reports = fixtures()
    writes = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        state = {"role": "linetech", "kind": "linetech"}

        def route(q):
            if q.request.resource_type not in ("fetch", "xhr"):
                q.continue_()
                return
            path = urlparse(q.request.url).path
            body = {}
            if q.request.method == "POST":
                writes.append(path)
            if path == "/health":
                body = {"status": "ok", "service": "Synthetic preview"}
            elif path == "/api/v1/hmi-access/login":
                body = {"token": "synthetic-tablet"}
            elif path == "/api/v1/hmi/config":
                body = {"lines": [{"id": 1, "name": "Rovema", "machines": []}]}
            elif path == "/api/v1/hmi/lines":
                body = {
                    "lines": [
                        {
                            "line_id": 1,
                            "production_line": "Rovema",
                            "has_active_run": False,
                            "open_fault_count": 0,
                            "stale_status": "no_active_run",
                        }
                    ]
                }
            elif path == "/api/v1/engineering/login":
                body = {
                    "token": "synthetic-engineer",
                    "engineer_name": "Kuri",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            elif path.startswith("/api/v1/engineering/"):
                body = {"items": [], "total": 0}
            elif path == "/api/v1/reports/options":
                body = {
                    "report_types": (
                        ["linetech"]
                        if state["role"] == "linetech"
                        else ["linetech", "engineering"]
                    ),
                    "lines": ["Rovema"],
                }
            elif path == "/api/v1/reports" and q.request.method == "POST":
                state["kind"] = q.request.post_data_json["report_type"]
                body = reports[state["kind"]]
            elif path == "/api/v1/reports":
                body = {"reports": []}
            elif path.endswith("/preview"):
                body = {"html": render_report(reports[state["kind"]])}
            q.fulfill(
                status=200, content_type="application/json", body=json.dumps(body)
            )

        page.route("**/api/**", route)
        page.route("**/health*", route)
        page.goto("http://127.0.0.1:5183/hmi")
        page.get_by_label("Tablet name").fill("Synthetic tablet")
        page.get_by_label("Tablet PIN").fill("synthetic-only")
        page.get_by_role("button", name="Sign in to tablet").click()
        page.get_by_role("button", name="Generate Shift Report", exact=True).wait_for()
        # The existing End Shift integration test separately proves this event
        # fires once, only after the handover write succeeds.
        event = "window.dispatchEvent(new CustomEvent('pulse-shift-complete',{detail:{line:'Rovema',finishedAt:'2026-01-13T06:00:00Z'}}))"
        page.evaluate(event)
        page.get_by_role("region", name="Shift Complete").screenshot(
            path=str(OUT / "end-shift-prompt.png")
        )
        before = len(writes)
        page.get_by_role("button", name="Not Now", exact=True).click()
        assert len(writes) == before
        page.evaluate(event)
        page.get_by_role("button", name="Generate Report", exact=True).click()
        assert page.locator("select").nth(3).input_value() == "Night"
        assert page.get_by_label("Shift start date").input_value() == "2026-01-12"
        page.get_by_role("button", name="Generate Report", exact=True).click()
        page.frame_locator("iframe").locator("h1").wait_for()
        assert "/api/v1/reports" in writes
        state["role"] = "engineering"
        page.goto("http://127.0.0.1:5183/engineering")
        page.locator("#engineering-engineer-name").select_option("Kuri")
        page.get_by_label("Engineering PIN").fill("synthetic-only")
        page.get_by_role("button", name="Sign In", exact=True).click()
        page.get_by_role("button", name="Generate Shift Report", exact=True).click()
        page.get_by_role("button", name="Generate Report", exact=True).wait_for()
        assert page.get_by_role("option", name="Management Shift Report").count() == 0
        page.get_by_role("button", name="Generate Report", exact=True).click()
        frame = page.frame_locator("iframe")
        frame.locator("h1").wait_for()
        page.locator("iframe").evaluate("el => el.scrollIntoView({block: 'start'})")
        page.screenshot(
            path=str(OUT / "engineering-workspace-report.png"), full_page=True
        )
        assert not errors, errors
        assert not any("/runs" in path or "stoppage" in path for path in writes)
        browser.close()
    result = {
        "optional_end_shift_prompt": "passed",
        "not_now_report_requests": 0,
        "night_boundary_context": "passed",
        "engineering_template_access": "passed",
        "production_write_requests": 0,
        "browser_errors": errors,
        "note": "Synthetic event exercises the real prompt; HmiScreen.changeover.test.tsx verifies the successful handover dispatch.",
    }
    (OUT / "entrypoints-verification.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
