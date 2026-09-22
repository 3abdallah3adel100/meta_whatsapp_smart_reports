from __future__ import annotations

import json
import os
import re

# ============================================================
# ALLOCATION / BALANCE CONFIG ONLY
# ============================================================
# These values intentionally mirror the working balance-main reference project.
# Spend / Age / Governorate code is NOT affected by this file.

# Smart Report uses TAHER_BUSINESS_IDS as one source of truth for both
# performance and allocation. The old ALLOCATION_BUSINESS_IDS remains a
# fallback only when this file is run independently of the workflow.
DEFAULT_BUSINESS_IDS = [
    "751488620224306",
    "1178859133269743",
    "1370772291128896",
]
_raw_business_ids = (
    os.getenv("TAHER_BUSINESS_IDS", "").strip()
    or os.getenv("ALLOCATION_BUSINESS_IDS", "").strip()
    or ",".join(DEFAULT_BUSINESS_IDS)
)
BUSINESS_IDS = list(dict.fromkeys(
    value.strip() for value in re.split(r"[,;\n]+", _raw_business_ids)
    if value.strip()
))

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


def _read_allocation_budget() -> float:
    """Read the Allocation budget, with the working project value as fallback.

    The working balance-main/config.py has OVERALL_ALLOCATION_BUDGET = 80000.0.
    We keep 80,000 as the safe fallback so Allocation can never show
    NOT CONFIGURED merely because GitHub failed to expose the optional secret.

    Accepted environment names:
      OVERALL_ALLOCATION_BUDGET
      ALLOCATION_BUDGET

    Accepted examples:
      80000
      80,000
      80000 EGP
      EGP 80,000
    """
    for name in ("OVERALL_ALLOCATION_BUDGET", "ALLOCATION_BUDGET"):
        raw = str(os.getenv(name, "") or "").strip()
        if not raw:
            continue

        normalized = raw.replace(",", "")
        match = re.search(r"-?\d+(?:\.\d+)?", normalized)
        if not match:
            continue

        try:
            value = float(match.group(0))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass

    # Exact working reference value.
    return 80000.0


OVERALL_ALLOCATION_BUDGET = _read_allocation_budget()

# Same thresholds as the working balance-main project.
CRITICAL_COVERAGE_DAYS = 1.0
TARGET_COVERAGE_DAYS = 3.0
ALLOCATION_ALIGNED_TOLERANCE_PCT = 10.0
ALLOCATION_SIGNIFICANT_DIFF_PCT = 30.0

# Meta budget/balance money fields are returned in minor units.
CURRENCY_MINOR_UNIT_SCALE = {
    "EGP": 100.0,
    "USD": 100.0,
    "EUR": 100.0,
    "GBP": 100.0,
    "AED": 100.0,
    "SAR": 100.0,
}
DEFAULT_MINOR_UNIT_SCALE = 100.0

# Exact working discovery behavior.
INCLUDE_ME_AD_ACCOUNTS = True
ME_ACCOUNT_NAME_PREFIXES = (
    "OK-FB-HR-",
    "OK-FB-NF-",
    "US-FB-HR-",
    "US-BO-HR-",
)

# Optional overrides remain supported from the Smart Report secret.
MANUAL_BALANCE_OVERRIDES: dict[str, float] = {}
raw_overrides = str(os.getenv("MANUAL_BALANCE_OVERRIDES_JSON", "") or "").strip()
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
    """Exact media-buyer extraction used by the working balance-main project."""
    text = normalize_text(account_name)
    for code in MEDIA_BUYER_MAP:
        pattern = rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])"
        if re.search(pattern, text):
            return code
    return "UNKNOWN"


def buyer_name(code: str) -> str:
    return MEDIA_BUYER_MAP.get(code, "Unknown")
