"""Shared pytest fixtures and helpers for Assignment A tests."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from solution.config import AppConfig, DEFAULT_SCHEMAS_DIR, SecretsConfig


@pytest.fixture(scope="session")
def schemas_dir() -> Path:
    return DEFAULT_SCHEMAS_DIR


@pytest.fixture()
def test_secrets() -> SecretsConfig:
    return SecretsConfig(
        encryption_key=Fernet.generate_key(),
        hmac_key=b"test-hmac-secret-key-for-unit-tests-only",
    )


@pytest.fixture()
def make_config(tmp_path: Path, schemas_dir: Path, test_secrets: SecretsConfig):
    def _factory(db_name: str = "ledger.db") -> AppConfig:
        return AppConfig(
            db_path=tmp_path / db_name,
            schemas_dir=schemas_dir,
            secrets=test_secrets,
        )

    return _factory


def write_lead_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_events_jsonl(path: Path, lines: list[dict[str, Any] | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in lines:
            if isinstance(item, str):
                handle.write(item + "\n")
            else:
                handle.write(json.dumps(item) + "\n")
    return path

