from contextlib import nullcontext
from dataclasses import dataclass
import os
import json
from typing import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from dotenv import load_dotenv

try:
    from . import pulse_calculations as calc
    from . import hourly_performance as performance
    from .factory_time import (
        CLOCK_HOUR,
        clock_hour_label,
        clock_hour_start,
        is_clock_hour_start,
        normalise_shift_name,
        operational_shift_window,
        shift_name_at,
        shift_window_containing,
        to_london,
    )
except ImportError:
    import pulse_calculations as calc
    import hourly_performance as performance
    from factory_time import (
        CLOCK_HOUR,
        clock_hour_label,
        clock_hour_start,
        is_clock_hour_start,
        normalise_shift_name,
        operational_shift_window,
        shift_name_at,
        shift_window_containing,
        to_london,
    )


# ==========================================================
# ENVIRONMENT
# ==========================================================

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


# ==========================================================
# DATABASE CONNECTION
# ==========================================================


def get_database_connection():
    if not DATABASE_URL:
        raise ValueError(
            "DATABASE_URL was not found in .env"
        )

    return psycopg.connect(
        DATABASE_URL
    )


def check_schema_readiness():
    """For GET /health/ready: one short read that proves the database
    answers and has one marker object from each migration this code
    needs. Returns {migration: present}. A 5-second connect timeout
    keeps an unreachable database from hanging the check."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL was not found in .env")

    query = """
        SELECT
            to_regclass('public.management_audit_log') IS NOT NULL AS m0001,
            EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'downtime_events'
                  AND column_name = 'accepted_at'
            ) AS m0002,
            to_regclass('public.hmi_idempotency_keys') IS NOT NULL AS m0003,
            (
                to_regclass('public.run_next_step_legacy_baseline') IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'changeovers'
                      AND column_name = 'line_stoppage_id'
                )
            ) AS m0004,
            (to_regclass('public.casepacker_requests') IS NOT NULL
             AND to_regclass('public.casepacker_updates') IS NOT NULL
             AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='casepacker_run_start' AND NOT tgisinternal)) AS casepacker,
            EXISTS (SELECT 1 FROM pg_constraint WHERE conname='casepacker_handover_note_required') AS handover,
            (to_regclass('public.run_operating_speed_changes') IS NOT NULL
             AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='immutable_production_standard' AND NOT tgisinternal)
             AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='production_runs' AND column_name='standard_speed_ppm')) AS fixed_standard,
            (to_regclass('public.production_standard_versions') IS NOT NULL
             AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='select_management_standard' AND NOT tgisinternal)
             AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='immutable_standard_version' AND NOT tgisinternal)
             AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='production_runs' AND column_name='standard_version_id')) AS management_standard,
            (to_regclass('public.task_observations') IS NOT NULL
             AND to_regprocedure('public.canonical_product(text)') IS NOT NULL
             AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='immutable_task_observation' AND NOT tgisinternal)) AS tasks,
            to_regprocedure('public.resolve_production_standard(text,numeric,text,text,integer,integer,timestamp with time zone)') IS NOT NULL AS line_weight_standard,
            EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='changeovers' AND column_name='workflow') AS linetech,
            (EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='weekly_tonnage_targets' AND column_name='notes') AND EXISTS (SELECT 1 FROM pg_constraint WHERE conname='no_overlapping_site_target_weeks')) AS live_operations,
            to_regclass('public.shift_report_snapshots') IS NOT NULL AS shift_reports;
    """

    with psycopg.connect(DATABASE_URL, connect_timeout=5) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query)
            row = cursor.fetchone()

    return {
        "0001": bool(row["m0001"]),
        "0002": bool(row["m0002"]),
        "0003": bool(row["m0003"]),
        "0004": bool(row["m0004"]),
        "20261002234341": bool(row.get("casepacker")),
        "20261004092500": bool(row.get("handover")),
        "20261005054638": bool(row.get("fixed_standard")),
        "20261005135527": bool(row.get("management_standard")),
        "20261006021758": bool(row.get("tasks")),
        "20261008232014": bool(row.get("line_weight_standard")),
        "20261012232758": bool(row.get("shift_reports")),
        "20261009110303": bool(row.get("linetech")),
        "20261009131928": bool(row.get("live_operations")),
    }


# ==========================================================
# PRODUCTION RUN
# ==========================================================


def save_production_run(run):
    # `format` (migration 0003) is written only when supplied, so every
    # existing caller - the CLI and a Start Run request without a
    # format - issues exactly the same INSERT as before that migration.
    has_format = run.get("format") is not None
    format_column = ",\n            format" if has_format else ""
    format_value = ",\n            %(format)s" if has_format else ""

    query = f"""
        INSERT INTO public.production_runs (
            production_line,
            line_technician,
            shift,
            customer,
            product,
            pack_weight_kg,
            packs_per_case,
            pack_type,
            target_speed_ppm,
            cases_per_pallet,
            starting_pallets_remaining,
            pallets_remaining,
            previous_run_completed{format_column}
        )
        VALUES (
            %(production_line)s,
            %(line_technician)s,
            %(shift)s,
            %(customer)s,
            %(product)s,
            %(pack_weight_kg)s,
            %(packs_per_case)s,
            %(pack_type)s,
            %(target_speed_ppm)s,
            %(cases_per_pallet)s,
            %(starting_pallets_remaining)s,
            %(pallets_remaining)s,
            %(previous_run_completed)s{format_value}
        )
        RETURNING id, started_at;
    """

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                query,
                run,
            )

            saved_run = (
                cursor.fetchone()
            )

        connection.commit()

    if saved_run is None:
        raise RuntimeError(
            "Production Run was inserted "
            "but no database ID was returned."
        )

    return saved_run[0]


# ==========================================================
# HOURLY UPDATE
# ==========================================================


def save_hourly_update(update):
    query = """
        INSERT INTO public.hourly_updates (
            production_run_id,
            oee,
            pallets_completed,
            planned_downtime,
            planned_downtime_minutes,
            expected_packs,
            actual_packs,
            expected_pallets,
            actual_pallets,
            production_variance_packs,
            estimated_lost_packs,
            estimated_lost_minutes,
            unexplained_loss,
            unexplained_loss_reason,
            pallets_remaining
        )
        VALUES (
            %(production_run_id)s,
            %(oee)s,
            %(pallets_completed)s,
            %(planned_downtime)s,
            %(planned_downtime_minutes)s,
            %(expected_packs)s,
            %(actual_packs)s,
            %(expected_pallets)s,
            %(actual_pallets)s,
            %(production_variance_packs)s,
            %(estimated_lost_packs)s,
            %(estimated_lost_minutes)s,
            %(unexplained_loss)s,
            %(unexplained_loss_reason)s,
            %(pallets_remaining)s
        )
        RETURNING id;
    """

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                query,
                update,
            )

            saved_update = (
                cursor.fetchone()
            )

        connection.commit()

    if saved_update is None:
        raise RuntimeError(
            "Hourly Update was inserted "
            "but no database ID was returned."
        )

    return saved_update[0]

def save_downtime_event(event):
    query = """
        INSERT INTO public.downtime_events (
            production_run_id,
            fault_id,
            machine,
            reason,
            reported_by,
            engineer_called,
            production_status,
            engineering_status,
            engineer,
            retrospective,
            opened_at,
            resolved_at
        )
        VALUES (
            %(production_run_id)s,
            %(fault_id)s,
            %(machine)s,
            %(reason)s,
            %(reported_by)s,
            %(engineer_called)s,
            %(production_status)s,
            %(engineering_status)s,
            %(engineer)s,
            %(retrospective)s,
            %(opened_at)s,
            %(resolved_at)s
        )
        RETURNING id;
    """

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, event)
            downtime_event_id = cursor.fetchone()[0]

        connection.commit()

    return downtime_event_id

def save_engineering_update(update):
    query = """
        INSERT INTO public.engineering_updates (
            downtime_event_id,
            production_run_id,
            fault_id,
            engineer,
            update_type,
            finding,
            action,
            engineering_status
        )
        VALUES (
            %(downtime_event_id)s,
            %(production_run_id)s,
            %(fault_id)s,
            %(engineer)s,
            %(update_type)s,
            %(finding)s,
            %(action)s,
            %(engineering_status)s
        )
        RETURNING id;
    """

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                query,
                update,
            )

            saved_update = cursor.fetchone()

        connection.commit()

    if saved_update is None:
        raise RuntimeError(
            "Engineering Update was inserted "
            "but no database ID was returned."
        )

    return saved_update[0]


def get_open_faults(production_run_id):
    query = """
        SELECT *
        FROM public.downtime_events
        WHERE production_run_id = %s
        AND production_status = 'Ongoing'
        AND resolved_at IS NULL;
    """

    with get_database_connection() as connection:
       with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                query,
                (production_run_id,),
            )

            open_faults = cursor.fetchall()

    return open_faults

def get_active_production_run(production_line):
    query = """
        SELECT
            id,
            production_line,
            line_technician,
            shift,
            customer,
            product,
            pack_weight_kg,
            packs_per_case,
            pack_type,
            target_speed_ppm,
            cases_per_pallet,
            starting_pallets_remaining,
            pallets_remaining,
            previous_run_completed,
            total_pallets_completed,
            potential_overrun_pallets,
            confirmed_overrun_pallets,
            status,
            started_at
        FROM public.production_runs
        WHERE production_line = %s
          AND status = 'Active'
        ORDER BY started_at DESC
        LIMIT 1;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                query,
                (production_line,),
            )

            active_run = cursor.fetchone()

    return active_run

def update_production_run_progress(
    production_run_id,
    pallets_remaining,
    total_pallets_completed,
    potential_overrun_pallets,
    confirmed_overrun_pallets,
):
    query = """
        UPDATE public.production_runs
        SET
            pallets_remaining = %(pallets_remaining)s,
            total_pallets_completed = %(total_pallets_completed)s,
            potential_overrun_pallets = %(potential_overrun_pallets)s,
            confirmed_overrun_pallets = %(confirmed_overrun_pallets)s
        WHERE id = %(production_run_id)s
        RETURNING id;
    """

    update = {
        "production_run_id":
            production_run_id,

        "pallets_remaining":
            pallets_remaining,

        "total_pallets_completed":
            total_pallets_completed,

        "potential_overrun_pallets":
            potential_overrun_pallets,

        "confirmed_overrun_pallets":
            confirmed_overrun_pallets,
    }

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                query,
                update,
            )

            updated_run = cursor.fetchone()

        connection.commit()

    if updated_run is None:
        raise RuntimeError(
            "Production Run progress was not updated."
        )

    return updated_run[0]

def save_changeover_type(
    production_run_id,
    changeover_type,
):
    query = """
        UPDATE public.production_runs
        SET
            changeover_type = %(changeover_type)s
        WHERE id = %(production_run_id)s
        RETURNING id;
    """

    update = {
        "production_run_id":
            production_run_id,

        "changeover_type":
            changeover_type,
    }

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                query,
                update,
            )

            updated_run = cursor.fetchone()

        connection.commit()

    if updated_run is None:
        raise RuntimeError(
            "Production Run changeover type was not saved."
        )

    return updated_run[0]


def get_production_run_by_id(production_run_id):
    """Plain, unfiltered lookup by id - unlike the dashboard read
    functions, this does NOT exclude TEST- rows, since operational
    endpoints (e.g. completing a run) must see every real row
    regardless of naming convention."""
    query = """
        SELECT
            id,
            production_line,
            status
        FROM public.production_runs
        WHERE id = %(production_run_id)s
        LIMIT 1;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"production_run_id": production_run_id})
            return cursor.fetchone()


def close_production_run(
    production_run_id,
    finished_at,
):
    query = """
        UPDATE public.production_runs
        SET
            status = 'Completed',
            finished_at = %(finished_at)s
        WHERE id = %(production_run_id)s
        RETURNING id;
    """

    update = {
        "production_run_id":
            production_run_id,

        "finished_at":
            finished_at,
    }

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                query,
                update,
            )

            closed_run = cursor.fetchone()

        connection.commit()

    if closed_run is None:
        raise RuntimeError(
            "Production Run was not closed."
        )

    return closed_run[0]

def update_downtime_event_state(
    downtime_event_id,
    engineer_called,
    production_status,
    engineering_status,
    engineer,
    resolved_at=None,
):
    query = """
        UPDATE public.downtime_events
        SET
            engineer_called = %(engineer_called)s,
            production_status = CASE WHEN resolved_at IS NOT NULL THEN 'Resolved' ELSE %(production_status)s END,
            engineering_status = %(engineering_status)s,
            engineer = %(engineer)s,
            resolved_at = COALESCE(resolved_at, %(resolved_at)s)
        WHERE id = %(downtime_event_id)s
        RETURNING id;
    """

    update = {
        "downtime_event_id":
            downtime_event_id,

        "engineer_called":
            engineer_called,

        "production_status":
            production_status,

        "engineering_status":
            engineering_status,

        "engineer":
            engineer,

        "resolved_at":
            resolved_at,
    }

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                query,
                update,
            )

            updated_event = cursor.fetchone()

        connection.commit()

    if updated_event is None:
        raise RuntimeError(
            "Downtime Event was not updated."
        )

    return updated_event[0]


# ==========================================================
# DASHBOARD READ API
# ==========================================================
#
# Read-only. Every query below applies _TEST_DATA_EXCLUSION_SQL so
# test/demo data (see tests/test_phase1_mvp_system.py and friends)
# never appears in dashboard results without deleting anything.
#
# All filter values are bound as parameters (%(name)s) - never string-
# interpolated into SQL. _TEST_DATA_EXCLUSION_SQL itself is a fixed
# literal maintained by this codebase, not user input.
#
# Schema gaps affecting these queries (see docs/dashboard_integration.md):
#   - hourly_updates has no timestamp column, so per-hour ordering uses
#     submission sequence (row id), not wall-clock time.
#   - engineering_updates has no timestamp column either; date filters
#     for it are approximated via its parent downtime_events.opened_at.
#   - There is no fault/downtime-type taxonomy beyond free text, so
#     "Machine Setup" vs "Machine Repair" cannot be computed here.

_TEST_DATA_EXCLUSION_SQL = """
    pr.production_line NOT ILIKE 'TEST-%%'
    AND pr.customer NOT ILIKE '%%TEST-%%'
    AND pr.product NOT ILIKE '%%TEST-%%'
    AND pr.shift NOT ILIKE '%%TEST-%%'
"""


def _run_conditions(filters):
    """Conditions/params for queries filtered at the production_runs
    level (aliased pr). Applies: date_from/date_to (pr.started_at date),
    production_line, shift, product, customer, technician, run_status."""
    filters = filters or {}
    conditions = [_TEST_DATA_EXCLUSION_SQL]
    params = {}

    if filters.get("date_from") is not None:
        conditions.append("pr.started_at::date >= %(date_from)s")
        params["date_from"] = filters["date_from"]

    if filters.get("date_to") is not None:
        conditions.append("pr.started_at::date <= %(date_to)s")
        params["date_to"] = filters["date_to"]

    if filters.get("production_line") is not None:
        conditions.append("pr.production_line = %(production_line)s")
        params["production_line"] = filters["production_line"]

    if filters.get("shift") is not None:
        conditions.append("lower(trim(pr.shift)) = ANY(%(shift_aliases)s)")
        from src.catalogue import aliases
        params["shift_aliases"] = aliases("shift", filters["shift"])
        params["shift"] = filters["shift"]

    if filters.get("product") is not None:
        conditions.append("lower(trim(pr.product)) = ANY(%(product_aliases)s)")
        from src.catalogue import aliases
        params["product_aliases"] = aliases("product", filters["product"])
        params["product"] = filters["product"]

    if filters.get("customer") is not None:
        conditions.append("lower(trim(pr.customer)) = ANY(%(customer_aliases)s)")
        from src.catalogue import aliases
        params["customer_aliases"] = aliases("customer", filters["customer"])
        params["customer"] = filters["customer"]

    if filters.get("technician") is not None:
        conditions.append("pr.line_technician = %(technician)s")
        params["technician"] = filters["technician"]

    if filters.get("run_status") is not None:
        conditions.append("pr.status = %(run_status)s")
        params["run_status"] = filters["run_status"]

    return conditions, params


def _hourly_conditions(filters):
    """_run_conditions plus hourly_updates.planned_downtime (downtime_type).
    Used for queries joining hourly_updates AS hu to production_runs AS pr."""
    conditions, params = _run_conditions(filters)
    filters = filters or {}

    if filters.get("downtime_type") is not None:
        conditions.append("hu.planned_downtime = %(downtime_type)s")
        params["downtime_type"] = filters["downtime_type"]

    return conditions, params


def _fault_conditions(filters):
    """_run_conditions (date range reinterpreted against downtime_events.
    opened_at, since a fault may open on a different date than its run
    started) plus machine (partial match), engineer, fault_status.
    Used for queries joining downtime_events AS de to production_runs AS pr."""
    filters = filters or {}
    conditions = [_TEST_DATA_EXCLUSION_SQL]
    params = {}

    if filters.get("date_from") is not None:
        conditions.append("de.opened_at::date >= %(date_from)s")
        params["date_from"] = filters["date_from"]

    if filters.get("date_to") is not None:
        conditions.append("de.opened_at::date <= %(date_to)s")
        params["date_to"] = filters["date_to"]

    if filters.get("production_line") is not None:
        conditions.append("pr.production_line = %(production_line)s")
        params["production_line"] = filters["production_line"]

    if filters.get("shift") is not None:
        conditions.append("lower(trim(pr.shift)) = ANY(%(shift_aliases)s)")
        from src.catalogue import aliases
        params["shift_aliases"] = aliases("shift", filters["shift"])
        params["shift"] = filters["shift"]

    if filters.get("product") is not None:
        conditions.append("lower(trim(pr.product)) = ANY(%(product_aliases)s)")
        from src.catalogue import aliases
        params["product_aliases"] = aliases("product", filters["product"])
        params["product"] = filters["product"]

    if filters.get("customer") is not None:
        conditions.append("lower(trim(pr.customer)) = ANY(%(customer_aliases)s)")
        from src.catalogue import aliases
        params["customer_aliases"] = aliases("customer", filters["customer"])
        params["customer"] = filters["customer"]

    if filters.get("technician") is not None:
        conditions.append("pr.line_technician = %(technician)s")
        params["technician"] = filters["technician"]

    if filters.get("run_status") is not None:
        conditions.append("pr.status = %(run_status)s")
        params["run_status"] = filters["run_status"]

    if filters.get("machine") is not None:
        conditions.append("de.machine ILIKE %(machine)s")
        params["machine"] = f"%{filters['machine']}%"

    if filters.get("engineer") is not None:
        conditions.append("de.engineer = %(engineer)s")
        params["engineer"] = filters["engineer"]

    if filters.get("fault_status") is not None:
        conditions.append("de.production_status = %(fault_status)s")
        params["fault_status"] = filters["fault_status"]

    return conditions, params


def _engineering_conditions(filters):
    """Same base as _fault_conditions (joined via downtime_events AS de,
    since engineering_updates has no timestamp/production_line of its
    own) plus engineer and engineering_class (update_type)."""
    conditions, params = _fault_conditions(filters)
    filters = filters or {}

    if filters.get("engineering_class") is not None:
        conditions.append("eu.update_type = %(engineering_class)s")
        params["engineering_class"] = filters["engineering_class"]

    return conditions, params


def get_dashboard_filter_options():
    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            def distinct_run_values(column):
                cursor.execute(
                    f"""
                    SELECT DISTINCT {column}
                    FROM public.production_runs AS pr
                    WHERE {_TEST_DATA_EXCLUSION_SQL}
                    ORDER BY {column};
                    """
                )
                return [row[0] for row in cursor.fetchall() if row[0] is not None]

            production_lines = distinct_run_values("production_line")
            shifts = distinct_run_values("shift")
            products = distinct_run_values("product")
            customers = distinct_run_values("customer")
            technicians = distinct_run_values("line_technician")
            run_statuses = distinct_run_values("status")

            cursor.execute(
                f"""
                SELECT DISTINCT de.machine
                FROM public.downtime_events AS de
                JOIN public.production_runs AS pr ON pr.id = de.production_run_id
                WHERE {_TEST_DATA_EXCLUSION_SQL}
                ORDER BY de.machine;
                """
            )
            machines = [row[0] for row in cursor.fetchall() if row[0] is not None]

            cursor.execute(
                f"""
                SELECT DISTINCT de.production_status
                FROM public.downtime_events AS de
                JOIN public.production_runs AS pr ON pr.id = de.production_run_id
                WHERE {_TEST_DATA_EXCLUSION_SQL}
                ORDER BY de.production_status;
                """
            )
            fault_statuses = [row[0] for row in cursor.fetchall() if row[0] is not None]

            cursor.execute(
                f"""
                SELECT DISTINCT eu.engineer
                FROM public.engineering_updates AS eu
                JOIN public.downtime_events AS de ON de.id = eu.downtime_event_id
                JOIN public.production_runs AS pr ON pr.id = eu.production_run_id
                WHERE {_TEST_DATA_EXCLUSION_SQL}
                ORDER BY eu.engineer;
                """
            )
            engineers = [row[0] for row in cursor.fetchall() if row[0] is not None]

            cursor.execute(
                f"""
                SELECT DISTINCT eu.update_type
                FROM public.engineering_updates AS eu
                JOIN public.downtime_events AS de ON de.id = eu.downtime_event_id
                JOIN public.production_runs AS pr ON pr.id = eu.production_run_id
                WHERE {_TEST_DATA_EXCLUSION_SQL}
                ORDER BY eu.update_type;
                """
            )
            engineering_classes = [row[0] for row in cursor.fetchall() if row[0] is not None]

            cursor.execute(
                f"""
                SELECT DISTINCT hu.planned_downtime
                FROM public.hourly_updates AS hu
                JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
                WHERE {_TEST_DATA_EXCLUSION_SQL}
                ORDER BY hu.planned_downtime;
                """
            )
            downtime_types = [row[0] for row in cursor.fetchall() if row[0] is not None]

    return {
        "production_lines": production_lines,
        "shifts": shifts,
        "products": products,
        "customers": customers,
        "technicians": technicians,
        "engineers": engineers,
        "machines": machines,
        "downtime_types": downtime_types,
        "engineering_classes": engineering_classes,
        "run_statuses": run_statuses,
        "fault_statuses": fault_statuses,
        # "format" has no reliable column mapping anywhere in the schema
        # (see docs/dashboard_integration.md) - documented, not guessed.
        "unsupported_filters": ["format"],
    }


def list_dashboard_runs(filters=None, limit=25, offset=0):
    conditions, params = _run_conditions(filters)
    where_sql = " AND ".join(conditions)

    # Per-run output uses the same stored-pallet tonnes formula as
    # get_dashboard_summary() and get_technician_performance(). A run
    # with no hourly updates gets NULL figures (never 0), so "no data
    # yet" can't be mistaken for zero output.
    query = f"""
        SELECT
            pr.id AS run_id,
            pr.production_line,
            pr.line_technician,
            pr.shift,
            pr.customer,
            pr.product,
            pr.pack_type,
            pr.status,
            pr.started_at,
            pr.finished_at,
            pr.pallets_remaining,
            pr.total_pallets_completed,
            pr.changeover_type,
            run_output.hourly_update_count,
            ROUND(run_output.expected_pallets, 4) AS expected_pallets,
            ROUND(run_output.actual_pallets, 4) AS actual_pallets,
            ROUND(run_output.expected_tonnes, 3) AS expected_tonnes,
            ROUND(run_output.actual_tonnes, 3) AS actual_tonnes,
            -- GREATEST() ignores NULLs, so the gap is NULL explicitly
            -- whenever either side is missing.
            CASE
                WHEN run_output.expected_tonnes IS NULL OR run_output.actual_tonnes IS NULL THEN NULL
                ELSE ROUND(GREATEST(run_output.expected_tonnes - run_output.actual_tonnes, 0), 3)
            END AS output_gap_tonnes,
            CASE
                WHEN run_output.expected_pallets IS NULL OR run_output.actual_pallets IS NULL THEN NULL
                ELSE ROUND(GREATEST(run_output.expected_pallets - run_output.actual_pallets, 0), 4)
            END AS output_gap_pallets
        FROM public.production_runs AS pr
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS hourly_update_count,
                SUM(hu.expected_pallets) AS expected_pallets,
                SUM(hu.actual_pallets) AS actual_pallets,
                SUM(
                    hu.expected_pallets * pr.cases_per_pallet * pr.packs_per_case * pr.pack_weight_kg
                ) / 1000.0 AS expected_tonnes,
                SUM(
                    hu.actual_pallets * pr.cases_per_pallet * pr.packs_per_case * pr.pack_weight_kg
                ) / 1000.0 AS actual_tonnes
            FROM public.hourly_updates AS hu
            WHERE hu.production_run_id = pr.id
        ) AS run_output ON TRUE
        WHERE {where_sql}
        ORDER BY pr.started_at DESC
        LIMIT %(limit)s OFFSET %(offset)s;
    """
    count_query = f"""
        SELECT COUNT(*)
        FROM public.production_runs AS pr
        WHERE {where_sql};
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {**params, "limit": limit, "offset": offset})
            rows = cursor.fetchall()

        with connection.cursor() as count_cursor:
            count_cursor.execute(count_query, params)
            total = count_cursor.fetchone()[0]

    return rows, total


