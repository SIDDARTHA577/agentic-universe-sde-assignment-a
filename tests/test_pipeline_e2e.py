"""End-to-end tests using the official synthetic generator, CLI subprocesses, and invariants."""

from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from solution.config import DEFAULT_REPO_ROOT
from solution.pipeline import run_ingest, run_report
from tests.conftest import write_lead_csv

GENERATOR_SCRIPT = DEFAULT_REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
REPORT_FILES = (
    "batch_summary.csv",
    "daily_overview.csv",
    "lead_report.csv",
    "rejections.csv",
    "data_quality.json",
)


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "solution.cli", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _assert_invariants(reports_dir: Path, db_path: Path) -> None:
    for fname in REPORT_FILES:
        assert (reports_dir / fname).is_file(), f"Missing report file: {fname}"

    dq = json.loads((reports_dir / "data_quality.json").read_text(encoding="utf-8"))
    expected_keys = {
        "lead_rows_total",
        "lead_rows_rejected",
        "rejections_by_reason",
        "duplicate_lead_rows_merged",
        "field_warnings",
        "events_total",
        "malformed_events",
        "duplicate_events_dropped",
        "orphan_events",
        "unknown_disposition_events",
    }
    assert set(dq.keys()) == expected_keys
    assert sum(dq["rejections_by_reason"].values()) == dq["lead_rows_rejected"]

    with (reports_dir / "lead_report.csv").open("r", encoding="utf-8", newline="") as f:
        lead_reader = csv.DictReader(f)
        assert lead_reader.fieldnames == [
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
        ]
        lead_rows = list(lead_reader)

    expected_unique_leads = (
        dq["lead_rows_total"] - dq["lead_rows_rejected"] - dq["duplicate_lead_rows_merged"]
    )
    assert len(lead_rows) == expected_unique_leads
    assert [(r["workflow_id"], r["external_ref"]) for r in lead_rows] == sorted(
        (r["workflow_id"], r["external_ref"]) for r in lead_rows
    )

    for row in lead_rows:
        assert row["phone_masked"].startswith("+91******")
        assert len(row["phone_masked"]) == 13
        total_calls = int(row["total_calls"])
        conn_calls = int(row["connected_calls"])
        assert 0 <= conn_calls <= total_calls
        assert row["ever_success"] in ("true", "false")
        if total_calls == 0:
            assert row["bucket"] == "yet_to_reach_out"
            assert row["last_call_at"] == ""
            assert row["current_disposition"] == ""
        else:
            assert row["last_call_at"].endswith("+05:30")

    with (reports_dir / "batch_summary.csv").open("r", encoding="utf-8", newline="") as f:
        batch_reader = csv.DictReader(f)
        assert batch_reader.fieldnames == [
            "workflow_id",
            "batch_id",
            "total_leads",
            "yet_to_reach_out",
            "in_progress_telephony_failed",
            "in_progress_dialed",
            "completed",
        ]
        batch_rows = list(batch_reader)

    assert [(r["workflow_id"], r["batch_id"]) for r in batch_rows] == sorted(
        (r["workflow_id"], r["batch_id"]) for r in batch_rows
    )
    total_batch_leads = 0
    for row in batch_rows:
        b_total = int(row["total_leads"])
        bucket_sum = (
            int(row["yet_to_reach_out"])
            + int(row["in_progress_telephony_failed"])
            + int(row["in_progress_dialed"])
            + int(row["completed"])
        )
        assert b_total == bucket_sum
        total_batch_leads += b_total
    assert total_batch_leads == len(lead_rows)

    with (reports_dir / "daily_overview.csv").open("r", encoding="utf-8", newline="") as f:
        daily_reader = csv.DictReader(f)
        assert daily_reader.fieldnames == [
            "workflow_id",
            "date",
            "total_calls",
            "connected_calls",
            "connectivity_rate",
            "success_leads",
        ]
        daily_rows = list(daily_reader)

    assert [(r["workflow_id"], r["date"]) for r in daily_rows] == sorted(
        (r["workflow_id"], r["date"]) for r in daily_rows
    )
    for row in daily_rows:
        tc = int(row["total_calls"])
        cc = int(row["connected_calls"])
        sl = int(row["success_leads"])
        assert tc >= 1
        assert 0 <= cc <= tc
        assert sl >= 0
        rate_str = row["connectivity_rate"]
        assert len(rate_str.split(".")[1]) == 4
        assert 0.0 <= float(rate_str) <= 1.0

    with (reports_dir / "rejections.csv").open("r", encoding="utf-8", newline="") as f:
        rej_reader = csv.DictReader(f)
        assert rej_reader.fieldnames == ["workflow_id", "batch_id", "row_number", "reason"]
        rej_rows = list(rej_reader)

    assert len(rej_rows) == dq["lead_rows_rejected"]
    assert [(r["workflow_id"], r["batch_id"], int(r["row_number"])) for r in rej_rows] == sorted(
        (r["workflow_id"], r["batch_id"], int(r["row_number"])) for r in rej_rows
    )

    # Confirm DB counts match lead_report and no PII in lead_field_values.value_text.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        db_lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        assert db_lead_count == len(lead_rows)
        leaked = conn.execute(
            "SELECT COUNT(*) FROM lead_field_values WHERE is_pii = 1 AND value_text IS NOT NULL"
        ).fetchone()[0]
        assert leaked == 0
    finally:
        conn.close()


