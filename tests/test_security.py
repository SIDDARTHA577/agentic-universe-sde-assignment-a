"""Security, PII encryption, HMAC lookup, DB constraint, and append-only trigger tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from solution.config import ConfigurationError, init_env_file, load_secrets
from solution.db import connect, initialize_database
from solution.pipeline import run_ingest, run_report
from solution.schemas import load_registry
from solution.security import PIIProtector
from tests.conftest import write_events_jsonl, write_lead_csv


def test_pii_protector_encryption_hmac_and_masking(test_secrets) -> None:
    protector = PIIProtector(test_secrets)
    enc = protector.encrypt("Aarav Sharma")
    assert b"Aarav" not in enc
    assert protector.decrypt(enc) == "Aarav Sharma"

    tok1 = protector.phone_token("motor_renewal", "+919876543210")
    tok2 = protector.phone_token("motor_renewal", "+919876543210")
    tok_other_wf = protector.phone_token("policy_feedback", "+919876543210")
    assert tok1 == tok2
    assert tok1 != tok_other_wf
    assert "9876543210" not in tok1

    assert protector.mask_phone("+919876543210") == "+91******3210"
    assert protector.sanitize_payload(
        {
            "phone": "9876543210",
            "reason": "qa",
            "meta": {"customer_name": "Secret", "note": "ok"},
            "items": [{"mobile": "9876543210", "tag": "x"}],
        }
    ) == {"reason": "qa", "meta": {"note": "ok"}, "items": [{"tag": "x"}]}


def test_secret_loading_and_init_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("LEDGER_HMAC_KEY", raising=False)

    missing_env = tmp_path / ".env"
    with pytest.raises(ConfigurationError):
        load_secrets(env_file=missing_env)

    created = init_env_file(env_path=missing_env)
    assert created == missing_env
    loaded = load_secrets(env_file=missing_env)
    assert loaded.encryption_key
    assert loaded.hmac_key

    with pytest.raises(ConfigurationError):
        init_env_file(env_path=missing_env, force=False)


def test_db_enforces_no_plaintext_pii_and_append_only_events(
    tmp_path: Path, make_config, schemas_dir: Path
) -> None:
    cfg = make_config("security.db")
    drop_dir = tmp_path / "drop_1"
    write_lead_csv(
        drop_dir / "leads" / "motor_renewal" / "batch_001.csv",
        ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"],
        [["Zoya Kulkarni", "+91 98111 22334", "MOT/2026/000001", "2026-11-01", "5000", "en"]],
    )
    write_events_jsonl(
        drop_dir / "events" / "events_0001.jsonl",
        [
            {
                "event_id": "evt_1",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_1",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9811122334",
                "payload": {},
            },
            {
                "event_id": "evt_bad_phone",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_2",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "022-2222-3333",
                "payload": {},
            },
        ],
    )

    run_ingest(cfg, [drop_dir])
    run_report(cfg, tmp_path / "reports")

    # Scan raw SQLite database bytes for any leaked plaintext PII.
    raw_db_bytes = cfg.db_path.read_bytes()
    for forbidden in [b"Zoya", b"Kulkarni", b"9811122334", b"98111 22334", b"022-2222-3333"]:
        assert forbidden not in raw_db_bytes

    # Verify DB CHECK constraint rejects plaintext PII and triggers reject event mutation.
    conn = connect(cfg.db_path)
    try:
        initialize_database(conn, load_registry(schemas_dir))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO lead_field_values (lead_id, field_name, is_pii, value_text, value_encrypted)
                VALUES (1, 'leak_test', 1, 'plaintext_name', NULL)
                """
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE interaction_events SET event_type = 'call.ended' WHERE event_id = 'evt_1'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM interaction_events WHERE event_id = 'evt_1'")

        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

