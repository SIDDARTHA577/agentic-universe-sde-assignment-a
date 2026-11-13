"""Report derivation and exact CSV/JSON output generation."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from solution.db import resolve_event_links
from solution.security import PIIProtector


@dataclass(frozen=True)
class CallFact:
    """Derived facts for a single counted call (linked call.initiated present)."""

    workflow_id: str
    call_id: str
    lead_id: int
    initiated_utc: str
    initiated_ist: str
    call_date_ist: str
    is_connected: bool
    has_telephony_failed: bool
    effective_disposition: str | None
    is_terminal: bool
    is_success: bool


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=",", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _load_dispositions(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[bool, bool]]:
    rows = conn.execute(
        "SELECT workflow_id, code, is_terminal, is_success FROM dispositions"
    ).fetchall()
    return {
        (row["workflow_id"], row["code"]): (bool(row["is_terminal"]), bool(row["is_success"]))
        for row in rows
    }


def _derive_call_facts(
    conn: sqlite3.Connection,
    dispositions: dict[tuple[str, str], tuple[bool, bool]],
) -> list[CallFact]:
    """Compute CallFact records from linked events in the append-only ledger."""
    init_rows = conn.execute(
        """
        SELECT e.workflow_id, e.call_id, el.lead_id,
               e.occurred_at_utc, e.occurred_at_ist, e.occurred_date_ist, e.event_id
        FROM interaction_events e
        JOIN event_links el ON el.event_id = e.event_id
        WHERE e.event_type = 'call.initiated'
        ORDER BY e.occurred_at_utc ASC, e.event_id ASC
        """
    ).fetchall()

    initiated_by_call: dict[tuple[str, str], sqlite3.Row] = {}
    for row in init_rows:
        key = (row["workflow_id"], row["call_id"])
        if key not in initiated_by_call:
            initiated_by_call[key] = row

    if not initiated_by_call:
        return []

    connected_calls: set[tuple[str, str]] = {
        (row["workflow_id"], row["call_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT e.workflow_id, e.call_id
            FROM interaction_events e
            JOIN event_links el ON el.event_id = e.event_id
            WHERE e.event_type = 'call.connected'
            """
        ).fetchall()
    }

    failed_calls: set[tuple[str, str]] = {
        (row["workflow_id"], row["call_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT e.workflow_id, e.call_id
            FROM interaction_events e
            JOIN event_links el ON el.event_id = e.event_id
            WHERE e.event_type = 'call.telephony_failed'
            """
        ).fetchall()
    }

    disp_rows = conn.execute(
        """
        SELECT e.workflow_id, e.call_id, e.event_type, e.disposition_code,
               e.occurred_at_utc, e.event_id
        FROM interaction_events e
        JOIN event_links el ON el.event_id = e.event_id
        WHERE e.event_type IN ('disposition.set', 'disposition.override')
          AND e.disposition_code IS NOT NULL
        """
    ).fetchall()

    latest_override: dict[tuple[str, str], tuple[tuple[str, str], str]] = {}
    latest_set: dict[tuple[str, str], tuple[tuple[str, str], str]] = {}

    for row in disp_rows:
        wf = row["workflow_id"]
        code = row["disposition_code"]
        if (wf, code) not in dispositions:
            continue
        key = (wf, row["call_id"])
        sort_key = (row["occurred_at_utc"], row["event_id"])
        target = latest_override if row["event_type"] == "disposition.override" else latest_set
        prev = target.get(key)
        if prev is None or sort_key > prev[0]:
            target[key] = (sort_key, code)

    facts: list[CallFact] = []
    for key, init_row in initiated_by_call.items():
        wf, call_id = key
        eff_code: str | None = None
        if key in latest_override:
            eff_code = latest_override[key][1]
        elif key in latest_set:
            eff_code = latest_set[key][1]

        is_terminal = False
        is_success = False
        if eff_code is not None:
            is_terminal, is_success = dispositions[(wf, eff_code)]

        facts.append(
            CallFact(
                workflow_id=wf,
                call_id=call_id,
                lead_id=int(init_row["lead_id"]),
                initiated_utc=init_row["occurred_at_utc"],
                initiated_ist=init_row["occurred_at_ist"],
                call_date_ist=init_row["occurred_date_ist"],
                is_connected=key in connected_calls,
                has_telephony_failed=key in failed_calls,
                effective_disposition=eff_code,
                is_terminal=is_terminal,
                is_success=is_success,
            )
        )

    return facts


def _compute_lead_bucket(calls: list[CallFact]) -> str:
    if not calls:
        return "yet_to_reach_out"
    if any(c.is_terminal for c in calls):
        return "completed"
    if all(c.has_telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def _build_data_quality(conn: sqlite3.Connection) -> dict[str, Any]:
    lead_agg = conn.execute(
        """
        SELECT
            COALESCE(SUM(rows_total), 0) AS lead_rows_total,
            COALESCE(SUM(rows_rejected), 0) AS lead_rows_rejected,
            COALESCE(SUM(duplicates_merged), 0) AS duplicate_lead_rows_merged,
            COALESCE(SUM(field_warnings), 0) AS field_warnings
        FROM ingested_files
        WHERE file_kind = 'leads' AND status = 'completed'
        """
    ).fetchone()

    reason_rows = conn.execute(
        """
        SELECT reason, COUNT(*) AS cnt
        FROM lead_rejections
        GROUP BY reason
        ORDER BY reason ASC
        """
    ).fetchall()
    rejections_by_reason = {row["reason"]: int(row["cnt"]) for row in reason_rows}

    event_agg = conn.execute(
        """
        SELECT
            COALESCE(SUM(events_total), 0) AS events_total,
            COALESCE(SUM(malformed_events), 0) AS malformed_events,
            COALESCE(SUM(duplicate_events_dropped), 0) AS duplicate_events_dropped
        FROM ingested_files
        WHERE file_kind = 'events' AND status = 'completed'
        """
    ).fetchone()

    orphan_row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM interaction_events e
        LEFT JOIN event_links el ON el.event_id = e.event_id
        WHERE el.event_id IS NULL
        """
    ).fetchone()

    unknown_disp_row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM interaction_events e
        JOIN event_links el ON el.event_id = e.event_id
        LEFT JOIN dispositions d
          ON d.workflow_id = e.workflow_id
         AND d.code = e.disposition_code
        WHERE e.event_type IN ('disposition.set', 'disposition.override')
          AND (e.disposition_code IS NULL OR d.code IS NULL)
        """
    ).fetchone()

    return {
        "lead_rows_total": int(lead_agg["lead_rows_total"]),
        "lead_rows_rejected": int(lead_agg["lead_rows_rejected"]),
        "rejections_by_reason": rejections_by_reason,
        "duplicate_lead_rows_merged": int(lead_agg["duplicate_lead_rows_merged"]),
        "field_warnings": int(lead_agg["field_warnings"]),
        "events_total": int(event_agg["events_total"]),
        "malformed_events": int(event_agg["malformed_events"]),
        "duplicate_events_dropped": int(event_agg["duplicate_events_dropped"]),
        "orphan_events": int(orphan_row["cnt"]),
        "unknown_disposition_events": int(unknown_disp_row["cnt"]),
    }


def generate_reports(
    conn: sqlite3.Connection,
    out_dir: Path,
    protector: PIIProtector,
) -> dict[str, Any]:
    """Generate the five contract reports in out_dir and return data_quality metrics."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Ensure any pending link resolution is up to date before reporting.
    conn.execute("BEGIN")
    try:
        resolve_event_links(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    dispositions = _load_dispositions(conn)
    call_facts = _derive_call_facts(conn, dispositions)

    calls_by_lead: dict[int, list[CallFact]] = defaultdict(list)
    calls_by_wf_date: dict[tuple[str, str], list[CallFact]] = defaultdict(list)
    for call in call_facts:
        calls_by_lead[call.lead_id].append(call)
        calls_by_wf_date[(call.workflow_id, call.call_date_ist)].append(call)

    # 1. lead_report.csv and 2. batch_summary.csv
    lead_rows = conn.execute(
        """
        SELECT id, workflow_id, external_ref, batch_id, phone_encrypted
        FROM leads
        ORDER BY workflow_id ASC, external_ref ASC, id ASC
        """
    ).fetchall()

    lead_report_rows: list[list[Any]] = []
    batch_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {
            "total_leads": 0,
            "yet_to_reach_out": 0,
            "in_progress_telephony_failed": 0,
            "in_progress_dialed": 0,
            "completed": 0,
        }
    )

    for lead in lead_rows:
        lead_id = int(lead["id"])
        wf = lead["workflow_id"]
        batch_id = lead["batch_id"]
        canonical_phone = protector.decrypt(lead["phone_encrypted"])
        phone_masked = protector.mask_phone(canonical_phone)

        lead_calls = calls_by_lead.get(lead_id, [])
        bucket = _compute_lead_bucket(lead_calls)
        total_calls = len(lead_calls)
        connected_calls = sum(1 for c in lead_calls if c.is_connected)

        if lead_calls:
            latest_call = max(lead_calls, key=lambda c: (c.initiated_utc, c.call_id))
            last_call_at = latest_call.initiated_ist
        else:
            last_call_at = ""

        calls_with_disp = [c for c in lead_calls if c.effective_disposition is not None]
        if calls_with_disp:
            latest_disp_call = max(calls_with_disp, key=lambda c: (c.initiated_utc, c.call_id))
            current_disposition = latest_disp_call.effective_disposition or ""
        else:
            current_disposition = ""

        ever_success = "true" if any(c.is_success for c in lead_calls) else "false"

        lead_report_rows.append(
            [
                wf,
                lead["external_ref"],
                batch_id,
                phone_masked,
                bucket,
                total_calls,
                connected_calls,
                last_call_at,
                current_disposition,
                ever_success,
            ]
        )

        b_stats = batch_counts[(wf, batch_id)]
        b_stats["total_leads"] += 1
        b_stats[bucket] += 1

    _write_csv(
        out_dir / "lead_report.csv",
        [
            "workflow_id",
            "external_ref",
            "batch_id",
            "phone_masked",
            "bucket",
            "total_calls",
            "connected_calls",
            "last_call_at",
            "current_disposition",
            "ever_success",
        ],
        lead_report_rows,
    )

    batch_summary_rows: list[list[Any]] = []
    for wf, batch_id in sorted(batch_counts.keys()):
        b_stats = batch_counts[(wf, batch_id)]
        batch_summary_rows.append(
            [
                wf,
                batch_id,
                b_stats["total_leads"],
                b_stats["yet_to_reach_out"],
                b_stats["in_progress_telephony_failed"],
                b_stats["in_progress_dialed"],
                b_stats["completed"],
            ]
        )

    _write_csv(
        out_dir / "batch_summary.csv",
        [
            "workflow_id",
            "batch_id",
            "total_leads",
            "yet_to_reach_out",
            "in_progress_telephony_failed",
            "in_progress_dialed",
            "completed",
        ],
        batch_summary_rows,
    )

    # 3. daily_overview.csv
    daily_rows: list[list[Any]] = []
    for wf, call_date in sorted(calls_by_wf_date.keys()):
        day_calls = calls_by_wf_date[(wf, call_date)]
        total_calls = len(day_calls)
        connected_calls = sum(1 for c in day_calls if c.is_connected)
        rate = (Decimal(connected_calls) / Decimal(total_calls)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        success_leads = len({c.lead_id for c in day_calls if c.is_success})
        daily_rows.append(
            [
                wf,
                call_date,
                total_calls,
                connected_calls,
                f"{rate:.4f}",
                success_leads,
            ]
        )

    _write_csv(
        out_dir / "daily_overview.csv",
        [
            "workflow_id",
            "date",
            "total_calls",
            "connected_calls",
            "connectivity_rate",
            "success_leads",
        ],
        daily_rows,
    )

    # 4. rejections.csv
    rejection_records = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM lead_rejections
        ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
        """
    ).fetchall()
    rejection_rows = [
        [r["workflow_id"], r["batch_id"], int(r["row_number"]), r["reason"]]
        for r in rejection_records
    ]
    _write_csv(
        out_dir / "rejections.csv",
        ["workflow_id", "batch_id", "row_number", "reason"],
        rejection_rows,
    )

    # 5. data_quality.json
    data_quality = _build_data_quality(conn)
    (out_dir / "data_quality.json").write_text(
        json.dumps(data_quality, indent=2) + "\n",
        encoding="utf-8",
    )

    return data_quality

