"""Append-only report snapshots; generation/revision audit is atomic with save."""

from uuid import UUID
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from . import database as db
from .report_models import ReportSnapshot


def get_report(report_id, actor, connection=None):
    from contextlib import nullcontext

    with nullcontext(connection) if connection else db.get_database_connection() as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT snapshot FROM public.shift_report_snapshots WHERE id=%s AND report_type=ANY(%s)",
                (str(report_id), list(actor.report_types)),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(
            404, "Report not found or not available to your credentials."
        )
    return row["snapshot"]


def history(actor, offset=0):
    with db.get_database_connection() as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id,report_type,production_line,period_start,period_end,generated_at,version,revision_of
                FROM public.shift_report_snapshots WHERE report_type=ANY(%s) ORDER BY generated_at DESC,id DESC LIMIT 100 OFFSET %s""",
                (list(actor.report_types), offset),
            )
            return cur.fetchall()


def generate_once(actor, key, fingerprint, revision_of, build):
    with db.get_database_connection() as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute("SET LOCAL lock_timeout='3s'")
            # Serialises duplicate clicks without locking any production table.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"report:{actor.role}:{actor.name}:{key}",),
            )
            cur.execute(
                "SELECT fingerprint,snapshot FROM public.shift_report_snapshots WHERE actor_role=%s AND actor_name=%s AND request_key=%s",
                (actor.role, actor.name, str(key)),
            )
            old = cur.fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise HTTPException(
                        409, "This request key belongs to different report options."
                    )
                return old["snapshot"]
            prior = get_report(revision_of, actor, con) if revision_of else None
            root = prior.get("root_id", prior["id"]) if prior else None
            if root:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("report-revision:" + root,),
                )
            snapshot = build(prior)
            actor.require(snapshot["type"])
            ReportSnapshot.model_validate(snapshot)
            version = 1
            if root:
                cur.execute(
                    "SELECT MAX(version)+1 AS version FROM public.shift_report_snapshots WHERE root_id=%s",
                    (root,),
                )
                version = cur.fetchone()["version"]
            snapshot.update(
                version=version,
                root_id=root or snapshot["id"],
                revision_of=str(revision_of) if revision_of else None,
            )
            cur.execute(
                """INSERT INTO public.shift_report_snapshots
              (id,report_type,production_line,period_start,period_end,generated_at,actor_role,actor_name,revision_of,root_id,version,request_key,fingerprint,snapshot)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    snapshot["id"],
                    snapshot["type"],
                    snapshot["line"],
                    snapshot["window"]["start"],
                    snapshot["window"]["end"],
                    snapshot["generated_at"],
                    actor.role,
                    actor.name,
                    str(revision_of) if revision_of else None,
                    snapshot["root_id"],
                    version,
                    str(key),
                    fingerprint,
                    Jsonb(snapshot),
                ),
            )
            cur.execute(
                """INSERT INTO public.management_audit_log (action,manager_name,record_type,record_id,new_value)
                VALUES (%s,%s,'shift_report',%s,%s)""",
                (
                    "report_revised" if prior else "report_generated",
                    actor.role + ": " + actor.name,
                    snapshot["id"],
                    Jsonb(
                        {
                            "report_type": snapshot["type"],
                            "version": version,
                            "revision_of": snapshot["revision_of"],
                        }
                    ),
                ),
            )
            return snapshot
