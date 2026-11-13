"""Unit tests for field and timestamp normalization."""

from __future__ import annotations

from datetime import timezone

import pytest

from solution.config import IST
from solution.normalizer import (
    build_header_mapping,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
    parse_timestamp,
)
from solution.schemas import load_registry


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("91-77187-50036", "+917718750036"),
        ("(+91) 8123456789", "+918123456789"),
        ("+916000000001", "+916000000001"),
        ("  91 7000000009  ", "+917000000009"),
        ("6123456789", "+916123456789"),
        ("7123456789", "+917123456789"),
        ("8123456789", "+918123456789"),
        ("9123456789", "+919123456789"),
    ],
)
def test_normalize_phone_valid(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "987654321",          # 9 digits
        "5876543210",         # starts with 5
        "022-2345-6789",      # landline
        "+1 415 555 0134",    # non-India 11 digits starting with 1
        "N/A",
        "9198765432100",      # 13 digits
        "1234567890",         # starts with 1
    ],
)
def test_normalize_phone_invalid(raw: str) -> None:
    assert normalize_phone(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-15", "2026-10-15"),
        ("01/02/2026", "2026-02-01"),   # day-first check: 1 Feb 2026
        ("15-11-2026", "2026-11-15"),
        ("01-Dec-2026", "2026-12-01"),
        ("01-dec-2026", "2026-12-01"),
        ("29/02/2024", "2024-02-29"),   # leap year valid
    ],
)
def test_normalize_date_valid(raw: str, expected: str) -> None:
    assert normalize_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "31/02/2026",
        "29/02/2025",  # non-leap year
        "2026-13-01",
        "soon",
        "TBD",
        "13/13/2026",
        "2026/10/15",  # unsupported slash-first-year format
    ],
)
def test_normalize_date_invalid(raw: str) -> None:
    assert normalize_date(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", "0.00"),
        ("15000", "15000.00"),
        ("15000.5", "15000.50"),
        ("15000.555", "15000.56"),
        ("15000.554", "15000.55"),
        ("₹12,345", "12345.00"),
        ("Rs. 12,345.6", "12345.60"),
        ("rs 999.99", "999.99"),
        ("INR 1,00,000.25", "100000.25"),
        ("inr4500.5", "4500.50"),
        ("₹0", "0.00"),
        ("Rs500", "500.00"),
        ("Rs.500", "500.00"),
    ],
)
def test_normalize_decimal_valid(raw: str, expected: str) -> None:
    assert normalize_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "N/A", "-", "-100", "-0.01", "Rs. -50", "twelve thousand", "Rs.", "NaN", "12_345"],
)
def test_normalize_decimal_invalid(raw: str) -> None:
    assert normalize_decimal(raw) is None


def test_normalize_enum() -> None:
    allowed = ("en", "hi", "ta", "kn", "te", "mr")
    assert normalize_enum("EN", allowed) == "en"
    assert normalize_enum("  Hi ", allowed) == "hi"
    assert normalize_enum("Hindi", allowed) is None


def test_header_normalization_and_aliases(schemas_dir) -> None:
    assert normalize_header("  Customer__Name ") == "customer name"
    assert normalize_header("Mobile   No") == "mobile no"
    assert normalize_header("  __Mobile___No__ ") == "mobile no"

    reg = load_registry(schemas_dir)
    schema = reg.workflows["motor_renewal"]
    headers = ["Language", "Insured_Name", "agent_notes", "Policy  No", "Mobile No", "Expiry Date", "Premium"]
    mapping = build_header_mapping(headers, schema)
    assert mapping == {
        "preferred_language": 0,
        "customer_name": 1,
        "policy_number": 3,
        "phone": 4,
        "policy_expiry": 5,
        "premium_amount": 6,
    }


def test_parse_timestamp_formats() -> None:
    utc_dt, ist_dt = parse_timestamp("2026-10-05T19:00:00Z")
    assert utc_dt.tzinfo == timezone.utc
    assert ist_dt.tzinfo == IST
    assert ist_dt.date().isoformat() == "2026-10-06"  # UTC 19:00 == IST 00:30 next day

    utc_dt2, ist_dt2 = parse_timestamp("2026-10-06T00:30:00+05:30")
    assert utc_dt2 == utc_dt
    assert ist_dt2 == ist_dt

    utc_dt3, ist_dt3 = parse_timestamp("2026-10-06 00:30:00")
    assert utc_dt3 == utc_dt
    assert ist_dt3 == ist_dt

    # 23:59:59 UTC -> 05:29:59 next day IST
    _, ist_late = parse_timestamp("2026-10-05T23:59:59Z")
    assert ist_late.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-06 05:29:59"

    # 18:30:00 UTC == 00:00:00 IST next day
    _, ist_midnight = parse_timestamp("2026-10-05T18:30:00Z")
    assert ist_midnight.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-06 00:00:00"

    # Negative offset (-04:00) -> IST (+05:30, +9h30m ahead)
    _, ist_offset = parse_timestamp("2026-10-05T15:00:00-04:00")
    assert ist_offset.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-06 00:30:00"


