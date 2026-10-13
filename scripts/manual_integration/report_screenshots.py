"""Render synthetic fixtures only; browser calls never reach a production service."""

import sys, os, json, copy
from urllib.parse import urlparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.manual_integration.report_fixtures import fixtures
from src.report_render import render_report, export_report
from pypdf import PdfReader
from playwright.sync_api import sync_playwright

OUT = Path("docs/report-preview")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PULSE_REPORT_BROWSER_CHANNEL", "msedge")
    reports = fixtures()
    checks = []
    if not os.environ.get("REPORT_UI_ONLY"):
        for kind, report in reports.items():
            (OUT / f"{kind}.html").write_text(render_report(report), encoding="utf-8")
            for fmt in ("png", "pdf"):
                (OUT / f"{kind}.{fmt}").write_bytes(export_report(report, fmt))
            pdf = PdfReader(OUT / f"{kind}.pdf")
            for index, page in enumerate(pdf.pages):
                content = page.extract_text()
                assert report["id"] in content
                assert str(index + 1) in content
            checks.append(
                {
                    "report": kind,
                    "pdf_pages": len(pdf.pages),
                    "report_id_each_page": True,
                    "png_bytes": (OUT / f"{kind}.png").stat().st_size,
                }
            )
        # Long table proves pagination preserves first and last records.
        long = copy.deepcopy(reports["linetech"])
        event = long["losses"]["events"][0]
        long["key_events"] = [
            {
                **event,
                "description": f"Pagination record {i:03d} - synthetic seal inspection",
            }
            for i in range(80)
        ]
        (OUT / "pagination-test.pdf").write_bytes(export_report(long, "pdf"))
        pdf = PdfReader(OUT / "pagination-test.pdf")
        content = "\n".join(p.extract_text() for p in pdf.pages)
        assert all(f"Pagination record {i:03d}" in content for i in range(80))
        checks.append(
            {
                "report": "long-table",
                "pdf_pages": len(pdf.pages),
                "records_preserved": 80,
            }
        )
    # Actual React reporting screen, with synthetic API responses in browser only.
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for name, width, height in [
            ("desktop", 1440, 1000),
            ("tablet", 820, 1180),
            ("mobile", 390, 844),
        ]:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script(
                "if(window===window.top)sessionStorage.setItem('pulse.management.session.v1',JSON.stringify({token:'synthetic-only',managerName:'Preview manager',expiresAt:'2099-01-01T00:00:00Z'}))"
            )
            selected = {"kind": "management"}

            def route(request):
                if request.request.resource_type not in ("fetch", "xhr"):
                    request.continue_()
                    return
                path = urlparse(request.request.url).path
                body = {}
                if path == "/api/v1/management/session":
                    body = {
                        "manager_name": "Preview manager",
                        "expires_at": "2099-01-01T00:00:00Z",
                    }
                elif path == "/api/v1/reports/options":
                    body = {"report_types": list(reports), "lines": ["Rovema"]}
                elif path == "/api/v1/reports" and request.request.method == "POST":
                    selected["kind"] = request.request.post_data_json["report_type"]
                    body = reports[selected["kind"]]
                elif path == "/api/v1/reports":
                    body = {"reports": []}
                elif path.endswith("/preview"):
                    body = {"html": render_report(reports[selected["kind"]])}
                elif "/export/" in path:
                    fmt = path.rsplit("/", 1)[-1]
                    request.fulfill(
                        status=200,
                        content_type="application/pdf" if fmt == "pdf" else "image/png",
                        body=(OUT / f'{selected["kind"]}.{fmt}').read_bytes(),
                    )
                    return
                elif path.startswith("/api/v1/reports/"):
                    body = reports[selected["kind"]]
                elif path.startswith("/health"):
                    body = {"status": "ok"}
                request.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(body),
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "*",
                        "Access-Control-Allow-Methods": "*",
                    },
                )

            page.route("**/api/**", route)
            page.route("**/health*", route)
            page.goto("http://127.0.0.1:5183/management/reports")
            page.get_by_role("button", name="Generate Report", exact=True).wait_for()
            print("Screen loaded:", name, flush=True)
            for kind in reports:
                page.get_by_label("Report type").select_option(kind)
                page.get_by_label("Production line").select_option("Rovema")
                page.get_by_label("Reporting period").select_option(
                    "production_day" if kind == "management" else "shift"
                )
                page.get_by_label("Shift start date").fill("2026-01-12")
                page.get_by_role("button", name="Generate Report", exact=True).click()
                frame = page.frame_locator('iframe[title="Shift report preview"]')
                frame.locator("h1").wait_for()
                page.get_by_role("button", name="Download PDF", exact=True).wait_for(
                    state="visible"
                )
                assert page.evaluate("document.documentElement.scrollWidth") <= width
                with page.expect_download() as info:
                    page.get_by_role("button", name="Download PDF", exact=True).click()
                assert info.value.suggested_filename.endswith(".pdf")
                page.get_by_role("status").filter(
                    has_text="Report downloaded."
                ).wait_for()
                with page.expect_download() as info:
                    page.get_by_role("button", name="Save as Image", exact=True).click()
                assert info.value.suggested_filename.endswith(".png")
                page.get_by_role("status").filter(
                    has_text="Report downloaded."
                ).wait_for()
                # Chromium desktop has no file-sharing implementation: fallback must download.
                page.evaluate(
                    'Object.defineProperty(navigator,"canShare",{value:undefined,configurable:true})'
                )
                with page.expect_download() as info:
                    page.get_by_role("button", name="Share image", exact=True).click()
                assert info.value.suggested_filename.endswith(".png")
                page.get_by_text(
                    "Sharing unavailable on this device.", exact=False
                ).wait_for()
                page.locator("iframe").evaluate("el => el.scrollIntoView({block: 'start'})")
                page.locator("iframe").screenshot(
                    path=str(OUT / f"{kind}-{name}-preview.png")
                )
                page.screenshot(
                    path=str(OUT / f"{kind}-{name}-screen.png"), full_page=True
                )
                page.reload()
                page.get_by_role(
                    "button", name="Generate Report", exact=True
                ).wait_for()
            assert not errors, errors
            checks.append(
                {
                    "viewport": name,
                    "horizontal_overflow": False,
                    "templates": 3,
                    "pdf_download": "passed",
                    "png_download": "passed",
                    "share_fallback": "passed",
                    "browser_errors": errors,
                }
            )
            page.close()
        browser.close()
    (OUT / "verification.json").write_text(
        json.dumps(checks, indent=2), encoding="utf-8"
    )
    print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()