def get_dashboard_run(run_id):
    query = f"""
        SELECT
            id AS run_id,
            production_line,
            line_technician,
            shift,
            customer,
            product,
            pack_weight_kg,
            packs_per_case,
            pack_type,
            target_speed_ppm,
            cases_per_pallet,
            starting_pallets_remaining,
            pallets_remaining,
            previous_run_completed,
            total_pallets_completed,
            potential_overrun_pallets,
            confirmed_overrun_pallets,
            status,
            started_at,
            finished_at,
            changeover_type
        FROM public.production_runs AS pr
        WHERE pr.id = %(run_id)s
          AND {_TEST_DATA_EXCLUSION_SQL}
        LIMIT 1;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"run_id": run_id})
            run = cursor.fetchone()

    return run


def get_dashboard_hourly_updates(filters=None):
    conditions, params = _hourly_conditions(filters)
    where_sql = " AND ".join(conditions)

    query = f"""
        SELECT
            hu.id,
            hu.production_run_id,
            pr.production_line,
            hu.oee,
            hu.pallets_completed,
            hu.planned_downtime,
            hu.planned_downtime_minutes,
            hu.expected_packs,
            hu.actual_packs,
            hu.expected_pallets,
            hu.actual_pallets,
            hu.production_variance_packs,
            hu.estimated_lost_packs,
            hu.estimated_lost_minutes,
            hu.unexplained_loss,
            hu.unexplained_loss_reason,
            hu.pallets_remaining
        FROM public.hourly_updates AS hu
        JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
        WHERE {where_sql}
        ORDER BY hu.production_run_id, hu.id;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

    return rows


def get_dashboard_output_timeline(filters=None):
    conditions, params = _hourly_conditions(filters)
    where_sql = " AND ".join(conditions)

    # No timestamp column exists on hourly_updates - sequence_in_run
    # (submission order via id) is the only available ordering proxy.
    query = f"""
        SELECT
            hu.id AS hourly_update_id,
            hu.production_run_id,
            pr.production_line,
            pr.pack_weight_kg,
            pr.packs_per_case,
            pr.cases_per_pallet,
            hu.expected_pallets,
            hu.actual_pallets,
            ROW_NUMBER() OVER (
                PARTITION BY hu.production_run_id
                ORDER BY hu.id
            ) AS sequence_in_run
        FROM public.hourly_updates AS hu
        JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
        WHERE {where_sql}
        ORDER BY hu.production_run_id, hu.id;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

    return rows


def get_dashboard_planned_downtime(filters=None):
    conditions, params = _hourly_conditions(filters)
    where_sql = " AND ".join(conditions)

    query = f"""
        SELECT
            hu.planned_downtime AS downtime_type,
            COALESCE(SUM(hu.planned_downtime_minutes), 0) AS total_minutes,
            COUNT(*) AS occurrences
        FROM public.hourly_updates AS hu
        JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
        WHERE {where_sql}
        GROUP BY hu.planned_downtime
        ORDER BY total_minutes DESC;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

    return rows


def get_dashboard_downtime_events(filters=None):
    """Raw downtime_events rows (no computed duration). Building block
    for /engineering-downtime; see get_dashboard_faults for the
    duration-enriched version used by /faults."""
    conditions, params = _fault_conditions(filters)
    where_sql = " AND ".join(conditions)

    query = f"""
        SELECT
            de.id AS downtime_event_id,
            de.production_run_id,
            pr.production_line,
            de.fault_id,
            de.machine,
            de.reason,
            de.reported_by,
            de.engineer_called,
            de.production_status,
            de.engineering_status,
            de.engineer,
            de.retrospective,
            de.opened_at,
            de.resolved_at
        FROM public.downtime_events AS de
        JOIN public.production_runs AS pr ON pr.id = de.production_run_id
        WHERE {where_sql}
        ORDER BY de.opened_at DESC;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

    return rows


def get_dashboard_engineering_updates(filters=None):
    conditions, params = _engineering_conditions(filters)
    where_sql = " AND ".join(conditions)

    query = f"""
        SELECT
            eu.id,
            eu.downtime_event_id,
            eu.production_run_id,
            pr.production_line,
            eu.fault_id,
            eu.engineer,
            eu.update_type,
            eu.finding,
            eu.action,
            eu.engineering_status,
            de.machine,
            de.opened_at,
            de.resolved_at
        FROM public.engineering_updates AS eu
        JOIN public.downtime_events AS de ON de.id = eu.downtime_event_id
        JOIN public.production_runs AS pr ON pr.id = eu.production_run_id
        WHERE {where_sql}
        ORDER BY eu.id DESC;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

    return rows


def get_dashboard_faults(filters=None):
    """downtime_events enriched with a computed duration, for /faults.
    duration_minutes uses resolved_at when present, else NOW() (server
    UTC time) - duration_is_active marks that second case."""
    conditions, params = _fault_conditions(filters)
    where_sql = " AND ".join(conditions)

    query = f"""
        SELECT
            de.id AS downtime_event_id,
            de.production_run_id,
            pr.production_line,
            de.fault_id,
            de.machine,
            de.reason,
            de.reported_by,
            de.engineer_called,
            de.production_status,
            de.engineering_status,
            de.engineer,
            de.retrospective,
            de.opened_at,
            de.resolved_at,
            EXTRACT(
                EPOCH FROM (COALESCE(de.resolved_at, NOW()) - de.opened_at)
            ) / 60.0 AS duration_minutes,
            (de.resolved_at IS NULL) AS duration_is_active
        FROM public.downtime_events AS de
        JOIN public.production_runs AS pr ON pr.id = de.production_run_id
        WHERE {where_sql}
        ORDER BY de.opened_at DESC;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

    return rows


def get_dashboard_summary(filters=None):
    run_conditions, run_params = _run_conditions(filters)
    run_where = " AND ".join(run_conditions)

    hourly_conditions, hourly_params = _hourly_conditions(filters)
    hourly_where = " AND ".join(hourly_conditions)

    fault_conditions, fault_params = _fault_conditions(filters)
    fault_where = " AND ".join(fault_conditions)

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total_runs
                FROM public.production_runs AS pr
                WHERE {run_where};
                """,
                run_params,
            )
            total_runs = cursor.fetchone()["total_runs"]

            cursor.execute(
                f"""
                SELECT
                    COALESCE(SUM(hu.expected_pallets), 0) AS expected_pallets,
                    COALESCE(SUM(hu.actual_pallets), 0) AS actual_pallets,
                    COALESCE(
                        SUM(
                            hu.expected_pallets
                            * pr.cases_per_pallet
                            * pr.packs_per_case
                            * pr.pack_weight_kg
                        ) / 1000.0,
                        0
                    ) AS expected_tonnes,
                    COALESCE(
                        SUM(
                            hu.actual_pallets
                            * pr.cases_per_pallet
                            * pr.packs_per_case
                            * pr.pack_weight_kg
                        ) / 1000.0,
                        0
                    ) AS actual_tonnes,
                    COALESCE(SUM(hu.planned_downtime_minutes), 0) AS planned_downtime_minutes,
                    COALESCE(SUM(hu.estimated_lost_packs), 0) AS estimated_lost_packs,
                    COALESCE(SUM(hu.estimated_lost_minutes), 0) AS estimated_lost_minutes,
                    COALESCE(
                        SUM(
                            hu.estimated_lost_packs
                            / NULLIF(pr.packs_per_case, 0)
                            / NULLIF(pr.cases_per_pallet, 0)
                        ),
                        0
                    ) AS estimated_lost_pallets,
                    COALESCE(
                        SUM(hu.estimated_lost_packs * pr.pack_weight_kg) / 1000.0,
                        0
                    ) AS estimated_lost_tonnes
                FROM public.hourly_updates AS hu
                JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
                WHERE {hourly_where};
                """,
                hourly_params,
            )
            hourly_totals = cursor.fetchone()

            cursor.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE de.production_status = 'Ongoing') AS open_faults,
                    COUNT(*) FILTER (WHERE de.production_status = 'Resolved') AS resolved_faults,
                    COALESCE(
                        SUM(
                            EXTRACT(
                                EPOCH FROM (COALESCE(de.resolved_at, NOW()) - de.opened_at)
                            ) / 60.0
                        ),
                        0
                    ) AS unplanned_downtime_minutes,
                    COALESCE(BOOL_OR(de.resolved_at IS NULL), FALSE) AS includes_active_faults
                FROM public.downtime_events AS de
                JOIN public.production_runs AS pr ON pr.id = de.production_run_id
                WHERE {fault_where};
                """,
                fault_params,
            )
            fault_totals = cursor.fetchone()

    expected_pallets = float(hourly_totals["expected_pallets"])
    actual_pallets = float(hourly_totals["actual_pallets"])
    expected_tonnes = float(hourly_totals["expected_tonnes"])
    actual_tonnes = float(hourly_totals["actual_tonnes"])

    return {
        "total_runs": total_runs,
        "expected_pallets": expected_pallets,
        "actual_pallets": actual_pallets,
        "expected_tonnes": expected_tonnes,
        "actual_tonnes": actual_tonnes,
        "output_gap_pallets": max(expected_pallets - actual_pallets, 0),
        "output_gap_tonnes": max(expected_tonnes - actual_tonnes, 0),
        "target_achievement_percent": (
            (actual_pallets / expected_pallets * 100)
            if expected_pallets > 0
            else None
        ),
        "planned_downtime_minutes": float(hourly_totals["planned_downtime_minutes"]),
        "unplanned_downtime_minutes": float(fault_totals["unplanned_downtime_minutes"]),
        "unplanned_downtime_includes_active_faults": bool(
            fault_totals["includes_active_faults"]
        ),
        "estimated_lost_packs": float(hourly_totals["estimated_lost_packs"]),
        "estimated_lost_minutes": float(hourly_totals["estimated_lost_minutes"]),
        "estimated_lost_pallets": float(hourly_totals["estimated_lost_pallets"]),
        "estimated_lost_tonnes": float(hourly_totals["estimated_lost_tonnes"]),
        "open_faults": fault_totals["open_faults"],
        "resolved_faults": fault_totals["resolved_faults"],
        "machine_setup_minutes": None,
        "machine_repair_minutes": None,
        "machine_classification_status": "not_captured",
    }


# ==========================================================
# MANAGEMENT AREA
# ==========================================================
#
# Config tables (production_lines, machines, buttons) and the audit
# log are new, additive tables (see migrations/0001_management_area.sql).
# Disable/rename operations are always UPDATE, never DELETE, so
# historical downtime_events/engineering_updates rows (which reference
# machines only by free-text `machine` name, not a foreign key) stay
# valid regardless of later renames.
#
# Deliberately NOT applying _TEST_DATA_EXCLUSION_SQL here: Management
# must be able to see and force-close TEST-marked runs too (this is
# exactly the manual process used earlier to clear stuck test runs).


# ----------------------------------------------------------
# PRODUCTION LINES
# ----------------------------------------------------------


def list_production_lines():
    query = """
        SELECT id, name, active, display_order, created_at, updated_at
        FROM public.production_lines
        ORDER BY display_order, name;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query)
            return cursor.fetchall()


def get_production_line(line_id):
    query = """
        SELECT id, name, active, display_order, created_at, updated_at
        FROM public.production_lines
        WHERE id = %(line_id)s
        LIMIT 1;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"line_id": line_id})
            return cursor.fetchone()


def _configuration_audit(cursor, action, actor, kind, before, after, record_id=None):
    """Required audit for management writes; failure aborts the surrounding transaction."""
    if actor is None or after is None:
        return
    encode = lambda value: json.dumps(value, default=str)
    cursor.execute("""INSERT INTO public.management_audit_log
 (action,manager_name,record_type,record_id,previous_value,new_value)
 VALUES (%s,%s,%s,%s,%s,%s)""",(action,actor,kind,str(record_id if record_id is not None else after['id']),
        Json(before,dumps=encode) if before is not None else None,Json(after,dumps=encode)))


def create_production_line(name, display_order=0, audit_actor=None):
    query = """
        INSERT INTO public.production_lines (name, display_order)
        VALUES (%(name)s, %(display_order)s)
        RETURNING id, name, active, display_order, created_at, updated_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"name": name, "display_order": display_order})
            created = cursor.fetchone()
            _configuration_audit(cursor,"create_line",audit_actor,"production_line",None,created)

        connection.commit()

    return created


def update_production_line(line_id, name=None, active=None, display_order=None, audit_actor=None):
    fields = []
    params = {"line_id": line_id}

    if name is not None:
        fields.append("name = %(name)s")
        params["name"] = name

    if active is not None:
        fields.append("active = %(active)s")
        params["active"] = active

    if display_order is not None:
        fields.append("display_order = %(display_order)s")
        params["display_order"] = display_order

    fields.append("updated_at = now()")

    query = f"""
        UPDATE public.production_lines
        SET {", ".join(fields)}
        WHERE id = %(line_id)s
        RETURNING id, name, active, display_order, created_at, updated_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            before = None
            if audit_actor is not None:
                cursor.execute("SELECT * FROM public.production_lines WHERE id = %s FOR UPDATE",(line_id,))
                before = cursor.fetchone()
            cursor.execute(query, params)
            updated = cursor.fetchone()
            _configuration_audit(cursor,"update_line",audit_actor,"production_line",before,updated)

        connection.commit()

    return updated


# ----------------------------------------------------------
# MACHINES / SECTIONS
# ----------------------------------------------------------


def list_machines(line_id):
    query = """
        SELECT id, production_line_id, name, active, display_order, created_at, updated_at
        FROM public.machines
        WHERE production_line_id = %(line_id)s
        ORDER BY display_order, name;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"line_id": line_id})
            return cursor.fetchall()


def get_machine(machine_id):
    query = """
        SELECT id, production_line_id, name, active, display_order, created_at, updated_at
        FROM public.machines
        WHERE id = %(machine_id)s
        LIMIT 1;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"machine_id": machine_id})
            return cursor.fetchone()


def create_machine(line_id, name, display_order=0, audit_actor=None):
    query = """
        INSERT INTO public.machines (production_line_id, name, display_order)
        VALUES (%(line_id)s, %(name)s, %(display_order)s)
        RETURNING id, production_line_id, name, active, display_order, created_at, updated_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                query,
                {"line_id": line_id, "name": name, "display_order": display_order},
            )
            created = cursor.fetchone()
            _configuration_audit(cursor,"create_machine",audit_actor,"machine",None,created)

        connection.commit()

    return created


def update_machine(machine_id, name=None, active=None, display_order=None, audit_actor=None):
    fields = []
    params = {"machine_id": machine_id}

    if name is not None:
        fields.append("name = %(name)s")
        params["name"] = name

    if active is not None:
        fields.append("active = %(active)s")
        params["active"] = active

    if display_order is not None:
        fields.append("display_order = %(display_order)s")
        params["display_order"] = display_order

    fields.append("updated_at = now()")

    query = f"""
        UPDATE public.machines
        SET {", ".join(fields)}
        WHERE id = %(machine_id)s
        RETURNING id, production_line_id, name, active, display_order, created_at, updated_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            before = None
            if audit_actor is not None:
                cursor.execute("SELECT * FROM public.machines WHERE id = %s FOR UPDATE",(machine_id,))
                before = cursor.fetchone()
            cursor.execute(query, params)
            updated = cursor.fetchone()
            _configuration_audit(cursor,"update_machine",audit_actor,"machine",before,updated)

        connection.commit()

    return updated


# ----------------------------------------------------------
# FAULT / PLANNED-DOWNTIME BUTTONS
# ----------------------------------------------------------


def list_buttons(machine_id):
    query = """
        SELECT
            id, machine_id, name, event_type, ownership, fault_category,
            display_order, active, created_at, updated_at
        FROM public.buttons
        WHERE machine_id = %(machine_id)s
        ORDER BY display_order, name;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"machine_id": machine_id})
            return cursor.fetchall()


def get_button(button_id):
    query = """
        SELECT
            id, machine_id, name, event_type, ownership, fault_category,
            display_order, active, created_at, updated_at
        FROM public.buttons
        WHERE id = %(button_id)s
        LIMIT 1;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"button_id": button_id})
            return cursor.fetchone()


def create_button(
    machine_id,
    name,
    event_type,
    ownership,
    fault_category=None,
    display_order=0,
    audit_actor=None,
):
    query = """
        INSERT INTO public.buttons (
            machine_id, name, event_type, ownership, fault_category, display_order
        )
        VALUES (
            %(machine_id)s, %(name)s, %(event_type)s, %(ownership)s,
            %(fault_category)s, %(display_order)s
        )
        RETURNING
            id, machine_id, name, event_type, ownership, fault_category,
            display_order, active, created_at, updated_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                query,
                {
                    "machine_id": machine_id,
                    "name": name,
                    "event_type": event_type,
                    "ownership": ownership,
                    "fault_category": fault_category,
                    "display_order": display_order,
                },
            )
            created = cursor.fetchone()
            _configuration_audit(cursor,"create_button",audit_actor,"button",None,created)

        connection.commit()

    return created


