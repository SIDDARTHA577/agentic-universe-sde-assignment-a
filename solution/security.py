"""PII encryption, HMAC lookup tokenization, masking, and payload sanitization."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from cryptography.fernet import Fernet

from solution.config import SecretsConfig

_PII_PAYLOAD_KEYS = frozenset({"phone", "mobile", "customer_name", "name", "insured_name"})


class PIIProtector:
    """Handles symmetric encryption and HMAC-SHA256 lookup tokens for PII fields."""

    def __init__(self, secrets_cfg: SecretsConfig) -> None:
        self._fernet = Fernet(secrets_cfg.encryption_key)
        self._hmac_key = secrets_cfg.hmac_key

    def encrypt(self, value: str) -> bytes:
        """Encrypt a non-empty PII string into Fernet ciphertext bytes."""
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, token: bytes | memoryview) -> str:
        """Decrypt Fernet ciphertext bytes back into a UTF-8 string."""
        raw = bytes(token) if isinstance(token, memoryview) else token
        return self._fernet.decrypt(raw).decode("utf-8")

    def phone_token(self, workflow_id: str, canonical_phone: str) -> str:
        """Compute deterministic keyed HMAC-SHA256 token scoped to workflow_id."""
        message = f"{workflow_id}\x00{canonical_phone}".encode("utf-8")
        return hmac.new(self._hmac_key, message, hashlib.sha256).hexdigest()

    @staticmethod
    def mask_phone(canonical_phone: str) -> str:
        """Mask +91XXXXXXXXXX as +91******XXXX."""
        if not canonical_phone.startswith("+91") or len(canonical_phone) != 13:
            raise ValueError("Expected canonical Indian phone number in +91XXXXXXXXXX format.")
        return f"+91******{canonical_phone[-4:]}"

    @staticmethod
    def _sanitize_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(k): PIIProtector._sanitize_value(v)
                for k, v in value.items()
                if str(k).strip().lower() not in _PII_PAYLOAD_KEYS
            }
        if isinstance(value, list):
            return [PIIProtector._sanitize_value(item) for item in value]
        return value

    @staticmethod
    def sanitize_payload(payload: Any) -> dict[str, Any]:
        """Return a recursively sanitized dict copy of an event payload without raw PII keys."""
        if not isinstance(payload, dict):
            return {}
        return PIIProtector._sanitize_value(payload)

