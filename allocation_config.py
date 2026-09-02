from __future__ import annotations

import json
import os
import re

# ============================================================
# Allocation / Balance configuration ONLY
# ============================================================
# This file intentionally does not affect Spend / Age / Governorate.

DEFAULT_ALLOCATION_BUSINESS_IDS = [
    "751488620224306",
    "1178859133269743",
]

# Keep Allocation aware of the same businesses used by the Smart reports.
# This prevents valid agents/accounts from disappearing simply because they
# live in REPORT_BUSINESS_IDS rather than the older Allocation scope.
DEFAULT_REPORT_BUSINESS_IDS = [
    "1935536750225128",
    "751488620224306",
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


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


# Allocation keeps its original Business IDs, but ALSO inherits the report
# Business IDs. This changes only Allocation account discovery.
_allocation_ids = _split_values(os.getenv("ALLOCATION_BUSINESS_IDS", ""))
if not _allocation_ids:
    _allocation_ids = DEFAULT_ALLOCATION_BUSINESS_IDS.copy()

_report_ids = _split_values(os.getenv("REPORT_BUSINESS_IDS", ""))
if not _report_ids:
    _report_ids = DEFAULT_REPORT_BUSINESS_IDS.copy()

BUSINESS_IDS = _dedupe(_allocation_ids + _report_ids)


def _read_money_env(*names: str, default: float = 0.0) -> float:
    """Read an allocation money value robustly from env.

    Supports both OVERALL_ALLOCATION_BUDGET and the shorter ALLOCATION_BUDGET,
    and accepts values such as 250000 or 250,000.
    """
    for name in names:
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            continue
    return float(default)


OVERALL_ALLOCATION_BUDGET = _read_money_env(
    "OVERALL_ALLOCATION_BUDGET",
    "ALLOCATION_BUDGET",
    default=0.0,
)

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
        # Standard separated code: ...-AA-... / ... AA ...
        pattern = rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])"
        if re.search(pattern, text):
            return code

    # Fallback for compact account naming such as AA01 / EK02 while keeping
    # the match tied to a separator before the code.
    for code in MEDIA_BUYER_MAP:
        compact = rf"(?<![A-Z0-9]){re.escape(code)}(?=\d)"
        if re.search(compact, text):
            return code

    return "UNKNOWN"


def buyer_name(code: str) -> str:
    return MEDIA_BUYER_MAP.get(code, "Unknown")
