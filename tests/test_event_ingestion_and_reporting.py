"""Hand-designed fixture tests for event ledger, orphan recovery, overrides, buckets, and exact reports."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from solution.pipeline import run_ingest, run_report
from tests.conftest import write_events_jsonl, write_lead_csv


def test_handcrafted_events_orphan_recovery_overrides_buckets_and_reports(
    tmp_path: Path, make_config
) -> None:
    cfg = make_config("handcrafted.db")
    drop1 = tmp_path / "drop_1"
    drop2 = tmp_path / "drop_2"

    # Drop 1 leads:
    # L1 (MOT/001, 9000000001): will have 2 calls on 2026-10-06 IST:
    #   call_1: connected, disposition.set = renewed (success, terminal) -> bucket completed
    #   call_2: connected, disposition.set = payment_link_sent (overridden to renewed? No, let's test another combo!)
    # Let's design 4 leads covering every bucket & tricky rule:
    # - L1 (MOT/001, 9000000001): 2 calls on same day 2026-10-06 IST:
    #     call_1 (09:00 IST): connected + disposition.set=renewed (is_success=true, terminal)
    #     call_2 (10:00 IST): connected + disposition.set=renewed -> tests distinct success_leads = 1 on 2026-10-06!
    # - L2 (MOT/002, 9000000002): arrives in DROP 2! Its events arrive in DROP 1 (including override BEFORE initiated!):
    #     call_3 (2026-10-05T19:00:00Z = 2026-10-06T00:30:00+05:30 IST):
    #       disposition.override (not_interested, terminal=true, success=false) arrives BEFORE call.initiated
    #       disposition.set (renewed)
    #       -> effective disposition is not_interested (bucket=completed, ever_success=false)
    # - L3 (MOT/003, 9000000003): 2 calls:
    #     call_4 (2026-10-06 11:00:00 naive IST): connected + disposition.set=callback_requested
    #     call_5 (2026-10-06 15:00:00 naive IST): telephony_failed (no disposition)
    #     -> bucket=in_progress_dialed (not all failed), current_disposition=callback_requested (most recent call THAT HAS ONE!)
    # - L4 (MOT/004, 9000000004): 1 call:
    #     call_6 (2026-10-07 10:00:00+05:30): telephony_failed
    #     -> bucket=in_progress_telephony_failed
    # - L5 (MOT/005, 9000000005): 0 calls (also has a call.connected event WITHOUT call.initiated -> must NOT count!)
    #     -> bucket=yet_to_reach_out, total_calls=0
    write_lead_csv(
        drop1 / "leads" / "motor_renewal" / "batch_001.csv",
        ["customer_name", "phone", "policy_number"],
        [
            ["Lead One", "9000000001", "MOT/001"],
            ["Lead Three", "9000000003", "MOT/003"],
            ["Lead Four", "9000000004", "MOT/004"],
            ["Lead Five", "9000000005", "MOT/005"],
        ],
    )
    write_lead_csv(
        drop2 / "leads" / "motor_renewal" / "batch_002.csv",
        ["customer_name", "phone", "policy_number"],
        [
            ["Lead Two", "9000000002", "MOT/002"],
        ],
    )

    write_events_jsonl(
        drop1 / "events" / "events_0001.jsonl",
        [
            # Malformed JSON line
            '{"event_id": "evt_bad_json"',
            # Missing event_id
            {"event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_x", "occurred_at": "2026-10-06T10:00:00Z"},
            # L1 call_1 & call_2 on 2026-10-06 IST
            {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-06 09:00:00", "phone": "9000000001", "payload": {}},
            {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-06 09:00:10", "phone": "+91 90000 00001", "payload": {}},
            {"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-06 09:01:00", "phone": "09000000001", "payload": {"disposition_code": "renewed"}},
            {"event_id": "e4", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_2", "occurred_at": "2026-10-06 10:00:00", "phone": "9000000001", "payload": {}},
            {"event_id": "e5", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_2", "occurred_at": "2026-10-06 10:00:10", "phone": "9000000001", "payload": {}},
            {"event_id": "e6", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_2", "occurred_at": "2026-10-06 10:01:00", "phone": "9000000001", "payload": {"disposition_code": "renewed"}},
            # Duplicate of e1 in same file
            {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-06 09:00:00", "phone": "9000000001", "payload": {}},
            # L2 (lead arrives in drop_2!), override appears BEFORE call.initiated, UTC 19:00 on Oct 5 == IST 00:30 on Oct 6
            {"event_id": "e7", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-07T09:00:00Z", "payload": {"disposition_code": "not_interested", "overridden_by": "sup_1", "reason": "qa"}},
            {"event_id": "e8", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-05T19:00:00Z", "phone": "9000000002", "payload": {}},
            {"event_id": "e9", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-05T19:00:20Z", "phone": "9000000002", "payload": {}},
            {"event_id": "e10", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-05T19:02:00Z", "phone": "9000000002", "payload": {"disposition_code": "renewed"}},
            # L3 call_4 & call_5
            {"event_id": "e11", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_4", "occurred_at": "2026-10-06 11:00:00", "phone": "9000000003", "payload": {}},
            {"event_id": "e12", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_4", "occurred_at": "2026-10-06 11:00:15", "phone": "9000000003", "payload": {}},
            {"event_id": "e13", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_4", "occurred_at": "2026-10-06 11:01:00", "phone": "9000000003", "payload": {"disposition_code": "callback_requested"}},
            {"event_id": "e13_unk", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_4", "occurred_at": "2026-10-06 12:00:00", "payload": {"disposition_code": "BOGUS_CODE"}},  # unknown override -> ignored!
            {"event_id": "e14", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_5", "occurred_at": "2026-10-06 15:00:00", "phone": "9000000003", "payload": {}},
            {"event_id": "e15", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "call_5", "occurred_at": "2026-10-06 15:00:05", "phone": "9000000003", "payload": {"reason": "sip_503"}},
            # L5 call without call.initiated -> must NOT be counted
            {"event_id": "e16", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_no_init", "occurred_at": "2026-10-06 16:00:00", "phone": "9000000005", "payload": {}},
            # Permanent orphan with unknown disposition -> must count ONLY as orphan (precedence 3 > 4!)
            {"event_id": "e_orphan_unk", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_orphan", "occurred_at": "2026-10-06 16:30:00", "phone": "9999999999", "payload": {"disposition_code": "BOGUS"}},
        ],
    )

    write_events_jsonl(
        drop2 / "events" / "events_1001.jsonl",
        [
            # Duplicate of e8 from drop_1
            {"event_id": "e8", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-05T19:00:00Z", "phone": "9000000002", "payload": {}},
            # L4 call_6 on 2026-10-07
            {"event_id": "e17", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_6", "occurred_at": "2026-10-07T10:00:00+05:30", "phone": "9000000004", "payload": {}},
            {"event_id": "e18", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "call_6", "occurred_at": "2026-10-07T10:00:04+05:30", "phone": "9000000004", "payload": {"reason": "carrier_rejected"}},
        ],
    )

    # First ingest drop_1 and check intermediate orphan count (L2's 4 events + e_orphan_unk = 5)
    run_ingest(cfg, [drop1])
    q1 = run_report(cfg, tmp_path / "reports_drop1")
    assert q1["orphan_events"] == 5
    assert q1["unknown_disposition_events"] == 1  # e13_unk

    # Now ingest drop_2 -> L2 arrives, so L2's 4 events become linked!
    run_ingest(cfg, [drop2])
    rep_dir = tmp_path / "reports_final"
    q2 = run_report(cfg, rep_dir)

    assert q2 == {
        "lead_rows_total": 5,
        "lead_rows_rejected": 0,
        "rejections_by_reason": {},
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "events_total": 24,
        "malformed_events": 2,
        "duplicate_events_dropped": 2,
        "orphan_events": 1,  # only e_orphan_unk remains orphan
        "unknown_disposition_events": 1,  # only e13_unk; e_orphan_unk is orphan (precedence 3 before 4)
    }

    with (rep_dir / "lead_report.csv").open("r", encoding="utf-8", newline="") as f:
        lead_rows = list(csv.DictReader(f))
    assert lead_rows == [
        {
            "workflow_id": "motor_renewal",
            "external_ref": "MOT/001",
            "batch_id": "batch_001",
            "phone_masked": "+91******0001",
            "bucket": "completed",
            "total_calls": "2",
            "connected_calls": "2",
            "last_call_at": "2026-10-06T10:00:00+05:30",
            "current_disposition": "renewed",
            "ever_success": "true",
        },
        {
            "workflow_id": "motor_renewal",
            "external_ref": "MOT/002",
            "batch_id": "batch_002",
            "phone_masked": "+91******0002",
            "bucket": "completed",
            "total_calls": "1",
            "connected_calls": "1",
            "last_call_at": "2026-10-06T00:30:00+05:30",
            "current_disposition": "not_interested",
            "ever_success": "false",
        },
        {
            "workflow_id": "motor_renewal",
            "external_ref": "MOT/003",
            "batch_id": "batch_001",
            "phone_masked": "+91******0003",
            "bucket": "in_progress_dialed",
            "total_calls": "2",
            "connected_calls": "1",
            "last_call_at": "2026-10-06T15:00:00+05:30",
            "current_disposition": "callback_requested",
            "ever_success": "false",
        },
        {
            "workflow_id": "motor_renewal",
            "external_ref": "MOT/004",
            "batch_id": "batch_001",
            "phone_masked": "+91******0004",
            "bucket": "in_progress_telephony_failed",
            "total_calls": "1",
            "connected_calls": "0",
            "last_call_at": "2026-10-07T10:00:00+05:30",
            "current_disposition": "",
            "ever_success": "false",
        },
        {
            "workflow_id": "motor_renewal",
            "external_ref": "MOT/005",
            "batch_id": "batch_001",
            "phone_masked": "+91******0005",
            "bucket": "yet_to_reach_out",
            "total_calls": "0",
            "connected_calls": "0",
            "last_call_at": "",
            "current_disposition": "",
            "ever_success": "false",
        },
    ]

    with (rep_dir / "daily_overview.csv").open("r", encoding="utf-8", newline="") as f:
        daily_rows = list(csv.DictReader(f))
    # 2026-10-06 has 5 calls: call_1 (conn, renewed), call_2 (conn, renewed), call_3 (conn, overridden->not_interested),
    # call_4 (conn, callback_requested), call_5 (failed) -> total=5, connected=4, rate=0.8000, success_leads=1 (only L1!)
    # 2026-10-07 has 1 call: call_6 (failed) -> total=1, connected=0, rate=0.0000, success_leads=0
    assert daily_rows == [
        {
            "workflow_id": "motor_renewal",
            "date": "2026-10-06",
            "total_calls": "5",
            "connected_calls": "4",
            "connectivity_rate": "0.8000",
            "success_leads": "1",
        },
        {
            "workflow_id": "motor_renewal",
            "date": "2026-10-07",
            "total_calls": "1",
            "connected_calls": "0",
            "connectivity_rate": "0.0000",
            "success_leads": "0",
        },
    ]

    with (rep_dir / "batch_summary.csv").open("r", encoding="utf-8", newline="") as f:
        batch_rows = list(csv.DictReader(f))
    assert batch_rows == [
        {
            "workflow_id": "motor_renewal",
            "batch_id": "batch_001",
            "total_leads": "4",
            "yet_to_reach_out": "1",
            "in_progress_telephony_failed": "1",
            "in_progress_dialed": "1",
            "completed": "1",
        },
        {
            "workflow_id": "motor_renewal",
            "batch_id": "batch_002",
            "total_leads": "1",
            "yet_to_reach_out": "0",
            "in_progress_telephony_failed": "0",
            "in_progress_dialed": "0",
            "completed": "1",
        },
    ]


def test_multiple_overrides_and_timestamp_tie_breaking(tmp_path: Path, make_config) -> None:
    cfg = make_config("overrides.db")
    drop = tmp_path / "drop_1"
    write_lead_csv(
        drop / "leads" / "motor_renewal" / "batch_001.csv",
        ["customer_name", "phone", "policy_number"],
        [
            ["Lead A", "9111111111", "MOT/A"],
            ["Lead B", "9222222222", "MOT/B"],
        ],
    )
    write_events_jsonl(
        drop / "events" / "events_0001.jsonl",
        [
            # Lead A: two overrides at different times -> latest occurred_at wins (renewed)
            {"event_id": "a1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "ca", "occurred_at": "2026-10-06T09:00:00Z", "phone": "9111111111", "payload": {}},
            {"event_id": "a2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "ca", "occurred_at": "2026-10-06T09:00:10Z", "phone": "9111111111", "payload": {}},
            {"event_id": "a3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "ca", "occurred_at": "2026-10-06T09:01:00Z", "phone": "9111111111", "payload": {"disposition_code": "callback_requested"}},
            {"event_id": "a4", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "ca", "occurred_at": "2026-10-06T12:00:00Z", "payload": {"disposition_code": "renewed"}},
            {"event_id": "a5", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "ca", "occurred_at": "2026-10-06T10:00:00Z", "payload": {"disposition_code": "not_interested"}},
            # Lead B: two overrides at IDENTICAL occurred_at -> deterministic tie-breaker by event_id ("b_ov_2" > "b_ov_1")
            {"event_id": "b1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "cb", "occurred_at": "2026-10-06T09:00:00Z", "phone": "9222222222", "payload": {}},
            {"event_id": "b2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "cb", "occurred_at": "2026-10-06T09:00:10Z", "phone": "9222222222", "payload": {}},
            {"event_id": "b_ov_2", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "cb", "occurred_at": "2026-10-06T10:00:00Z", "payload": {"disposition_code": "dnd"}},
            {"event_id": "b_ov_1", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "cb", "occurred_at": "2026-10-06T10:00:00Z", "payload": {"disposition_code": "renewed"}},
        ],
    )
    run_ingest(cfg, [drop])
    rep = tmp_path / "reports"
    run_report(cfg, rep)

    with (rep / "lead_report.csv").open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["external_ref"] == "MOT/A"
    assert rows[0]["current_disposition"] == "renewed"
    assert rows[0]["ever_success"] == "true"

    assert rows[1]["external_ref"] == "MOT/B"
    assert rows[1]["current_disposition"] == "dnd"
    assert rows[1]["ever_success"] == "false"


def test_cross_workflow_call_id_isolation_out_of_order_and_multiple_sets(
    tmp_path: Path, make_config
) -> None:
    cfg = make_config("adv_events.db")
    drop = tmp_path / "drop_1"

    write_lead_csv(
        drop / "leads" / "motor_renewal" / "batch_001.csv",
        ["customer_name", "phone", "policy_number"],
        [["Motor Lead", "9333333333", "MOT/SHARED"]],
    )
    write_lead_csv(
        drop / "leads" / "policy_feedback" / "batch_001.csv",
        ["customer_name", "phone", "order_id"],
        [["Feedback Lead", "9444444444", "ORD-SHARED"]],
    )

    # Same call_id "call_shared" appears in BOTH workflows!
    # Also test: out-of-order events (call.ended -> disposition.set -> call.connected -> call.initiated),
    # multiple disposition.set (latest occurred_at wins), unknown workflow orphan, invalid phone orphan,
    # and 23:59Z -> next day IST.
    write_events_jsonl(
        drop / "events" / "events_0001.jsonl",
        [
            "",  # empty JSONL line -> ignored and NOT counted in events_total
            # Motor call_shared: reverse chronological arrival, 2 disposition.set events (latest is renewed)
            {"event_id": "m_end", "event_type": "call.ended", "workflow_id": "motor_renewal", "call_id": "call_shared", "occurred_at": "2026-10-05T23:59:30Z", "phone": "9333333333", "payload": {"duration_sec": 25, "end_reason": "completed"}},
            {"event_id": "m_set_late", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_shared", "occurred_at": "2026-10-06T00:05:00Z", "phone": "9333333333", "payload": {"disposition_code": "renewed"}},
            {"event_id": "m_set_early", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_shared", "occurred_at": "2026-10-06T00:01:00Z", "phone": "9333333333", "payload": {"disposition_code": "callback_requested"}},
            {"event_id": "m_conn", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_shared", "occurred_at": "2026-10-05T23:59:05Z", "phone": "9333333333", "payload": {}},
            {"event_id": "m_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_shared", "occurred_at": "2026-10-05T23:59:00Z", "phone": "9333333333", "payload": {}},
            # Policy feedback call_shared: override MUST link ONLY to policy_feedback lead, NOT motor_renewal!
            {"event_id": "p_ov", "event_type": "disposition.override", "workflow_id": "policy_feedback", "call_id": "call_shared", "occurred_at": "2026-10-06T12:00:00+05:30", "payload": {"disposition_code": "feedback_captured", "overridden_by": "qa", "reason": "fix"}},
            {"event_id": "p_init", "event_type": "call.initiated", "workflow_id": "policy_feedback", "call_id": "call_shared", "occurred_at": "2026-10-06 00:00:00", "phone": "9444444444", "payload": {}},
            {"event_id": "p_conn", "event_type": "call.connected", "workflow_id": "policy_feedback", "call_id": "call_shared", "occurred_at": "2026-10-06 00:00:10", "phone": "9444444444", "payload": {}},
            # Unknown workflow event -> orphan (must NOT fail FK)
            {"event_id": "unk_wf", "event_type": "call.initiated", "workflow_id": "unknown_wf", "call_id": "c_unk", "occurred_at": "2026-10-06T10:00:00Z", "phone": "9333333333", "payload": {}},
            # Invalid phone event -> orphan
            {"event_id": "bad_ph", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c_bad", "occurred_at": "2026-10-06T10:00:00Z", "phone": "022-2345-6789", "payload": {}},
        ],
    )

    run_ingest(cfg, [drop])
    rep = tmp_path / "reports"
    dq = run_report(cfg, rep)

    assert dq["events_total"] == 10  # blank line excluded
    assert dq["orphan_events"] == 2  # unk_wf + bad_ph

    with (rep / "lead_report.csv").open("r", encoding="utf-8", newline="") as f:
        lead_rows = list(csv.DictReader(f))
    assert lead_rows == [
        {
            "workflow_id": "motor_renewal",
            "external_ref": "MOT/SHARED",
            "batch_id": "batch_001",
            "phone_masked": "+91******3333",
            "bucket": "completed",
            "total_calls": "1",
            "connected_calls": "1",
            "last_call_at": "2026-10-06T05:29:00+05:30",
            "current_disposition": "renewed",
            "ever_success": "true",
        },
        {
            "workflow_id": "policy_feedback",
            "external_ref": "ORD-SHARED",
            "batch_id": "batch_001",
            "phone_masked": "+91******4444",
            "bucket": "completed",
            "total_calls": "1",
            "connected_calls": "1",
            "last_call_at": "2026-10-06T00:00:00+05:30",
            "current_disposition": "feedback_captured",
            "ever_success": "true",
        },
    ]

    with (rep / "daily_overview.csv").open("r", encoding="utf-8", newline="") as f:
        daily_rows = list(csv.DictReader(f))
    assert daily_rows == [
        {
            "workflow_id": "motor_renewal",
            "date": "2026-10-06",
            "total_calls": "1",
            "connected_calls": "1",
            "connectivity_rate": "1.0000",
            "success_leads": "1",
        },
        {
            "workflow_id": "policy_feedback",
            "date": "2026-10-06",
            "total_calls": "1",
            "connected_calls": "1",
            "connectivity_rate": "1.0000",
            "success_leads": "1",
        },
    ]