def update_button(
    button_id,
    name=None,
    event_type=None,
    ownership=None,
    fault_category=None,
    display_order=None,
    active=None,
    audit_actor=None,
):
    # Note: passing None for fault_category means "leave unchanged", not
    # "clear it" - there is no way to blank an existing fault_category via
    # this dynamic-update pattern (matches how every other optional field
    # here behaves). Not needed for the MVP; documented as a limitation.
    fields = []
    params = {"button_id": button_id}

    if name is not None:
        fields.append("name = %(name)s")
        params["name"] = name

    if event_type is not None:
        fields.append("event_type = %(event_type)s")
        params["event_type"] = event_type

    if ownership is not None:
        fields.append("ownership = %(ownership)s")
        params["ownership"] = ownership

    if fault_category is not None:
        fields.append("fault_category = %(fault_category)s")
        params["fault_category"] = fault_category

    if display_order is not None:
        fields.append("display_order = %(display_order)s")
        params["display_order"] = display_order

    if active is not None:
        fields.append("active = %(active)s")
        params["active"] = active

    fields.append("updated_at = now()")

    query = f"""
        UPDATE public.buttons
        SET {", ".join(fields)}
        WHERE id = %(button_id)s
        RETURNING
            id, machine_id, name, event_type, ownership, fault_category,
            display_order, active, created_at, updated_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            before = None
            if audit_actor is not None:
                cursor.execute("SELECT * FROM public.buttons WHERE id = %s FOR UPDATE",(button_id,))
                before = cursor.fetchone()
            cursor.execute(query, params)
            updated = cursor.fetchone()
            _configuration_audit(cursor,"update_button",audit_actor,"button",before,updated)

        connection.commit()

    return updated


# ----------------------------------------------------------
# PUBLIC HMI CONFIGURATION (read-only, active rows only)
# ----------------------------------------------------------


def get_public_hmi_config():
    """Flat rows of every active line -> active machine -> active button.
    The API layer assembles these into a nested tree. A line with no
    active machines, or a machine with no active buttons, still appears
    (via LEFT JOIN) with null machine_id/button_id fields."""
    query = """
        SELECT
            pl.id AS line_id,
            pl.name AS line_name,
            pl.linetech_config,
            pl.display_order AS line_display_order,
            m.id AS machine_id,
            m.name AS machine_name,
            m.display_order AS machine_display_order,
            b.id AS button_id,
            b.name AS button_name,
            b.event_type,
            b.ownership,
            b.fault_category,
            b.display_order AS button_display_order
        FROM public.production_lines AS pl
        LEFT JOIN public.machines AS m
            ON m.production_line_id = pl.id AND m.active = true
        LEFT JOIN public.buttons AS b
            ON b.machine_id = m.id AND b.active = true
        WHERE pl.active = true
        ORDER BY
            pl.display_order, pl.name,
            m.display_order, m.name,
            b.display_order, b.name;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query)
            return cursor.fetchall()


def get_hmi_line_state():
    """One row per ACTIVE configured line, with the authoritative state
    of that line right now - read-only, and safe for the unauthenticated
    factory-floor HMI.

    Deliberately excludes everything Management-only: no tonnage, no
    achievement or OEE figures, no targets, no waste, no costs and no
    technician performance. Only what a tablet needs to decide whether a
    line can be started and, if not, what is happening on it.

    A line with no active run returns NULL for every run column - that
    is "available", not missing data. Inactive configured lines are
    excluded entirely (pl.active = true), exactly like the HMI config
    endpoint.
    """
    query = """
        SELECT
            pl.id   AS line_id,
            pl.name AS line_name,
            r.id    AS run_id,
            r.line_technician,
            r.shift,
            r.customer,
            r.product,
            r.started_at,
            (
                SELECT MAX(COALESCE(hu.period_ended_at, hu.created_at))
                FROM public.hourly_updates AS hu
                WHERE hu.production_run_id = r.id
            ) AS last_hourly_update_at,
            (
                SELECT pde.id
                FROM public.planned_downtime_events AS pde
                WHERE pde.production_run_id = r.id AND pde.ended_at IS NULL
                LIMIT 1
            ) AS open_planned_downtime_id,
            (
                SELECT MAX(pde.started_at)
                FROM public.planned_downtime_events AS pde
                WHERE pde.production_run_id = r.id
            ) AS last_planned_downtime_at,
            (
                SELECT co.id
                FROM public.changeovers AS co
                WHERE co.previous_production_run_id = r.id AND co.status = 'Open'
                LIMIT 1
            ) AS open_changeover_id,
            (
                SELECT MAX(co.started_at)
                FROM public.changeovers AS co
                WHERE co.previous_production_run_id = r.id
            ) AS last_changeover_at,
            (
                SELECT COUNT(*)
                FROM public.downtime_events AS de
                WHERE de.production_run_id = r.id AND de.production_status = 'Ongoing'
            ) AS open_fault_count,
            (
                SELECT MAX(de.opened_at)
                FROM public.downtime_events AS de
                WHERE de.production_run_id = r.id
            ) AS last_fault_opened_at,
            (
                SELECT COUNT(*)
                FROM public.downtime_events AS de
                JOIN public.production_runs AS fr ON fr.id = de.production_run_id
                WHERE fr.production_line = pl.name AND de.production_status = 'Ongoing'
            ) AS line_open_fault_count,
            ls.id         AS stoppage_id,
            ls.kind       AS stoppage_kind,
            EXISTS(SELECT 1 FROM public.changeovers ch WHERE ch.line_stoppage_id=ls.id AND ch.workflow IS NOT NULL) AS stoppage_linetech,
            ls.reason     AS stoppage_reason,
            ls.started_at AS stoppage_started_at,
            ls.started_by AS stoppage_started_by,
            ls.physical_ended_at AS stoppage_physical_ended_at,
            -- A run ended with no next step chosen (the End Run choice was
            -- abandoned). It stays until a technician or manager resolves
            -- it - no time limit - and Start Run is refused meanwhile. Runs
            -- that ended before migration 0004 are in the legacy baseline
            -- and never wait.
            lr.id AS last_run_id,
            lr.line_technician AS last_run_technician,
            lr.finished_at AS last_run_finished_at,
            (
                lr.id IS NOT NULL
                AND ls.id IS NULL
                AND NOT EXISTS (
                    SELECT 1 FROM public.line_stoppages AS x
                    WHERE x.previous_production_run_id = lr.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM public.run_next_step_legacy_baseline AS b
                    WHERE b.production_run_id = lr.id
                )
            ) AS awaiting_next_step
        FROM public.production_lines AS pl
        LEFT JOIN public.line_stoppages AS ls
            ON ls.production_line = pl.name AND ls.ended_at IS NULL
        LEFT JOIN LATERAL (
            SELECT fr.id, fr.line_technician, fr.finished_at
            FROM public.production_runs AS fr
            WHERE fr.production_line = pl.name AND fr.status <> 'Active' AND fr.finished_at IS NOT NULL
            ORDER BY fr.finished_at DESC
            LIMIT 1
        ) AS lr ON TRUE
        LEFT JOIN public.production_runs AS r
            ON r.production_line = pl.name AND r.status = 'Active'
        WHERE pl.active = true
        ORDER BY pl.display_order, pl.name;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query)
            return cursor.fetchall()


# ----------------------------------------------------------
# ACTIVE RUNS (ACROSS ALL LINES) + FORCE CLOSE
# ----------------------------------------------------------


def get_all_active_runs():
    """Every Active run, with what a manager needs before force-closing
    it: when output was last reported, any planned stop or changeover
    still open on the run, and how many faults are still open on the
    line (faults belong to the line, not the run, and carry over)."""
    query = """
        SELECT
            r.id,
            r.production_line,
            r.line_technician,
            r.shift,
            r.customer,
            r.product,
            r.status,
            r.started_at,
            (
                SELECT MAX(COALESCE(hu.period_ended_at, hu.created_at))
                FROM public.hourly_updates AS hu
                WHERE hu.production_run_id = r.id
            ) AS last_hourly_update_at,
            (
                SELECT COUNT(*)
                FROM public.hourly_updates AS hu
                WHERE hu.production_run_id = r.id
            ) AS hourly_update_count,
            pde.reason AS open_planned_stop_reason,
            pde.started_at AS open_planned_stop_started_at,
            (
                SELECT co.id
                FROM public.changeovers AS co
                WHERE co.previous_production_run_id = r.id AND co.status = 'Open'
                LIMIT 1
            ) AS open_changeover_id,
            (
                SELECT COUNT(*)
                FROM public.downtime_events AS de
                JOIN public.production_runs AS fr ON fr.id = de.production_run_id
                WHERE fr.production_line = r.production_line
                  AND de.production_status = 'Ongoing'
            ) AS open_line_fault_count
        FROM public.production_runs AS r
        LEFT JOIN public.planned_downtime_events AS pde
            ON pde.production_run_id = r.id AND pde.ended_at IS NULL
        WHERE r.status = 'Active'
        ORDER BY r.started_at DESC;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query)
            return cursor.fetchall()


def force_close_production_run(production_run_id, finished_at, closed_by, reason=None, manager_name=None):
    """Closes an abandoned run as ONE transaction, with the run row
    locked. Returns None if the run is no longer Active (never existed,
    or another manager closed it first - the partial unique index
    idx_unique_active_run_per_line and the lock guarantee at most one
    close). No hourly output is written: hours nobody reported stay
    missing, never estimated.

    A planned stop still open on the run is ended at the close time,
    as End Run would require - left open it would keep running with no
    run behind it. An open changeover is refused (409): a changeover
    has no truthful 'abandoned' state, so it must be completed on the
    tablet first. Open faults are left alone - they belong to the line
    and carry over to the next run."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT *
                FROM public.production_runs
                WHERE id = %(production_run_id)s AND status = 'Active'
                FOR UPDATE;
                """,
                {"production_run_id": production_run_id},
            )
            before = cursor.fetchone()
            if before is None:
                connection.rollback()
                return None

            cursor.execute(
                """
                SELECT id FROM public.changeovers
                WHERE previous_production_run_id = %(production_run_id)s AND status = 'Open';
                """,
                {"production_run_id": production_run_id},
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                raise PulseCaptureError(
                    409,
                    "A changeover is still open on this run. Complete it on the line tablet "
                    "(Changeover Complete) before force-closing the run.",
                )

            cursor.execute(
                f"""
                SELECT {_PLANNED_DOWNTIME_COLUMNS}
                FROM public.planned_downtime_events
                WHERE production_run_id = %(production_run_id)s AND ended_at IS NULL
                FOR UPDATE;
                """,
                {"production_run_id": production_run_id},
            )
            open_stop = cursor.fetchone()
            ended_stop = None
            if open_stop is not None:
                ended_at = max(finished_at, open_stop["started_at"])
                cursor.execute(
                    f"""
                    UPDATE public.planned_downtime_events
                    SET ended_at = %(ended_at)s,
                        ended_by = %(ended_by)s,
                        duration_minutes = %(duration_minutes)s
                    WHERE id = %(id)s AND ended_at IS NULL
                    RETURNING {_PLANNED_DOWNTIME_COLUMNS};
                    """,
                    {
                        "id": open_stop["id"],
                        "ended_at": ended_at,
                        "ended_by": closed_by,
                        "duration_minutes": calc.for_storage(
                            calc.duration_minutes(open_stop["started_at"], ended_at)
                        ),
                    },
                )
                ended_stop = cursor.fetchone()

            cursor.execute(
                """
                UPDATE public.production_runs
                SET
                    status = 'Cancelled',
                    finished_at = %(finished_at)s
                WHERE id = %(production_run_id)s
                  AND status = 'Active'
                RETURNING id, production_line, status, finished_at;
                """,
                {"production_run_id": production_run_id, "finished_at": finished_at},
            )
            closed = cursor.fetchone()
            snapshot = {**closed, "ended_planned_stop": ended_stop}
            encode = lambda value: json.dumps(value, default=str)
            cursor.execute("""INSERT INTO public.management_audit_log
 (action,manager_name,record_type,record_id,previous_value,new_value,reason)
 VALUES ('force_close_run',%(manager)s,'production_run',%(id)s,%(before)s,%(after)s,%(reason)s)""",
                {"manager":manager_name or closed_by,"id":str(production_run_id),
                 "before":Json(before,dumps=encode),"after":Json(snapshot,dumps=encode),
                 "reason":reason or "Legacy force-close: reason not supplied"})

        connection.commit()

    return {**closed, "ended_planned_stop": ended_stop}


# ----------------------------------------------------------
# AUDIT LOG
# ----------------------------------------------------------


def insert_audit_log(
    action,
    manager_name,
    record_type,
    record_id,
    previous_value=None,
    new_value=None,
    reason=None,
):
    query = """
        INSERT INTO public.management_audit_log (
            action, manager_name, record_type, record_id,
            previous_value, new_value, reason
        )
        VALUES (
            %(action)s, %(manager_name)s, %(record_type)s, %(record_id)s,
            %(previous_value)s, %(new_value)s, %(reason)s
        )
        RETURNING id, created_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                query,
                {
                    "action": action,
                    "manager_name": manager_name,
                    "record_type": record_type,
                    "record_id": str(record_id) if record_id is not None else None,
                    "previous_value": Json(previous_value) if previous_value is not None else None,
                    "new_value": Json(new_value) if new_value is not None else None,
                    "reason": reason,
                },
            )
            created = cursor.fetchone()

        connection.commit()

    return created


# ----------------------------------------------------------
# TECHNICIAN PERFORMANCE
# ----------------------------------------------------------


def get_technician_performance(filters=None):
    """Completed-run cohort, selected by London start date; shared reconciliation.
    Fault evidence is line-wide so carried stops are clipped to the selected runs.
    """
    try:
        from .technician_reports import build_technician_performance
    except ImportError:
        from technician_reports import build_technician_performance
    conditions, params = _run_conditions({**(filters or {}), "run_status": "Completed"})
    where = " AND ".join(conditions).replace("pr.started_at::date", "(pr.started_at AT TIME ZONE 'Europe/London')::date")
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"SELECT pr.* FROM public.production_runs pr WHERE {where} ORDER BY pr.id",params)
            runs = cursor.fetchall()
            if not runs:
                return []
            ids = [r["id"] for r in runs]
            cursor.execute("SELECT production_run_id,pallets_completed,period_started_at,period_ended_at FROM public.hourly_updates WHERE production_run_id = ANY(%s)",(ids,))
            readings = cursor.fetchall()
            cursor.execute("SELECT production_run_id,started_at,ended_at FROM public.planned_downtime_events WHERE production_run_id = ANY(%s)",(ids,))
            planned = cursor.fetchall()
            cursor.execute(f"""SELECT DISTINCT de.id, origin.production_line,de.opened_at,de.resolved_at
 FROM public.downtime_events de JOIN public.production_runs origin ON origin.id=de.production_run_id
 JOIN public.production_runs selected ON selected.production_line=origin.production_line
 WHERE {_TEST_DATA_EXCLUSION_SQL.replace("pr.", "origin.")}
 AND selected.id=ANY(%s) AND de.opened_at < selected.finished_at
 AND (de.resolved_at IS NULL OR de.resolved_at > selected.started_at)""",(ids,))
            faults = cursor.fetchall()
    return build_technician_performance(runs,readings,planned,faults)


# ==========================================================
# ENGINEERING WORKFLOW (Stage 5A)
# ==========================================================
#
# New, additive functions only - no existing function above this
# section is modified. get_engineering_faults() is a dedicated sibling
# to get_dashboard_faults() (same _fault_conditions filter builder,
# reused rather than duplicated) rather than a change to that
# function, so the existing, tested /api/v1/dashboard/faults endpoint
# and its Pydantic response model are never touched.
#
# All accept/close writes use a guarded UPDATE ... WHERE <still in the
# expected state> RETURNING pattern, the same atomic-compare-and-set
# idiom already proven by force_close_production_run() above: the
# database's own row lock during the UPDATE is what makes two
# concurrent requests safe, not application-level locking.


def get_downtime_event_by_id(downtime_event_id):
    """Plain, unfiltered lookup by id (mirrors get_production_run_by_id
    above) - used by the Engineering API for 404 checks and for
    working out *why* a guarded UPDATE matched zero rows."""
    query = """
        SELECT
            id,
            production_run_id,
            fault_id,
            machine,
            reason,
            reported_by,
            engineer,
            production_status,
            engineering_status,
            opened_at,
            accepted_at,
            resolved_at
        FROM public.downtime_events
        WHERE id = %(downtime_event_id)s
        LIMIT 1;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"downtime_event_id": downtime_event_id})
            return cursor.fetchone()


def get_engineering_repair_updates(downtime_event_ids, *, connection=None):
    """Repair-update history for a specific set of downtime_events rows,
    including the Stage 5A repair_classification / Machine Setting /
    created_at columns. Kept separate from
    get_dashboard_engineering_updates() above so that function's
    existing, tested query and consumer (/api/v1/dashboard/
    engineering-downtime) are never touched by this new column set."""
    if not downtime_event_ids:
        return []

    query = """
        SELECT
            eu.id,
            eu.downtime_event_id,
            eu.engineer,
            eu.update_type,
            eu.repair_classification,
            eu.finding,
            eu.action,
            eu.notes,
            eu.setting_name,
            eu.previous_value,
            eu.new_value,
            eu.reason_for_change,
            eu.affected_products_or_formats,
            eu.engineering_status,
            eu.created_at
        FROM public.engineering_updates AS eu
        WHERE eu.downtime_event_id = ANY(%(downtime_event_ids)s)
        ORDER BY eu.downtime_event_id, eu.id;
    """

    with (nullcontext(connection) if connection is not None else get_database_connection()) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {"downtime_event_ids": list(downtime_event_ids)})
            return cursor.fetchall()


def get_engineering_faults(filters=None, *, connection=None):
    """Open + resolved faults for the Engineering API, with nested
    repair_updates. Reuses _fault_conditions() (the same filter builder
    get_dashboard_faults() uses) rather than duplicating its
    business rules; adds accepted_at (new Stage 5A column) and omits
    fields the Engineering brief did not ask for (engineer_called,
    retrospective) to avoid exposing unrelated internal data."""
    supplied_connection = connection
    conditions, params = _fault_conditions(filters)
    # This endpoint filters engineering work, not production downtime.
    conditions = [c.replace("de.production_status =", "de.engineering_status =") for c in conditions]
    conditions.append("de.engineer_called = TRUE")
    where_sql = " AND ".join(conditions)

    query = f"""
        SELECT
            de.id AS downtime_event_id,
            de.production_run_id,
            pr.production_line,
            de.fault_id,
            de.machine,
            de.reason,
            de.report_note,
            de.reported_by,
            de.engineer,
            de.production_status,
            de.engineering_status,
            de.opened_at,
            de.accepted_at,
            de.resolved_at,
            EXTRACT(
                EPOCH FROM (COALESCE(de.resolved_at, NOW()) - de.opened_at)
            ) / 60.0 AS duration_minutes,
            (de.resolved_at IS NULL) AS duration_is_active
        FROM public.downtime_events AS de
        JOIN public.production_runs AS pr ON pr.id = de.production_run_id
        WHERE {where_sql}
        ORDER BY de.opened_at DESC;
    """

    with (nullcontext(connection) if connection is not None else get_database_connection()) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

    updates_by_fault = {}
    for update in get_engineering_repair_updates([row["downtime_event_id"] for row in rows], connection=supplied_connection):
        updates_by_fault.setdefault(update["downtime_event_id"], []).append(update)

    for row in rows:
        row["repair_updates"] = updates_by_fault.get(row["downtime_event_id"], [])

    return rows


def accept_engineering_fault(downtime_event_id, engineer, accepted_at):
    """Atomic compare-and-set: only assigns when the fault is still
    Ongoing AND (unassigned OR already assigned to this same engineer).
    Returns None if a second engineer already holds it, or if it is no
    longer Ongoing - the caller does one get_downtime_event_by_id
    lookup to tell those cases apart, same idiom as
    management_api.force_close_run(). COALESCE keeps the original
    accepted_at on a repeat call from the same engineer (idempotent).

    engineering_status is set to 'Ongoing', not 'Investigating': a
    read-only live-schema check confirmed chk_engineering_status
    permits exactly 'Not Started' / 'Ongoing' / 'Resolved' -
    'Investigating' is not a legal value for this column."""
    query = """
        UPDATE public.downtime_events
        SET
            engineer = %(engineer)s,
            engineering_status = 'Ongoing',
            accepted_at = COALESCE(accepted_at, %(accepted_at)s)
        WHERE id = %(downtime_event_id)s
          AND engineering_status <> 'Resolved'
          AND engineer_called = TRUE
          AND (engineer IS NULL OR engineer = %(engineer)s)
        RETURNING
            id, production_run_id, fault_id, machine, reason, reported_by,
            engineer, production_status, engineering_status, opened_at,
            accepted_at, resolved_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                query,
                {
                    "downtime_event_id": downtime_event_id,
                    "engineer": engineer,
                    "accepted_at": accepted_at,
                },
            )
            accepted = cursor.fetchone()

        connection.commit()

    return accepted


def add_engineering_repair_update(downtime_event_id, repair_update):
    """Plain insert for an interim repair-progress update (between
    Accept and Close). Does not change downtime_events' own state -
    engineering_status stays 'Ongoing', production_status is
    untouched, mirroring the CLI's existing "Follow Up" semantics for
    a non-final engineering communication.

    engineering_status is set to 'Ongoing', not 'Investigating': a
    read-only live-schema check confirmed chk_engineering_update_status
    permits exactly 'Ongoing' / 'Resolved' - 'Investigating' is not a
    legal value for this column."""
    query = """
        WITH owned_job AS (
            SELECT id FROM public.downtime_events
            WHERE id = %(downtime_event_id)s AND engineer = %(engineer)s
              AND engineering_status <> 'Resolved'
            FOR UPDATE
        )
        INSERT INTO public.engineering_updates (
            downtime_event_id, production_run_id, fault_id, engineer,
            update_type, repair_classification, finding, action, notes,
            setting_name, previous_value, new_value, reason_for_change,
            affected_products_or_formats, engineering_status
        )
        SELECT
            %(downtime_event_id)s, %(production_run_id)s, %(fault_id)s, %(engineer)s,
            'Follow Up', %(repair_classification)s, %(finding)s, %(action)s, %(notes)s,
            %(setting_name)s, %(previous_value)s, %(new_value)s, %(reason_for_change)s,
            %(affected_products_or_formats)s, 'Ongoing'
        FROM owned_job
        RETURNING id, created_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, {**repair_update, "downtime_event_id": downtime_event_id})
            created = cursor.fetchone()

        connection.commit()

    # The ownership/status guard can lose a race to handover or closure.
    # No matching job means no insert, not a database outage.
    return created


