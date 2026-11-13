"""High-level drop discovery and ingestion/reporting orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from solution.config import AppConfig
from solution.db import connect, initialize_database
from solution.event_ingestion import EventFileStats, ingest_event_file
from solution.lead_ingestion import LeadFileStats, ingest_lead_file
from solution.reporting import generate_reports
from solution.schemas import load_registry
from solution.security import PIIProtector


@dataclass(frozen=True)
class IngestSummary:
    """Aggregated summary for one CLI ingest invocation."""

    lead_files: list[LeadFileStats]
    event_files: list[EventFileStats]


def discover_drops(paths: list[Path]) -> list[Path]:
    """Resolve CLI input paths into an ordered list of drop directories."""
    drops: list[Path] = []
    for raw_path in paths:
        path = raw_path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"Input path does not exist: {raw_path}")
        if not path.is_dir():
            raise ValueError(f"Expected a drop directory, got file: {raw_path}")

        if (path / "leads").is_dir() or (path / "events").is_dir():
            drops.append(path)
            continue

        child_drops = [
            child
            for child in sorted(path.iterdir(), key=lambda p: p.name)
            if child.is_dir() and ((child / "leads").is_dir() or (child / "events").is_dir())
        ]
        if not child_drops:
            raise ValueError(
                f"No 'leads' or 'events' folders found in {raw_path} or its subdirectories."
            )
        drops.extend(child_drops)
    return drops


def run_ingest(cfg: AppConfig, input_paths: list[Path]) -> IngestSummary:
    """Initialize DB if needed and ingest all discovered drops sequentially."""
    registry = load_registry(cfg.schemas_dir)
    protector = PIIProtector(cfg.secrets)
    drops = discover_drops(input_paths)

    lead_stats: list[LeadFileStats] = []
    event_stats: list[EventFileStats] = []

    conn = connect(cfg.db_path)
    try:
        initialize_database(conn, registry)

        for drop_dir in drops:
            leads_root = drop_dir / "leads"
            if leads_root.is_dir():
                for wf_dir in sorted(leads_root.iterdir(), key=lambda p: p.name):
                    if not wf_dir.is_dir():
                        continue
                    workflow_id = wf_dir.name
                    schema = registry.workflows.get(workflow_id)
                    if schema is None:
                        raise ValueError(f"Unknown workflow directory '{workflow_id}' in {leads_root}")
                    for csv_path in sorted(wf_dir.glob("*.csv"), key=lambda p: p.name):
                        lead_stats.append(ingest_lead_file(conn, csv_path, schema, protector))

            events_root = drop_dir / "events"
            if events_root.is_dir():
                for jsonl_path in sorted(events_root.glob("*.jsonl"), key=lambda p: p.name):
                    event_stats.append(ingest_event_file(conn, jsonl_path, protector))
    finally:
        conn.close()

    return IngestSummary(lead_files=lead_stats, event_files=event_stats)


def run_report(cfg: AppConfig, out_dir: Path) -> dict[str, object]:
    """Generate the five output reports from the persisted database state."""
    if not cfg.db_path.exists():
        raise FileNotFoundError(
            f"Database not found at {cfg.db_path}. Run 'python -m solution.cli ingest <drop>' first."
        )

    registry = load_registry(cfg.schemas_dir)
    protector = PIIProtector(cfg.secrets)

    conn = connect(cfg.db_path)
    try:
        initialize_database(conn, registry)
        return generate_reports(conn, out_dir.resolve(), protector)
    finally:
        conn.close()

