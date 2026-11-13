"""JSONL event ingestion into the append-only interaction ledger."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solution.db import resolve_event_links, utc_now_iso
from solution.lead_ingestion import file_sha256
from solution.normalizer import (
    format_ist_iso,
    format_utc_sort_key,
    normalize_phone,
    parse_timestamp,
)
from solution.security import PIIProtector

VALID_EVENT_TYPES = frozenset(
    {
        "call.initiated",
        "call.telephony_failed",
        "call.connected",
        "call.ended",
        "disposition.set",
        "disposition.override",
    }
)


@dataclass(frozen=True)
class EventFileStats:
    """Summary counts for a single event JSONL file ingestion."""

    file_path: Path
    skipped_as_duplicate_file: bool
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0
    events_inserted: int = 0
    events_linked: int = 0


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def ingest_event_file(
    conn: sqlite3.Connection,
    jsonl_path: Path,
    protector: PIIProtector,
) -> EventFileStats:
    """Ingest one JSONL event file into the append-only interaction_events table."""
    resolved_path = str(jsonl_path.resolve())
    checksum = file_sha256(jsonl_path)

    existing_file = conn.execute(
        "SELECT id, file_sha256, status FROM ingested_files WHERE file_path = ?",
        (resolved_path,),
    ).fetchone()
    if existing_file is not None:
        if existing_file["file_sha256"] == checksum and existing_file["status"] == "completed":
            return EventFileStats(file_path=jsonl_path, skipped_as_duplicate_file=True)
        raise ValueError(
            f"Event file '{jsonl_path.name}' was already ingested with a different checksum."
        )

    now_iso = utc_now_iso()
    events_total = 0
    malformed_events = 0
    duplicate_events_dropped = 0
    events_inserted = 0

    conn.execute("BEGIN")
    try:
        cur_file = conn.execute(
            """
            INSERT INTO ingested_files (
                file_path, file_kind, workflow_id, batch_id, file_sha256,
                status, ingested_at
            ) VALUES (?, 'events', NULL, ?, ?, 'completed', ?)
            """,
            (resolved_path, jsonl_path.stem, checksum, now_iso),
        )
        file_id = int(cur_file.lastrowid)

        with jsonl_path.open("r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                events_total += 1

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    malformed_events += 1
                    continue

                if not isinstance(data, dict):
                    malformed_events += 1
                    continue

                event_id = _non_empty_str(data.get("event_id"))
                event_type = _non_empty_str(data.get("event_type"))
                workflow_id = _non_empty_str(data.get("workflow_id"))
                call_id = _non_empty_str(data.get("call_id"))
                occurred_at_raw = _non_empty_str(data.get("occurred_at"))

                if (
                    event_id is None
                    or event_type is None
                    or event_type not in VALID_EVENT_TYPES
                    or workflow_id is None
                    or call_id is None
                    or occurred_at_raw is None
                ):
                    malformed_events += 1
                    continue

                try:
                    dt_utc, dt_ist = parse_timestamp(occurred_at_raw)
                except ValueError:
                    malformed_events += 1
                    continue

                occurred_at_utc = format_utc_sort_key(dt_utc)
                occurred_at_ist = format_ist_iso(dt_ist)
                occurred_date_ist = dt_ist.date().isoformat()

                phone_hmac: str | None = None
                if event_type != "disposition.override":
                    phone_raw = _non_empty_str(data.get("phone"))
                    if phone_raw is not None:
                        canonical_phone = normalize_phone(phone_raw)
                        if canonical_phone is not None:
                            phone_hmac = protector.phone_token(workflow_id, canonical_phone)

                payload = data.get("payload")
                sanitized_payload = protector.sanitize_payload(payload)
                disposition_code: str | None = None
                if event_type in ("disposition.set", "disposition.override"):
                    disposition_code = _non_empty_str(sanitized_payload.get("disposition_code"))

                payload_json = json.dumps(
                    sanitized_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )

                cur_ins = conn.execute(
                    """
                    INSERT INTO interaction_events (
                        event_id, file_id, event_type, workflow_id, call_id,
                        occurred_at_utc, occurred_at_ist, occurred_date_ist,
                        phone_hmac, disposition_code, payload_json, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    (
                        event_id,
                        file_id,
                        event_type,
                        workflow_id,
                        call_id,
                        occurred_at_utc,
                        occurred_at_ist,
                        occurred_date_ist,
                        phone_hmac,
                        disposition_code,
                        payload_json,
                        now_iso,
                    ),
                )
                if cur_ins.rowcount == 0:
                    duplicate_events_dropped += 1
                else:
                    events_inserted += 1

        conn.execute(
            """
            UPDATE ingested_files
            SET events_total = ?,
                malformed_events = ?,
                duplicate_events_dropped = ?
            WHERE id = ?
            """,
            (events_total, malformed_events, duplicate_events_dropped, file_id),
        )
        linked_phone, linked_call = resolve_event_links(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return EventFileStats(
        file_path=jsonl_path,
        skipped_as_duplicate_file=False,
        events_total=events_total,
        malformed_events=malformed_events,
        duplicate_events_dropped=duplicate_events_dropped,
        events_inserted=events_inserted,
        events_linked=linked_phone + linked_call,
    )

