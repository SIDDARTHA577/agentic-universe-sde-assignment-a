"""SQLite database connection management, schema initialization, and event linking."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from solution.schemas import SchemaRegistry

SCHEMA_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    reference_field TEXT NOT NULL,
    schema_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success INTEGER NOT NULL CHECK (is_success IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
);

CREATE TABLE IF NOT EXISTS ingested_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    file_kind TEXT NOT NULL CHECK (file_kind IN ('leads', 'events')),
    workflow_id TEXT,
    batch_id TEXT,
    file_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed')),
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    duplicates_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    events_total INTEGER NOT NULL DEFAULT 0,
    malformed_events INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
    phone_hmac TEXT NOT NULL,
    phone_encrypted BLOB NOT NULL,
    batch_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workflow_id, phone_hmac)
);

CREATE INDEX IF NOT EXISTS idx_leads_wf_ref
    ON leads (workflow_id, external_ref);

CREATE INDEX IF NOT EXISTS idx_leads_wf_batch
    ON leads (workflow_id, batch_id);

CREATE TABLE IF NOT EXISTS lead_field_values (
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    is_pii INTEGER NOT NULL CHECK (is_pii IN (0, 1)),
    value_text TEXT,
    value_encrypted BLOB,
    PRIMARY KEY (lead_id, field_name),
    CHECK ((is_pii = 1 AND value_text IS NULL) OR is_pii = 0)
);

CREATE TABLE IF NOT EXISTS lead_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES ingested_files(id),
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL CHECK (row_number >= 1),
    reason TEXT NOT NULL,
    UNIQUE (file_id, row_number)
);

CREATE INDEX IF NOT EXISTS idx_rejections_sort
    ON lead_rejections (workflow_id, batch_id, row_number);

CREATE TABLE IF NOT EXISTS interaction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    file_id INTEGER NOT NULL REFERENCES ingested_files(id),
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    occurred_at_ist TEXT NOT NULL,
    occurred_date_ist TEXT NOT NULL,
    phone_hmac TEXT,
    disposition_code TEXT,
    payload_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_wf_phone
    ON interaction_events (workflow_id, phone_hmac);

CREATE INDEX IF NOT EXISTS idx_events_wf_call
    ON interaction_events (workflow_id, call_id);

CREATE INDEX IF NOT EXISTS idx_events_type
    ON interaction_events (event_type);

CREATE TRIGGER IF NOT EXISTS trg_interaction_events_no_update
BEFORE UPDATE ON interaction_events
BEGIN
    SELECT RAISE(ABORT, 'interaction_events is append-only and cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS trg_interaction_events_no_delete
BEFORE DELETE ON interaction_events
BEGIN
    SELECT RAISE(ABORT, 'interaction_events is append-only and cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS event_links (
    event_id TEXT PRIMARY KEY REFERENCES interaction_events(event_id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    linked_via TEXT NOT NULL CHECK (linked_via IN ('phone', 'call_id')),
    linked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_links_lead
    ON event_links (lead_id);
"""


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection with explicit transaction control."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def initialize_database(conn: sqlite3.Connection, registry: SchemaRegistry) -> None:
    """Create schema tables/indexes/triggers and sync workflow & disposition metadata."""
    conn.executescript(SCHEMA_DDL)
    conn.execute("BEGIN")
    try:
        for wf_id, schema in registry.workflows.items():
            conn.execute(
                """
                INSERT INTO workflows (workflow_id, description, reference_field, schema_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    description = excluded.description,
                    reference_field = excluded.reference_field,
                    schema_json = excluded.schema_json
                """,
                (wf_id, schema.description, schema.reference_field, schema.raw_json),
            )

        for wf_id, codes in registry.dispositions.items():
            for code, spec in codes.items():
                conn.execute(
                    """
                    INSERT INTO dispositions (workflow_id, code, label, is_terminal, is_success)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id, code) DO UPDATE SET
                        label = excluded.label,
                        is_terminal = excluded.is_terminal,
                        is_success = excluded.is_success
                    """,
                    (
                        wf_id,
                        code,
                        spec.label,
                        1 if spec.is_terminal else 0,
                        1 if spec.is_success else 0,
                    ),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def resolve_event_links(conn: sqlite3.Connection) -> tuple[int, int]:
    """Link unresolved events to leads via phone HMAC first, then call_id for overrides."""
    now_iso = utc_now_iso()

    cur_phone = conn.execute(
        """
        INSERT OR IGNORE INTO event_links (event_id, lead_id, linked_via, linked_at)
        SELECT e.event_id, l.id, 'phone', ?
        FROM interaction_events e
        JOIN leads l
          ON l.workflow_id = e.workflow_id
         AND l.phone_hmac = e.phone_hmac
        LEFT JOIN event_links el
          ON el.event_id = e.event_id
        WHERE el.event_id IS NULL
          AND e.phone_hmac IS NOT NULL
        """,
        (now_iso,),
    )

    cur_call = conn.execute(
        """
        INSERT OR IGNORE INTO event_links (event_id, lead_id, linked_via, linked_at)
        SELECT e.event_id, peer.lead_id, 'call_id', ?
        FROM interaction_events e
        JOIN (
            SELECT ie.workflow_id, ie.call_id, MIN(el.lead_id) AS lead_id
            FROM interaction_events ie
            JOIN event_links el
              ON el.event_id = ie.event_id
            GROUP BY ie.workflow_id, ie.call_id
        ) peer
          ON peer.workflow_id = e.workflow_id
         AND peer.call_id = e.call_id
        LEFT JOIN event_links existing
          ON existing.event_id = e.event_id
        WHERE existing.event_id IS NULL
          AND e.event_type = 'disposition.override'
        """,
        (now_iso,),
    )

    return cur_phone.rowcount, cur_call.rowcount

