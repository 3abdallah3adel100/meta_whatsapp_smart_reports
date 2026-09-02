from __future__ import annotations

import json
import os
import re

# This module keeps the allocation/balance logic configurable without Streamlit.

DEFAULT_BUSINESS_IDS = [
    "751488620224306",
    "1178859133269743",
]

MEDIA_BUYER_MAP = {
    "AA": "Abdallah Adel",
    "HM": "Ahmed Hesham",
    "BM": "Bassem Shalawy",
    "EK": "Esraa Kamal",
    "MA": "Mahmoud",
    "AF": "Amr Fathy",
    "SQ": "(R)Ahmed Sharkawy",
    "OS": "(R)Osama Serwe",
    "MM": "(R)Mohamed Mahmoud",
    "NB": "(R)Mohamed Nabih",
}


def _split_values(raw: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,;\n]+", raw or "")
        if item.strip()
    ]


BUSINESS_IDS = _split_values(
    os.getenv(
        "ALLOCATION_BUSINESS_IDS",
        ",".join(DEFAULT_BUSINESS_IDS),
    )
)

try:
    OVERALL_ALLOCATION_BUDGET = float(
        os.getenv("OVERALL_ALLOCATION_BUDGET", "0") or 0
    )
except ValueError:
    OVERALL_ALLOCATION_BUDGET = 0.0

CRITICAL_COVERAGE_DAYS = 1.0
TARGET_COVERAGE_DAYS = 3.0

ALLOCATION_ALIGNED_TOLERANCE_PCT = 10.0
ALLOCATION_SIGNIFICANT_DIFF_PCT = 30.0

CURRENCY_MINOR_UNIT_SCALE = {
    "EGP": 100.0,
    "USD": 100.0,
    "EUR": 100.0,
    "GBP": 100.0,
    "AED": 100.0,
    "SAR": 100.0,
}
DEFAULT_MINOR_UNIT_SCALE = 100.0

INCLUDE_ME_AD_ACCOUNTS = (
    os.getenv("ALLOCATION_INCLUDE_ME_AD_ACCOUNTS", "true")
    .strip()
    .lower()
    in {"1", "true", "yes", "on"}
)

ME_ACCOUNT_NAME_PREFIXES = (
    "OK-FB-HR-",
    "OK-FB-NF-",
    "US-FB-HR-",
    "US-BO-HR-",
)

MANUAL_BALANCE_OVERRIDES: dict[str, float] = {}
raw_overrides = os.getenv("MANUAL_BALANCE_OVERRIDES_JSON", "").strip()
if raw_overrides:
    try:
        payload = json.loads(raw_overrides)
        if isinstance(payload, dict):
            for key, value in payload.items():
                try:
                    MANUAL_BALANCE_OVERRIDES[str(key)] = float(value)
                except (TypeError, ValueError):
                    pass
    except json.JSONDecodeError:
        pass


def normalize_text(value) -> str:
    return str(value or "").strip().upper()


def clean_account_id(value) -> str:
    return str(value or "").replace("act_", "").strip()


def extract_buyer_code(account_name: str) -> str:
    text = normalize_text(account_name)
    for code in MEDIA_BUYER_MAP:
        pattern = rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])"
        if re.search(pattern, text):
            return code
    return "UNKNOWN"


def buyer_name(code: str) -> str:
    return MEDIA_BUYER_MAP.get(code, "Unknown")
