# Submission

## Candidate
- **Name:** Sai Siddartha Gorantla
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** Approximately 6–7 hours

## How to run
Requires Python 3.11 or newer on Windows PowerShell (run from the repository root):

```powershell
# 1. Create and activate a virtual environment, then install dependencies
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Generate local encryption and HMAC secrets (.env is gitignored)
#    Alternatively, set LEDGER_ENCRYPTION_KEY and LEDGER_HMAC_KEY in your environment.
python -m solution.cli init-env

# 3. Generate synthetic test data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest drops incrementally and generate reports
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2
python -m solution.cli report --out reports

# 5. Run the automated test suite
python -m pytest -v
```

Equivalent combined ingestion (`python -m solution.cli ingest data`) produces byte-identical reports.

## Design
The solution is a modular Python 3.11+ pipeline backed by SQLite (`solution/`) structured into focused components: CLI entry point (`cli.py`), runtime configuration & secret validation (`config.py`), dynamic schema loading (`schemas.py`), PII cryptography & masking (`security.py`), field normalization (`normalizer.py`), transactional SQLite operations (`db.py`), atomic CSV lead ingestion & merging (`lead_ingestion.py`), append-only JSONL event ledger (`event_ingestion.py`), metric derivation & export (`reporting.py`), and end-to-end orchestration (`pipeline.py`).

```text
CSV Lead Drops ──▶ lead_ingestion.py ──┐
                                       ├──▶ SQLite (ledger.db) ──▶ reporting.py ──▶ 5 Contract Reports
JSONL Events   ──▶ event_ingestion.py ─┘         ▲
                        │                        │
                        └── resolve_event_links ─┘
```

### Architectural Highlights
1. **Dynamic Workflow Schemas:** Schemas (`motor_renewal.json`, `policy_feedback.json`) and `dispositions.csv` are loaded dynamically at runtime without hardcoding column names or workflows.
2. **Why SQLite:** SQLite is embedded, zero-configuration, ACID-compliant, supports transactional rollbacks and triggers, and is well-suited for local batch processing and auditability without running background database daemon processes.
3. **Encrypted PII & Searchable HMAC Tokens:** Schema fields marked `"pii": true` (`customer_name`, `phone`) are encrypted at rest using AES-128-CBC authenticated encryption (`cryptography.fernet.Fernet`). To match events to leads by phone without plaintext phone storage, phone numbers are tokenized using keyed HMAC-SHA256 scoped by workflow ID (`workflow_id + "\x00" + canonical_phone`). A database `CHECK ((is_pii = 1 AND value_text IS NULL) OR is_pii = 0)` constraint prevents plaintext storage in the database.
4. **Append-Only Ledger & Event Linking:** `interaction_events` records raw call and disposition events. SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers enforce strict immutability. Link state is maintained in a dedicated `event_links` table (`event_id -> lead_id`). This allows late-arriving leads (`drop_1` event arriving before `drop_2` lead) and phone-free supervisor overrides (`disposition.override` linking via `(workflow_id, call_id)`) to resolve idempotently without modifying historical event rows.
5. **Database Idempotency:** Completed files are tracked in `ingested_files` by resolved path, file kind, SHA-256 checksum, and atomic ingestion counters. Re-ingesting an already completed file is a safe no-op. If an already ingested file path is re-run with modified content (different checksum), ingestion safely aborts with an error.