def close_engineering_fault(downtime_event_id, repair_update, resolved_at):
    """Close Engineering only. The final history row records closure time.
    Production status and resolved_at belong to the technician restart action.
    Ownership and engineering state are checked atomically; a failed guard
    rolls back the final history insert as well.
    """
    insert_query = """
        INSERT INTO public.engineering_updates (
            downtime_event_id, production_run_id, fault_id, engineer,
            update_type, repair_classification, finding, action, notes,
            setting_name, previous_value, new_value, reason_for_change,
            affected_products_or_formats, engineering_status, created_at
        )
        VALUES (
            %(downtime_event_id)s, %(production_run_id)s, %(fault_id)s, %(engineer)s,
            'Resolution', %(repair_classification)s, %(finding)s, %(action)s, %(notes)s,
            %(setting_name)s, %(previous_value)s, %(new_value)s, %(reason_for_change)s,
            %(affected_products_or_formats)s, 'Resolved', %(closed_at)s
        )
        RETURNING id;
    """

    update_query = """
        UPDATE public.downtime_events
        SET
            engineering_status = 'Resolved',
            maintenance_preventable = %(maintenance_preventable)s
        WHERE id = %(downtime_event_id)s
          AND engineering_status <> 'Resolved'
          AND engineer = %(engineer)s
        RETURNING
            id, production_run_id, fault_id, machine, reason, reported_by,
            engineer, production_status, engineering_status, opened_at,
            accepted_at, resolved_at, maintenance_preventable;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(insert_query, {**repair_update, "downtime_event_id": downtime_event_id, "closed_at": resolved_at})
            cursor.fetchone()

            cursor.execute(
                update_query,
                {
                    "downtime_event_id": downtime_event_id,
                    "engineer": repair_update["engineer"],
                    "maintenance_preventable": repair_update["maintenance_preventable"],
                },
            )
            closed = cursor.fetchone()

        if closed is None:
            connection.rollback()
            return None

        connection.commit()

    return closed


def hand_over_engineering_fault(downtime_event_id, handover):
    """Releases an owned, in-progress downtime_event back to unassigned,
    then records the handover note as a 'Follow Up' engineering_updates
    row - in a single database transaction on one connection (psycopg3's
    implicit BEGIN/commit-or-rollback-on-exit).

    Order is deliberately the reverse of close_engineering_fault(): the
    guarded UPDATE runs FIRST here. Its WHERE clause requires the fault
    to still be Ongoing, its engineering_status to still be 'Ongoing',
    engineer to be this exact authenticated engineer, and accepted_at to
    be non-NULL (i.e. genuinely accepted, not merely assigned) - the
    same atomic compare-and-set idiom used by accept_engineering_fault()
    / close_engineering_fault(): the database's own row lock during the
    UPDATE is what makes concurrent requests (a second handover, a
    close, another accept racing this one) safe, not application-level
    locking.

    If the UPDATE matches zero rows, this function returns None
    immediately, WITHOUT ever attempting the history INSERT - there is
    nothing to roll back because nothing has been written yet. Only
    once the UPDATE succeeds does the handover-note INSERT run, on the
    same connection/cursor. If that INSERT raises for any reason, it is
    caught here and the transaction is explicitly rolled back (undoing
    the UPDATE too) before the exception is re-raised - so no code path
    can ever commit the fault's release without its history record, or
    vice versa: both statements land together, or neither does.

    engineering_status literals here ('Not Started' for the released
    fault, 'Ongoing' for the history row) are deliberately NOT
    'Investigating': a read-only live-schema check confirmed
    chk_engineering_status permits exactly 'Not Started' / 'Ongoing' /
    'Resolved', and chk_engineering_update_status permits exactly
    'Ongoing' / 'Resolved' - 'Investigating' is not a legal value for
    either column."""
    update_query = """
        UPDATE public.downtime_events
        SET
            engineer = NULL,
            accepted_at = NULL,
            engineering_status = 'Not Started'
        WHERE id = %(downtime_event_id)s
          AND engineering_status = 'Ongoing'
          AND engineer = %(engineer)s
          AND accepted_at IS NOT NULL
        RETURNING
            id, production_run_id, fault_id, machine, reason, reported_by,
            engineer, production_status, engineering_status, opened_at,
            accepted_at, resolved_at;
    """

    insert_query = """
        INSERT INTO public.engineering_updates (
            downtime_event_id, production_run_id, fault_id, engineer,
            update_type, finding, action, engineering_status
        )
        VALUES (
            %(downtime_event_id)s, %(production_run_id)s, %(fault_id)s, %(engineer)s,
            'Follow Up', %(finding)s, %(action)s, 'Ongoing'
        )
        RETURNING id, created_at;
    """

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                update_query,
                {"downtime_event_id": downtime_event_id, "engineer": handover["engineer"]},
            )
            released = cursor.fetchone()

            if released is None:
                connection.rollback()
                return None

            try:
                cursor.execute(insert_query, {**handover, "downtime_event_id": downtime_event_id})
                cursor.fetchone()
            except Exception:
                connection.rollback()
                raise

        connection.commit()

    return released


# ==========================================================
# PULSE PHASE 1 FOUNDATION (Stage 6B1 / 6B2)
# ==========================================================
#
# Requires migrations/0003_pulse_phase1_foundation.sql. Every write
# below runs in ONE transaction:
#   1. it first claims the client's Idempotency-Key (hmi_idempotency_keys),
#      so a double tap or a retry after a timeout replays the original
#      response instead of writing twice;
#   2. it locks the production_runs row (SELECT ... FOR UPDATE) before
#      reading anything it depends on, so concurrent writes on the same
#      run are serialised;
#   3. it stores the response it is about to return in the same
#      transaction before COMMIT.
# All manufacturing arithmetic is delegated to src/pulse_calculations.py
# (Decimal); this module only reads and writes rows.


class PulseCaptureError(Exception):
    """A capture request the database state rejects. `status_code` is
    the HTTP status the API layer should return; `message` is always a
    safe, pre-written sentence (never exception text)."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class IdempotentReplay(Exception):
    """The Idempotency-Key was already completed with the same request:
    the caller must return the stored response unchanged."""

    def __init__(self, status_code, body):
        super().__init__("idempotent replay")
        self.status_code = status_code
        self.body = body


#: How long a used Idempotency-Key keeps protecting its action. Must
#: match the expires_at DEFAULT in migration 0003.
IDEMPOTENCY_RETENTION_DAYS = 90


@dataclass(frozen=True)
class IdempotencyRequest:
    key: str
    action: str
    fingerprint: str
    # respond(result) -> (status_code, JSON-serialisable body). Called
    # inside the transaction so the stored response is exactly what the
    # first successful request returned.
    respond: Callable


def _claim_idempotency(cursor, connection, idempotency, production_run_id=None):
    if idempotency is None:
        return

    params = {
        "key": idempotency.key,
        "action": idempotency.action,
        "fingerprint": idempotency.fingerprint,
        "production_run_id": production_run_id,
    }

    # A concurrent request with the same key blocks here until the
    # first one commits (then conflicts) or rolls back (then succeeds).
    #
    # DO UPDATE ... WHERE expires_at <= now() is the expired-key rule: a
    # key whose retention period has passed is taken over as a brand-new
    # logical action, inside this same transaction. A key still within
    # its period never matches that WHERE, so no row is returned and we
    # fall through to the replay/conflict path below - a live key is
    # never silently overwritten. created_at and expires_at come from
    # the database clock, never from the request.
    cursor.execute(
        f"""
        INSERT INTO public.hmi_idempotency_keys (
            idempotency_key, action, request_fingerprint, production_run_id
        )
        VALUES (%(key)s, %(action)s, %(fingerprint)s, %(production_run_id)s)
        ON CONFLICT (idempotency_key) DO UPDATE
        SET action = EXCLUDED.action,
            request_fingerprint = EXCLUDED.request_fingerprint,
            production_run_id = EXCLUDED.production_run_id,
            response_status = NULL,
            response_body = NULL,
            created_at = now(),
            expires_at = now() + interval '{IDEMPOTENCY_RETENTION_DAYS} days'
        WHERE public.hmi_idempotency_keys.expires_at <= now()
        RETURNING idempotency_key;
        """,
        params,
    )
    if cursor.fetchone() is not None:
        return

    cursor.execute(
        """
        SELECT action, request_fingerprint, response_status, response_body
        FROM public.hmi_idempotency_keys
        WHERE idempotency_key = %(key)s;
        """,
        params,
    )
    existing = cursor.fetchone()
    connection.rollback()

    if existing is None or existing["response_status"] is None:
        raise PulseCaptureError(
            409, "This action is still being processed. Wait a moment, then check before trying again."
        )

    if existing["action"] != idempotency.action or existing["request_fingerprint"] != idempotency.fingerprint:
        raise PulseCaptureError(
            409,
            "This request was already used with different details. Nothing new was saved - "
            "refresh to see the latest state.",
        )

    raise IdempotentReplay(existing["response_status"], existing["response_body"])


def delete_expired_idempotency_keys(batch_size=None):
    """MAINTENANCE ONLY - deletes idempotency keys whose retention period
    has already passed, and nothing else.

    This is deliberately never called from an HMI write. Sweeping on the
    write path would put an unbounded DELETE inside a request that an
    operator is waiting on, and a clock skew or a bug there could remove
    a key that is still protecting an in-flight action. Run it as a
    scheduled maintenance job instead.

    Safety properties:
      - the WHERE clause is `expires_at <= now()` and cannot be widened
        by a caller; there is no "delete all" mode and no key argument,
      - `now()` is the database clock, so a wrong clock on whatever host
        runs the job cannot expire a key early,
      - a non-expired key is never matched, so replay protection for
        every live key is unaffected,
      - leaving expired rows in place is harmless: they keep protecting
        their key until this runs.

    batch_size, when given, limits one call to that many oldest expired
    rows so a large first sweep can be run in steps. Returns the number
    of rows actually deleted.
    """
    if batch_size is not None and batch_size < 1:
        raise ValueError("batch_size must be at least 1 when given.")

    if batch_size is None:
        statement = """
            DELETE FROM public.hmi_idempotency_keys
            WHERE expires_at <= now();
        """
        params = {}
    else:
        statement = """
            DELETE FROM public.hmi_idempotency_keys
            WHERE idempotency_key IN (
                SELECT idempotency_key
                FROM public.hmi_idempotency_keys
                WHERE expires_at <= now()
                ORDER BY expires_at
                LIMIT %(batch_size)s
            );
        """
        params = {"batch_size": batch_size}

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(statement, params)
            return cursor.rowcount


def _store_idempotent_response(cursor, idempotency, result):
    if idempotency is None:
        return

    status_code, body = idempotency.respond(result)
    cursor.execute(
        """
        UPDATE public.hmi_idempotency_keys
        SET response_status = %(status)s, response_body = %(body)s
        WHERE idempotency_key = %(key)s;
        """,
        {"status": status_code, "body": Json(body), "key": idempotency.key},
    )


_CAPTURE_RUN_COLUMNS = """
    id, production_line, line_technician, shift, customer, product, format,
    pack_type, pack_weight_kg, packs_per_case, cases_per_pallet, target_speed_ppm, standard_speed_ppm, standard_version_id,
    starting_pallets_remaining, pallets_remaining, total_pallets_completed,
    potential_overrun_pallets, status, started_at, finished_at
"""


def _lock_active_run(cursor, connection, production_run_id):
    cursor.execute(
        f"""
        SELECT {_CAPTURE_RUN_COLUMNS}
        FROM public.production_runs
        WHERE id = %(production_run_id)s
        FOR UPDATE;
        """,
        {"production_run_id": production_run_id},
    )
    run = cursor.fetchone()

    if run is None:
        connection.rollback()
        raise PulseCaptureError(404, f"Production Run {production_run_id} was not found.")

    if run["status"] != "Active":
        connection.rollback()
        raise PulseCaptureError(409, f"Production Run {production_run_id} is not active.")

    return run


def _run_shift_name(run, moment):
    """The run's recorded shift is authoritative; the clock is only a
    fallback for a legacy run whose shift label is unrecognised."""
    return normalise_shift_name(run["shift"]) or shift_name_at(moment)


def _planned_intervals(cursor, production_run_id, period_start, period_end):
    cursor.execute(
        """
        SELECT reason, started_at, ended_at
        FROM public.planned_downtime_events
        WHERE production_run_id = %(production_run_id)s
          AND started_at < %(period_end)s
          AND (ended_at IS NULL OR ended_at > %(period_start)s);
        """,
        {
            "production_run_id": production_run_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    )
    return cursor.fetchall()


def _speed_changes(cursor, production_run_id):
    cursor.execute(
        """
        SELECT previous_speed_ppm, new_speed_ppm, effective_at
        FROM public.run_target_speed_changes
        WHERE production_run_id = %(production_run_id)s
        ORDER BY effective_at;
        """,
        {"production_run_id": production_run_id},
    )
    return cursor.fetchall()


def _operating_changes(cursor, production_run_id):
    cursor.execute("SELECT * FROM public.run_operating_speed_changes WHERE production_run_id=%s ORDER BY effective_at, id",
                   (production_run_id,))
    return cursor.fetchall()


def _review_period(cursor, run, start, end, pallets, note=None):
    planned = _planned_intervals(cursor, run["id"], start, end)
    cursor.execute("""SELECT de.opened_at, de.resolved_at, de.reason FROM public.downtime_events de
        JOIN public.production_runs pr ON pr.id=de.production_run_id
        WHERE pr.production_line=%s AND de.opened_at < %s
        AND (de.resolved_at IS NULL OR de.resolved_at > %s)""", (run["production_line"], end, start))
    faults = cursor.fetchall()
    review = calc.reconciliation_api(calc.reconcile_production(
        run.get("standard_speed_ppm"), start, end,
        calc.PackConfig.from_row(run).pallets_to_packs(pallets) if pallets is not None else None,
        [(p["started_at"], p["ended_at"] or end) for p in planned],
        [(f["opened_at"], f["resolved_at"] or end) for f in faults],
        _operating_changes(cursor, run["id"]), note=note))
    report = performance.period_report(run.get("standard_speed_ppm"), start, end,
        calc.PackConfig.from_row(run).pallets_to_packs(pallets) if pallets is not None else None,
        planned, faults, note)
    review["production_report"] = performance.report_api(report)
    review["estimated_oee"] = performance.aggregate_oee([report])
    return review



def _local_clock(moment):
    return f"{to_london(moment):%H:%M}"


def _insert_hourly_update(
    cursor, connection, run, pallets_produced, line_technician, other_loss_reason, recorded_at,
    hour_start, final=False,
):
    """Writes the reading for ONE named clock hour of an already-locked
    run and advances the run's progress.

    The reading covers the part of the hour the run was open:
    [max(hour start, run start), min(hour end, run end)). A normal
    hourly update is only accepted once that clock hour has finished; a
    FINAL reading (End Run) covers the current hour up to `recorded_at`,
    the moment the run's output clock stops. At most one reading per run
    per hour - a missed hour is reported on its own, never merged into
    another. Target packs use the immutable agreed standard. Legacy runs retain
    historical target snapshots, explicitly unavailable to fixed-standard reconciliation. The output belongs to the
    clock shift the hour falls in."""
    production_run_id = run["id"]

    if not is_clock_hour_start(hour_start):
        connection.rollback()
        raise PulseCaptureError(422, "Choose a whole clock hour, for example 06:00–07:00.")

    label = clock_hour_label(hour_start)
    window = calc.reading_window(hour_start, run["started_at"], recorded_at if final else None)
    if window is None:
        connection.rollback()
        if final:
            raise PulseCaptureError(
                409,
                "No part of the current hour has run yet, so there is nothing to add. "
                "Answer No to production in the final part hour.",
            )
        raise PulseCaptureError(422, f"{label} is outside this run.")

    period_start, period_end = window
    if not final and hour_start + CLOCK_HOUR > recorded_at:
        connection.rollback()
        raise PulseCaptureError(
            409,
            f"{label} has not finished yet. Report it after {_local_clock(hour_start + CLOCK_HOUR)}.",
        )

    cursor.execute(
        """
        SELECT id FROM public.hourly_updates
        WHERE production_run_id = %(production_run_id)s AND hour_start = %(hour_start)s;
        """,
        {"production_run_id": production_run_id, "hour_start": hour_start},
    )
    if cursor.fetchone() is not None:
        connection.rollback()
        raise PulseCaptureError(409, f"{label} has already been reported for this run.")

    timeline = ([(None, calc.to_decimal(run["standard_speed_ppm"]))] if run.get("standard_speed_ppm") is not None
                else calc.speed_timeline(run["target_speed_ppm"], _speed_changes(cursor, production_run_id)))
    period_minutes = calc.minutes_between(period_start, period_end)
    target_packs = calc.target_packs_between(timeline, period_start, period_end)

    planned_rows = _planned_intervals(cursor, production_run_id, period_start, period_end)
    planned_pieces = calc.intersect_intervals(
        [(row["started_at"], row["ended_at"] or period_end) for row in planned_rows],
        [(period_start, period_end)],
    )
    planned_minutes = calc.total_minutes(planned_pieces)
    planned_reasons = sorted({row["reason"] for row in planned_rows})

    values = calc.hourly_update_values(
        calc.PackConfig.from_row(run),
        pallets_produced,
        period_minutes,
        planned_minutes,
        run,
        expected_packs=target_packs,
    )

    loss_review = _review_period(cursor, run, period_start, period_end, pallets_produced, other_loss_reason)

    shift_name = shift_name_at(hour_start)
    shift_window = shift_window_containing(hour_start)

    cursor.execute(
        """
        INSERT INTO public.hourly_updates (
            production_run_id, pallets_completed, planned_downtime,
            planned_downtime_minutes, expected_packs, actual_packs,
            expected_pallets, actual_pallets, production_variance_packs,
            estimated_lost_packs, estimated_lost_minutes, unexplained_loss,
            unexplained_loss_reason, pallets_remaining, created_at,
            period_started_at, period_ended_at, period_minutes, shift,
            shift_window_start, line_technician, other_loss_reason, submitted_via,
            hour_start
        )
        VALUES (
            %(production_run_id)s, %(pallets_completed)s, %(planned_downtime)s,
            %(planned_downtime_minutes)s, %(expected_packs)s, %(actual_packs)s,
            %(expected_pallets)s, %(actual_pallets)s, %(production_variance_packs)s,
            %(estimated_lost_packs)s, %(estimated_lost_minutes)s, FALSE,
            NULL, %(pallets_remaining)s, %(recorded_at)s,
            %(period_started_at)s, %(period_ended_at)s, %(period_minutes)s, %(shift)s,
            %(shift_window_start)s, %(line_technician)s, %(other_loss_reason)s, 'react_hmi',
            %(hour_start)s
        )
        RETURNING id;
        """,
        {
            **values,
            "production_run_id": production_run_id,
            "planned_downtime": ", ".join(planned_reasons) or "None",
            "recorded_at": recorded_at,
            "period_started_at": period_start,
            "period_ended_at": period_end,
            "hour_start": hour_start,
            "shift": shift_name,
            "shift_window_start": shift_window.start,
            "line_technician": line_technician,
            "other_loss_reason": other_loss_reason,
        },
    )
    hourly_update_id = cursor.fetchone()["id"]

    cursor.execute(
        """
        UPDATE public.production_runs
        SET
            pallets_remaining = %(pallets_remaining)s,
            total_pallets_completed = %(total_pallets_completed)s,
            potential_overrun_pallets = %(potential_overrun_pallets)s
        WHERE id = %(production_run_id)s
        RETURNING id;
        """,
        {**values, "production_run_id": production_run_id},
    )
    cursor.fetchone()

    for key in ("pallets_remaining", "total_pallets_completed", "potential_overrun_pallets"):
        run[key] = values[key]

    return {
        **values,
        "hourly_update_id": hourly_update_id,
        "loss_review": loss_review,
        "hour_start": hour_start,
        "hour_label": label,
        "period_started_at": period_start,
        "period_ended_at": period_end,
        "shift": shift_name,
        "shift_window_start": shift_window.start,
    }


def review_hourly_loss(production_run_id, hour_start, pallets_produced, now, final=False):
    """Read-only preview. Save recomputes with the same function under the run lock."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"SELECT {_CAPTURE_RUN_COLUMNS} FROM public.production_runs WHERE id=%s", (production_run_id,))
            run = cursor.fetchone()
            if run is None:
                raise PulseCaptureError(404, "Run not found.")
            window = calc.reading_window(hour_start, run["started_at"], now if final else run["finished_at"])
            if final and window is None and calc.to_decimal(pallets_produced) == calc.ZERO:
                return calc.reconciliation_api(calc.reconcile_production(run.get("standard_speed_ppm"),now,now,calc.ZERO))
            if window is None or (not final and window[1] > now):
                raise PulseCaptureError(422, "Choose a completed hour within this run.")
            return _review_period(cursor, run, *window, pallets_produced)


def record_hourly_update(
    production_run_id,
    hour_start,
    pallets_produced,
    line_technician,
    other_loss_reason,
    recorded_at,
    idempotency=None,
):
    """Persists the reading for one named clock hour of a run, and the
    run's progress. The Idempotency-Key replays a retried request; the
    (run, hour) rule refuses a second, different reading for the same
    hour."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency, production_run_id)
            run = _lock_active_run(cursor, connection, production_run_id)
            saved = _insert_hourly_update(
                cursor, connection, run, pallets_produced, line_technician, other_loss_reason,
                recorded_at, hour_start,
            )
            result = {
                **saved,
                "production_run_id": production_run_id,
                "production_line": run["production_line"],
                "config": calc.PackConfig.from_row(run),
            }
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


def start_planned_downtime(production_run_id, reason, started_by, started_at, idempotency=None, component=None):
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency, production_run_id)
            run = _lock_active_run(cursor, connection, production_run_id)
            if component is not None:
                cursor.execute("SELECT linetech_config FROM public.production_lines WHERE name=%s", (run["production_line"],))
                configured = cursor.fetchone()
                config = configured["linetech_config"] if configured else {}
                choices = [p for p in config.get("planned", []) if p.get("active", True) and p["reason"] == reason]
                if not config.get("enabled") or not choices or component not in choices[0].get("components", []):
                    raise PulseCaptureError(422, "Select a configured planned-stop component.")


            cursor.execute(
                """
                SELECT id FROM public.planned_downtime_events
                WHERE production_run_id = %(production_run_id)s AND ended_at IS NULL;
                """,
                {"production_run_id": production_run_id},
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                raise PulseCaptureError(
                    409, "Planned downtime is already in progress for this run."
                )

            cursor.execute(
                """
                INSERT INTO public.planned_downtime_events (
                    production_run_id, production_line, reason, started_by, started_at, component
                )
                VALUES (
                    %(production_run_id)s, %(production_line)s, %(reason)s,
                    %(started_by)s, %(started_at)s, %(component)s
                )
                RETURNING id, production_run_id, production_line, reason, component,
                          started_by, started_at, ended_by, ended_at, duration_minutes;
                """,
                {
                    "production_run_id": production_run_id,
                    "production_line": run["production_line"],
                    "reason": reason, "component": component,
                    "started_by": started_by,
                    "started_at": started_at,
                },
            )
            event = cursor.fetchone()
            _store_idempotent_response(cursor, idempotency, event)

        connection.commit()

    return event


def end_planned_downtime(planned_downtime_event_id, ended_by, ended_at, idempotency=None):
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)

            cursor.execute(
                """
                SELECT id, started_at, ended_at
                FROM public.planned_downtime_events
                WHERE id = %(id)s
                FOR UPDATE;
                """,
                {"id": planned_downtime_event_id},
            )
            event = cursor.fetchone()

            if event is None:
                connection.rollback()
                raise PulseCaptureError(
                    404, f"Planned downtime {planned_downtime_event_id} was not found."
                )

            if event["ended_at"] is not None:
                connection.rollback()
                raise PulseCaptureError(
                    409, f"Planned downtime {planned_downtime_event_id} has already ended."
                )

            if ended_at < event["started_at"]:
                connection.rollback()
                raise PulseCaptureError(
                    409, "Planned downtime cannot end before it started."
                )

            cursor.execute(
                """
                SELECT id FROM public.changeovers
                WHERE planned_downtime_event_id = %(id)s AND status = 'Open';
                """,
                {"id": planned_downtime_event_id},
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                raise PulseCaptureError(
                    409,
                    "This planned stop belongs to a changeover. Use Changeover Complete "
                    "once acceptable packs of the new run are being produced.",
                )

            cursor.execute(
                """
                UPDATE public.planned_downtime_events
                SET ended_at = %(ended_at)s,
                    ended_by = %(ended_by)s,
                    duration_minutes = %(duration_minutes)s
                WHERE id = %(id)s AND ended_at IS NULL
                RETURNING id, production_run_id, production_line, reason, component,
                          started_by, started_at, ended_by, ended_at, duration_minutes;
                """,
                {
                    "id": planned_downtime_event_id,
                    "ended_at": ended_at,
                    "ended_by": ended_by,
                    "duration_minutes": calc.for_storage(
                        calc.duration_minutes(event["started_at"], ended_at)
                    ),
                },
            )
            ended = cursor.fetchone()
            _store_idempotent_response(cursor, idempotency, ended)

        connection.commit()

    return ended


def report_fault_to_engineering(production_run_id, report, opened_at, idempotency=None):
    """Creates an Ongoing / Not Started downtime_events row, the same
    shape the Engineering workflow (Stage 5A) already lists and accepts.
    fault_id is the next per-run sequence number, read under the run's
    row lock so two simultaneous reports can never share one."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency, production_run_id)
            run = _lock_active_run(cursor, connection, production_run_id)

            machine_name = report["machine"]
            reason = report["reason"]
            self_resolved = report.get("outcome") in ("resolved", "resolved_waiting_restart")
            restart = report.get("restored_at") if self_resolved else None
            event_start = report.get("started_at") if self_resolved else opened_at
            if self_resolved and (
                event_start is None or event_start < run["started_at"] or event_start > opened_at
                or (report.get("outcome") == "resolved" and restart is None)
                or (restart is not None and (restart < event_start or restart > opened_at))
            ):
                raise PulseCaptureError(422, "Downtime must be within this run and finish no later than now.")

            if report.get("machine_id") is not None:
                cursor.execute(
                    """
                    SELECT m.id, m.name, m.active, pl.name AS production_line
                    FROM public.machines AS m
                    JOIN public.production_lines AS pl ON pl.id = m.production_line_id
                    WHERE m.id = %(machine_id)s;
                    """,
                    {"machine_id": report["machine_id"]},
                )
                machine = cursor.fetchone()

                if (
                    machine is None
                    or not machine["active"]
                    or machine["production_line"] != run["production_line"]
                ):
                    connection.rollback()
                    raise PulseCaptureError(
                        422, "That machine is not an active machine on this production line."
                    )

                machine_name = machine["name"]

            if report.get("button_id") is not None:
                cursor.execute(
                    """
                    SELECT id, machine_id, name, event_type, active
                    FROM public.buttons
                    WHERE id = %(button_id)s;
                    """,
                    {"button_id": report["button_id"]},
                )
                button = cursor.fetchone()

                if (
                    button is None
                    or not button["active"]
                    or button["event_type"] != "unplanned_fault"
                    or button["machine_id"] != report.get("machine_id")
                ):
                    connection.rollback()
                    raise PulseCaptureError(
                        422, "That fault button does not belong to the selected machine."
                    )

                reason = button["name"]

            cursor.execute(
                """
                SELECT COALESCE(MAX(fault_id), 0) + 1 AS next_fault_id
                FROM public.downtime_events
                WHERE production_run_id = %(production_run_id)s;
                """,
                {"production_run_id": production_run_id},
            )
            fault_id = cursor.fetchone()["next_fault_id"]

            cursor.execute(
                """
                INSERT INTO public.downtime_events (
                    production_run_id, fault_id, machine, machine_id, button_id,
                    reason, reported_by, engineer_called, production_status,
                    engineering_status, engineer, retrospective, opened_at,
                    resolved_at, reported_via, report_note, linetech_resolved_at
                )
                VALUES (
                    %(production_run_id)s, %(fault_id)s, %(machine)s, %(machine_id)s,
                    %(button_id)s, %(reason)s, %(reported_by)s, %(engineer_called)s, %(production_status)s,
                    'Not Started', NULL, %(retrospective)s, %(opened_at)s, %(resolved_at)s, 'react_hmi',
                    %(note)s, %(linetech_resolved_at)s
                )
                RETURNING id, production_run_id, fault_id, machine, reason,
                          reported_by, production_status, engineering_status, opened_at, linetech_resolved_at;
                """,
                {
                    "production_run_id": production_run_id,
                    "fault_id": fault_id,
                    "machine": machine_name,
                    "machine_id": report.get("machine_id"),
                    "button_id": report.get("button_id"),
                    "reason": reason,
                    "reported_by": report["reported_by"],
                    "opened_at": event_start,
                    "resolved_at": restart,
                    "engineer_called": not self_resolved,
                    "production_status": "Resolved" if restart else "Ongoing",
                    "linetech_resolved_at": opened_at if self_resolved else None,
                    "retrospective": self_resolved,
                    "note": report.get("note"),
                },
            )
            created = {**cursor.fetchone(), "production_line": run["production_line"]}
            _store_idempotent_response(cursor, idempotency, created)

        connection.commit()

    return created


def _sum_pallets(cursor, production_run_id, after_hourly_update_id=None):
    cursor.execute(
        """
        SELECT
            COALESCE(SUM(pallets_completed), 0) AS pallets,
            MAX(id) AS last_id
        FROM public.hourly_updates
        WHERE production_run_id = %(production_run_id)s
          AND (%(after_id)s::bigint IS NULL OR id > %(after_id)s::bigint);
        """,
        {"production_run_id": production_run_id, "after_id": after_hourly_update_id},
    )
    row = cursor.fetchone()
    return calc.to_decimal(row["pallets"]), row["last_id"]


def _last_xray_coverage(cursor, production_run_id):
    cursor.execute(
        """
        SELECT MAX(covered_to_hourly_update_id) AS covered_to
        FROM public.production_run_xray_counts
        WHERE production_run_id = %(production_run_id)s;
        """,
        {"production_run_id": production_run_id},
    )
    return cursor.fetchone()["covered_to"]


def get_completion_basis(production_run_id):
    """Read-only inputs for the Complete Run / end-of-shift review
    screen: the run's configuration, every pallet recorded so far, and
    the pallets not yet covered by an earlier X-ray capture."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {_CAPTURE_RUN_COLUMNS} FROM public.production_runs WHERE id = %(id)s;",
                {"id": production_run_id},
            )
            run = cursor.fetchone()
            if run is None:
                return None

            total_pallets, _last_id = _sum_pallets(cursor, production_run_id)
            covered_to = _last_xray_coverage(cursor, production_run_id)
            uncovered_pallets, _last_id = _sum_pallets(cursor, production_run_id, covered_to)

    return {"run": run, "total_pallets": total_pallets, "uncovered_pallets": uncovered_pallets}


