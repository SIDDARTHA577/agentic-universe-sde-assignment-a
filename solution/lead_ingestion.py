"""Lead CSV ingestion, validation, normalization, rejection tracking, and duplicate merging."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from solution.db import resolve_event_links, utc_now_iso
from solution.normalizer import build_header_mapping, normalize_field_value
from solution.schemas import WorkflowSchema
from solution.security import PIIProtector


@dataclass(frozen=True)
class LeadFileStats:
    """Summary counts for a single lead CSV file ingestion."""

    file_path: Path
    skipped_as_duplicate_file: bool
    rows_total: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    duplicates_merged: int = 0
    field_warnings: int = 0
    events_linked: int = 0


def file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_phone_field_name(schema: WorkflowSchema) -> str:
    for field in schema.fields:
        if field.field_type == "phone":
            return field.name
    raise ValueError(f"Workflow {schema.workflow_id} has no phone field.")


def ingest_lead_file(
    conn: sqlite3.Connection,
    csv_path: Path,
    schema: WorkflowSchema,
    protector: PIIProtector,
) -> LeadFileStats:
    """Ingest a single lead CSV file inside an atomic SQLite transaction."""
    resolved_path = str(csv_path.resolve())
    checksum = file_sha256(csv_path)

    existing_file = conn.execute(
        "SELECT id, file_sha256, status FROM ingested_files WHERE file_path = ?",
        (resolved_path,),
    ).fetchone()
    if existing_file is not None:
        if existing_file["file_sha256"] == checksum and existing_file["status"] == "completed":
            return LeadFileStats(file_path=csv_path, skipped_as_duplicate_file=True)
        raise ValueError(
            f"Lead file '{csv_path.name}' was already ingested with a different checksum."
        )

    batch_id = csv_path.stem
    phone_field_name = _find_phone_field_name(schema)
    now_iso = utc_now_iso()

    rows_total = 0
    rows_accepted = 0
    rows_rejected = 0
    duplicates_merged = 0
    field_warnings = 0

    conn.execute("BEGIN")
    try:
        cur_file = conn.execute(
            """
            INSERT INTO ingested_files (
                file_path, file_kind, workflow_id, batch_id, file_sha256,
                status, ingested_at
            ) VALUES (?, 'leads', ?, ?, ?, 'completed', ?)
            """,
            (resolved_path, schema.workflow_id, batch_id, checksum, now_iso),
        )
        file_id = int(cur_file.lastrowid)

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is not None:
                col_map = build_header_mapping(header, schema)
                row_number = 0
                for raw_row in reader:
                    if not raw_row or all(cell.strip() == "" for cell in raw_row):
                        continue
                    row_number += 1
                    rows_total += 1

                    parsed_values: dict[str, str | None] = {}
                    rejection_reason: str | None = None
                    row_warning_count = 0

                    for field in schema.fields:
                        col_idx = col_map.get(field.name)
                        raw_val = (
                            raw_row[col_idx].strip()
                            if (col_idx is not None and col_idx < len(raw_row))
                            else ""
                        )

                        if raw_val == "":
                            if field.required and rejection_reason is None:
                                rejection_reason = f"missing_required:{field.name}"
                            parsed_values[field.name] = None
                            continue

                        normalized = normalize_field_value(raw_val, field)
                        if normalized is None:
                            if field.required:
                                if rejection_reason is None:
                                    rejection_reason = (
                                        "invalid_phone"
                                        if field.field_type == "phone"
                                        else f"invalid_{field.name}"
                                    )
                            else:
                                row_warning_count += 1
                            parsed_values[field.name] = None
                        else:
                            parsed_values[field.name] = normalized

                    if rejection_reason is not None:
                        rows_rejected += 1
                        conn.execute(
                            """
                            INSERT INTO lead_rejections (
                                file_id, workflow_id, batch_id, row_number, reason
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (file_id, schema.workflow_id, batch_id, row_number, rejection_reason),
                        )
                        continue

                    rows_accepted += 1
                    field_warnings += row_warning_count

                    canonical_phone = parsed_values[phone_field_name]
                    assert canonical_phone is not None
                    phone_hmac = protector.phone_token(schema.workflow_id, canonical_phone)
                    phone_encrypted = protector.encrypt(canonical_phone)
                    external_ref = parsed_values[schema.reference_field]
                    assert external_ref is not None

                    existing_lead = conn.execute(
                        """
                        SELECT id FROM leads
                        WHERE workflow_id = ? AND phone_hmac = ?
                        """,
                        (schema.workflow_id, phone_hmac),
                    ).fetchone()

                    if existing_lead is None:
                        cur_lead = conn.execute(
                            """
                            INSERT INTO leads (
                                workflow_id, phone_hmac, phone_encrypted, batch_id,
                                external_ref, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                schema.workflow_id,
                                phone_hmac,
                                phone_encrypted,
                                batch_id,
                                external_ref,
                                now_iso,
                                now_iso,
                            ),
                        )
                        lead_id = int(cur_lead.lastrowid)
                        for field in schema.fields:
                            val = parsed_values[field.name]
                            if field.pii:
                                enc_val = protector.encrypt(val) if val is not None else None
                                conn.execute(
                                    """
                                    INSERT INTO lead_field_values (
                                        lead_id, field_name, is_pii, value_text, value_encrypted
                                    ) VALUES (?, ?, 1, NULL, ?)
                                    """,
                                    (lead_id, field.name, enc_val),
                                )
                            else:
                                conn.execute(
                                    """
                                    INSERT INTO lead_field_values (
                                        lead_id, field_name, is_pii, value_text, value_encrypted
                                    ) VALUES (?, ?, 0, ?, NULL)
                                    """,
                                    (lead_id, field.name, val),
                                )
                    else:
                        duplicates_merged += 1
                        lead_id = int(existing_lead["id"])
                        conn.execute(
                            """
                            UPDATE leads
                            SET external_ref = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (external_ref, now_iso, lead_id),
                        )
                        for field in schema.fields:
                            val = parsed_values[field.name]
                            if val is None:
                                continue
                            if field.pii:
                                enc_val = protector.encrypt(val)
                                conn.execute(
                                    """
                                    INSERT INTO lead_field_values (
                                        lead_id, field_name, is_pii, value_text, value_encrypted
                                    ) VALUES (?, ?, 1, NULL, ?)
                                    ON CONFLICT(lead_id, field_name) DO UPDATE SET
                                        value_text = NULL,
                                        value_encrypted = excluded.value_encrypted
                                    """,
                                    (lead_id, field.name, enc_val),
                                )
                            else:
                                conn.execute(
                                    """
                                    INSERT INTO lead_field_values (
                                        lead_id, field_name, is_pii, value_text, value_encrypted
                                    ) VALUES (?, ?, 0, ?, NULL)
                                    ON CONFLICT(lead_id, field_name) DO UPDATE SET
                                        value_text = excluded.value_text,
                                        value_encrypted = NULL
                                    """,
                                    (lead_id, field.name, val),
                                )

        conn.execute(
            """
            UPDATE ingested_files
            SET rows_total = ?,
                rows_rejected = ?,
                duplicates_merged = ?,
                field_warnings = ?
            WHERE id = ?
            """,
            (rows_total, rows_rejected, duplicates_merged, field_warnings, file_id),
        )
        linked_phone, linked_call = resolve_event_links(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return LeadFileStats(
        file_path=csv_path,
        skipped_as_duplicate_file=False,
        rows_total=rows_total,
        rows_accepted=rows_accepted,
        rows_rejected=rows_rejected,
        duplicates_merged=duplicates_merged,
        field_warnings=field_warnings,
        events_linked=linked_phone + linked_call,
    )


def get_effective_lead_fields(
    conn: sqlite3.Connection,
    lead_id: int,
    schema: WorkflowSchema,
    protector: PIIProtector,
) -> dict[str, str | None]:
    """Return merged lead field values with schema defaults applied to still-null fields."""
    rows = conn.execute(
        """
        SELECT field_name, is_pii, value_text, value_encrypted
        FROM lead_field_values
        WHERE lead_id = ?
        """,
        (lead_id,),
    ).fetchall()
    stored: dict[str, str | None] = {}
    for row in rows:
        if row["is_pii"]:
            enc = row["value_encrypted"]
            stored[row["field_name"]] = protector.decrypt(enc) if enc is not None else None
        else:
            stored[row["field_name"]] = row["value_text"]

    effective: dict[str, str | None] = {}
    for field in schema.fields:
        val = stored.get(field.name)
        if val is None and field.default is not None:
            val = field.default
        effective[field.name] = val
    return effective

