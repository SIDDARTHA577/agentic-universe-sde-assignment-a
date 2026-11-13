"""Tests for lead CSV ingestion, rejections, warnings, duplicate merging, and defaults."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from solution.db import connect
from solution.lead_ingestion import get_effective_lead_fields
from solution.pipeline import run_ingest, run_report
from solution.schemas import load_registry
from solution.security import PIIProtector
from tests.conftest import write_lead_csv


def test_blank_rows_numbering_rejection_precedence_and_warnings(tmp_path: Path, make_config) -> None:
    cfg = make_config("leads_rules.db")
    drop = tmp_path / "drop_1"
    csv_path = drop / "leads" / "motor_renewal" / "batch_001.csv"

    write_lead_csv(
        csv_path,
        ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"],
        [
            ["", "", "", "", "", ""],  # blank row -> skipped, NOT counted
            ["   ", "bad_phone", "", "bad_date", "", ""],  # row 1: missing_required:customer_name (warnings ignored!)
            ["Aarav", "022-2345-6789", "", "31/02/2026", "N/A", "Hindi"],  # row 2: invalid_phone (warnings ignored!)
            ["Vivaan", "9876543210", "", "31/02/2026", "bad", ""],  # row 3: missing_required:policy_number
            ["Diya", "9876543211", "MOT/1", "31/02/2026", "N/A", "Hindi"],  # row 4: accepted with 3 field warnings
            ["", "   ", "", "", "", ""],  # blank row -> skipped
            ["Ishaan", "9876543212", "MOT/2", "15-Dec-2026", "Rs. 12,500.5", "TA"],  # row 5: accepted cleanly
        ],
    )

    run_ingest(cfg, [drop])
    reports_dir = tmp_path / "reports"
    quality = run_report(cfg, reports_dir)

    assert quality["lead_rows_total"] == 5
    assert quality["lead_rows_rejected"] == 3
    assert quality["rejections_by_reason"] == {
        "invalid_phone": 1,
        "missing_required:customer_name": 1,
        "missing_required:policy_number": 1,
    }
    assert quality["field_warnings"] == 3

    with (reports_dir / "rejections.csv").open("r", encoding="utf-8", newline="") as f:
        rej_rows = list(csv.DictReader(f))
    assert rej_rows == [
        {"workflow_id": "motor_renewal", "batch_id": "batch_001", "row_number": "1", "reason": "missing_required:customer_name"},
        {"workflow_id": "motor_renewal", "batch_id": "batch_001", "row_number": "2", "reason": "invalid_phone"},
        {"workflow_id": "motor_renewal", "batch_id": "batch_001", "row_number": "3", "reason": "missing_required:policy_number"},
    ]

    # Check rejections.csv contains no PII.
    rej_text = (reports_dir / "rejections.csv").read_text(encoding="utf-8")
    assert "Aarav" not in rej_text
    assert "022-2345-6789" not in rej_text
    assert "9876543210" not in rej_text


def test_duplicate_merging_permanent_batch_and_defaults(
    tmp_path: Path, make_config, schemas_dir: Path
) -> None:
    cfg = make_config("merge.db")
    drop1 = tmp_path / "drop_1"
    drop2 = tmp_path / "drop_2"

    # Lead 1 first seen in batch_001 with preferred_language="hi", no expiry, premium=10000
    # Lead 2 first seen in batch_001 with preferred_language="", expiry=01/02/2026
    write_lead_csv(
        drop1 / "leads" / "motor_renewal" / "batch_001.csv",
        ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"],
        [
            ["Ananya Rao", "9876500001", "MOT/001", "", "10000", "hi"],
            ["Karthik Nair", "+91 98765 00002", "MOT/002", "01/02/2026", "", ""],
        ],
    )
    # Drop 2 batch_003 uses aliases, merges Lead 1 with empty language (must NOT overwrite "hi"!)
    # and updates expiry & premium; Lead 2 gets no update so default "en" applies.
    write_lead_csv(
        drop2 / "leads" / "motor_renewal" / "batch_003.csv",
        ["Insured Name", "Mobile No", "Policy No", "Expiry Date", "Premium", "Language", "extra_col"],
        [
            ["Ananya S Rao", "09876500001", "MOT/001-REV", "20-Dec-2026", "INR 11,250.456", "", "ignored"],
            ["Ananya S Rao", "91-98765-00001", "MOT/001-REV", "", "", "Hindi", "ignored2"],  # invalid enum -> warning, null, does not overwrite "hi"
        ],
    )

    run_ingest(cfg, [drop1])
    run_ingest(cfg, [drop2])
    quality = run_report(cfg, tmp_path / "reports")

    assert quality["lead_rows_total"] == 4
    assert quality["duplicate_lead_rows_merged"] == 2
    assert quality["field_warnings"] == 1

    registry = load_registry(schemas_dir)
    protector = PIIProtector(cfg.secrets)
    conn = connect(cfg.db_path)
    try:
        leads = conn.execute("SELECT id, batch_id, external_ref FROM leads ORDER BY id").fetchall()
        assert len(leads) == 2

        # Permanent first-seen batch_id = batch_001
        assert leads[0]["batch_id"] == "batch_001"
        assert leads[0]["external_ref"] == "MOT/001-REV"
        fields_1 = get_effective_lead_fields(conn, leads[0]["id"], registry.workflows["motor_renewal"], protector)
        assert fields_1 == {
            "customer_name": "Ananya S Rao",
            "phone": "+919876500001",
            "policy_number": "MOT/001-REV",
            "policy_expiry": "2026-12-20",
            "premium_amount": "11250.46",
            "preferred_language": "hi",  # preserved!
        }

        assert leads[1]["batch_id"] == "batch_001"
        fields_2 = get_effective_lead_fields(conn, leads[1]["id"], registry.workflows["motor_renewal"], protector)
        assert fields_2 == {
            "customer_name": "Karthik Nair",
            "phone": "+919876500002",
            "policy_number": "MOT/002",
            "policy_expiry": "2026-02-01",
            "premium_amount": None,
            "preferred_language": "en",  # default applied after merge
        }
    finally:
        conn.close()


def test_policy_feedback_workflow_dynamic_schema(tmp_path: Path, make_config) -> None:
    cfg = make_config("feedback.db")
    drop = tmp_path / "drop_1"
    write_lead_csv(
        drop / "leads" / "policy_feedback" / "batch_010.csv",
        ["Order ID", "Name", "Contact", "Product", "Order Date", "Language"],
        [
            ["ORD-100", "Meera Iyer", "8123456789", "Laptop", "05-Nov-2026", "kn"],
        ],
    )
    run_ingest(cfg, [drop])
    run_report(cfg, tmp_path / "reports")

    with (tmp_path / "reports" / "lead_report.csv").open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [
        {
            "workflow_id": "policy_feedback",
            "external_ref": "ORD-100",
            "batch_id": "batch_010",
            "phone_masked": "+91******6789",
            "bucket": "yet_to_reach_out",
            "total_calls": "0",
            "connected_calls": "0",
            "last_call_at": "",
            "current_disposition": "",
            "ever_success": "false",
        }
    ]


def test_missing_phone_unicode_utf8_bom_intra_file_duplicates_and_cross_workflow_isolation(
    tmp_path: Path, make_config
) -> None:
    cfg = make_config("adv_leads.db")
    drop1 = tmp_path / "drop_1"

    # UTF-8 with BOM + Unicode customer name + missing_required:phone + intra-file duplicate
    bom_csv = drop1 / "leads" / "motor_renewal" / "batch_001.csv"
    bom_csv.parent.mkdir(parents=True, exist_ok=True)
    bom_csv.write_text(
        "\ufeffcustomer_name,phone,policy_number,preferred_language\n"
        "आर्यमन शर्मा,,MOT/EMPTY,hi\n"
        "Meera José,9876543210,MOT/100,ta\n"
        "Meera José,+91 98765 43210,MOT/101,\n",
        encoding="utf-8",
    )
    # Cross-file duplicate in same drop (batch_002)
    write_lead_csv(
        drop1 / "leads" / "motor_renewal" / "batch_002.csv",
        ["customer_name", "phone", "policy_number"],
        [["Meera José-Updated", "09876543210", "MOT/102"]],
    )
    # Same phone in policy_feedback MUST stay a distinct lead with its own batch_id
    write_lead_csv(
        drop1 / "leads" / "policy_feedback" / "batch_005.csv",
        ["customer_name", "phone", "order_id"],
        [["Meera José", "9876543210", "ORD-999"]],
    )

    run_ingest(cfg, [drop1])
    rep = tmp_path / "reports"
    dq = run_report(cfg, rep)

    assert dq["lead_rows_total"] == 5
    assert dq["lead_rows_rejected"] == 1
    assert dq["rejections_by_reason"] == {"missing_required:phone": 1}
    assert dq["duplicate_lead_rows_merged"] == 2

    with (rep / "lead_report.csv").open("r", encoding="utf-8", newline="") as f:
        lead_rows = list(csv.DictReader(f))
    assert [(r["workflow_id"], r["external_ref"], r["batch_id"]) for r in lead_rows] == [
        ("motor_renewal", "MOT/102", "batch_001"),
        ("policy_feedback", "ORD-999", "batch_005"),
    ]