def record_xray_capture(production_run_id, capture, captured_at, complete_run, idempotency=None):
    """Records an end-of-shift or run-completion X-ray capture - and,
    when the technician reports production since the last hourly update,
    that final hourly update FIRST - covering every hourly update since
    the run's previous capture. For run completion the run is closed in
    the SAME transaction, so a run can never be Completed without its
    final production and X-ray record, or vice versa."""
    capture_point = "run_completion" if complete_run else "shift_end"

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency, production_run_id)
            run = _lock_active_run(cursor, connection, production_run_id)
            config = calc.PackConfig.from_row(run)
            shift_name = _run_shift_name(run, captured_at)
            shift_window = operational_shift_window(shift_name, captured_at)

            if capture_point == "shift_end":
                cursor.execute(
                    """
                    SELECT id FROM public.production_run_xray_counts
                    WHERE production_run_id = %(production_run_id)s
                      AND capture_point = 'shift_end'
                      AND shift_window_start = %(shift_window_start)s;
                    """,
                    {
                        "production_run_id": production_run_id,
                        "shift_window_start": shift_window.start,
                    },
                )
                if cursor.fetchone() is not None:
                    connection.rollback()
                    raise PulseCaptureError(
                        409, "An end-of-shift X-ray count was already recorded for this shift."
                    )

            if complete_run:
                # End Run stops the product run's output clock; a planned
                # stop left open would keep running with no run behind it.
                cursor.execute(
                    """
                    SELECT reason FROM public.planned_downtime_events
                    WHERE production_run_id = %(production_run_id)s AND ended_at IS NULL;
                    """,
                    {"production_run_id": production_run_id},
                )
                open_stop = cursor.fetchone()
                if open_stop is not None:
                    connection.rollback()
                    raise PulseCaptureError(
                        409, f"End the {open_stop['reason']} planned stop before ending the run."
                    )

            if complete_run and capture.get("final_pallets_produced") is None:
                if captured_at > max(clock_hour_start(captured_at), run["started_at"]):
                    capture = {**capture, "final_pallets_produced": calc.ZERO}
            final_update = None
            if capture.get("final_pallets_produced") is not None:
                if not complete_run:
                    # A mid-run capture must not split a clock hour: that
                    # hour's pallets are reported with Hourly Update.
                    connection.rollback()
                    raise PulseCaptureError(
                        409,
                        "Report production with Hourly Update for each clock hour. An "
                        "end-of-shift X-ray count cannot include pallets.",
                    )
                # The final reading is the current clock hour, up to the
                # moment End Run is confirmed.
                final_update = _insert_hourly_update(
                    cursor,
                    connection,
                    run,
                    capture["final_pallets_produced"],
                    capture["line_technician"],
                    capture.get("other_loss_reason"),
                    captured_at,
                    clock_hour_start(captured_at),
                    final=True,
                )

            previously_covered = _last_xray_coverage(cursor, production_run_id)
            palletised_pallets, last_id = _sum_pallets(cursor, production_run_id, previously_covered)
            palletised_packs = config.pallets_to_packs(palletised_pallets)
            covered_to = last_id if last_id is not None else previously_covered
            total_pallets, _last_id = _sum_pallets(cursor, production_run_id)

            if capture["count_available"]:
                waste = calc.xray_waste(capture["xray_pack_count"], palletised_packs)
            else:
                waste = {
                    "waste_status": "unavailable",
                    "post_xray_pack_difference": None,
                    "estimated_post_xray_waste_percent": None,
                    "warning": None,
                }

            # Completing a run is final and cannot be undone from the HMI,
            # so it must not close on figures that contradict each other
            # (more packs palletised than the X-ray ever counted). The
            # whole transaction rolls back - including the final hourly
            # update inserted above - so the operator goes back, corrects
            # the input and retries with nothing half-written. The
            # contract has no Management override today and one is
            # deliberately not invented here.
            #
            # An end-of-shift capture (complete_run=False) is NOT blocked:
            # it is a mid-run observation, is stored with its warning, and
            # is already excluded from every waste figure because its
            # waste_status is not "estimated".
            if complete_run and waste["waste_status"] == "data_quality_warning":
                connection.rollback()
                raise PulseCaptureError(409, waste["warning"])

            cursor.execute(
                """
                INSERT INTO public.production_run_xray_counts (
                    production_run_id, production_line, capture_point, shift,
                    shift_window_start, line_technician, count_available,
                    xray_pack_count, unavailable_reason, covered_to_hourly_update_id,
                    palletised_pallets, palletised_packs, post_xray_pack_difference,
                    estimated_waste_percent, waste_status, captured_at
                )
                VALUES (
                    %(production_run_id)s, %(production_line)s, %(capture_point)s, %(shift)s,
                    %(shift_window_start)s, %(line_technician)s, %(count_available)s,
                    %(xray_pack_count)s, %(unavailable_reason)s, %(covered_to)s,
                    %(palletised_pallets)s, %(palletised_packs)s, %(difference)s,
                    %(waste_percent)s, %(waste_status)s, %(captured_at)s
                )
                RETURNING id, captured_at;
                """,
                {
                    "production_run_id": production_run_id,
                    "production_line": run["production_line"],
                    "capture_point": capture_point,
                    "shift": shift_name,
                    "shift_window_start": shift_window.start,
                    "line_technician": capture["line_technician"],
                    "count_available": capture["count_available"],
                    "xray_pack_count": capture["xray_pack_count"],
                    "unavailable_reason": capture["unavailable_reason"],
                    "covered_to": covered_to,
                    "palletised_pallets": calc.for_storage(palletised_pallets),
                    "palletised_packs": calc.for_storage(palletised_packs),
                    "difference": calc.for_storage(waste["post_xray_pack_difference"]),
                    "waste_percent": calc.for_storage(waste["estimated_post_xray_waste_percent"]),
                    "waste_status": waste["waste_status"],
                    "captured_at": captured_at,
                },
            )
            saved = cursor.fetchone()

            if complete_run:
                cursor.execute(
                    """
                    UPDATE public.production_runs
                    SET status = 'Completed', finished_at = %(finished_at)s
                    WHERE id = %(production_run_id)s AND status = 'Active'
                    RETURNING id;
                    """,
                    {"production_run_id": production_run_id, "finished_at": captured_at},
                )
                if cursor.fetchone() is None:
                    connection.rollback()
                    raise PulseCaptureError(
                        409, f"Production Run {production_run_id} is already completed."
                    )

            result = {
                "xray_capture_id": saved["id"],
                "captured_at": saved["captured_at"],
                "capture_point": capture_point,
                "production_run_id": production_run_id,
                "production_line": run["production_line"],
                "shift": shift_name,
                "count_available": capture["count_available"],
                "xray_pack_count": capture["xray_pack_count"],
                "unavailable_reason": capture["unavailable_reason"],
                "final_hourly_update": final_update,
                "total_pallets_recorded": total_pallets,
                "palletised_pallets": palletised_pallets,
                "palletised_packs": palletised_packs,
                "pallets_remaining": run["pallets_remaining"],
                "total_pallets_completed": run["total_pallets_completed"],
                "potential_overrun_pallets": run["potential_overrun_pallets"],
                **waste,
            }
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


# ----------------------------------------------------------
# Changeovers (a paired planned stop + QC record)
# ----------------------------------------------------------

_CHANGEOVER_COLUMNS = """
    id, production_line, line_technician, shift, status, started_at,
    completed_at, completed_by, duration_minutes, previous_production_run_id,
    planned_downtime_event_id, previous_customer, previous_product,
    previous_pack_weight_kg, previous_format, new_production_run_id,
    new_customer, new_product, new_pack_weight_kg, new_format, note, workflow
"""

_PLANNED_DOWNTIME_COLUMNS = """
    id, production_run_id, production_line, reason, component, started_by, started_at,
    ended_by, ended_at, duration_minutes
"""


def start_run_changeover(production_run_id, line_technician, new_configuration, note, started_at, idempotency=None):
    """Start Changeover as ONE transaction: the 'Changeover' planned stop
    on the run and the structured changeover record are created
    together, or neither is. The previous configuration is snapshotted
    from the locked run; the new one is what the technician entered."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency, production_run_id)
            run = _lock_active_run(cursor, connection, production_run_id)

            cursor.execute(
                """
                SELECT id FROM public.planned_downtime_events
                WHERE production_run_id = %(production_run_id)s AND ended_at IS NULL;
                """,
                {"production_run_id": production_run_id},
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                raise PulseCaptureError(
                    409,
                    "Planned downtime is already in progress for this run. End it before "
                    "starting a changeover.",
                )

            cursor.execute(
                """
                SELECT id FROM public.changeovers
                WHERE production_line = %(production_line)s AND status = 'Open'
                FOR UPDATE;
                """,
                {"production_line": run["production_line"]},
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                raise PulseCaptureError(
                    409, f"Production Line '{run['production_line']}' already has an open changeover."
                )

            try:
                cursor.execute(
                    f"""
                    INSERT INTO public.planned_downtime_events (
                        production_run_id, production_line, reason, started_by, started_at
                    )
                    VALUES (
                        %(production_run_id)s, %(production_line)s, 'Changeover',
                        %(started_by)s, %(started_at)s
                    )
                    RETURNING {_PLANNED_DOWNTIME_COLUMNS};
                    """,
                    {
                        "production_run_id": production_run_id,
                        "production_line": run["production_line"],
                        "started_by": line_technician,
                        "started_at": started_at,
                    },
                )
                planned = cursor.fetchone()

                cursor.execute(
                    f"""
                    INSERT INTO public.changeovers (
                        production_line, line_technician, shift, status, started_at,
                        previous_production_run_id, planned_downtime_event_id,
                        previous_customer, previous_product, previous_pack_weight_kg,
                        previous_format, new_customer, new_product, new_pack_weight_kg,
                        new_format, note
                    )
                    VALUES (
                        %(production_line)s, %(line_technician)s, %(shift)s, 'Open',
                        %(started_at)s, %(production_run_id)s, %(planned_id)s,
                        %(previous_customer)s, %(previous_product)s,
                        %(previous_pack_weight_kg)s, %(previous_format)s,
                        %(new_customer)s, %(new_product)s, %(new_pack_weight_kg)s,
                        %(new_format)s, %(note)s
                    )
                    RETURNING {_CHANGEOVER_COLUMNS};
                    """,
                    {
                        **new_configuration,
                        "production_line": run["production_line"],
                        "line_technician": line_technician,
                        "shift": _run_shift_name(run, started_at),
                        "started_at": started_at,
                        "production_run_id": production_run_id,
                        "planned_id": planned["id"],
                        "previous_customer": run["customer"],
                        "previous_product": run["product"],
                        "previous_pack_weight_kg": run["pack_weight_kg"],
                        "previous_format": run["format"],
                        "note": note,
                    },
                )
                changeover = cursor.fetchone()

            except psycopg.errors.UniqueViolation:
                connection.rollback()
                raise PulseCaptureError(
                    409, f"Production Line '{run['production_line']}' already has an open changeover."
                )

            result = {"changeover": changeover, "planned_downtime": planned}
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


def complete_run_changeover(changeover_id, completed_by, note, completed_at, idempotency=None):
    """Changeover Complete (first ACCEPTABLE packs of the new run) as ONE
    transaction: the changeover is completed and its paired planned stop
    is ended together, or neither is."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)

            cursor.execute(
                f"""
                SELECT {_CHANGEOVER_COLUMNS}
                FROM public.changeovers
                WHERE id = %(id)s
                FOR UPDATE;
                """,
                {"id": changeover_id},
            )
            changeover = cursor.fetchone()

            if changeover is None:
                connection.rollback()
                raise PulseCaptureError(404, f"Changeover {changeover_id} was not found.")

            if changeover["status"] != "Open":
                connection.rollback()
                raise PulseCaptureError(409, f"Changeover {changeover_id} is already completed.")

            if completed_at < changeover["started_at"]:
                connection.rollback()
                raise PulseCaptureError(409, "A changeover cannot complete before it started.")

            duration = calc.for_storage(calc.duration_minutes(changeover["started_at"], completed_at))

            cursor.execute(
                f"""
                UPDATE public.changeovers
                SET status = 'Completed',
                    completed_at = %(completed_at)s,
                    completed_by = %(completed_by)s,
                    duration_minutes = %(duration_minutes)s,
                    note = COALESCE(%(note)s, note)
                WHERE id = %(id)s AND status = 'Open'
                RETURNING {_CHANGEOVER_COLUMNS};
                """,
                {
                    "id": changeover_id,
                    "completed_at": completed_at,
                    "completed_by": completed_by,
                    "duration_minutes": duration,
                    "note": note,
                },
            )
            completed = cursor.fetchone()

            cursor.execute(
                f"""
                UPDATE public.planned_downtime_events
                SET ended_at = %(ended_at)s,
                    ended_by = %(ended_by)s,
                    duration_minutes = %(duration_minutes)s
                WHERE id = %(id)s AND ended_at IS NULL
                RETURNING {_PLANNED_DOWNTIME_COLUMNS};
                """,
                {
                    "id": changeover["planned_downtime_event_id"],
                    "ended_at": completed_at,
                    "ended_by": completed_by,
                    "duration_minutes": duration,
                },
            )
            planned = cursor.fetchone()

            if planned is None:
                connection.rollback()
                raise PulseCaptureError(
                    409,
                    "This changeover's planned stop was already ended, so the changeover was not "
                    "completed. Ask a manager to review it.",
                )

            result = {"changeover": completed, "planned_downtime": planned}
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


