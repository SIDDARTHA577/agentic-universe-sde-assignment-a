# Assignment A Solution: Interaction Ledger & Reporting Pipeline

This directory contains the Python 3.11+ implementation for Assignment A using SQLite, `cryptography` (Fernet + HMAC-SHA256), and `pytest`.

## Architecture

- [`config.py`](config.py): Resolves paths, parses `.env` configuration, loads and validates `LEDGER_ENCRYPTION_KEY` and `LEDGER_HMAC_KEY`, and provides `init_env_file`.
- [`schemas.py`](schemas.py): Dynamically loads workflow JSON schemas and `dispositions.csv` from `assignment-a-data/schemas/`.
- [`security.py`](security.py): `PIIProtector` wraps Fernet authenticated encryption for recoverable PII (`customer_name`, `phone`), workflow-scoped keyed HMAC-SHA256 tokens for phone matching, `+91******XXXX` phone masking, and event payload sanitization.
- [`normalizer.py`](normalizer.py): Header normalization and alias matching, Indian mobile normalization (`+91XXXXXXXXXX`), day-first/ISO date parsing, nonnegative `Decimal` currency parsing, case-insensitive enum parsing, and UTC/offset/naive-IST timestamp normalization.
- [`db.py`](db.py): Normalized SQLite DDL, database-level `CHECK` constraint blocking plaintext storage of `is_pii=1` fields, SQLite `BEFORE UPDATE` / `BEFORE DELETE` triggers enforcing append-only immutability on `interaction_events`, and `resolve_event_links` for late-arriving leads and phone-free `disposition.override` linking.
- [`lead_ingestion.py`](lead_ingestion.py): Atomic per-file CSV ingestion, blank-row skipping, schema-ordered validation/rejection/warnings, `(workflow_id, phone_hmac)` duplicate merging (last-write-wins for non-null fields, permanent first-seen `batch_id`), and post-merge schema default resolution.
- [`event_ingestion.py`](event_ingestion.py): Atomic per-file JSONL ingestion following the exact precedence: malformed -> duplicate (`event_id`) -> store in ledger -> link resolution (`phone_hmac` / `call_id`) -> orphan / unknown disposition classification.
- [`reporting.py`](reporting.py): Derives counted calls, effective dispositions, lead buckets, and writes the five contract files (`batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`, `data_quality.json`).
- [`pipeline.py`](pipeline.py) & [`cli.py`](cli.py): Drop discovery, orchestration, and CLI entry point (`init-env`, `ingest`, `report`).

## Windows PowerShell Quickstart

Run from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Generate local .env secrets (never committed)
python -m solution.cli init-env

# Generate synthetic data (example seed)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# Incremental ingestion and reporting
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2
python -m solution.cli report --out reports

# Run automated tests
python -m pytest -v
```

## CLI Options

- `python -m solution.cli init-env [--env-file .env] [--force]`
- `python -m solution.cli ingest <drop_or_parent_dir>... [--db ledger.db] [--schemas-dir ...] [--env-file .env]`
- `python -m solution.cli report --out <dir> [--db ledger.db] [--schemas-dir ...] [--env-file .env]`