### Database Schema
- **`workflows` & `dispositions`:** Syncs workflow definitions and disposition outcomes (`is_terminal`, `is_success`).
- **`ingested_files`:** Records file path, kind (`leads`/`events`), batch ID, SHA-256 checksum, completion status, and per-file counters (`rows_total`, `rows_rejected`, `duplicates_merged`, `field_warnings`, `events_total`, `malformed_events`, `duplicate_events_dropped`).
- **`leads`:** Unique per `(workflow_id, phone_hmac)`. Stores `phone_encrypted`, permanent first-seen `batch_id`, and current `external_ref`.
- **`lead_field_values`:** Key-value attributes per `(lead_id, field_name)` with strict PII CHECK constraint.
- **`lead_rejections`:** Records rejected rows with non-PII reasons and 1-based non-blank row numbers.
- **`interaction_events`:** Append-only ledger with `UNIQUE(event_id)`, microsecond UTC sort keys, IST dates, sanitized payload JSON, and update/delete triggers.
- **`event_links`:** Maps `event_id` to `lead_id` (`linked_via` in `'phone'`, `'call_id'`).

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| SQLite for local persistence | Embedded, zero external setup, reliable ACID transactions, triggers, and reproducible evaluation on Windows | PostgreSQL via Docker Compose |
| Fernet symmetric encryption for recoverable PII | Authenticated encryption (AES-128-CBC + HMAC-SHA256) protects customer names and phone numbers at rest | Plain hashing (irreversible, cannot produce masked phone for reports) |
| Workflow-scoped HMAC-SHA256 tokens for phone matching | Enables exact indexed lookups while preventing cross-workflow phone correlation | Global phone hashing without workflow scoping |
| Separate `event_links` table | Preserves append-only immutability of `interaction_events` while resolving late-arriving leads and `call_id` overrides | Updating a nullable foreign key on `interaction_events` directly |
| File SHA-256 checksum tracking in `ingested_files` | Guarantees idempotent re-runs of identical files while detecting modified files | Tracking file modification times or paths alone |
| Deterministic tie-breaking for dispositions and calls | Dispositions break timestamp ties by `(occurred_at_utc, event_id)`; most recent call uses `(initiated_utc, call_id)` | Relying on database insertion order |
| Defer schema defaults until after duplicate merging | Prevents batch-level defaults (e.g. `preferred_language="en"`) from overwriting valid values from earlier batches | Applying defaults on initial row insert |
| Rejection of changed files at an already-ingested path | Prevents silent data drift or broken idempotency if an input file is mutated in place | Silently skipping or overwriting previously ingested data |
| No foreign key constraint on `interaction_events.workflow_id` | Allows events with unknown workflow IDs to be safely recorded and counted as orphans per spec precedence | Strict foreign key constraint causing premature transaction failure |

## Failure modes considered
- **Malformed JSONL records:** Lines with invalid JSON or missing required metadata (`event_id`, `event_type`, `workflow_id`, `call_id`, `occurred_at`) are counted as `malformed_events` and skipped without aborting ingestion.
- **Invalid CSV rows & blank rows:** Completely blank lines are skipped without advancing row counts. Non-blank rows are evaluated in schema order; the first required field failure records a rejection reason (`missing_required:<field>` or `invalid_phone`). Optional invalid fields store `null` and increment `field_warnings` only on accepted rows.
- **Duplicate leads:** Leads with identical `(workflow_id, canonical_phone)` merge non-null fields (last write wins), increment `duplicate_lead_rows_merged`, and permanently retain the first-seen `batch_id`.
- **Duplicate & orphan events:** Duplicate `event_id`s across lines or files are dropped and counted in `duplicate_events_dropped`. Events that cannot be linked to an accepted lead remain in the ledger as orphans (`orphan_events`) and are excluded from business reports.
- **Late-arriving leads:** When an event arrives before its lead, it is initially recorded as an orphan. As soon as the lead is ingested in a subsequent drop, `resolve_event_links` links the event, making it reportable and updating data quality metrics.
- **Supervisor overrides:** `disposition.override` events lack phone numbers and link via `(workflow_id, call_id)` to the corresponding call. Overrides never edit or delete original events.
- **Unknown dispositions:** Disposition codes not present in `dispositions.csv` for that workflow are counted as `unknown_disposition_events` and ignored during effective disposition calculation.
- **Transaction boundaries & crash safety:** Each file is ingested inside an explicit SQLite `BEGIN ... COMMIT` transaction. Any unexpected read or parsing failure triggers a rollback, preventing partial or corrupted file states.
- **Repeated ingestion:** Re-ingesting drops checks file checksums and `event_id` uniqueness, ensuring strict idempotency.
- **Secret configuration:** Missing or placeholder encryption keys fail immediately with a descriptive `ConfigurationError` rather than generating ephemeral keys or corrupting state.
- **Known limitations:** In-place modification of an already-ingested file at the same path requires a database reset; large-scale dataset benchmarking (`--scale large`) was not executed.