def get_open_changeover(production_line):
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                SELECT {_CHANGEOVER_COLUMNS}
                FROM public.changeovers
                WHERE production_line = %(production_line)s AND status = 'Open';
                """,
                {"production_line": production_line},
            )
            return cursor.fetchone()


# ----------------------------------------------------------
# HMI state (active-run recovery)
# ----------------------------------------------------------


def get_hmi_run_state(production_run_id):
    """Everything the tablet needs to restore a run after a refresh,
    read from the database - never from local fixtures. None if the run
    does not exist."""
    params = {"production_run_id": production_run_id}

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {_CAPTURE_RUN_COLUMNS} FROM public.production_runs WHERE id = %(production_run_id)s;",
                params,
            )
            run = cursor.fetchone()
            if run is None:
                return None

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS hourly_update_count,
                    COALESCE(SUM(pallets_completed), 0) AS pallets_recorded,
                    COALESCE(SUM(expected_packs), 0) AS expected_packs,
                    MAX(COALESCE(period_ended_at, created_at)) AS last_period_ended_at
                FROM public.hourly_updates
                WHERE production_run_id = %(production_run_id)s;
                """,
                params,
            )
            hourly = cursor.fetchone()

            cursor.execute(
                f"""
                SELECT {_PLANNED_DOWNTIME_COLUMNS}
                FROM public.planned_downtime_events
                WHERE production_run_id = %(production_run_id)s
                ORDER BY started_at;
                """,
                params,
            )
            planned = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT {_CHANGEOVER_COLUMNS}
                FROM public.changeovers
                WHERE previous_production_run_id = %(production_run_id)s AND status = 'Open';
                """,
                params,
            )
            open_changeover = cursor.fetchone()

            cursor.execute(
                """
                SELECT opened_at, resolved_at, production_status
                FROM public.downtime_events
                WHERE production_run_id = %(production_run_id)s;
                """,
                params,
            )
            faults = cursor.fetchall()

            speed_changes = _speed_changes(cursor, production_run_id)
            operating_changes = _operating_changes(cursor, production_run_id)

            cursor.execute(
                """
                SELECT hour_start FROM public.hourly_updates
                WHERE production_run_id = %(production_run_id)s AND hour_start IS NOT NULL
                ORDER BY hour_start;
                """,
                params,
            )
            reported_hours = [row["hour_start"] for row in cursor.fetchall()]

            # Faults carried on the LINE from any run, not only this one.
            line_faults = _open_line_faults(cursor, run["production_line"], run["started_at"])

    return {
        "run": run,
        "hourly": hourly,
        "planned": planned,
        "open_changeover": open_changeover,
        "faults": faults,
        "speed_changes": speed_changes,
        "operating_changes": operating_changes,
        "reported_hours": reported_hours,
        "line_faults": line_faults,
    }


# ----------------------------------------------------------
# Production run start (idempotent)
# ----------------------------------------------------------


def create_production_run(run, idempotency=None):
    """Start Run with a server-enforced Idempotency-Key: the key claim,
    the one-active-run-per-line check and the INSERT share one
    transaction, so a double tap or a retry after a timeout returns the
    original run instead of a misleading 'line already active' error."""
    run = dict(run, target_speed_ppm=None)  # Database trigger selects management standard.
    has_format = run.get("format") is not None
    format_column = ", format" if has_format else ""
    format_value = ", %(format)s" if has_format else ""

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)

            cursor.execute(
                """
                SELECT id FROM public.production_runs
                WHERE production_line = %(production_line)s AND status = 'Active'
                FOR UPDATE;
                """,
                {"production_line": run["production_line"]},
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                raise PulseCaptureError(
                    409, f"Production Line '{run['production_line']}' already has an active Production Run."
                )

            handover, open_stoppage = _require_line_ready_for_new_run(
                cursor, connection, run["production_line"]
            )

            try:
                cursor.execute(
                    f"""
                    INSERT INTO public.production_runs (
                        production_line, line_technician, shift, customer, product,
                        pack_weight_kg, packs_per_case, pack_type, target_speed_ppm, standard_speed_ppm,
                        cases_per_pallet, starting_pallets_remaining, pallets_remaining,
                        previous_run_completed{format_column}
                    )
                    VALUES (
                        %(production_line)s, %(line_technician)s, %(shift)s, %(customer)s,
                        %(product)s, %(pack_weight_kg)s, %(packs_per_case)s, %(pack_type)s,
                        %(target_speed_ppm)s, %(target_speed_ppm)s, %(cases_per_pallet)s,
                        %(starting_pallets_remaining)s, %(pallets_remaining)s,
                        %(previous_run_completed)s{format_value}
                    )
                    RETURNING id, started_at, target_speed_ppm, standard_speed_ppm, standard_version_id;
                    """,
                    run,
                )
            except psycopg.errors.CheckViolation as error:
                connection.rollback()
                if error.diag.constraint_name == 'management_standard_required':
                    raise PulseCaptureError(409, 'Management must configure a standard for this line, product and pack configuration before starting a run.') from error
                if error.diag.constraint_name in ('linetech_ready_before_run', 'linetech_configuration_match'):
                    raise PulseCaptureError(409, str(error.diag.message_primary)) from error
                if error.diag.constraint_name == 'casepacker_ready_before_run':
                    raise PulseCaptureError(409, 'Engineering must mark the casepacker ready before starting a run.') from error
                raise
            except psycopg.errors.UniqueViolation:
                # idx_unique_active_run_per_line: a different request
                # started a run on this line at the same moment.
                connection.rollback()
                raise PulseCaptureError(
                    409, f"Production Line '{run['production_line']}' already has an active Production Run."
                )

            inserted = cursor.fetchone()
            result = {**run, **inserted, "run_id": inserted["id"]}
            if open_stoppage is not None:
                # The new run's clock starts the moment the Handover /
                # Changeover stops - the same instant, so no minute is
                # counted twice or lost between them.
                _close_stoppage_at_run_start(cursor, open_stoppage, run, inserted)
            if handover is not None:
                _link_stoppages_to_new_run(cursor, run, result["run_id"], handover["id"])
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


# ----------------------------------------------------------
# Handover: line stoppages and carried faults
# ----------------------------------------------------------
#
# After End Run the line has no product run. The technician chooses End
# Shift (nothing more), Changeover or Other. Changeover / Other are
# line_stoppages: they belong to the LINE, lower its hourly result and
# are never charged to a product run. A new run cannot start while one
# is open, nor while any fault still open on the line has not been
# acknowledged (and escalated) since the last run ended.

_LINE_STOPPAGE_COLUMNS = """
    id, production_line, kind, reason, previous_production_run_id,
    next_production_run_id, follows_stoppage_id, started_by, started_at,
    physical_ended_by, physical_ended_at, ended_by, ended_at, duration_minutes
"""

_STOPPAGE_LABEL = {
    "changeover": "changeover", "other": "line stop", "handover": "handover",
    "restart_delay": "restart delay", "not_scheduled": "not scheduled time",
}

# Kinds the next run's Start Run ends (a Changeover only once its
# physical work is done - see _closes_at_run_start).
_ENDED_BY_NEXT_RUN = ("handover", "restart_delay", "not_scheduled")


def _closes_at_run_start(stoppage):
    """Stops that the next run's Start Run ends: a Handover, a Restart
    delay (after an Other stop is resolved), Not scheduled time, and a
    Changeover whose physical work is done (the new-run setup part)."""
    return stoppage["kind"] in _ENDED_BY_NEXT_RUN or (
        stoppage["kind"] == "changeover" and stoppage["physical_ended_at"] is not None
    )


def _next_step_state(cursor, run_id):
    """What is known about what happened after this ended run:
      'legacy'   - it ended before migration 0004 (legacy baseline): no
                   choice is required and none is fabricated;
      'recorded' - a line stop records the choice;
      None       - nobody has chosen yet."""
    cursor.execute(
        """
        SELECT CASE
            WHEN EXISTS (SELECT 1 FROM public.run_next_step_legacy_baseline
                         WHERE production_run_id = %(run_id)s) THEN 'legacy'
            WHEN EXISTS (SELECT 1 FROM public.line_stoppages
                         WHERE previous_production_run_id = %(run_id)s) THEN 'recorded'
        END AS state;
        """,
        {"run_id": run_id},
    )
    row = cursor.fetchone()
    return row["state"] if row else None


def _close_stoppage_at_run_start(cursor, stoppage, run, inserted):
    """Ends the Handover / Changeover at the instant the new run starts,
    recording the incoming technician. A changeover's QA record is
    completed with the TOTAL time (physical work + new-run setup)."""
    ended_at = inserted["started_at"]
    duration = calc.for_storage(calc.duration_minutes(stoppage["started_at"], ended_at))
    cursor.execute(
        """
        UPDATE public.line_stoppages
        SET ended_at = %(ended_at)s, ended_by = %(ended_by)s,
            duration_minutes = %(duration)s, next_production_run_id = %(run_id)s
        WHERE id = %(id)s AND ended_at IS NULL
        RETURNING id;
        """,
        {"id": stoppage["id"], "ended_at": ended_at, "ended_by": run["line_technician"],
         "duration": duration, "run_id": inserted["id"]},
    )
    cursor.fetchone()
    if stoppage["kind"] == "changeover":
        cursor.execute(
            """
            UPDATE public.changeovers
            SET status = 'Completed', completed_at = %(ended_at)s, completed_by = %(ended_by)s,
                duration_minutes = %(duration)s, new_production_run_id = %(run_id)s,
                new_customer = %(customer)s, new_product = %(product)s,
                new_pack_weight_kg = %(pack_weight_kg)s, new_format = %(format)s
            WHERE line_stoppage_id = %(id)s AND status = 'Open';
            """,
            {"id": stoppage["id"], "ended_at": ended_at, "ended_by": run["line_technician"],
             "duration": duration, "run_id": inserted["id"], "customer": run["customer"],
             "product": run["product"], "pack_weight_kg": run["pack_weight_kg"], "format": run.get("format")},
        )


def _latest_finished_run(cursor, production_line):
    """The run that most recently ended on the line - the handover point."""
    cursor.execute(
        """
        SELECT id, finished_at, target_speed_ppm, line_technician, customer, product,
               pack_weight_kg, COALESCE(format, pack_type) AS format
        FROM public.production_runs
        WHERE production_line = %(production_line)s
          AND status <> 'Active'
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC
        LIMIT 1;
        """,
        {"production_line": production_line},
    )
    return cursor.fetchone()


def _open_line_faults(cursor, production_line, acknowledged_since):
    """Every fault still open on the line, from ANY run - faults persist
    across shifts and changeovers until Engineering closes them.
    `acknowledged` is True when an acknowledgement was recorded at or
    after `acknowledged_since` (the handover point)."""
    cursor.execute(
        """
        SELECT
            de.id AS downtime_event_id, de.production_run_id, de.fault_id, de.machine,
            de.reason, de.reported_by, de.engineer, de.engineering_status, de.opened_at,
            COALESCE(de.escalation_count, 0) AS escalation_count,
            de.last_escalated_at, de.last_escalated_by,
            EXISTS (
                SELECT 1 FROM public.fault_acknowledgements AS fa
                WHERE fa.downtime_event_id = de.id
                  AND (CAST(%(since)s AS timestamptz) IS NULL
                       OR fa.acknowledged_at >= CAST(%(since)s AS timestamptz))
            ) AS acknowledged
        FROM public.downtime_events AS de
        JOIN public.production_runs AS pr ON pr.id = de.production_run_id
        WHERE pr.production_line = %(production_line)s
          AND de.production_status = 'Ongoing'
        ORDER BY de.opened_at;
        """,
        {"production_line": production_line, "since": acknowledged_since},
    )
    return cursor.fetchall()


def _require_line_ready_for_new_run(cursor, connection, production_line):
    """Refuses a new run while a line stoppage is still open, or while a
    fault carried over from an earlier run has not been acknowledged and
    escalated by the incoming technician. Returns the handover run."""
    cursor.execute(
        f"""
        SELECT {_LINE_STOPPAGE_COLUMNS},
               EXISTS (SELECT 1 FROM public.casepacker_requests q
                       WHERE q.line_stoppage_id = line_stoppages.id AND q.ready_at IS NULL AND q.cancelled_at IS NULL) AS casepacker_pending
        FROM public.line_stoppages
        WHERE production_line = %(production_line)s AND ended_at IS NULL
        FOR UPDATE;
        """,
        {"production_line": production_line},
    )
    open_stoppage = cursor.fetchone()
    if open_stoppage is not None and open_stoppage.get("casepacker_pending"):
        raise PulseCaptureError(409, "Engineering must mark the casepacker ready before starting a run.")
    if open_stoppage is not None and not _closes_at_run_start(open_stoppage):
        connection.rollback()
        if open_stoppage["kind"] == "changeover":
            message = f"Press End Changeover on {production_line} when the changeover work is done."
        else:
            message = f"Resolve the line stop on {production_line} before starting a new run."
        raise PulseCaptureError(409, message)

    handover = _latest_finished_run(cursor, production_line)
    if handover is not None and open_stoppage is None and _next_step_state(cursor, handover["id"]) is None:
        # End Run was confirmed but nobody said what happened next. The
        # gap would otherwise be silently absorbed by the new run.
        connection.rollback()
        raise PulseCaptureError(
            409,
            f"Choose what happened after the last run on {production_line} "
            "(End Shift, Changeover or Other) before starting a new run.",
        )
    faults = _open_line_faults(cursor, production_line, handover["finished_at"] if handover else None)
    waiting = [fault for fault in faults if not fault["acknowledged"]]
    if waiting:
        connection.rollback()
        noun = "fault" if len(waiting) == 1 else "faults"
        raise PulseCaptureError(
            409,
            f"Acknowledge and escalate the {len(waiting)} open {noun} on {production_line} "
            "before starting a run.",
        )
    return handover, open_stoppage


def _link_stoppages_to_new_run(cursor, run, new_run_id, previous_run_id):
    """The run that follows a Changeover / Other closes the handover:
    the stoppage records which run came next, and a changeover's QA
    record takes the new product's details from the confirmed run."""
    cursor.execute(
        """
        UPDATE public.line_stoppages
        SET next_production_run_id = %(new_run_id)s
        WHERE production_line = %(production_line)s
          AND previous_production_run_id = %(previous_run_id)s
          AND next_production_run_id IS NULL
          AND ended_at IS NOT NULL
        RETURNING id, kind;
        """,
        {
            "new_run_id": new_run_id,
            "production_line": run["production_line"],
            "previous_run_id": previous_run_id,
        },
    )
    changeover_ids = [row["id"] for row in (cursor.fetchall() or []) if row["kind"] == "changeover"]
    if changeover_ids:
        cursor.execute(
            """
            UPDATE public.changeovers
            SET new_production_run_id = %(new_run_id)s,
                new_customer = %(customer)s,
                new_product = %(product)s,
                new_pack_weight_kg = %(pack_weight_kg)s,
                new_format = %(format)s
            WHERE line_stoppage_id = ANY(%(stoppage_ids)s);
            """,
            {
                "new_run_id": new_run_id,
                "customer": run["customer"],
                "product": run["product"],
                "pack_weight_kg": run["pack_weight_kg"],
                "format": run.get("format"),
                "stoppage_ids": changeover_ids,
            },
        )


def start_line_stoppage(production_line, kind, reason, started_by, started_at, casepacker_details=None, idempotency=None, changeover_selection=None):
    """End Shift (Handover), Changeover, Other or Not scheduled, chosen after End Run -
    by the technician on the HMI, or later by a manager. Only while the
    line has no active run; at most one open per line; ONE choice per
    ended run, so a second tap or a different choice cannot add another.

    The event starts when the previous run ENDED, not when the choice is
    tapped: the time between them is part of what happened next, so no
    minute of the gap is lost however late the choice is made. With no
    earlier run on the line it starts at `started_at`.

    A Changeover also opens its QA changeover record, with the previous
    product snapshotted from the run that ended and the new product
    filled in when the next run is confirmed."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)

            cursor.execute(
                """
                SELECT id FROM public.production_runs
                WHERE production_line = %(production_line)s AND status = 'Active'
                FOR UPDATE;
                """,
                {"production_line": production_line},
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                raise PulseCaptureError(409, f"End the run on {production_line} first.")

            cursor.execute(
                """
                SELECT id FROM public.line_stoppages
                WHERE production_line = %(production_line)s AND ended_at IS NULL
                FOR UPDATE;
                """,
                {"production_line": production_line},
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                raise PulseCaptureError(409, f"A stop is already running on {production_line}.")

            previous = _latest_finished_run(cursor, production_line)
            if previous is not None:
                state = _next_step_state(cursor, previous["id"])
                if state == "legacy":
                    connection.rollback()
                    raise PulseCaptureError(
                        409,
                        f"The last run on {production_line} ended before the End Run next-step "
                        "workflow started. Start a new run when the line is ready.",
                    )
                if state == "recorded":
                    connection.rollback()
                    raise PulseCaptureError(
                        409, f"What happened after the last run on {production_line} has already been recorded."
                    )
                started_at = previous["finished_at"]

            try:
                cursor.execute(
                    f"""
                    INSERT INTO public.line_stoppages (
                        production_line, kind, reason, previous_production_run_id,
                        started_by, started_at
                    )
                    VALUES (
                        %(production_line)s, %(kind)s, %(reason)s, %(previous_run_id)s,
                        %(started_by)s, %(started_at)s
                    )
                    RETURNING {_LINE_STOPPAGE_COLUMNS};
                    """,
                    {
                        "production_line": production_line,
                        "kind": kind,
                        "reason": reason,
                        "previous_run_id": previous["id"] if previous else None,
                        "started_by": started_by,
                        "started_at": started_at,
                    },
                )
                stoppage = cursor.fetchone()

                changeover = None
                if kind == "changeover":
                    if previous is None:
                        connection.rollback()
                        raise PulseCaptureError(
                            409, f"There is no ended run on {production_line} to change over from."
                        )
                    cursor.execute(
                        f"""
                        INSERT INTO public.changeovers (
                            production_line, line_technician, shift, status, started_at,
                            previous_production_run_id, line_stoppage_id, previous_customer,
                            previous_product, previous_pack_weight_kg, previous_format
                        )
                        VALUES (
                            %(production_line)s, %(started_by)s, %(shift)s, 'Open', %(started_at)s,
                            %(previous_run_id)s, %(stoppage_id)s, %(customer)s, %(product)s,
                            %(pack_weight_kg)s, %(format)s
                        )
                        RETURNING {_CHANGEOVER_COLUMNS};
                        """,
                        {
                            "production_line": production_line,
                            "started_by": started_by,
                            "shift": shift_name_at(started_at),
                            "started_at": started_at,
                            "previous_run_id": previous["id"],
                            "stoppage_id": stoppage["id"],
                            "customer": previous["customer"],
                            "product": previous["product"],
                            "pack_weight_kg": previous["pack_weight_kg"],
                            "format": previous["format"],
                        },
                    )
                    changeover = cursor.fetchone()
                    if changeover_selection:
                        from .linetech import prepare_changeover
                        cursor.execute("SELECT linetech_config FROM public.production_lines WHERE name=%s", (production_line,))
                        config_row = cursor.fetchone()
                        try:
                            workflow = prepare_changeover(config_row["linetech_config"] if config_row else {}, changeover_selection, previous)
                        except ValueError as error:
                            raise PulseCaptureError(422, str(error)) from error
                        cursor.execute("UPDATE public.changeovers SET workflow=%s WHERE id=%s", (Json(workflow), changeover["id"]))
                        changeover["workflow"] = workflow
                        casepacker_details = (f"{workflow['kind'].title()} changeover: {workflow['previous_value']} -> {workflow['next_value']}"
                                              if workflow["engineering_required"] else None)
                    if casepacker_details:
                        cursor.execute(
                            """INSERT INTO public.casepacker_requests
                               (line_stoppage_id, details, requested_by)
                               VALUES (%s, %s, %s);""",
                            (stoppage["id"], casepacker_details, started_by),
                        )

            except psycopg.errors.UniqueViolation:
                # uq_line_stoppages_one_open_per_line or ..._one_next_step_per_run:
                # another tablet made the choice at the same moment.
                connection.rollback()
                raise PulseCaptureError(
                    409, f"What happened after the last run on {production_line} has already been recorded."
                )

            result = {"stoppage": stoppage, "changeover": changeover}
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


def end_line_stoppage(stoppage_id, ended_by, ended_at, idempotency=None):
    """End Changeover / Resolve.

    - other:      Resolve - the stop ends now and a Restart delay starts
                  at the same instant, in the same transaction; it ends
                  when the next run is confirmed. A repeated Resolve
                  returns the stop and its Restart delay unchanged.
    - changeover: End Changeover - marks the PHYSICAL work complete; the
                  event keeps running through the new-run form and ends
                  when the new run starts (create_production_run).
                  Pressing it again changes nothing and returns the same
                  state, so a repeated tap cannot move the time.
    - handover / restart_delay / not_scheduled: ended only by the next
                  run's Start Run."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)

            cursor.execute(
                f"SELECT {_LINE_STOPPAGE_COLUMNS} FROM public.line_stoppages WHERE id = %(id)s FOR UPDATE;",
                {"id": stoppage_id},
            )
            stoppage = cursor.fetchone()
            if stoppage is None:
                connection.rollback()
                raise PulseCaptureError(404, f"Line stop {stoppage_id} was not found.")
            if stoppage["ended_at"] is not None:
                if stoppage["kind"] == "other":
                    # A second Resolve tap: nothing moves, nothing is added.
                    delay = _restart_delay_after(cursor, stoppage)
                    connection.rollback()
                    return {"stoppage": stoppage, "changeover": None, "restart_delay": delay}
                connection.rollback()
                raise PulseCaptureError(409, "This stop has already ended.")
            if ended_at < stoppage["started_at"]:
                connection.rollback()
                raise PulseCaptureError(409, "A stop cannot end before it started.")
            if stoppage["kind"] == "handover":
                connection.rollback()
                raise PulseCaptureError(
                    409, "A handover ends when the incoming technician starts a run."
                )
            if stoppage["kind"] == "restart_delay":
                connection.rollback()
                raise PulseCaptureError(409, "A restart delay ends when the next run is confirmed.")
            if stoppage["kind"] == "not_scheduled":
                connection.rollback()
                raise PulseCaptureError(409, "Not scheduled time ends when the next run is confirmed.")

            if stoppage["kind"] == "changeover":
                if stoppage["physical_ended_at"] is None:
                    cursor.execute(
                        f"""
                        UPDATE public.line_stoppages
                        SET physical_ended_at = %(at)s, physical_ended_by = %(by)s
                        WHERE id = %(id)s AND physical_ended_at IS NULL AND ended_at IS NULL
                        RETURNING {_LINE_STOPPAGE_COLUMNS};
                        """,
                        {"id": stoppage_id, "at": ended_at, "by": ended_by},
                    )
                    stoppage = cursor.fetchone()
                result = {"stoppage": stoppage, "changeover": None}
                _store_idempotent_response(cursor, idempotency, result)
                connection.commit()
                return result

            duration = calc.for_storage(calc.duration_minutes(stoppage["started_at"], ended_at))
            cursor.execute(
                f"""
                UPDATE public.line_stoppages
                SET ended_at = %(ended_at)s, ended_by = %(ended_by)s,
                    duration_minutes = %(duration_minutes)s
                WHERE id = %(id)s AND ended_at IS NULL
                RETURNING {_LINE_STOPPAGE_COLUMNS};
                """,
                {"id": stoppage_id, "ended_at": ended_at, "ended_by": ended_by, "duration_minutes": duration},
            )
            ended = cursor.fetchone()

            # The cause is fixed, but the line is not running yet: the
            # time until the next run starts is its own unplanned reason.
            cursor.execute(
                f"""
                INSERT INTO public.line_stoppages (
                    production_line, kind, reason, previous_production_run_id,
                    follows_stoppage_id, started_by, started_at
                )
                VALUES (
                    %(production_line)s, 'restart_delay', %(reason)s, %(previous_run_id)s,
                    %(follows_id)s, %(started_by)s, %(started_at)s
                )
                RETURNING {_LINE_STOPPAGE_COLUMNS};
                """,
                {
                    "production_line": ended["production_line"],
                    "reason": ended["reason"],
                    "previous_run_id": ended["previous_production_run_id"],
                    "follows_id": ended["id"],
                    "started_by": ended_by,
                    "started_at": ended_at,
                },
            )
            delay = cursor.fetchone()

            result = {"stoppage": ended, "changeover": None, "restart_delay": delay}
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


