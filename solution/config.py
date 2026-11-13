"""Configuration and secret loading for the interaction ledger pipeline."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMAS_DIR = DEFAULT_REPO_ROOT / "assignment-a-data" / "schemas"
DEFAULT_DB_PATH = DEFAULT_REPO_ROOT / "ledger.db"
DEFAULT_ENV_FILE = DEFAULT_REPO_ROOT / ".env"

ENV_ENCRYPTION_KEY = "LEDGER_ENCRYPTION_KEY"
ENV_HMAC_KEY = "LEDGER_HMAC_KEY"


class ConfigurationError(RuntimeError):
    """Raised when required configuration or secrets are missing or invalid."""


@dataclass(frozen=True)
class SecretsConfig:
    """Validated cryptographic keys for PII protection."""

    encryption_key: bytes
    hmac_key: bytes


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration for ingestion and reporting commands."""

    db_path: Path
    schemas_dir: Path
    secrets: SecretsConfig


def parse_env_file(env_path: Path) -> dict[str, str]:
    """Parse key=value pairs from a .env file without modifying os.environ."""
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def load_secrets(env_file: Path | None = None) -> SecretsConfig:
    """Load and validate encryption and HMAC secrets from env or .env file."""
    target_env = env_file if env_file is not None else DEFAULT_ENV_FILE
    file_vars = parse_env_file(target_env)

    enc_raw = os.environ.get(ENV_ENCRYPTION_KEY) or file_vars.get(ENV_ENCRYPTION_KEY, "")
    hmac_raw = os.environ.get(ENV_HMAC_KEY) or file_vars.get(ENV_HMAC_KEY, "")

    enc_raw = enc_raw.strip()
    hmac_raw = hmac_raw.strip()

    if not enc_raw or not hmac_raw or "CHANGE_ME" in enc_raw or "CHANGE_ME" in hmac_raw:
        raise ConfigurationError(
            "Missing or placeholder PII secrets. Set LEDGER_ENCRYPTION_KEY and "
            "LEDGER_HMAC_KEY in your environment or .env file "
            "(run 'python -m solution.cli init-env' to generate a local .env file)."
        )

    try:
        enc_bytes = enc_raw.encode("utf-8")
        Fernet(enc_bytes)
    except Exception as exc:
        raise ConfigurationError(
            "Invalid LEDGER_ENCRYPTION_KEY: expected a 32-byte URL-safe base64 Fernet key."
        ) from exc

    if len(hmac_raw) < 16:
        raise ConfigurationError(
            "Invalid LEDGER_HMAC_KEY: expected at least 16 characters of secret material."
        )

    return SecretsConfig(
        encryption_key=enc_bytes,
        hmac_key=hmac_raw.encode("utf-8"),
    )


def init_env_file(env_path: Path | None = None, force: bool = False) -> Path:
    """Generate a local .env file with random Fernet and HMAC secrets."""
    target = env_path if env_path is not None else DEFAULT_ENV_FILE
    if target.exists() and not force:
        raise ConfigurationError(
            f"Refusing to overwrite existing env file at {target}. Pass --force to replace it."
        )

    enc_key = Fernet.generate_key().decode("ascii")
    hmac_key = secrets.token_hex(32)
    content = (
        "# Local secrets for Assignment A PII protection (never commit this file)\n"
        f"{ENV_ENCRYPTION_KEY}={enc_key}\n"
        f"{ENV_HMAC_KEY}={hmac_key}\n"
    )
    target.write_text(content, encoding="utf-8")
    return target


def build_config(
    db_path: Path | None = None,
    schemas_dir: Path | None = None,
    env_file: Path | None = None,
) -> AppConfig:
    """Assemble validated runtime configuration."""
    resolved_db = (db_path or DEFAULT_DB_PATH).resolve()
    resolved_schemas = (schemas_dir or DEFAULT_SCHEMAS_DIR).resolve()
    if not resolved_schemas.is_dir():
        raise ConfigurationError(f"Schemas directory not found: {resolved_schemas}")

    return AppConfig(
        db_path=resolved_db,
        schemas_dir=resolved_schemas,
        secrets=load_secrets(env_file=env_file),
    )