## Testing
The automated test suite (`tests/`) contains 77 tests run via `python -m pytest -v`:
- `tests/test_normalizer.py`: Phone normalization/rejection across all 6 formats and starting digits `6`–`9`, all 4 date formats, day-first vs US month interpretation, leap years, invalid dates, currency prefix and `Decimal` (`ROUND_HALF_UP`) parsing, case-insensitive enums, header aliases with consecutive underscores/spaces, and UTC/offset/naive-IST timestamp parsing (including `23:59:59Z` and `18:30:00Z` -> `00:00:00` IST rollover).
- `tests/test_security.py`: Fernet encryption round-trip, workflow-scoped HMAC tokens, phone masking (`+91******XXXX`), recursive payload sanitization, `.env` secret validation, raw SQLite database byte scanning confirming zero plaintext PII, database CHECK constraint enforcement, event ledger immutability triggers, and `PRAGMA integrity_check` / `PRAGMA foreign_key_check`.
- `tests/test_lead_ingestion.py`: Blank-row skipping, 1-based non-blank row numbering, schema-order rejection precedence, optional warnings on accepted rows only, intra-file/cross-file/cross-drop duplicate merging, cross-workflow phone isolation, Unicode names with UTF-8 BOM, permanent first `batch_id`, and post-merge defaults.
- `tests/test_event_ingestion_and_reporting.py`: Handcrafted fixtures verifying malformed events, cross-drop deduplication, orphan-before-unknown-disposition precedence, late-arriving lead orphan recovery, phone-free `disposition.override` arriving before `call.initiated`, cross-workflow `call_id` isolation, out-of-order call events, multiple overrides/sets and timestamp tie-breaking, bucket precedence, exclusion of calls without `call.initiated`, distinct `success_leads`, and exact CSV outputs.
- `tests/test_pipeline_e2e.py`: End-to-end CLI runs across seeds `42`, `7`, `99`, `12345`, and `2026` comparing sequential (`drop_1` -> `drop_2`), idempotent re-ingest, intermediate-report-then-drop-2, and combined (`data/`) ingestion for byte-identical outputs, plus report invariants, mid-file rollback tests, and changed-file checksum rejection.

Clean-environment testing on Windows with Python 3.11.9 verified that all 77 tests pass cleanly.

## Results
Observed on Windows PowerShell (Python 3.11.9) with `generate_data.py --seed 42 --scale small`:
- **Execution Timings:**
  - `ingest data/drop_1`: `0.75s` (1444 accepted rows, 71 rejected, 29 merged duplicates, 4160 events inserted)
  - `ingest data/drop_2`: `0.66s` (505 accepted rows, 37 rejected, 28 merged duplicates, 4340 events inserted)
  - `report --out reports`: `0.35s`
- **Data Quality Output (`seed=42`, small):**
  - `lead_rows_total`: 2057
  - `lead_rows_rejected`: 108
  - `duplicate_lead_rows_merged`: 57
  - `field_warnings`: 122
  - `events_total`: 8745
  - `malformed_events`: 8
  - `duplicate_events_dropped`: 237
  - `orphan_events`: 462
  - `unknown_disposition_events`: 18
- **Reports Generated & Validated:**
  1. `batch_summary.csv`: Exact header, sorted by `(workflow_id, batch_id)`, bucket sums match total leads.
  2. `daily_overview.csv`: Exact header, sorted by `(workflow_id, date)` in IST, 4-decimal `connectivity_rate`, distinct `success_leads` per date.
  3. `lead_report.csv`: Exact header, sorted by `(workflow_id, external_ref)`, masked phone, `+05:30` IST timestamps, effective disposition.
  4. `rejections.csv`: Exact header, sorted by `(workflow_id, batch_id, row_number)`, zero PII.
  5. `data_quality.json`: Exact keys and metrics reflecting final linked state.
- **Automated Test Suite:** 77 passed in 22.04s (and 77 passed in 23.28s in a clean isolated virtual environment).

## If I had another day
I would prioritize testing the application with larger datasets to verify its correctness, stability and performance when processing significantly more leads and call events.

I would benchmark large-scale ingestion and reporting, identify bottlenecks and optimize database queries or batching where necessary.