def _restart_delay_after(cursor, other):
    """The stop that began when this Other stop was resolved (its Restart
    delay - possibly reclassified since, e.g. to Not scheduled)."""
    cursor.execute(
        f"""
        SELECT {_LINE_STOPPAGE_COLUMNS} FROM public.line_stoppages
        WHERE follows_stoppage_id = %(id)s
        LIMIT 1;
        """,
        {"id": other["id"]},
    )
    return cursor.fetchone()


# ----------------------------------------------------------
# Manager corrections: reclassify a line stop, with an audit trail
# ----------------------------------------------------------
#
# A late next-step decision can be wrong (e.g. recorded as Other when the
# line was simply not scheduled). A manager may reclassify it. The stop
# stays ONE interval - only its kind/reason change - so every report is
# recalculated from the corrected row with nothing counted twice. Each
# change is recorded in line_stoppage_reclassifications: previous and new
# classification, who, when and why.
#
# Allowed:
#   handover / other / not_scheduled   <->  each other (other needs a reason)
#   a Restart delay (the stop that follows a resolved Other)
#       <-> not_scheduled / other / back to restart_delay
#   an Other that already has a Restart delay: its reason only - correct
#       the Restart delay itself instead.
#   Changeovers are not reclassified here (they carry a QA record and a
#       physical / setup split).

def reclassify_line_stoppage(stoppage_id, new_kind, new_reason, changed_by, note, changed_at, idempotency=None):
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)

            cursor.execute(
                f"SELECT {_LINE_STOPPAGE_COLUMNS} FROM public.line_stoppages WHERE id = %(id)s FOR UPDATE;",
                {"id": stoppage_id},
            )
            stoppage = cursor.fetchone()
            if stoppage is None:
                connection.rollback()
                raise PulseCaptureError(404, f"Line stop {stoppage_id} was not found.")

            has_follower = _restart_delay_after(cursor, stoppage) is not None
            allowed = calc.allowed_line_stop_reclassifications(
                stoppage["kind"], stoppage["follows_stoppage_id"], has_follower
            )
            if new_kind not in allowed:
                connection.rollback()
                if stoppage["kind"] == "changeover":
                    message = "A changeover cannot be reclassified here."
                elif stoppage["kind"] == "other" and has_follower:
                    message = (
                        "This Other stop was resolved and has a Restart delay after it. "
                        "Correct its reason here, or reclassify the Restart delay."
                    )
                else:
                    label = _STOPPAGE_LABEL.get(stoppage["kind"], stoppage["kind"])
                    message = f"A {label} cannot be reclassified as {_STOPPAGE_LABEL.get(new_kind, new_kind)}."
                raise PulseCaptureError(409, message)

            reason = new_reason if new_kind in ("other", "restart_delay", "not_scheduled") else None
            if new_kind == "restart_delay":
                # Back to what it was: it carries the reason of the Other it follows.
                cursor.execute(
                    "SELECT reason FROM public.line_stoppages WHERE id = %(id)s;",
                    {"id": stoppage["follows_stoppage_id"]},
                )
                followed = cursor.fetchone()
                reason = followed["reason"] if followed else stoppage["reason"]
            if new_kind == stoppage["kind"] and (reason or None) == (stoppage["reason"] or None):
                connection.rollback()
                raise PulseCaptureError(409, "That is already how this stop is classified.")

            cursor.execute(
                f"""
                UPDATE public.line_stoppages
                SET kind = %(kind)s, reason = %(reason)s
                WHERE id = %(id)s
                RETURNING {_LINE_STOPPAGE_COLUMNS};
                """,
                {"id": stoppage_id, "kind": new_kind, "reason": reason},
            )
            updated = cursor.fetchone()

            cursor.execute(
                """
                INSERT INTO public.line_stoppage_reclassifications (
                    line_stoppage_id, previous_kind, previous_reason, new_kind, new_reason,
                    changed_by, changed_at, note
                )
                VALUES (
                    %(stoppage_id)s, %(previous_kind)s, %(previous_reason)s, %(new_kind)s,
                    %(new_reason)s, %(changed_by)s, %(changed_at)s, %(note)s
                )
                RETURNING id, line_stoppage_id, previous_kind, previous_reason, new_kind, new_reason,
                          changed_by, changed_at, note;
                """,
                {
                    "stoppage_id": stoppage_id,
                    "previous_kind": stoppage["kind"],
                    "previous_reason": stoppage["reason"],
                    "new_kind": new_kind,
                    "new_reason": reason,
                    "changed_by": changed_by,
                    "changed_at": changed_at,
                    "note": note,
                },
            )
            audit = cursor.fetchone()

            result = {"stoppage": updated, "changeover": None, "reclassification": audit}
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


def get_line_stop_log(since, production_line=None):
    """Line stops between runs that started since `since` or are still
    open, newest first, each with its reclassification history. Read-only."""
    line_filter = "AND ls.production_line = %(production_line)s" if production_line else ""
    params = {"since": since, "production_line": production_line}
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                SELECT ls.id, ls.production_line, ls.kind, ls.reason, ls.previous_production_run_id,
                       ls.next_production_run_id, ls.follows_stoppage_id, ls.started_by, ls.started_at,
                       ls.physical_ended_by, ls.physical_ended_at, ls.ended_by, ls.ended_at,
                       ls.duration_minutes,
                       EXISTS (SELECT 1 FROM public.line_stoppages AS f
                               WHERE f.follows_stoppage_id = ls.id) AS has_follower
                FROM public.line_stoppages AS ls
                WHERE (ls.started_at >= %(since)s OR ls.ended_at IS NULL OR ls.ended_at >= %(since)s)
                  AND ls.production_line NOT ILIKE 'TEST-%%'
                  {line_filter}
                ORDER BY ls.started_at DESC
                LIMIT 200;
                """,
                params,
            )
            stops = cursor.fetchall()
            ids = [stop["id"] for stop in stops]
            history = []
            if ids:
                cursor.execute(
                    """
                    SELECT id, line_stoppage_id, previous_kind, previous_reason, new_kind, new_reason,
                           changed_by, changed_at, note
                    FROM public.line_stoppage_reclassifications
                    WHERE line_stoppage_id = ANY(%(ids)s)
                    ORDER BY changed_at, id;
                    """,
                    {"ids": ids},
                )
                history = cursor.fetchall()
    return {"stops": stops, "reclassifications": history}


def get_open_line_faults(production_line):
    """Faults still open on the line and whether each has been
    acknowledged since the last run ended. Read-only."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            handover = _latest_finished_run(cursor, production_line)
            since = handover["finished_at"] if handover else None
            return {"handover_at": since, "faults": _open_line_faults(cursor, production_line, since)}


def restore_fault_production(downtime_event_id, production_line, technician, note, restored_at, recorded_at, idempotency=None):
    """Stop production downtime without closing Engineering. Replay-safe and audited."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)
            cursor.execute(
                """SELECT de.id, de.opened_at, de.resolved_at, de.production_status,
                          de.engineering_status, pr.production_line
                   FROM public.downtime_events de
                   JOIN public.production_runs pr ON pr.id = de.production_run_id
                   WHERE de.id = %(id)s FOR UPDATE OF de;""", {"id": downtime_event_id},
            )
            fault = cursor.fetchone()
            if fault is None or fault["production_line"] != production_line:
                raise PulseCaptureError(404, "Fault not found on this line.")
            if fault["resolved_at"] is not None or fault["production_status"] == "Resolved":
                raise PulseCaptureError(409, "Production restart has already been recorded.")
            if restored_at < fault["opened_at"] or restored_at > recorded_at:
                raise PulseCaptureError(422, "Restart time must be between the fault opening and now.")
            cursor.execute(
                """UPDATE public.downtime_events
                   SET production_status = 'Resolved', resolved_at = %(at)s
                   WHERE id = %(id)s
                   RETURNING id AS downtime_event_id, production_status, engineering_status, resolved_at;""",
                {"id": downtime_event_id, "at": restored_at},
            )
            result = cursor.fetchone()
            cursor.execute(
                """INSERT INTO public.management_audit_log
                   (action, manager_name, record_type, record_id, previous_value, new_value, reason)
                   VALUES ('hmi_production_restored', %(by)s, 'downtime_event', %(id)s,
                           %(previous)s, %(new)s, %(note)s);""",
                {"by": technician, "id": str(downtime_event_id), "note": note,
                 "previous": Json({"production_status": fault["production_status"], "resolved_at": None}),
                 "new": Json({"production_status": "Resolved", "resolved_at": restored_at.isoformat(),
                              "recorded_at": recorded_at.isoformat(), "actor_role": "line_technician"})},
            )
            _store_idempotent_response(cursor, idempotency, result)
        connection.commit()
    return result


def acknowledge_line_fault(downtime_event_id, production_line, acknowledged_by, note, acknowledged_at, idempotency=None):
    """The incoming technician acknowledges a fault still open on the
    line and escalates it. Escalation UPDATES the existing fault
    (count, time, who) - it never creates another fault."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)

            cursor.execute(
                """
                SELECT de.id, de.production_status, pr.production_line
                FROM public.downtime_events AS de
                JOIN public.production_runs AS pr ON pr.id = de.production_run_id
                WHERE de.id = %(id)s
                FOR UPDATE OF de;
                """,
                {"id": downtime_event_id},
            )
            fault = cursor.fetchone()
            if fault is None or fault["production_line"] != production_line:
                connection.rollback()
                raise PulseCaptureError(404, f"Fault {downtime_event_id} is not on {production_line}.")
            if fault["production_status"] != "Ongoing":
                connection.rollback()
                raise PulseCaptureError(409, "This fault has already been closed by Engineering.")

            cursor.execute(
                """
                INSERT INTO public.fault_acknowledgements (
                    downtime_event_id, production_line, acknowledged_by, acknowledged_at,
                    escalated, note
                )
                VALUES (%(id)s, %(production_line)s, %(by)s, %(at)s, TRUE, %(note)s)
                RETURNING id, acknowledged_at;
                """,
                {"id": downtime_event_id, "production_line": production_line, "by": acknowledged_by,
                 "at": acknowledged_at, "note": note},
            )
            acknowledgement = cursor.fetchone()

            cursor.execute(
                """
                UPDATE public.downtime_events
                SET escalation_count = COALESCE(escalation_count, 0) + 1,
                    last_escalated_at = %(at)s,
                    last_escalated_by = %(by)s
                WHERE id = %(id)s
                RETURNING id AS downtime_event_id, escalation_count, last_escalated_at, last_escalated_by;
                """,
                {"id": downtime_event_id, "at": acknowledged_at, "by": acknowledged_by},
            )
            escalated = cursor.fetchone()

            result = {**escalated, "acknowledgement_id": acknowledgement["id"],
                      "acknowledged_at": acknowledgement["acknowledged_at"]}
            _store_idempotent_response(cursor, idempotency, result)

        connection.commit()

    return result


# ----------------------------------------------------------
# Target speed changes
# ----------------------------------------------------------


def record_target_speed_change(production_run_id, new_speed_ppm, reason, changed_by, effective_at, idempotency=None):
    """Retired endpoint: never silently reinterpret old client requests."""
    raise PulseCaptureError(409, "Target changes are retired. Reload the tablet and record an operating speed; the standard stays fixed.")


def record_operating_speed_change(production_run_id, new_speed_ppm, reason, changed_by,
                                  effective_at, submitted_at, supersedes_id=None, idempotency=None):
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency, production_run_id)
            cursor.execute(f"SELECT {_CAPTURE_RUN_COLUMNS} FROM public.production_runs WHERE id=%s FOR UPDATE", (production_run_id,))
            run = cursor.fetchone()
            if run is None:
                raise PulseCaptureError(404, "Run not found.")
            effective = effective_at or submitted_at
            if not run["started_at"] <= effective <= min(run["finished_at"] or submitted_at, submitted_at):
                raise PulseCaptureError(422, "Effective time must be within this run and not in the future.")
            changes = _operating_changes(cursor, production_run_id)
            if supersedes_id is not None:
                if not any(r["id"] == supersedes_id for r in changes) or any(r["supersedes_id"] == supersedes_id for r in changes):
                    raise PulseCaptureError(409, "That setting has already been corrected or belongs to another run.")
            # Previous = best known setting immediately before this effective instant.
            active = [r for r in changes if r["id"] != supersedes_id]
            previous = calc.speed_at(calc.operating_timeline(active), effective)
            cursor.execute("""INSERT INTO public.run_operating_speed_changes
                (production_run_id, previous_speed_ppm, new_speed_ppm, reason, changed_by,
                 effective_at, submitted_at, supersedes_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (production_run_id, previous, new_speed_ppm, reason, changed_by, effective, submitted_at, supersedes_id))
            result = {**cursor.fetchone(), "production_line": run["production_line"]}
            _store_idempotent_response(cursor, idempotency, result)
        connection.commit()
    return result


# ----------------------------------------------------------
# Hour readings for the HMI, and the hourly report
# ----------------------------------------------------------


def get_run_hour_readings(production_run_id):
    """The run and its fixed-hour readings, for the HMI's list of hours
    to report. None if the run does not exist."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {_CAPTURE_RUN_COLUMNS} FROM public.production_runs WHERE id = %(id)s;",
                {"id": production_run_id},
            )
            run = cursor.fetchone()
            if run is None:
                return None
            cursor.execute(
                """
                SELECT hour_start, pallets_completed, period_started_at, period_ended_at
                FROM public.hourly_updates
                WHERE production_run_id = %(id)s AND hour_start IS NOT NULL
                ORDER BY hour_start;
                """,
                {"id": production_run_id},
            )
            readings = cursor.fetchall()
            return {"run": run, "readings": readings}


def get_hourly_report_data(window_start, window_end, production_line=None, *, connection=None):
    """Everything the fixed-hour Production view needs for one shift,
    in one connection: runs open in the window with their speed changes
    and readings, their planned stops, every fault on the lines (by
    line, not by run - a fault persists across runs) and the line
    stoppages. Test data is excluded like every dashboard read."""
    line_filter = "AND pr.production_line = %(production_line)s" if production_line else ""
    stop_line_filter = "AND ls.production_line = %(production_line)s" if production_line else ""
    params = {"window_start": window_start, "window_end": window_end, "production_line": production_line}

    with (nullcontext(connection) if connection is not None else get_database_connection()) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT name FROM public.production_lines WHERE active = TRUE ORDER BY display_order, name;"
            )
            lines = [row["name"] for row in cursor.fetchall()]

            cursor.execute(
                f"""
                SELECT pr.id, pr.production_line, pr.line_technician, pr.shift, pr.customer,
                       pr.product, pr.format, pr.pack_weight_kg, pr.packs_per_case,
                       pr.cases_per_pallet, pr.target_speed_ppm, pr.standard_speed_ppm, pr.status, pr.started_at,
                       pr.finished_at
                FROM public.production_runs AS pr
                WHERE {_TEST_DATA_EXCLUSION_SQL}
                  AND pr.started_at < %(window_end)s
                  AND (pr.finished_at IS NULL OR pr.finished_at > %(window_start)s)
                  {line_filter}
                ORDER BY pr.started_at;
                """,
                params,
            )
            runs = cursor.fetchall()
            params["run_ids"] = [run["id"] for run in runs]

            cursor.execute(
                """
                SELECT production_run_id, hour_start, pallets_completed, other_loss_reason
                FROM public.hourly_updates
                WHERE production_run_id = ANY(%(run_ids)s) AND hour_start IS NOT NULL
                  AND hour_start >= %(window_start)s AND hour_start < %(window_end)s;
                """,
                params,
            )
            readings = cursor.fetchall()

            cursor.execute(
                """
                SELECT production_run_id, previous_speed_ppm, new_speed_ppm, effective_at
                FROM public.run_target_speed_changes
                WHERE production_run_id = ANY(%(run_ids)s)
                ORDER BY effective_at;
                """,
                params,
            )
            speed_changes = cursor.fetchall()
            operating_changes = [row for run in runs for row in _operating_changes(cursor, run["id"])]

            cursor.execute(
                """
                SELECT production_run_id, reason, started_at, ended_at
                FROM public.planned_downtime_events
                WHERE production_run_id = ANY(%(run_ids)s)
                  AND started_at < %(window_end)s
                  AND (ended_at IS NULL OR ended_at > %(window_start)s);
                """,
                params,
            )
            planned = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT de.id, pr.production_line, de.machine, de.reason, de.opened_at, de.resolved_at
                FROM public.downtime_events AS de
                JOIN public.production_runs AS pr ON pr.id = de.production_run_id
                WHERE {_TEST_DATA_EXCLUSION_SQL}
                  AND de.opened_at < %(window_end)s
                  AND (de.resolved_at IS NULL OR de.resolved_at > %(window_start)s)
                  {line_filter};
                """,
                params,
            )
            faults = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT ls.id, ls.production_line, ls.kind, ls.reason, ls.started_at, ls.ended_at,
                       ls.physical_ended_at, ls.started_by, ls.ended_by,
                       ls.previous_production_run_id, prev.standard_speed_ppm AS reference_speed_ppm
                FROM public.line_stoppages AS ls
                LEFT JOIN public.production_runs AS prev ON prev.id = ls.previous_production_run_id
                WHERE ls.started_at < %(window_end)s
                  AND (ls.ended_at IS NULL OR ls.ended_at > %(window_start)s)
                  AND ls.production_line NOT ILIKE 'TEST-%%'
                  {stop_line_filter};
                """,
                params,
            )
            stoppages = cursor.fetchall()

    return {
        "lines": lines,
        "runs": runs,
        "readings": readings,
        "speed_changes": speed_changes,
        "operating_changes": operating_changes,
        "planned": planned,
        "faults": faults,
        "stoppages": stoppages,
    }


_CHANGEOVER_TEST_EXCLUSION_SQL = """
    co.production_line NOT ILIKE 'TEST-%%'
    AND COALESCE(co.previous_customer, '') NOT ILIKE '%%TEST-%%'
    AND COALESCE(co.new_customer, '') NOT ILIKE '%%TEST-%%'
    AND COALESCE(co.previous_product, '') NOT ILIKE '%%TEST-%%'
    AND COALESCE(co.new_product, '') NOT ILIKE '%%TEST-%%'
"""


