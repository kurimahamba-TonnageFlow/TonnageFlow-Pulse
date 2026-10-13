"""Local-only acceptance checks. Never accepts a production database URL."""

import os, sys, copy, json
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi import HTTPException
from fastapi.testclient import TestClient
from src import report_store, shift_reports, database, management_auth, engineering_auth
from src.report_auth import ReportActor, require_report_actor
from src.api import app
from scripts.manual_integration.report_fixtures import fixtures


def main():
    target = urlparse(os.environ.get("DATABASE_URL", ""))
    assert (
        target.hostname in ("127.0.0.1", "localhost")
        and target.path == "/pulse_reports_preview"
    ), "Dedicated local preview database required"
    actor = ReportActor("management", "Local reporting acceptance")
    samples = fixtures()

    def counts():
        with database.get_database_connection() as con:
            tables = con.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename NOT IN ('shift_report_snapshots','management_audit_log')"
            ).fetchall()
            from psycopg import sql

            return {
                name: con.execute(
                    sql.SQL("SELECT count(*) FROM public.{}").format(
                        sql.Identifier(name)
                    )
                ).fetchone()[0]
                for (name,) in tables
            }

    before = counts()

    def build(kind="management"):
        value = copy.deepcopy(samples[kind])
        value["id"] = str(uuid4())
        return value

    key = uuid4()

    def generate():
        return report_store.generate_once(
            actor, key, "same-options", None, lambda prior: build()
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = list(executor.map(lambda _: generate(), range(2)))
    assert a["id"] == b["id"]
    try:
        report_store.generate_once(
            actor, key, "changed-options", None, lambda prior: build()
        )
    except HTTPException as e:
        assert e.status_code == 409
    else:
        raise AssertionError("Reused request key accepted different options")
    revised = report_store.generate_once(
        actor, uuid4(), "revision", a["id"], lambda prior: build()
    )
    assert revised["version"] == 2 and revised["root_id"] == a["id"]
    assert report_store.get_report(a["id"], actor)["version"] == 1
    with database.get_database_connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM management_audit_log WHERE record_type='shift_report' AND record_id=ANY(%s)",
                ([a["id"], revised["id"]],),
            ).fetchone()[0]
            == 2
        )
        assert con.execute(
            "SELECT relrowsecurity FROM pg_class WHERE oid='public.shift_report_snapshots'::regclass"
        ).fetchone()[0]
        for role in ("anon", "authenticated"):
            assert not con.execute(
                "SELECT has_table_privilege(%s,'public.shift_report_snapshots','SELECT')",
                (role,),
            ).fetchone()[0]
    for statement in (
        "UPDATE public.shift_report_snapshots SET version=99 WHERE id=%s",
        "DELETE FROM public.shift_report_snapshots WHERE id=%s",
    ):
        try:
            with database.get_database_connection() as con:
                con.execute(statement, (a["id"],))
        except Exception as e:
            assert "immutable" in str(e)
        else:
            raise AssertionError("Snapshot was mutable")
    originals = shift_reports.load_report
    shift_reports.load_report = lambda kind, line, window, who, note, now: build(kind)
    saved = {
        kind: report_store.generate_once(
            actor, uuid4(), kind, None, lambda prior, k=kind: build(k)
        )
        for kind in samples
    }
    checked = 0
    try:
        client = TestClient(app)
        for role, allowed in [
            ("linetech", {"linetech"}),
            ("engineering", {"linetech", "engineering"}),
            ("management", set(samples)),
        ]:
            app.dependency_overrides[require_report_actor] = lambda r=role: ReportActor(
                r, "Local acceptance"
            )
            for kind, snapshot in saved.items():
                for suffix in ("", "/preview"):
                    response = client.get("/api/v1/reports/" + snapshot["id"] + suffix)
                    assert response.status_code == (
                        200 if kind in allowed else 404
                    ), response.text
                    checked += 1
                if kind not in allowed:
                    for suffix in ("/export/png", "/export/pdf"):
                        assert (
                            client.get(
                                "/api/v1/reports/" + snapshot["id"] + suffix
                            ).status_code
                            == 404
                        )
                        checked += 1
                    # Even claiming an allowed template cannot revise a forbidden report.
                    response = client.post(
                        "/api/v1/reports",
                        headers={"Idempotency-Key": str(uuid4())},
                        json={"report_type": "linetech", "revision_of": snapshot["id"]},
                    )
                    assert response.status_code == 404, response.text
                    checked += 1
            assert all(
                row["report_type"] in allowed
                for row in client.get("/api/v1/reports").json()["reports"]
            )
            assert client.get("/api/v1/reports?offset=1000000").json()["reports"] == []
    finally:
        app.dependency_overrides.pop(require_report_actor, None)
        shift_reports.load_report = originals
    # Exercise every actual SQL source reader in a repeatable read-only transaction.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for kind in samples:
        payload = shift_reports.load_report(
            kind,
            None,
            shift_reports.resolve_period("current", None, "Day", now),
            actor,
            "",
            now,
        )
        from src.report_models import ReportSnapshot

        ReportSnapshot.model_validate(payload)
    assert counts() == before, "Source table row counts changed"
    print(
        json.dumps(
            {
                "result": "passed",
                "cross_role_endpoint_checks": checked,
                "concurrent_duplicate_requests": "one snapshot",
                "revision": "original preserved; version 2 linked",
                "audit": "atomic entries verified",
                "database_permissions": "RLS and client revocations verified",
                "immutability": "update/delete rejected",
                "source_table_counts": "unchanged",
                "database": "dedicated local preview",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
