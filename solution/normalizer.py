"""Field and timestamp normalization utilities for leads and events."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from solution.config import IST
from solution.schemas import FieldSpec, WorkflowSchema

_HEADER_SEP_RE = re.compile(r"[_\s]+")
_NON_DIGIT_RE = re.compile(r"\D")
_DATE_YMD_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_DMY_SLASH_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_DATE_DMY_DASH_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
_DATE_DMY_MON_RE = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$")
_CURRENCY_PREFIX_RE = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_DECIMAL_BODY_RE = re.compile(r"^\d+(?:,\d+)*(?:\.\d+)?$")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def normalize_header(header: str) -> str:
    """Lowercase, trim, and collapse runs of underscores or whitespace to one space."""
    return _HEADER_SEP_RE.sub(" ", header.strip().lower()).strip()


def build_header_mapping(headers: list[str], schema: WorkflowSchema) -> dict[str, int]:
    """Map canonical schema field names to CSV column indices."""
    alias_to_field: dict[str, str] = {}
    canonical_norm_to_field: dict[str, str] = {}
    for field in schema.fields:
        norm_name = normalize_header(field.name)
        canonical_norm_to_field[norm_name] = field.name
        alias_to_field[norm_name] = field.name
        for alias in field.aliases:
            alias_to_field.setdefault(normalize_header(alias), field.name)

    mapping: dict[str, int] = {}
    # Prefer exact canonical header if both canonical and alias columns appear.
    for idx, raw_header in enumerate(headers):
        norm = normalize_header(raw_header)
        if norm in canonical_norm_to_field:
            mapping.setdefault(canonical_norm_to_field[norm], idx)

    for idx, raw_header in enumerate(headers):
        norm = normalize_header(raw_header)
        field_name = alias_to_field.get(norm)
        if field_name is not None:
            mapping.setdefault(field_name, idx)

    return mapping


def normalize_phone(raw: str) -> str | None:
    """Normalize an Indian mobile number to +91XXXXXXXXXX, or return None if invalid."""
    digits = _NON_DIGIT_RE.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in ("6", "7", "8", "9"):
        return f"+91{digits}"
    return None


def normalize_date(raw: str) -> str | None:
    """Parse supported day-first / ISO date formats into YYYY-MM-DD, or return None."""
    text = raw.strip()
    if not text:
        return None

    year: int
    month: int
    day: int

    if m := _DATE_YMD_RE.match(text):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := _DATE_DMY_SLASH_RE.match(text):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := _DATE_DMY_DASH_RE.match(text):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := _DATE_DMY_MON_RE.match(text):
        day = int(m.group(1))
        month_num = _MONTHS.get(m.group(2).lower())
        if month_num is None:
            return None
        month = month_num
        year = int(m.group(3))
    else:
        return None

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_decimal(raw: str) -> str | None:
    """Parse nonnegative currency/decimal strings rounded to 2 decimal places."""
    text = raw.strip()
    if not text:
        return None

    text = _CURRENCY_PREFIX_RE.sub("", text, count=1).strip()
    if not _DECIMAL_BODY_RE.match(text):
        return None

    cleaned = text.replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None

    if not value.is_finite() or value < Decimal("0"):
        return None

    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{quantized:.2f}"


def normalize_enum(raw: str, allowed_values: tuple[str, ...]) -> str | None:
    """Case-insensitive enum normalization returning lowercase canonical value or None."""
    candidate = raw.strip().lower()
    if candidate in allowed_values:
        return candidate
    return None


def normalize_field_value(raw: str, field: FieldSpec) -> str | None:
    """Normalize a non-empty trimmed value according to its schema FieldSpec."""
    if field.field_type == "string":
        return raw
    if field.field_type == "phone":
        return normalize_phone(raw)
    if field.field_type == "date":
        return normalize_date(raw)
    if field.field_type == "decimal":
        return normalize_decimal(raw)
    if field.field_type == "enum":
        return normalize_enum(raw, field.values)
    raise ValueError(f"Unsupported field type: {field.field_type}")


def parse_timestamp(raw: str) -> tuple[datetime, datetime]:
    """Parse UTC, offset, or naive (IST) timestamp into (utc_dt, ist_dt)."""
    if not isinstance(raw, str):
        raise ValueError("Timestamp must be a string.")
    text = raw.strip()
    if not text:
        raise ValueError("Timestamp cannot be empty.")

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc), dt.astimezone(IST)


def format_utc_sort_key(dt_utc: datetime) -> str:
    """Format UTC datetime with microsecond precision for exact lexicographical ordering."""
    return dt_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def format_ist_iso(dt_ist: datetime) -> str:
    """Format IST datetime as ISO-8601 (+05:30)."""
    local = dt_ist.astimezone(IST)
    if local.microsecond == 0:
        return local.isoformat(timespec="seconds")
    return local.isoformat()