@pytest.mark.parametrize("seed", [42, 7, 99, 12345, 2026])
def test_generated_data_sequential_vs_combined_and_idempotency(
    tmp_path: Path, seed: int
) -> None:
    data_dir = tmp_path / f"data_{seed}"
    subprocess.run(
        [
            sys.executable,
            str(GENERATOR_SCRIPT),
            "--seed",
            str(seed),
            "--scale",
            "small",
            "--out",
            str(data_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    env_file = tmp_path / f".env_{seed}"
    _run_cli(["init-env", "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)

    # Scenario A & E: Sequential drop_1 -> drop_2 across separate CLI processes
    db_seq = tmp_path / f"seq_{seed}.db"
    rep_seq = tmp_path / f"rep_seq_{seed}"
    _run_cli(["ingest", str(data_dir / "drop_1"), "--db", str(db_seq), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
    _run_cli(["ingest", str(data_dir / "drop_2"), "--db", str(db_seq), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
    _run_cli(["report", "--out", str(rep_seq), "--db", str(db_seq), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)

    _assert_invariants(rep_seq, db_seq)

    # Scenario C: Idempotent re-ingest of drop_1 and drop_2 (twice)
    rep_idemp = tmp_path / f"rep_idemp_{seed}"
    _run_cli(["ingest", str(data_dir / "drop_1"), "--db", str(db_seq), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
    _run_cli(["ingest", str(data_dir / "drop_2"), "--db", str(db_seq), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
    _run_cli(["report", "--out", str(rep_idemp), "--db", str(db_seq), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)

    for fname in REPORT_FILES:
        assert (rep_seq / fname).read_bytes() == (rep_idemp / fname).read_bytes()

    # Scenario B: Combined ingestion of parent data directory in a fresh DB
    db_comb = tmp_path / f"comb_{seed}.db"
    rep_comb = tmp_path / f"rep_comb_{seed}"
    _run_cli(["ingest", str(data_dir), "--db", str(db_comb), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
    _run_cli(["report", "--out", str(rep_comb), "--db", str(db_comb), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)

    for fname in REPORT_FILES:
        assert (rep_seq / fname).read_bytes() == (rep_comb / fname).read_bytes()

    # Scenario D (for seed 42): Intermediate report after drop_1, then drop_2 -> final matches Scenario A
    if seed == 42:
        db_mid = tmp_path / f"mid_{seed}.db"
        rep_mid = tmp_path / f"rep_mid_{seed}"
        rep_after_mid = tmp_path / f"rep_after_mid_{seed}"
        _run_cli(["ingest", str(data_dir / "drop_1"), "--db", str(db_mid), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
        _run_cli(["report", "--out", str(rep_mid), "--db", str(db_mid), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
        _assert_invariants(rep_mid, db_mid)
        _run_cli(["ingest", str(data_dir / "drop_2"), "--db", str(db_mid), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
        _run_cli(["report", "--out", str(rep_after_mid), "--db", str(db_mid), "--env-file", str(env_file)], cwd=DEFAULT_REPO_ROOT)
        for fname in REPORT_FILES:
            assert (rep_seq / fname).read_bytes() == (rep_after_mid / fname).read_bytes()


def test_failed_ingestion_rolls_back_atomically(tmp_path: Path, make_config) -> None:
    cfg = make_config("rollback.db")
    drop = tmp_path / "drop_bad"
    write_lead_csv(
        drop / "leads" / "motor_renewal" / "batch_001.csv",
        ["customer_name", "phone", "policy_number"],
        [["Valid Lead", "9876543210", "MOT/1"]],
    )
    # Create an unknown workflow directory so ingestion fails after batch_001 or within drop
    bad_csv = drop / "leads" / "unknown_workflow" / "batch_002.csv"
    write_lead_csv(bad_csv, ["a"], [["1"]])

    with pytest.raises(ValueError, match="Unknown workflow"):
        run_ingest(cfg, [drop])

    # Now test single-file mid-transaction rollback by passing invalid UTF-8 bytes after a valid row
    drop2 = tmp_path / "drop_corrupt"
    corrupt_csv = drop2 / "leads" / "motor_renewal" / "batch_099.csv"
    corrupt_csv.parent.mkdir(parents=True, exist_ok=True)
    corrupt_csv.write_bytes(
        b"customer_name,phone,policy_number\n"
        b"First Good,9876500099,MOT/99\n"
        b"Bad Bytes,\xff\xfe,MOT/100\n"
    )
    with pytest.raises(UnicodeDecodeError):
        run_ingest(cfg, [drop2])

    # Neither First Good nor batch_099 should exist in DB after rollback
    conn = sqlite3.connect(str(cfg.db_path))
    try:
        files_count = conn.execute(
            "SELECT COUNT(*) FROM ingested_files WHERE batch_id = 'batch_099'"
        ).fetchone()[0]
        leads_count = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE external_ref = 'MOT/99'"
        ).fetchone()[0]
        assert files_count == 0
        assert leads_count == 0
    finally:
        conn.close()

    # Event file mid-transaction rollback (invalid UTF-8 after a valid JSONL event)
    drop3 = tmp_path / "drop_corrupt_events"
    corrupt_jsonl = drop3 / "events" / "events_9999.jsonl"
    corrupt_jsonl.parent.mkdir(parents=True, exist_ok=True)
    corrupt_jsonl.write_bytes(
        b'{"event_id":"evt_good_before_fail","event_type":"call.initiated","workflow_id":"motor_renewal","call_id":"c_fail","occurred_at":"2026-10-06T10:00:00Z","phone":"9876543210","payload":{}}\n'
        b'\xff\xfe\n'
    )
    with pytest.raises(UnicodeDecodeError):
        run_ingest(cfg, [drop3])

    conn = sqlite3.connect(str(cfg.db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM interaction_events WHERE event_id = 'evt_good_before_fail'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ingested_files WHERE batch_id = 'events_9999'").fetchone()[0] == 0
    finally:
        conn.close()


def test_changed_file_at_same_path_fails_safely(tmp_path: Path, make_config) -> None:
    cfg = make_config("changed_file.db")
    drop = tmp_path / "drop_1"
    csv_path = drop / "leads" / "motor_renewal" / "batch_001.csv"
    write_lead_csv(
        csv_path,
        ["customer_name", "phone", "policy_number"],
        [["Original Lead", "9876543210", "MOT/ORIG"]],
    )
    run_ingest(cfg, [drop])

    # Modify the already-ingested file in place
    write_lead_csv(
        csv_path,
        ["customer_name", "phone", "policy_number"],
        [["Modified Lead", "9876543211", "MOT/MOD"]],
    )
    with pytest.raises(ValueError, match="different checksum"):
        run_ingest(cfg, [drop])

    # DB must retain original state untouched
    conn = sqlite3.connect(str(cfg.db_path))
    try:
        refs = [r[0] for r in conn.execute("SELECT external_ref FROM leads").fetchall()]
        assert refs == ["MOT/ORIG"]
    finally:
        conn.close()


