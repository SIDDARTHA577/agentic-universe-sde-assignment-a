"""Command-line interface for the Interaction Ledger & Reporting Pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from solution.config import (
    ConfigurationError,
    DEFAULT_DB_PATH,
    DEFAULT_ENV_FILE,
    DEFAULT_SCHEMAS_DIR,
    build_config,
    init_env_file,
)
from solution.pipeline import run_ingest, run_report


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m solution.cli",
        description="Assignment A: Interaction Ledger & Reporting Pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-env
    init_p = subparsers.add_parser(
        "init-env",
        help="Generate a local .env file with random encryption and HMAC secrets.",
    )
    init_p.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Destination .env file path (default: {DEFAULT_ENV_FILE})",
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .env file if present.",
    )

    # ingest
    ingest_p = subparsers.add_parser(
        "ingest",
        help="Ingest one or more data drop directories (or a parent data/ folder).",
    )
    ingest_p.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Drop folder(s) (e.g. data/drop_1) or parent folder containing drop_* dirs.",
    )
    ingest_p.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    ingest_p.add_argument(
        "--schemas-dir",
        type=Path,
        default=DEFAULT_SCHEMAS_DIR,
        help=f"Directory with workflow JSON schemas and dispositions.csv (default: {DEFAULT_SCHEMAS_DIR})",
    )
    ingest_p.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Path to .env file with PII secrets (default: {DEFAULT_ENV_FILE})",
    )

    # report
    report_p = subparsers.add_parser(
        "report",
        help="Generate the five contract report files from the SQLite database.",
    )
    report_p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for the five report files.",
    )
    report_p.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    report_p.add_argument(
        "--schemas-dir",
        type=Path,
        default=DEFAULT_SCHEMAS_DIR,
        help=f"Directory with workflow JSON schemas and dispositions.csv (default: {DEFAULT_SCHEMAS_DIR})",
    )
    report_p.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Path to .env file with PII secrets (default: {DEFAULT_ENV_FILE})",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point returning POSIX exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init-env":
            created = init_env_file(env_path=args.env_file, force=args.force)
            print(f"Generated local secret configuration at {created}")
            return 0

        cfg = build_config(
            db_path=args.db,
            schemas_dir=args.schemas_dir,
            env_file=args.env_file,
        )

        if args.command == "ingest":
            summary = run_ingest(cfg, args.paths)
            new_leads = sum(s.rows_accepted for s in summary.lead_files if not s.skipped_as_duplicate_file)
            rej_leads = sum(s.rows_rejected for s in summary.lead_files if not s.skipped_as_duplicate_file)
            merged_dups = sum(s.duplicates_merged for s in summary.lead_files if not s.skipped_as_duplicate_file)
            new_events = sum(s.events_inserted for s in summary.event_files if not s.skipped_as_duplicate_file)
            skipped_files = sum(
                1 for s in summary.lead_files if s.skipped_as_duplicate_file
            ) + sum(1 for s in summary.event_files if s.skipped_as_duplicate_file)

            print(
                f"Ingestion complete -> DB: {cfg.db_path} | "
                f"lead_files={len(summary.lead_files)}, event_files={len(summary.event_files)}, "
                f"skipped_idempotent_files={skipped_files}, "
                f"accepted_rows={new_leads}, rejected_rows={rej_leads}, "
                f"merged_duplicates={merged_dups}, events_inserted={new_events}"
            )
            return 0

        if args.command == "report":
            quality = run_report(cfg, args.out)
            print(
                f"Reports written to {args.out.resolve()} | "
                f"lead_rows_total={quality['lead_rows_total']}, "
                f"events_total={quality['events_total']}, "
                f"orphan_events={quality['orphan_events']}"
            )
            return 0

    except (ConfigurationError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