def list_changeovers(filters):
    """filters: started_from / started_to (aware datetimes), production_line,
    technician, shift, status, customer / product / format / pack_weight_kg
    (matched against the previous OR new value), min/max_duration_minutes."""
    conditions = [_CHANGEOVER_TEST_EXCLUSION_SQL]
    params = {}

    simple = {
        "production_line": "co.production_line = %(production_line)s",
        "technician": "co.line_technician = %(technician)s",
        "shift": "co.shift = %(shift)s",
        "status": "co.status = %(status)s",
        "started_from": "co.started_at >= %(started_from)s",
        "started_to": "co.started_at < %(started_to)s",
        "min_duration_minutes": "co.duration_minutes >= %(min_duration_minutes)s",
        "max_duration_minutes": "co.duration_minutes <= %(max_duration_minutes)s",
    }
    either = {
        "customer": ("previous_customer", "new_customer"),
        "product": ("previous_product", "new_product"),
        "format": ("previous_format", "new_format"),
        "pack_weight_kg": ("previous_pack_weight_kg", "new_pack_weight_kg"),
    }

    for name, sql in simple.items():
        if filters.get(name) is not None:
            if name == "shift":
                from src.catalogue import aliases
                conditions.append("lower(trim(co.shift)) = ANY(%(shift_aliases)s)")
                params["shift_aliases"] = aliases("shift", filters[name])
            else:
                conditions.append(sql)
            params[name] = filters[name]

    for name, (previous_column, new_column) in either.items():
        if filters.get(name) is not None:
            if name in ("customer", "product"):
                from src.catalogue import aliases
                conditions.append(f"(lower(trim(co.{previous_column})) = ANY(%({name}_aliases)s) OR lower(trim(co.{new_column})) = ANY(%({name}_aliases)s))")
                params[name + "_aliases"] = aliases(name, filters[name])
            else:
                conditions.append(f"(co.{previous_column} = %({name})s OR co.{new_column} = %({name})s)")
            params[name] = filters[name]

    where = " AND ".join(conditions)

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                SELECT {_CHANGEOVER_COLUMNS},
                    -- End Run -> Changeover flow: physical work, then new-run setup.
                    (SELECT EXTRACT(EPOCH FROM (ls.physical_ended_at - ls.started_at)) / 60.0
                     FROM public.line_stoppages AS ls WHERE ls.id = co.line_stoppage_id) AS physical_minutes,
                    (SELECT EXTRACT(EPOCH FROM (ls.ended_at - ls.physical_ended_at)) / 60.0
                     FROM public.line_stoppages AS ls WHERE ls.id = co.line_stoppage_id) AS setup_minutes
                FROM public.changeovers AS co
                WHERE {where}
                ORDER BY co.started_at DESC;
                """,
                params,
            )
            return cursor.fetchall()


# ----------------------------------------------------------
# Weekly tonnage targets
# ----------------------------------------------------------

_TARGET_COLUMNS = "id, week_start, scope, production_line, target_tonnes, set_by, created_at, updated_at"


def list_weekly_targets(week_start):
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                SELECT {_TARGET_COLUMNS}
                FROM public.weekly_tonnage_targets
                WHERE week_start = %(week_start)s
                ORDER BY scope DESC, production_line;
                """,
                {"week_start": week_start},
            )
            return cursor.fetchall()


def upsert_weekly_targets(week_start, targets, set_by):
    """targets: [{"scope", "production_line", "target_tonnes"}]. One
    transaction; returns [(previous_row_or_None, saved_row)] for the
    audit log."""
    results = []

    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            # Also serialises the initially absent row, so its audited previous value is correct.
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('weekly-targets:'+week_start.isoformat(),))
            for target in targets:
                cursor.execute(
                    f"""
                    SELECT {_TARGET_COLUMNS}
                    FROM public.weekly_tonnage_targets
                    WHERE week_start = %(week_start)s
                      AND scope = %(scope)s
                      AND COALESCE(production_line, '') = COALESCE(%(production_line)s, '')
                    FOR UPDATE;
                    """,
                    {**target, "week_start": week_start},
                )
                previous = cursor.fetchone()

                cursor.execute(
                    f"""
                    INSERT INTO public.weekly_tonnage_targets (
                        week_start, scope, production_line, target_tonnes, set_by
                    )
                    VALUES (
                        %(week_start)s, %(scope)s, %(production_line)s,
                        %(target_tonnes)s, %(set_by)s
                    )
                    ON CONFLICT (week_start, scope, (COALESCE(production_line, '')))
                    DO UPDATE SET
                        target_tonnes = EXCLUDED.target_tonnes,
                        set_by = EXCLUDED.set_by,
                        updated_at = now()
                    RETURNING {_TARGET_COLUMNS};
                    """,
                    {**target, "week_start": week_start, "set_by": set_by},
                )
                saved = cursor.fetchone()
                _configuration_audit(cursor,"set_weekly_tonnage_target",set_by,"weekly_tonnage_target",previous,saved,
                    f"{week_start.isoformat()}:{saved['scope']}:{saved['production_line'] or 'site'}")
                results.append((previous, saved))

        connection.commit()

    return results


# ----------------------------------------------------------
# Protected dashboard reads (window-based)
# ----------------------------------------------------------


def get_dashboard_window_data(window_start, window_end, production_line=None, shift_based=False, *, connection=None):
    """Everything the window-based dashboard calculations need, in one
    connection.

    Hourly output membership:
      - shift_based (current shift, factory day, production week): by the
        update's operational shift instance (hourly_updates.
        shift_window_start - the run's recorded shift), so a period
        ending exactly at 06:00/14:00/22:00, or crossing a boundary, stays
        with the shift that produced it;
      - otherwise (rolling 24h): by period end, end-inclusive.
    Downtime is fetched across the window AND the full span of the
    included periods (`attribution_bounds`), so a boundary-crossing
    period's own downtime is available for attribution. Legacy rows with
    no timestamp cannot be placed in any window and are only counted."""
    base = [_TEST_DATA_EXCLUSION_SQL]
    params = {"window_start": window_start, "window_end": window_end}

    if production_line is not None:
        base.append("pr.production_line = %(production_line)s")
        params["production_line"] = production_line

    run_where = " AND ".join(
        base
        + [
            "pr.started_at < %(window_end)s",
            "(pr.finished_at IS NULL OR pr.finished_at > %(window_start)s)",
        ]
    )
    base_where = " AND ".join(base)
    membership = (
        "hu.shift_window_start >= %(window_start)s AND hu.shift_window_start < %(window_end)s"
        if shift_based
        else "hu.period_ended_at > %(window_start)s AND hu.period_ended_at <= %(window_end)s"
    )

    with (nullcontext(connection) if connection is not None else get_database_connection()) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT name FROM public.production_lines
                WHERE active = TRUE
                ORDER BY display_order, name;
                """
            )
            lines = [row["name"] for row in cursor.fetchall()]

            cursor.execute(
                f"""
                SELECT
                    hu.id, hu.production_run_id, hu.expected_packs,
                    hu.pallets_completed AS actual_pallets, hu.period_started_at,
                    hu.period_ended_at, hu.created_at, hu.shift, hu.shift_window_start,
                    hu.other_loss_reason, hu.unexplained_loss_reason
                FROM public.hourly_updates AS hu
                JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
                WHERE {base_where}
                  AND {membership}
                ORDER BY hu.id;
                """,
                params,
            )
            hourly = cursor.fetchall()

            params["hourly_run_ids"] = sorted({row["production_run_id"] for row in hourly})
            if shift_based and hourly:
                params["fetch_start"] = min([window_start] + [r["period_started_at"] for r in hourly])
                params["fetch_end"] = max([window_end] + [r["period_ended_at"] for r in hourly])
            else:
                params["fetch_start"] = window_start
                params["fetch_end"] = window_end

            cursor.execute(
                f"""
                SELECT
                    pr.id, pr.production_line, pr.line_technician, pr.shift,
                    pr.customer, pr.product, pr.format, pr.pack_weight_kg,
                    pr.packs_per_case, pr.cases_per_pallet, pr.target_speed_ppm, pr.standard_speed_ppm,
                    pr.status, pr.started_at, pr.finished_at,
                    pr.pallets_remaining, pr.total_pallets_completed
                FROM public.production_runs AS pr
                WHERE ({run_where})
                   OR ({base_where} AND pr.status = 'Active')
                   OR ({base_where} AND pr.id = ANY(%(hourly_run_ids)s))
                ORDER BY pr.started_at;
                """,
                params,
            )
            runs = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT COUNT(*) AS legacy_count
                FROM public.hourly_updates AS hu
                JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
                WHERE {run_where} AND hu.period_ended_at IS NULL;
                """,
                params,
            )
            legacy_count = cursor.fetchone()["legacy_count"]

            cursor.execute(
                f"""
                SELECT pde.id, pde.production_run_id, pde.reason,
                       pde.started_at, pde.ended_at
                FROM public.planned_downtime_events AS pde
                JOIN public.production_runs AS pr ON pr.id = pde.production_run_id
                WHERE {base_where}
                  AND pde.started_at < %(fetch_end)s
                  AND (pde.ended_at IS NULL OR pde.ended_at > %(fetch_start)s);
                """,
                params,
            )
            planned = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT
                    de.id, de.production_run_id, pr.production_line, de.fault_id,
                    de.machine, de.machine_id, de.reason, de.production_status,
                    de.engineering_status, de.engineer, de.opened_at,
                    de.resolved_at, de.maintenance_preventable,
                    (
                        SELECT eu.repair_classification
                        FROM public.engineering_updates AS eu
                        WHERE eu.downtime_event_id = de.id
                          AND eu.repair_classification IS NOT NULL
                        ORDER BY eu.id DESC
                        LIMIT 1
                    ) AS repair_classification
                FROM public.downtime_events AS de
                JOIN public.production_runs AS pr ON pr.id = de.production_run_id
                WHERE {base_where}
                  AND de.opened_at < %(fetch_end)s
                  AND (de.resolved_at IS NULL OR de.resolved_at > %(fetch_start)s);
                """,
                params,
            )
            faults = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT
                    x.id, x.production_run_id, x.production_line, x.capture_point,
                    x.shift, x.count_available, x.xray_pack_count,
                    x.unavailable_reason, x.palletised_packs,
                    x.post_xray_pack_difference, x.estimated_waste_percent,
                    x.waste_status, x.captured_at
                FROM public.production_run_xray_counts AS x
                JOIN public.production_runs AS pr ON pr.id = x.production_run_id
                WHERE {base_where}
                  AND x.captured_at >= %(window_start)s
                  AND x.captured_at < %(window_end)s
                ORDER BY x.captured_at;
                """,
                params,
            )
            xray = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT activity.production_line, MAX(activity.at) AS latest_activity_at
                FROM (
                    SELECT pr.production_line, GREATEST(pr.started_at, pr.finished_at) AS at
                    FROM public.production_runs AS pr WHERE {base_where}
                    UNION ALL
                    SELECT pr.production_line, hu.created_at
                    FROM public.hourly_updates AS hu
                    JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
                    WHERE {base_where}
                    UNION ALL
                    SELECT pr.production_line,
                           GREATEST(de.opened_at, de.accepted_at, de.resolved_at)
                    FROM public.downtime_events AS de
                    JOIN public.production_runs AS pr ON pr.id = de.production_run_id
                    WHERE {base_where}
                    UNION ALL
                    SELECT pr.production_line, eu.created_at
                    FROM public.engineering_updates AS eu
                    JOIN public.production_runs AS pr ON pr.id = eu.production_run_id
                    WHERE {base_where}
                    UNION ALL
                    SELECT pr.production_line, GREATEST(pde.started_at, pde.ended_at)
                    FROM public.planned_downtime_events AS pde
                    JOIN public.production_runs AS pr ON pr.id = pde.production_run_id
                    WHERE {base_where}
                    UNION ALL
                    SELECT pr.production_line, x.captured_at
                    FROM public.production_run_xray_counts AS x
                    JOIN public.production_runs AS pr ON pr.id = x.production_run_id
                    WHERE {base_where}
                ) AS activity
                WHERE activity.at IS NOT NULL
                GROUP BY activity.production_line;
                """,
                params,
            )
            latest_activity = {
                row["production_line"]: row["latest_activity_at"] for row in cursor.fetchall()
            }

            cursor.execute(
                f"""
                SELECT pr.production_line, MAX(hu.created_at) AS last_hourly_update_at
                FROM public.hourly_updates AS hu
                JOIN public.production_runs AS pr ON pr.id = hu.production_run_id
                WHERE {base_where}
                GROUP BY pr.production_line;
                """,
                params,
            )
            last_hourly = {
                row["production_line"]: row["last_hourly_update_at"] for row in cursor.fetchall()
            }

            # Line stops between runs (Handover, Changeover, Other, Restart
            # delay): charged to the line, never to a product run.
            cursor.execute(
                f"""
                SELECT ls.id, ls.production_line, ls.kind, ls.reason, ls.started_at, ls.ended_at,
                       ls.physical_ended_at, ref_run.standard_speed_ppm AS reference_speed_ppm,
                       ref_run.pack_weight_kg AS reference_pack_weight_kg,
                       ref_run.packs_per_case AS reference_packs_per_case, ref_run.cases_per_pallet AS reference_cases_per_pallet
                FROM public.line_stoppages AS ls
                LEFT JOIN public.production_runs AS ref_run ON ref_run.id = ls.previous_production_run_id
                WHERE ls.started_at < %(window_end)s
                  AND (ls.ended_at IS NULL OR ls.ended_at > %(window_start)s)
                  AND ls.production_line NOT ILIKE 'TEST-%%'
                  {"AND ls.production_line = %(production_line)s" if production_line is not None else ""};
                """,
                params,
            )
            line_stops = cursor.fetchall()
            operating_changes = [row for run in runs for row in _operating_changes(cursor, run["id"])]

    return {
        "lines": lines,
        "runs": runs,
        "line_stops": line_stops,
        "operating_changes": operating_changes,
        "hourly": hourly,
        "attribution_bounds": (params["fetch_start"], params["fetch_end"]),
        "legacy_hourly_without_timestamp": legacy_count,
        "planned": planned,
        "faults": faults,
        "xray": xray,
        "latest_activity": latest_activity,
        "last_hourly": last_hourly,
    }


# Casepacker work belongs to a changeover, never a second downtime interval.
def get_casepacker_requests(stoppage_id=None):
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""
                SELECT q.*, c.workflow AS changeover_workflow, s.production_line, s.previous_production_run_id,
                       s.next_production_run_id,
                       COALESCE((SELECT jsonb_agg(to_jsonb(u) ORDER BY u.id)
                         FROM public.casepacker_updates u WHERE u.request_id=q.id), '[]'::jsonb) AS updates
                FROM public.casepacker_requests q
                JOIN public.line_stoppages s ON s.id=q.line_stoppage_id
                LEFT JOIN public.changeovers c ON c.line_stoppage_id=s.id
                WHERE (CAST(%(stop)s AS bigint) IS NULL OR q.line_stoppage_id=%(stop)s)
                  AND (CAST(%(stop)s AS bigint) IS NOT NULL OR s.next_production_run_id IS NULL)
                ORDER BY q.requested_at, q.id;
            """, {"stop": stoppage_id})
            return cursor.fetchall()


def update_casepacker_request(request_id, action, engineer, note, moment, idempotency=None):
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)
            cursor.execute('SELECT * FROM public.casepacker_requests WHERE id=%s FOR UPDATE', (request_id,))
            request = cursor.fetchone()
            if request is None:
                raise PulseCaptureError(404, 'Casepacker request not found.')
            if request.get('cancelled_at') is not None:
                raise PulseCaptureError(409, 'This changeover was cancelled.')
            if request['ready_at'] is not None:
                raise PulseCaptureError(409, 'The casepacker has already been marked ready.')
            if action == 'accept':
                if request['engineer'] is not None:
                    raise PulseCaptureError(409, 'This changeover request has already been accepted.')
                cursor.execute('UPDATE public.casepacker_requests SET engineer=%s, accepted_at=%s, first_accepted_at=COALESCE(first_accepted_at,%s) WHERE id=%s',
                               (engineer, moment, moment, request_id))
            elif action in ('update', 'ready', 'handover'):
                if request['engineer'] != engineer:
                    raise PulseCaptureError(409, 'Only the engineer who accepted this request can update, hand over or mark it ready.')
                if not note or not note.strip():
                    raise PulseCaptureError(422, 'Add details of the casepacker work.')
                if action == 'ready':
                    cursor.execute('UPDATE public.casepacker_requests SET ready_at=%s WHERE id=%s', (moment, request_id))
                elif action == 'handover':
                    cursor.execute('UPDATE public.casepacker_requests SET engineer=NULL, accepted_at=NULL WHERE id=%s', (request_id,))
            else:
                raise PulseCaptureError(422, 'Unknown casepacker action.')
            cursor.execute("""INSERT INTO public.casepacker_updates (request_id, action, engineer, note, created_at)
                              VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                           (request_id, action, engineer, note or '', moment))
            result = {'status': 'success', 'request_id': request_id, 'action': action, 'update_id': cursor.fetchone()['id']}
            _store_idempotent_response(cursor, idempotency, result)
        connection.commit()
    return result


def act_on_engineering_fault(event_id, action, engineer, payload, moment, idempotency=None):
    """Serialize job transitions and save their replay response in one transaction.

    Claim before looking at current ownership: retries must still work after
    closure or reassignment. The authenticated actor is part of the fingerprint.
    """
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            _claim_idempotency(cursor, connection, idempotency)
            cursor.execute('SELECT * FROM public.downtime_events WHERE id=%s FOR UPDATE', (event_id,))
            fault = cursor.fetchone()
            if fault is None:
                raise PulseCaptureError(404, 'Engineering job not found.')
            if not fault['engineer_called'] or fault['engineering_status'] == 'Resolved':
                raise PulseCaptureError(409, 'This Engineering job is closed or was not called to Engineering. Refresh the list.')
            if action == 'accept':
                if fault['engineer'] not in (None, engineer):
                    raise PulseCaptureError(409, 'Another engineer has accepted this job. Refresh the list.')
                cursor.execute("UPDATE public.downtime_events SET engineer=%s, accepted_at=COALESCE(accepted_at,%s), engineering_status='Ongoing' WHERE id=%s", (engineer, moment, event_id))
            else:
                if fault['engineer'] != engineer:
                    raise PulseCaptureError(409, 'This job is no longer assigned to you. Refresh the list.')
                if action not in ('update','close','handover'):
                    raise PulseCaptureError(422, 'Unknown Engineering action.')
                handover = action == 'handover'
                fields = dict(payload)
                fields.update({
                    'event_id': event_id, 'run_id': fault['production_run_id'], 'fault_id': fault['fault_id'],
                    'engineer': engineer, 'moment': moment,
                    'update_type': 'Resolution' if action == 'close' else 'Follow Up',
                    'status': 'Resolved' if action == 'close' else 'Ongoing',
                    'classification': None if handover else payload['classification'],
                    'finding': payload['note'] if handover else payload['finding'],
                    'action': 'Job handed over; engineer unassigned and fault returned to Open Production Faults.' if handover else payload['action'],
                })
                for name in ('notes','setting_name','previous_value','new_value','reason_for_change','affected_products_or_formats'):
                    fields.setdefault(name, None)
                cursor.execute("""INSERT INTO public.engineering_updates
                    (downtime_event_id,production_run_id,fault_id,engineer,update_type,repair_classification,
                     finding,action,notes,setting_name,previous_value,new_value,reason_for_change,
                     affected_products_or_formats,engineering_status,created_at)
                    VALUES (%(event_id)s,%(run_id)s,%(fault_id)s,%(engineer)s,%(update_type)s,%(classification)s,
                     %(finding)s,%(action)s,%(notes)s,%(setting_name)s,%(previous_value)s,%(new_value)s,
                     %(reason_for_change)s,%(affected_products_or_formats)s,%(status)s,%(moment)s)
                    RETURNING id,created_at""", fields)
                update = cursor.fetchone()
                if action == 'close':
                    cursor.execute("UPDATE public.downtime_events SET engineering_status='Resolved',maintenance_preventable=%s WHERE id=%s", (payload['maintenance_preventable'],event_id))
                elif handover:
                    cursor.execute("UPDATE public.downtime_events SET engineer=NULL,accepted_at=NULL,engineering_status='Not Started' WHERE id=%s", (event_id,))
            cursor.execute('SELECT engineer,engineering_status,production_status,accepted_at,resolved_at,maintenance_preventable FROM public.downtime_events WHERE id=%s', (event_id,))
            result = {'status':'success', 'downtime_event_id':event_id, **cursor.fetchone()}
            if action == 'update':
                result.update(engineering_update_id=update['id'], created_at=update['created_at'])
            _store_idempotent_response(cursor, idempotency, result)
        connection.commit()
    return result


def get_rovema_task_evidence(since, until):
    """Read actual planned-stop records; no estimated downtime or worker inference."""
    with get_database_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"""
                SELECT pd.id, pd.reason, pd.started_by, pd.ended_by,
                       pd.started_at, pd.ended_at, pr.product, pr.pack_type,
                       pr.pack_weight_kg, pr.packs_per_case, pr.cases_per_pallet
                FROM public.planned_downtime_events pd
                JOIN public.production_runs pr ON pr.id = pd.production_run_id
                WHERE pd.production_line = 'Rovema' AND pr.production_line = 'Rovema'
                  AND pd.started_at >= %s AND pd.started_at < %s
                  AND pr.status != 'Cancelled' AND {_TEST_DATA_EXCLUSION_SQL}
                ORDER BY pd.started_at DESC, pd.id DESC
            """, (since, until))
            rows = [dict(row, source="planned_stop", observation_complete=False) for row in cursor.fetchall()]
            cursor.execute("""SELECT id, task AS reason, technician AS started_by, technician AS ended_by,
                started_at, ended_at, product, from_configuration, to_configuration, waiting_minutes,
                shared_work, completed_successfully, notes, device_name
                FROM public.task_observations WHERE started_at >= %s AND started_at < %s
                AND production_line='Rovema' ORDER BY started_at DESC, id DESC""", (since, until))
            rows += [dict(row, source="task_observation", observation_complete=True) for row in cursor.fetchall()]
            return rows
