"""
Smart Meta Ads WhatsApp Report Runner
=====================================

GitHub Actions runner for flexible WhatsApp commands.

Report types:
- spend       : Spend / Leads / CPL + agent performance + Male Spend
- age         : Spend / Leads / CPL by Meta age bucket
- governorate : Spend / Leads / CPL by Meta region/governorate
- allocation  : Current Balance / Daily Budget / Coverage / Allocation

The Cloudflare Worker parses Arabic/English commands and sends structured
inputs to this script.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from allocation_config import (
    ALLOCATION_ALIGNED_TOLERANCE_PCT,
    ALLOCATION_SIGNIFICANT_DIFF_PCT,
    CRITICAL_COVERAGE_DAYS,
    MEDIA_BUYER_MAP,
    OVERALL_ALLOCATION_BUDGET,
    TARGET_COVERAGE_DAYS,
)
from allocation_meta import MetaClient as AllocationMetaClient
from allocation_meta import fetch_full_snapshot


BASE_URL = "https://graph.facebook.com"
REPORT_BUILD = "2026-09-03-SMART-FLEX-REPORTS-V3-CUSTOM-DATES"

TEAM_BUSINESS_IDS = OrderedDict([
    ("Cairo Team", "1935536750225128"),
    ("Taher Team", "751488620224306"),
])

AGE_BUCKET_ORDER = [
    "18-24",
    "25-34",
    "35-44",
    "45-54",
    "55-64",
    "65+",
]

REPORT_TYPE_ORDER = [
    "spend",
    "age",
    "governorate",
    "allocation",
]


@dataclass(frozen=True)
class Config:
    meta_access_token: str
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    recipient: str
    api_version: str
    timezone: str
    max_workers: int
    report_business_ids: list[str]


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    since: str
    until: str


# ============================================================
# Config / generic helpers
# ============================================================

def require_env(name: str, allow_empty: bool = False) -> str:
    value = os.getenv(name, "").strip()
    if not value and not allow_empty:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def split_values(raw: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,;\n]+", raw or "")
        if item.strip()
    ]


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))

    if digits.startswith("00"):
        digits = digits[2:]

    if len(digits) == 11 and digits.startswith("01"):
        digits = "20" + digits[1:]

    return digits


def load_config(recipient: str, dry_run: bool = False) -> Config:
    phone = normalize_phone(recipient)
    if not phone:
        raise RuntimeError("Missing or invalid WhatsApp recipient.")

    try:
        max_workers = max(1, min(16, int(os.getenv("MAX_WORKERS", "8"))))
    except ValueError:
        max_workers = 8

    default_business_ids = list(TEAM_BUSINESS_IDS.values())
    configured_ids = split_values(
        os.getenv(
            "REPORT_BUSINESS_IDS",
            ",".join(default_business_ids),
        )
    )

    # Team commands must always be able to reach both known businesses.
    # This does not change Taher filtering; it only guarantees Cairo is discoverable.
    for known_id in default_business_ids:
        if known_id not in configured_ids:
            configured_ids.append(known_id)

    return Config(
        meta_access_token=require_env("META_ACCESS_TOKEN"),
        whatsapp_access_token=require_env(
            "WHATSAPP_ACCESS_TOKEN",
            allow_empty=dry_run,
        ),
        whatsapp_phone_number_id=require_env(
            "WHATSAPP_PHONE_NUMBER_ID",
            allow_empty=dry_run,
        ),
        recipient=phone,
        api_version=os.getenv(
            "META_API_VERSION",
            "v26.0",
        ).strip() or "v26.0",
        timezone=os.getenv(
            "REPORT_TIMEZONE",
            "Africa/Cairo",
        ).strip() or "Africa/Cairo",
        max_workers=max_workers,
        report_business_ids=configured_ids or default_business_ids,
    )


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def safe_div(a: float, b: float) -> float | None:
    return a / b if b else None


def normalize_text(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_account_id(value: Any) -> str:
    return str(value or "").replace("act_", "").strip()


def mask_phone(phone: str) -> str:
    digits = normalize_phone(phone)
    if len(digits) <= 4:
        return digits or "Unknown"
    return "*" * (len(digits) - 4) + digits[-4:]


def extract_buyer_code(account_name: str) -> str:
    text = normalize_text(account_name)
    for code in MEDIA_BUYER_MAP:
        pattern = rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])"
        if re.search(pattern, text):
            return code
    return "UNKNOWN"


def buyer_name(code: str) -> str:
    return MEDIA_BUYER_MAP.get(code, "Unknown")


def money(value: Any, currency: str = "EGP") -> str:
    if value is None:
        return "N/A"
    try:
        number = float(value)
        if not math.isfinite(number):
            return "N/A"
    except (TypeError, ValueError):
        return "N/A"
    return f"{number:,.2f} {currency}"


def leads_text(value: Any) -> str:
    return f"{to_float(value):,.0f}"


def cpl_text(spend: float, leads: float) -> str:
    value = safe_div(spend, leads)
    return money(value) if value is not None else "N/A"


def percentage_text(part: float, total: float) -> str:
    value = safe_div(part * 100.0, total)
    return f"{value:.2f}%" if value is not None else "0.00%"


def pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:,.2f}%"


def days_text(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        value = float(value)
        if not math.isfinite(value):
            return "N/A"
    except (TypeError, ValueError):
        return "N/A"

    total_hours = max(0, int(round(value * 24)))
    whole_days, hours = divmod(total_hours, 24)
    day_label = "day" if whole_days == 1 else "days"
    hour_label = "hour" if hours == 1 else "hours"
    return f"{whole_days} {day_label} . {hours} {hour_label}"


def whole_money_100(value: Any, currency: str = "EGP") -> str:
    if value is None:
        return "N/A"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "N/A"
        rounded = (
            (number / Decimal("100"))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            * Decimal("100")
        )
        return f"{int(rounded):,} {currency}"
    except (InvalidOperation, TypeError, ValueError):
        return "N/A"


# ============================================================
# Range handling
# ============================================================

RANGE_LABELS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "7d": "Last 7 Days",
    "30d": "Last 30 Days",
    "this_month": "This Month",
    "last_month": "Last Month",
}


def normalize_range_key(value: str) -> str:
    raw = normalize_text(value)
    raw = raw.replace("_", " ").replace("-", " ")
    raw = re.sub(r"\s+", " ", raw).strip()

    aliases = {
        "TODAY": "today",
        "1": "today",
        "YESTERDAY": "yesterday",
        "2": "yesterday",
        "7D": "7d",
        "7 DAY": "7d",
        "7 DAYS": "7d",
        "LAST 7 DAYS": "7d",
        "3": "7d",
        "30D": "30d",
        "30 DAY": "30d",
        "30 DAYS": "30d",
        "LAST 30 DAYS": "30d",
        "4": "30d",
        "THIS MONTH": "this_month",
        "5": "this_month",
        "LAST MONTH": "last_month",
        "6": "last_month",
    }

    key = aliases.get(raw)
    if not key:
        raise ValueError(f"Unsupported range: {value}")
    return key


def _parse_iso_date(value: str):
    try:
        return datetime.strptime(
            str(value or "").strip(),
            "%Y-%m-%d",
        ).date()
    except ValueError as exc:
        raise ValueError(
            f"Invalid custom date: {value}. Expected YYYY-MM-DD."
        ) from exc


def _period_for_dates(
    key: str,
    label: str,
    since,
    until,
) -> Period:
    if until < since:
        raise ValueError(
            f"Invalid date range: {since} -> {until}"
        )

    return Period(
        key=key,
        label=label,
        since=since.isoformat(),
        until=until.isoformat(),
    )


def build_periods(
    timezone_name: str,
    range_value: str,
) -> tuple[datetime, list[Period], bool]:
    """Resolve preset, custom-range, single-day, or day-by-day requests.

    Encoded custom values are produced by the Cloudflare parser:
      custom:2026-08-01:2026-08-05
      daily:2026-08-01:2026-08-05

    Allocation remains a current-state report. The daily_split flag is used
    only for Spend/Age/Governorate; Allocation is appended once by main().
    """
    now = datetime.now(ZoneInfo(timezone_name))
    today = now.date()
    raw = str(range_value or "").strip()

    custom_match = re.fullmatch(
        r"(custom|daily):(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})",
        raw,
        flags=re.IGNORECASE,
    )

    if custom_match:
        mode = custom_match.group(1).lower()
        since = _parse_iso_date(custom_match.group(2))
        until = _parse_iso_date(custom_match.group(3))

        if until < since:
            raise ValueError(
                f"Custom range end is before start: {since} -> {until}"
            )

        if mode == "daily":
            periods: list[Period] = []
            current = since
            while current <= until:
                periods.append(
                    _period_for_dates(
                        key=f"day_{current.isoformat()}",
                        label=current.isoformat(),
                        since=current,
                        until=current,
                    )
                )
                current += timedelta(days=1)
            return now, periods, True

        label = (
            since.isoformat()
            if since == until
            else "Custom Range"
        )
        return now, [
            _period_for_dates(
                key="custom",
                label=label,
                since=since,
                until=until,
            )
        ], False

    key = normalize_range_key(raw)

    if key == "today":
        since = until = today
    elif key == "yesterday":
        since = until = today - timedelta(days=1)
    elif key == "7d":
        since = today - timedelta(days=6)
        until = today
    elif key == "30d":
        since = today - timedelta(days=29)
        until = today
    elif key == "this_month":
        since = today.replace(day=1)
        until = today
    elif key == "last_month":
        first_this_month = today.replace(day=1)
        until = first_this_month - timedelta(days=1)
        since = until.replace(day=1)
    else:
        raise ValueError(f"Unsupported range: {range_value}")

    return now, [
        _period_for_dates(
            key=key,
            label=RANGE_LABELS[key],
            since=since,
            until=until,
        )
    ], False


def build_period(
    timezone_name: str,
    range_value: str,
) -> tuple[datetime, Period]:
    """Backward-compatible single-period wrapper."""
    now, periods, daily_split = build_periods(
        timezone_name,
        range_value,
    )
    if daily_split and len(periods) > 1:
        raise ValueError(
            "Day-by-day range contains multiple periods; use build_periods()."
        )
    return now, periods[0]


def parse_report_types(raw: str) -> list[str]:
    values = [
        value.strip().lower()
        for value in split_values(raw)
        if value.strip()
    ]

    aliases = {
        "spend": "spend",
        "performance": "spend",
        "daily": "spend",
        "age": "age",
        "ages": "age",
        "governorate": "governorate",
        "governorates": "governorate",
        "government": "governorate",
        "region": "governorate",
        "regions": "governorate",
        "allocation": "allocation",
        "balance": "allocation",
    }

    out: list[str] = []
    for value in values:
        mapped = aliases.get(value)
        if not mapped:
            raise ValueError(f"Unsupported report type: {value}")
        if mapped not in out:
            out.append(mapped)

    if not out:
        out = ["spend"]

    return [
        report_type
        for report_type in REPORT_TYPE_ORDER
        if report_type in out
    ]


def normalize_agent_code(value: str) -> str:
    raw = normalize_text(value)

    if raw in {"", "ALL", "ANY", "الكل", "كل"}:
        return "ALL"

    if raw in MEDIA_BUYER_MAP:
        return raw

    for code, name in MEDIA_BUYER_MAP.items():
        if raw == normalize_text(name):
            return code

    raise ValueError(f"Unsupported agent: {value}")


def normalize_team_key(value: str) -> str:
    raw = str(value or "").strip().lower()

    if raw in {
        "", "taher", "taher team", "team taher",
        "طاهر", "تيم طاهر", "تييم طاهر",
    }:
        return "taher"

    if raw in {
        "cairo", "cairo team", "team cairo",
        "qaoud", "kaoud", "qaaoud",
        "القاهرة", "القاهره", "قاهرة", "قاهره",
        "قاعود", "تيم القاهرة", "تييم القاهرة",
        "تيم قاعود", "تييم قاعود",
    }:
        return "cairo"

    raise ValueError(f"Unsupported team: {value}")


def team_label(team_key: str) -> str:
    if team_key == "cairo":
        return "Cairo Team (Qaoud)"
    return "Taher Team"


def scope_label(agent_code: str) -> str:
    if agent_code == "ALL":
        return "All Agents"
    return f"{buyer_name(agent_code)} ({agent_code})"


def request_scope_label(team_key: str, agent_code: str) -> str:
    if team_key == "cairo":
        return "Cairo Team (Qaoud) — Overall"
    return scope_label(agent_code)


# ============================================================
# Objective / Leads logic
# ============================================================

def name_has_code(value: str, code: str) -> bool:
    text = normalize_text(value)
    return bool(
        re.search(
            rf"(?<![A-Z0-9]){re.escape(code)}[A-Z0-9]*",
            text,
        )
    )


def classify_objective_taher(campaign_name: str) -> str:
    text = normalize_text(campaign_name)

    if re.search(r"(?<![A-Z0-9])CONVL(?![A-Z0-9])", text):
        return "Conversion"
    if re.search(r"(?<![A-Z0-9])CONVS(?![A-Z0-9])", text):
        return "Conversion"
    if re.search(r"(?<![A-Z0-9])CONV(?![A-Z0-9])", text):
        return "Conversion"
    if re.search(r"(?<![A-Z0-9])WA(?![A-Z0-9])", text):
        return "Whatsapp Message"
    if re.search(r"(?<![A-Z0-9])LG(?![A-Z0-9])", text):
        return "Lead generation"
    if re.search(r"(?<![A-Z0-9])LM(?![A-Z0-9])", text):
        return "Lead - Message"

    return "Unknown"


def classify_objective_cairo(
    campaign_name: str,
    account_name: str = "",
) -> str:
    text = (
        f"{normalize_text(campaign_name)} "
        f"{normalize_text(account_name)}"
    )

    if name_has_code(text, "WP"):
        return "Whatsapp Message"
    if name_has_code(text, "LG"):
        return "Lead generation"
    if re.search(r"(?<![A-Z0-9])WA[A-Z0-9]*", text):
        return "Whatsapp Message"
    if re.search(r"(?<![A-Z0-9])LM[A-Z0-9]*", text):
        return "Lead - Message"
    if re.search(r"(?<![A-Z0-9])CONVL[A-Z0-9]*", text):
        return "Conversion"
    if re.search(r"(?<![A-Z0-9])CONVS[A-Z0-9]*", text):
        return "Conversion"
    if re.search(r"(?<![A-Z0-9])CONV[A-Z0-9]*", text):
        return "Conversion"

    return "Unknown"


def classify_objective_for_team(
    team_name: str,
    campaign_name: str,
    account_name: str = "",
) -> str:
    if normalize_text(team_name) == "CAIRO TEAM":
        return classify_objective_cairo(
            campaign_name,
            account_name,
        )

    return classify_objective_taher(campaign_name)


def flatten_actions(actions: Any) -> dict[str, float]:
    output: dict[str, float] = {}

    if not isinstance(actions, list):
        return output

    for item in actions:
        if not isinstance(item, dict):
            continue

        action_type = str(
            item.get("action_type", "")
        ).strip().lower()

        if not action_type:
            continue

        output[action_type] = (
            output.get(action_type, 0.0)
            + to_float(item.get("value"))
        )

    return output


def result_for_objective(
    objective: str,
    actions_map: dict[str, float],
) -> float:
    if objective in {
        "Lead generation",
        "Lead - Message",
    }:
        return sum(
            actions_map.get(key, 0.0)
            for key in (
                "offsite_conversion.fb_pixel_lead",
                "onsite_conversion.lead_grouped",
                "offsite_conversion.custom",
            )
        )

    if objective == "Conversion":
        return actions_map.get("purchase", 0.0)

    if objective == "Whatsapp Message":
        return sum(
            actions_map.get(key, 0.0)
            for key in (
                "onsite_conversion.messaging_conversation_started",
                "onsite_conversion.messaging_conversation_started_7d",
            )
        )

    return 0.0


def row_results(
    row: dict[str, Any],
    *,
    team_name: str,
    account_name: str,
) -> float:
    objective = classify_objective_for_team(
        team_name=team_name,
        campaign_name=str(
            row.get("campaign_name", "")
        ),
        account_name=account_name,
    )

    return result_for_objective(
        objective,
        flatten_actions(row.get("actions")),
    )


# ============================================================
# Meta request helpers
# ============================================================

class MetaAPIError(RuntimeError):
    pass


def request_json(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    attempts: int = 4,
) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                params=params,
                headers=headers,
                json=payload,
                timeout=90,
            )

            try:
                data = response.json()
            except ValueError:
                data = None

            if response.ok and isinstance(data, dict):
                return data

            if isinstance(data, dict):
                error = data.get("error", {})
                detail = (
                    f"{error.get('message') or f'HTTP {response.status_code}'}"
                    f" | code={error.get('code')}"
                    f" | subcode={error.get('error_subcode')}"
                    f" | trace={error.get('fbtrace_id')}"
                )
            else:
                detail = (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:1200]}"
                )

            retryable = response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }

            if not retryable:
                raise MetaAPIError(detail)

            last_error = MetaAPIError(detail)

        except requests.RequestException as exc:
            last_error = exc
        except MetaAPIError:
            raise
        except Exception as exc:
            last_error = exc

        if attempt < attempts:
            time.sleep(min(8, 2 ** (attempt - 1)))

    raise MetaAPIError(
        str(last_error or "Unknown Meta API error")
    )


def fetch_all_pages(
    url: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    next_url: str | None = url
    next_params: dict[str, Any] | None = params

    while next_url:
        payload = request_json(
            "GET",
            next_url,
            params=next_params,
        )
        rows.extend(payload.get("data", []))
        next_url = payload.get(
            "paging",
            {},
        ).get("next")
        next_params = None

    return rows


def team_name_for_business(business_id: str) -> str:
    for team_name, known_id in TEAM_BUSINESS_IDS.items():
        if str(business_id) == str(known_id):
            return team_name
    return "Generic Team"


def get_report_accounts(
    config: Config,
) -> tuple[list[dict[str, Any]], list[str]]:
    collected: list[dict[str, Any]] = []
    errors: list[str] = []

    for business_id in config.report_business_ids:
        team_name = team_name_for_business(business_id)

        for edge in (
            "owned_ad_accounts",
            "client_ad_accounts",
        ):
            url = (
                f"{BASE_URL}/{config.api_version}/"
                f"{business_id}/{edge}"
            )

            params = {
                "fields": (
                    "id,account_id,name,"
                    "account_status,currency"
                ),
                "access_token": config.meta_access_token,
                "limit": 500,
            }

            try:
                rows = fetch_all_pages(url, params)
                for row in rows:
                    item = dict(row)
                    item["_team"] = team_name
                    item["_business_id"] = business_id
                    item["_source_edge"] = edge
                    collected.append(item)
            except Exception as exc:
                errors.append(
                    f"{business_id}/{edge}: {exc}"
                )

    dedup: dict[str, dict[str, Any]] = {}

    for row in collected:
        account_id = normalize_account_id(
            row.get("id")
            or row.get("account_id")
        )

        if not account_id:
            continue

        # Keep the same account separately when it belongs to two different
        # Business IDs/teams, while still deduping owned/client duplicates
        # inside the same team.
        team_name = str(row.get("_team") or "Generic Team")
        dedup_key = f"{team_name}:{account_id}"

        if dedup_key not in dedup:
            item = dict(row)
            item["id"] = f"act_{account_id}"
            item["buyer_code"] = extract_buyer_code(
                str(item.get("name", ""))
            )
            dedup[dedup_key] = item

    accounts = list(dedup.values())
    accounts.sort(
        key=lambda item: str(
            item.get("name")
            or item.get("id")
            or ""
        ).casefold()
    )

    if not accounts:
        raise RuntimeError(
            "No Ad Accounts were discovered from "
            "REPORT_BUSINESS_IDS."
        )

    return accounts, errors


def filter_accounts_for_team(
    accounts: list[dict[str, Any]],
    team_key: str,
) -> list[dict[str, Any]]:
    wanted = (
        "Cairo Team"
        if team_key == "cairo"
        else "Taher Team"
    )

    return [
        account
        for account in accounts
        if str(account.get("_team") or "") == wanted
    ]


def filter_accounts_for_agent(
    accounts: list[dict[str, Any]],
    agent_code: str,
) -> list[dict[str, Any]]:
    if agent_code == "ALL":
        return accounts

    return [
        account
        for account in accounts
        if extract_buyer_code(
            str(account.get("name", ""))
        ) == agent_code
    ]


def fetch_insights(
    config: Config,
    account_id: str,
    period: Period,
    *,
    fields: str,
    level: str,
    breakdowns: str | None = None,
) -> list[dict[str, Any]]:
    clean_id = normalize_account_id(account_id)
    url = (
        f"{BASE_URL}/{config.api_version}/"
        f"act_{clean_id}/insights"
    )

    params: dict[str, Any] = {
        "fields": fields,
        "level": level,
        "time_range": json.dumps(
            {
                "since": period.since,
                "until": period.until,
            },
            separators=(",", ":"),
        ),
        "access_token": config.meta_access_token,
        "limit": 5000,
    }

    if breakdowns:
        params["breakdowns"] = breakdowns

    return fetch_all_pages(url, params)


def fetch_account_report_data(
    config: Config,
    account: dict[str, Any],
    period: Period,
    report_types: list[str],
) -> dict[str, Any]:
    account_id = normalize_account_id(
        account.get("id")
        or account.get("account_id")
    )
    account_name = str(
        account.get("name")
        or account_id
    )
    team_name = str(
        account.get("_team")
        or "Generic Team"
    )
    buyer_code = extract_buyer_code(account_name)

    result: dict[str, Any] = {
        "account_id": f"act_{account_id}",
        "account_name": account_name,
        "team_name": team_name,
        "buyer_code": buyer_code,
        "agent": buyer_name(buyer_code),
        "overall_rows": [],
        "gender_rows": [],
        "age_rows": [],
        "region_rows": [],
        "errors": [],
    }

    if "spend" in report_types:
        try:
            result["overall_rows"] = fetch_insights(
                config,
                account_id,
                period,
                fields=(
                    "campaign_id,campaign_name,"
                    "spend,actions"
                ),
                level="campaign",
            )
        except Exception as exc:
            result["errors"].append(
                f"overall: {exc}"
            )

        try:
            result["gender_rows"] = fetch_insights(
                config,
                account_id,
                period,
                fields="spend",
                level="account",
                breakdowns="gender",
            )
        except Exception as exc:
            result["errors"].append(
                f"gender: {exc}"
            )

    if "age" in report_types:
        try:
            result["age_rows"] = fetch_insights(
                config,
                account_id,
                period,
                fields=(
                    "campaign_id,campaign_name,"
                    "spend,actions"
                ),
                level="campaign",
                breakdowns="age",
            )
        except Exception as exc:
            result["errors"].append(
                f"age: {exc}"
            )

    if "governorate" in report_types:
        try:
            result["region_rows"] = fetch_insights(
                config,
                account_id,
                period,
                fields=(
                    "campaign_id,campaign_name,"
                    "spend,actions"
                ),
                level="campaign",
                breakdowns="region",
            )
        except Exception as exc:
            result["errors"].append(
                f"region: {exc}"
            )

    return result


def fetch_report_data(
    config: Config,
    period: Period,
    team_key: str,
    agent_code: str,
    report_types: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    accounts, discovery_errors = get_report_accounts(
        config
    )

    accounts = filter_accounts_for_team(
        accounts,
        team_key,
    )

    # Taher keeps the existing agent behavior exactly.
    # Cairo/Qaoud is always Overall and is never split by agent.
    if team_key == "taher":
        accounts = filter_accounts_for_agent(
            accounts,
            agent_code,
        )

    if not accounts:
        raise RuntimeError(
            f"No Ad Accounts found for {request_scope_label(team_key, agent_code)}."
        )

    print(
        f"Report accounts: {len(accounts)} "
        f"| Team={team_label(team_key)} "
        f"| Scope={request_scope_label(team_key, agent_code)}"
    )

    results: list[dict[str, Any]] = []
    errors = list(discovery_errors)

    with ThreadPoolExecutor(
        max_workers=min(
            config.max_workers,
            max(1, len(accounts)),
        )
    ) as executor:
        future_map = {
            executor.submit(
                fetch_account_report_data,
                config,
                account,
                period,
                report_types,
            ): account
            for account in accounts
        }

        for future in as_completed(future_map):
            account = future_map[future]
            try:
                result = future.result()
                results.append(result)
                print(
                    f"Fetched: {result['account_name']}"
                )
            except Exception as exc:
                account_name = str(
                    account.get("name")
                    or account.get("id")
                    or "Unknown"
                )
                errors.append(
                    f"{account_name}: {exc}"
                )

    if not results:
        raise RuntimeError(
            "All report account fetches failed."
        )

    return results, errors


# ============================================================
# Aggregation
# ============================================================

def aggregate_spend(
    account_results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    total_spend = 0.0
    total_leads = 0.0
    agents: dict[str, dict[str, float]] = {}
    male_details: list[dict[str, Any]] = []
    errors: list[str] = []

    for account in account_results:
        account_id = account["account_id"]
        account_name = account["account_name"]
        team_name = account["team_name"]
        buyer_code = account["buyer_code"]
        agent = account["agent"]

        account_spend = 0.0
        account_leads = 0.0

        for row in account.get("overall_rows", []):
            spend = to_float(row.get("spend"))
            leads = row_results(
                row,
                team_name=team_name,
                account_name=account_name,
            )

            account_spend += spend
            account_leads += leads
            total_spend += spend
            total_leads += leads

        agent_totals = agents.setdefault(
            buyer_code,
            {
                "spend": 0.0,
                "leads": 0.0,
            },
        )
        agent_totals["spend"] += account_spend
        agent_totals["leads"] += account_leads

        male_spend = sum(
            to_float(row.get("spend"))
            for row in account.get("gender_rows", [])
            if str(
                row.get("gender", "")
            ).strip().lower() == "male"
        )

        if male_spend > 0:
            male_details.append({
                "account_id": account_id,
                "account_name": account_name,
                "buyer_code": buyer_code,
                "agent": agent,
                "spend": male_spend,
            })

        for error in account.get("errors", []):
            errors.append(
                f"{account_name}: {error}"
            )

    male_details.sort(
        key=lambda row: row["spend"],
        reverse=True,
    )

    return {
        "spend": total_spend,
        "leads": total_leads,
        "agents": agents,
        "male_details": male_details,
        "errors": errors,
    }


def aggregate_age(
    account_results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    age_map = {
        bucket: {
            "spend": 0.0,
            "leads": 0.0,
        }
        for bucket in AGE_BUCKET_ORDER
    }
    errors: list[str] = []

    for account in account_results:
        account_name = account["account_name"]
        team_name = account["team_name"]

        for row in account.get("age_rows", []):
            bucket = str(
                row.get("age")
                or "Unknown"
            ).strip() or "Unknown"

            bucket_metrics = age_map.setdefault(
                bucket,
                {
                    "spend": 0.0,
                    "leads": 0.0,
                },
            )

            bucket_metrics["spend"] += to_float(
                row.get("spend")
            )
            bucket_metrics["leads"] += row_results(
                row,
                team_name=team_name,
                account_name=account_name,
            )

        for error in account.get("errors", []):
            if error.startswith("age:"):
                errors.append(
                    f"{account_name}: {error}"
                )

    return {
        "age": age_map,
        "spend": sum(
            row["spend"]
            for row in age_map.values()
        ),
        "leads": sum(
            row["leads"]
            for row in age_map.values()
        ),
        "errors": errors,
    }


def aggregate_governorate(
    account_results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    regions: dict[str, dict[str, float]] = {}
    errors: list[str] = []

    for account in account_results:
        account_name = account["account_name"]
        team_name = account["team_name"]

        for row in account.get("region_rows", []):
            region = str(
                row.get("region")
                or "Unknown"
            ).strip() or "Unknown"

            metrics = regions.setdefault(
                region,
                {
                    "spend": 0.0,
                    "leads": 0.0,
                },
            )

            metrics["spend"] += to_float(
                row.get("spend")
            )
            metrics["leads"] += row_results(
                row,
                team_name=team_name,
                account_name=account_name,
            )

        for error in account.get("errors", []):
            if error.startswith("region:"):
                errors.append(
                    f"{account_name}: {error}"
                )

    return {
        "regions": regions,
        "spend": sum(
            row["spend"]
            for row in regions.values()
        ),
        "leads": sum(
            row["leads"]
            for row in regions.values()
        ),
        "errors": errors,
    }


# ============================================================
# WhatsApp report builders
# ============================================================

def date_line(period: Period) -> str:
    if period.since == period.until:
        return period.since
    return f"{period.since} → {period.until}"


def build_spend_report(
    period: Period,
    agent_code: str,
    metrics: dict[str, Any],
    generated_at: datetime,
) -> str:
    total_spend = to_float(metrics.get("spend"))
    total_leads = to_float(metrics.get("leads"))
    agent_totals = metrics.get("agents", {})
    male_details = metrics.get("male_details", [])

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 *{period.label.upper()} PERFORMANCE*",
        f"👤 *{scope_label(agent_code)}*",
        f"🗓 {date_line(period)}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Spend: *{money(total_spend)}*",
        f"🎯 Leads: *{leads_text(total_leads)}*",
        f"📉 CPL: *{cpl_text(total_spend, total_leads)}*",
    ]

    if agent_code == "ALL":
        ranked: list[dict[str, Any]] = []

        for code, totals in agent_totals.items():
            if code == "UNKNOWN":
                continue

            spend = to_float(totals.get("spend"))
            leads = to_float(totals.get("leads"))
            cpl = safe_div(spend, leads)

            if spend > 0 and leads > 0 and cpl is not None:
                ranked.append({
                    "code": code,
                    "spend": spend,
                    "leads": leads,
                    "cpl": cpl,
                })

        highest = (
            max(ranked, key=lambda row: row["cpl"])
            if ranked else None
        )
        lowest = (
            min(ranked, key=lambda row: row["cpl"])
            if ranked else None
        )

        lines.extend([
            "",
            "🏆 *CPL CHECK*",
        ])

        if highest:
            lines.append(
                f"🔴 Highest: {buyer_name(highest['code'])} "
                f"({highest['code']}) — {money(highest['cpl'])}"
            )
        else:
            lines.append(
                "🔴 Highest: No valid CPL"
            )

        if lowest:
            lines.append(
                f"🟢 Lowest: {buyer_name(lowest['code'])} "
                f"({lowest['code']}) — {money(lowest['cpl'])}"
            )
        else:
            lines.append(
                "🟢 Lowest: No valid CPL"
            )

        lines.extend([
            "",
            "👥 *AGENTS*",
        ])

        visible_codes = [
            code
            for code in MEDIA_BUYER_MAP
            if code in agent_totals
        ]

        visible_codes.sort(
            key=lambda code: (
                -to_float(
                    agent_totals[code].get("spend")
                ),
                code,
            )
        )

        if not visible_codes:
            lines.append("No agent data found.")
        else:
            for code in visible_codes:
                row = agent_totals[code]
                spend = to_float(row.get("spend"))
                leads = to_float(row.get("leads"))

                lines.extend([
                    "",
                    f"*{buyer_name(code)} ({code})*",
                    f"Spend: {money(spend)}",
                    f"Leads: {leads_text(leads)}",
                    f"CPL: {cpl_text(spend, leads)}",
                ])

    else:
        row = agent_totals.get(
            agent_code,
            {
                "spend": total_spend,
                "leads": total_leads,
            },
        )
        spend = to_float(row.get("spend"))
        leads = to_float(row.get("leads"))

        lines.extend([
            "",
            "👤 *AGENT PERFORMANCE*",
            f"{buyer_name(agent_code)} ({agent_code})",
            f"Spend: {money(spend)}",
            f"Leads: {leads_text(leads)}",
            f"CPL: {cpl_text(spend, leads)}",
        ])

    lines.extend([
        "",
        "👨 *MALE SPEND*",
    ])

    if not male_details:
        lines.append(
            "✅ No Male Spend detected"
        )
    else:
        total_male = sum(
            to_float(row.get("spend"))
            for row in male_details
        )
        lines.append(
            f"⚠️ Total Male Spend: *{money(total_male)}*"
        )

        for index, row in enumerate(
            male_details,
            1,
        ):
            lines.extend([
                "",
                f"*Male Alert #{index}*",
                f"Spend: {money(row.get('spend'))}",
                f"Agent: {row.get('agent', 'Unknown')} "
                f"({row.get('buyer_code', 'UNKNOWN')})",
                f"Account: {row.get('account_name', '-')}",
                f"ID: {row.get('account_id', '-')}",
            ])

    errors = metrics.get("errors", [])
    if errors:
        lines.extend([
            "",
            f"⚠️ Data warnings: {len(errors)}",
        ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()


def ordered_age_buckets(
    age_map: dict[str, dict[str, float]],
) -> list[str]:
    extras = sorted(
        [
            key
            for key in age_map
            if key not in AGE_BUCKET_ORDER
        ],
        key=str.casefold,
    )
    return AGE_BUCKET_ORDER + extras


def build_age_report(
    period: Period,
    agent_code: str,
    metrics: dict[str, Any],
    generated_at: datetime,
) -> str:
    age_map = metrics.get("age", {})
    total_spend = to_float(metrics.get("spend"))
    total_leads = to_float(metrics.get("leads"))

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 *AGE REPORT — {period.label}*",
        f"👤 *{scope_label(agent_code)}*",
        f"🗓 {date_line(period)}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Total Spend: *{money(total_spend)}*",
        f"🎯 Total Leads: *{leads_text(total_leads)}*",
        f"📉 Overall CPL: *{cpl_text(total_spend, total_leads)}*",
        "",
    ]

    for bucket in ordered_age_buckets(age_map):
        row = age_map.get(
            bucket,
            {
                "spend": 0.0,
                "leads": 0.0,
            },
        )
        spend = to_float(row.get("spend"))
        leads = to_float(row.get("leads"))

        lines.extend([
            f"🔹 *{bucket}*",
            f"Spend: {money(spend)}",
            f"Leads: {leads_text(leads)}",
            f"CPL: {cpl_text(spend, leads)}",
            f"Share: {percentage_text(spend, total_spend)}",
            "",
        ])

    errors = metrics.get("errors", [])
    if errors:
        lines.append(
            f"⚠️ Data warnings: {len(errors)}"
        )

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()


def build_governorate_report(
    period: Period,
    agent_code: str,
    metrics: dict[str, Any],
    generated_at: datetime,
) -> str:
    regions = metrics.get("regions", {})
    total_spend = to_float(metrics.get("spend"))
    total_leads = to_float(metrics.get("leads"))

    sorted_regions = sorted(
        regions.items(),
        key=lambda item: (
            -to_float(item[1].get("spend")),
            item[0].casefold(),
        ),
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"🗺️ *GOVERNORATE REPORT — {period.label}*",
        f"👤 *{scope_label(agent_code)}*",
        f"🗓 {date_line(period)}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Total Spend: *{money(total_spend)}*",
        f"🎯 Total Leads: *{leads_text(total_leads)}*",
        f"📉 Overall CPL: *{cpl_text(total_spend, total_leads)}*",
        "",
    ]

    if not sorted_regions:
        lines.append(
            "No governorate data returned."
        )
    else:
        for index, (region, row) in enumerate(
            sorted_regions,
            1,
        ):
            spend = to_float(row.get("spend"))
            leads = to_float(row.get("leads"))

            lines.extend([
                f"*{index}. {region}*",
                f"Spend: {money(spend)}",
                f"Leads: {leads_text(leads)}",
                f"CPL: {cpl_text(spend, leads)}",
                f"Share: {percentage_text(spend, total_spend)}",
                "",
            ])

    errors = metrics.get("errors", [])
    if errors:
        lines.append(
            f"⚠️ Data warnings: {len(errors)}"
        )

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()



# ============================================================
# Cairo / Qaoud report builders
# ============================================================
# These are intentionally separate from the Taher builders above so the
# existing Taher message structure and agent logic remain untouched.

def build_cairo_spend_report(
    period: Period,
    metrics: dict[str, Any],
    generated_at: datetime,
) -> str:
    total_spend = to_float(metrics.get("spend"))
    total_leads = to_float(metrics.get("leads"))
    male_details = metrics.get("male_details", [])

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 *CAIRO TEAM — {period.label.upper()} PERFORMANCE*",
        "🏢 *Cairo Team (Qaoud) — Overall*",
        f"🗓 {date_line(period)}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Spend: *{money(total_spend)}*",
        f"🎯 Leads: *{leads_text(total_leads)}*",
        f"📉 CPL: *{cpl_text(total_spend, total_leads)}*",
        "",
        "👨 *MALE SPEND*",
    ]

    if not male_details:
        lines.append("✅ No Male Spend detected")
    else:
        total_male = sum(
            to_float(row.get("spend"))
            for row in male_details
        )
        lines.append(
            f"⚠️ Total Male Spend: *{money(total_male)}*"
        )

        for index, row in enumerate(male_details, 1):
            lines.extend([
                "",
                f"*Male Alert #{index}*",
                f"Spend: {money(row.get('spend'))}",
                f"Account: {row.get('account_name', '-')}",
                f"ID: {row.get('account_id', '-')}",
            ])

    errors = metrics.get("errors", [])
    if errors:
        lines.extend([
            "",
            f"⚠️ Data warnings: {len(errors)}",
        ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()


def build_cairo_age_report(
    period: Period,
    metrics: dict[str, Any],
    generated_at: datetime,
) -> str:
    age_map = metrics.get("age", {})
    total_spend = to_float(metrics.get("spend"))
    total_leads = to_float(metrics.get("leads"))

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 *CAIRO TEAM — AGE REPORT — {period.label}*",
        "🏢 *Cairo Team (Qaoud) — Overall*",
        f"🗓 {date_line(period)}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Total Spend: *{money(total_spend)}*",
        f"🎯 Total Leads: *{leads_text(total_leads)}*",
        f"📉 Overall CPL: *{cpl_text(total_spend, total_leads)}*",
        "",
    ]

    for bucket in ordered_age_buckets(age_map):
        row = age_map.get(
            bucket,
            {"spend": 0.0, "leads": 0.0},
        )
        spend = to_float(row.get("spend"))
        leads = to_float(row.get("leads"))

        lines.extend([
            f"🔹 *{bucket}*",
            f"Spend: {money(spend)}",
            f"Leads: {leads_text(leads)}",
            f"CPL: {cpl_text(spend, leads)}",
            f"Share: {percentage_text(spend, total_spend)}",
            "",
        ])

    errors = metrics.get("errors", [])
    if errors:
        lines.append(f"⚠️ Data warnings: {len(errors)}")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()


def build_cairo_governorate_report(
    period: Period,
    metrics: dict[str, Any],
    generated_at: datetime,
) -> str:
    regions = metrics.get("regions", {})
    total_spend = to_float(metrics.get("spend"))
    total_leads = to_float(metrics.get("leads"))

    sorted_regions = sorted(
        regions.items(),
        key=lambda item: (
            -to_float(item[1].get("spend")),
            item[0].casefold(),
        ),
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"🗺️ *CAIRO TEAM — GOVERNORATE REPORT — {period.label}*",
        "🏢 *Cairo Team (Qaoud) — Overall*",
        f"🗓 {date_line(period)}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Total Spend: *{money(total_spend)}*",
        f"🎯 Total Leads: *{leads_text(total_leads)}*",
        f"📉 Overall CPL: *{cpl_text(total_spend, total_leads)}*",
        "",
    ]

    if not sorted_regions:
        lines.append("No governorate data returned.")
    else:
        for index, (region, row) in enumerate(
            sorted_regions,
            1,
        ):
            spend = to_float(row.get("spend"))
            leads = to_float(row.get("leads"))

            lines.extend([
                f"*{index}. {region}*",
                f"Spend: {money(spend)}",
                f"Leads: {leads_text(leads)}",
                f"CPL: {cpl_text(spend, leads)}",
                f"Share: {percentage_text(spend, total_spend)}",
                "",
            ])

    errors = metrics.get("errors", [])
    if errors:
        lines.append(f"⚠️ Data warnings: {len(errors)}")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()


# ============================================================
# Allocation report (same core logic as reference project)
# ============================================================

def safe_ratio_pct(a: float, b: float) -> float | None:
    if b and b > 0:
        return (a / b) * 100.0
    return None


def build_allocation_note(
    active_daily_budget: float,
    allocation_budget: float,
) -> str:
    if allocation_budget <= 0:
        return (
            "Allocation Budget is not configured. "
            "Set OVERALL_ALLOCATION_BUDGET."
        )

    diff = active_daily_budget - allocation_budget
    diff_pct = (
        diff / allocation_budget
    ) * 100.0
    abs_pct = abs(diff_pct)

    if abs_pct <= ALLOCATION_ALIGNED_TOLERANCE_PCT:
        return (
            "Daily Budget is aligned with Allocation "
            f"({diff:+,.2f} EGP / {diff_pct:+.1f}%)."
        )

    if (
        diff < 0
        and abs_pct
        >= ALLOCATION_SIGNIFICANT_DIFF_PCT
    ):
        return (
            "Daily Budget is significantly BELOW Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )

    if diff < 0:
        return (
            "Daily Budget is below Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )

    if abs_pct >= ALLOCATION_SIGNIFICANT_DIFF_PCT:
        return (
            "Daily Budget is significantly ABOVE Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )

    return (
        "Daily Budget is above Allocation by "
        f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
    )


def filter_snapshot_agent(
    snapshot_df: pd.DataFrame,
    agent_code: str,
) -> pd.DataFrame:
    if snapshot_df.empty:
        return snapshot_df

    if agent_code == "ALL":
        return snapshot_df.copy()

    return snapshot_df[
        snapshot_df["buyer_code"]
        .astype(str)
        == agent_code
    ].copy()


def build_allocation_overall(
    snapshot_df: pd.DataFrame,
    generated_at: datetime,
) -> str:
    allocation = float(
        OVERALL_ALLOCATION_BUDGET
    )

    total_spend = (
        pd.to_numeric(
            snapshot_df.get(
                "spend_today",
                pd.Series(dtype=float),
            ),
            errors="coerce",
        )
        .fillna(0)
        .sum()
        if not snapshot_df.empty
        else 0.0
    )

    total_daily = (
        pd.to_numeric(
            snapshot_df.get(
                "active_daily_budget",
                pd.Series(dtype=float),
            ),
            errors="coerce",
        )
        .fillna(0)
        .sum()
        if not snapshot_df.empty
        else 0.0
    )

    spend_vs_allocation = safe_ratio_pct(
        total_spend,
        allocation,
    )
    remaining_allocation = (
        allocation - total_spend
        if allocation > 0
        else None
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "💰 *OVERALL ALLOCATION REPORT*",
        "👤 *All Agents*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Spend Today: *{money(total_spend)}*",
        f"Daily Budget: *{money(total_daily)}*",
        (
            f"Allocation Budget: *{money(allocation)}*"
            if allocation > 0
            else "Allocation Budget: *NOT CONFIGURED*"
        ),
        f"Spend vs Allocation: *{pct(spend_vs_allocation)}*",
        (
            f"Remaining Allocation: *{money(remaining_allocation)}*"
            if remaining_allocation is not None
            else "Remaining Allocation: *N/A*"
        ),
        f"Note: {build_allocation_note(total_daily, allocation)}",
        "",
        "👥 *AGENTS PERFORMANCE*",
    ]

    available_codes = [
        code
        for code in MEDIA_BUYER_MAP
        if (
            not snapshot_df.empty
            and (
                snapshot_df["buyer_code"]
                .astype(str)
                == code
            ).any()
        )
    ]

    for code in available_codes:
        agent_df = snapshot_df[
            snapshot_df["buyer_code"]
            .astype(str)
            == code
        ].copy()

        spend = (
            pd.to_numeric(
                agent_df["spend_today"],
                errors="coerce",
            )
            .fillna(0)
            .sum()
        )
        daily = (
            pd.to_numeric(
                agent_df["active_daily_budget"],
                errors="coerce",
            )
            .fillna(0)
            .sum()
        )

        spend_vs_daily = safe_ratio_pct(
            spend,
            daily,
        )
        remaining_daily = daily - spend

        lines.extend([
            "",
            f"*{buyer_name(code)} ({code})*",
            f"Daily Budget: {money(daily)}",
            f"Spend Today: {money(spend)}",
            f"Spend vs Daily: {pct(spend_vs_daily)}",
            f"Remaining vs Daily: {money(remaining_daily)}",
        ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()


def build_allocation_agent_balance(
    snapshot_df: pd.DataFrame,
    agent_code: str,
    generated_at: datetime,
) -> str:
    df = snapshot_df[
        (
            snapshot_df["buyer_code"]
            .astype(str)
            == agent_code
        )
        & (
            pd.to_numeric(
                snapshot_df["active_daily_budget"],
                errors="coerce",
            ).fillna(0)
            > 0
        )
    ].copy()

    df = df.sort_values("account_name")

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"💰 *{buyer_name(agent_code)} ({agent_code})*",
        "*Balance & Daily Budget*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if df.empty:
        lines.extend([
            "No accounts with Spend Today > 0 "
            "and a daily budget were found.",
            "",
            f"Overall Daily Budget: {money(0)}",
            f"Overall Balance: {money(0)}",
        ])
    else:
        for _, row in df.iterrows():
            coverage = row.get(
                "coverage_days"
            )
            daily_budget = to_float(
                row.get("active_daily_budget")
            )
            balance = row.get("balance")
            currency = str(
                row.get("currency")
                or "EGP"
            )

            coverage_value = (
                to_float(coverage, default=float("nan"))
                if coverage is not None
                else float("nan")
            )

            if (
                math.isfinite(coverage_value)
                and coverage_value
                <= CRITICAL_COVERAGE_DAYS
            ):
                alarm = (
                    "⚠️ Balance is below the 3-day target."
                )
            elif not math.isfinite(coverage_value):
                alarm = (
                    "⚠️ Balance unavailable — verify balance source."
                )
            else:
                alarm = (
                    "✅ Balance coverage is above the alarm threshold."
                )

            lines.extend([
                f"*Ad Account ID:* {row.get('account_id', '-')}",
                f"*Ad Account Name:* {row.get('account_name', '-')}",
                f"*Daily Budget:* {money(daily_budget, currency)}",
                f"*Balance:* {money(balance, currency)}",
                f"*Balance Coverage:* {days_text(coverage)}",
                f"*Alarm:* {alarm}",
                "",
            ])

        overall_budget = (
            pd.to_numeric(
                df["active_daily_budget"],
                errors="coerce",
            )
            .fillna(0)
            .sum()
        )
        overall_balance = (
            pd.to_numeric(
                df["balance"],
                errors="coerce",
            )
            .fillna(0)
            .sum()
        )

        lines.extend([
            "──────────────",
            f"*Overall Daily Budget:* {money(overall_budget)}",
            f"*Overall Balance:* {money(overall_balance)}",
        ])

    lines.extend([
        "",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()


def build_allocation_recharge(
    snapshot_df: pd.DataFrame,
    agent_code: str,
) -> str:
    df = snapshot_df[
        (
            snapshot_df["buyer_code"]
            .astype(str)
            == agent_code
        )
        & (
            pd.to_numeric(
                snapshot_df["active_daily_budget"],
                errors="coerce",
            ).fillna(0)
            > 0
        )
    ].copy()

    if not df.empty:
        coverage = pd.to_numeric(
            df["coverage_days"],
            errors="coerce",
        )
        df = df[
            coverage < TARGET_COVERAGE_DAYS
        ].copy()

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"🧾 *RECHARGE TO 3 DAYS — {buyer_name(agent_code)} ({agent_code})*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    recharge_count = 0

    for _, row in df.iterrows():
        daily_budget = pd.to_numeric(
            pd.Series([
                row.get("active_daily_budget")
            ]),
            errors="coerce",
        ).iloc[0]

        current_balance = pd.to_numeric(
            pd.Series([
                row.get("balance")
            ]),
            errors="coerce",
        ).iloc[0]

        if (
            pd.isna(daily_budget)
            or pd.isna(current_balance)
            or float(daily_budget) <= 0
        ):
            continue

        recharge_needed = max(
            0.0,
            (
                float(daily_budget)
                * float(TARGET_COVERAGE_DAYS)
            )
            - float(current_balance),
        )

        if recharge_needed <= 0:
            continue

        recharge_count += 1

        lines.extend([
            f"Acc ID : {row.get('account_id', '-')}",
            f"Balance : {whole_money_100(recharge_needed, str(row.get('currency') or 'EGP'))}",
            "",
        ])

    if recharge_count == 0:
        lines.append(
            "No accounts currently need recharge "
            "to reach the 3-day target."
        )

    return "\n".join(lines).strip()



def get_cairo_allocation_accounts(
    client: AllocationMetaClient,
) -> pd.DataFrame:
    """Load Cairo/Qaoud accounts only for Allocation.

    This uses the same Allocation Meta client and budget logic as Taher, but
    the account discovery is restricted to the Cairo Business ID so Taher data
    cannot leak into a Cairo request.
    """
    business_id = TEAM_BUSINESS_IDS["Cairo Team"]
    rows: list[dict[str, Any]] = []

    fields = (
        "id,account_id,name,account_status,currency,"
        "balance,amount_spent,spend_cap,funding_source_details,"
        "timezone_name,timezone_offset_hours_utc"
    )

    errors: list[str] = []

    for edge in ("owned_ad_accounts", "client_ad_accounts"):
        try:
            part = client.fetch_all_pages(
                f"{business_id}/{edge}",
                {
                    "fields": fields,
                    "limit": 500,
                },
            )
            rows.extend(part)
        except Exception as exc:
            errors.append(f"{business_id}/{edge}: {exc}")

    if errors:
        for error in errors:
            print(f"WARNING Cairo Allocation discovery: {error}")

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    if "id" not in df.columns:
        return pd.DataFrame()

    if "name" not in df.columns:
        df["name"] = df["id"].astype(str)

    if "currency" not in df.columns:
        df["currency"] = "EGP"

    df = (
        df.sort_values(
            [col for col in ["name", "id"] if col in df.columns]
        )
        .drop_duplicates("id", keep="first")
        .reset_index(drop=True)
    )

    return df


def build_cairo_allocation_report(
    snapshot_df: pd.DataFrame,
    generated_at: datetime,
) -> str:
    allocation = float(OVERALL_ALLOCATION_BUDGET)

    total_spend = (
        pd.to_numeric(
            snapshot_df.get(
                "spend_today",
                pd.Series(dtype=float),
            ),
            errors="coerce",
        ).fillna(0).sum()
        if not snapshot_df.empty
        else 0.0
    )

    total_daily = (
        pd.to_numeric(
            snapshot_df.get(
                "active_daily_budget",
                pd.Series(dtype=float),
            ),
            errors="coerce",
        ).fillna(0).sum()
        if not snapshot_df.empty
        else 0.0
    )

    total_balance = (
        pd.to_numeric(
            snapshot_df.get(
                "balance",
                pd.Series(dtype=float),
            ),
            errors="coerce",
        ).fillna(0).sum()
        if not snapshot_df.empty
        else 0.0
    )

    spend_vs_allocation = safe_ratio_pct(
        total_spend,
        allocation,
    )
    remaining_allocation = (
        allocation - total_spend
        if allocation > 0
        else None
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "💰 *CAIRO TEAM — ALLOCATION REPORT*",
        "🏢 *Cairo Team (Qaoud) — Overall*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Spend Today: *{money(total_spend)}*",
        f"Daily Budget: *{money(total_daily)}*",
        f"Overall Balance: *{money(total_balance)}*",
        (
            f"Allocation Budget: *{money(allocation)}*"
            if allocation > 0
            else "Allocation Budget: *NOT CONFIGURED*"
        ),
        f"Spend vs Allocation: *{pct(spend_vs_allocation)}*",
        (
            f"Remaining Allocation: *{money(remaining_allocation)}*"
            if remaining_allocation is not None
            else "Remaining Allocation: *N/A*"
        ),
        f"Note: {build_allocation_note(total_daily, allocation)}",
        "",
        "📂 *AD ACCOUNTS*",
    ]

    if snapshot_df.empty:
        lines.append("No Cairo Ad Account data found.")
    else:
        visible = snapshot_df.copy()
        visible["_daily"] = pd.to_numeric(
            visible.get(
                "active_daily_budget",
                pd.Series(index=visible.index, dtype=float),
            ),
            errors="coerce",
        ).fillna(0)
        visible["_spend"] = pd.to_numeric(
            visible.get(
                "spend_today",
                pd.Series(index=visible.index, dtype=float),
            ),
            errors="coerce",
        ).fillna(0)

        visible = visible.sort_values(
            ["_daily", "_spend", "account_name"],
            ascending=[False, False, True],
        )

        for _, row in visible.iterrows():
            daily = to_float(row.get("active_daily_budget"))
            spend = to_float(row.get("spend_today"))
            balance = row.get("balance")
            coverage = row.get("coverage_days")
            currency = str(row.get("currency") or "EGP")

            # Keep the message useful without any agent grouping.
            if daily <= 0 and spend <= 0:
                continue

            lines.extend([
                "",
                f"*{row.get('account_name', row.get('account_id', '-'))}*",
                f"ID: {row.get('account_id', '-')}",
                f"Spend Today: {money(spend, currency)}",
                f"Daily Budget: {money(daily, currency)}",
                f"Balance: {money(balance, currency)}",
                f"Coverage: {days_text(coverage)}",
            ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ {generated_at.strftime('%Y-%m-%d %H:%M:%S')} Cairo",
    ])

    return "\n".join(lines).strip()


def build_allocation_messages(
    config: Config,
    agent_code: str,
    generated_at: datetime,
    team_key: str = "taher",
) -> list[tuple[str, str]]:
    if team_key == "cairo":
        print(
            "Fetching current Cairo Allocation / Balance snapshot..."
        )

        cairo_client = AllocationMetaClient(
            access_token=config.meta_access_token,
            api_version=config.api_version,
        )

        cairo_accounts = get_cairo_allocation_accounts(
            cairo_client
        )

        if cairo_accounts.empty:
            raise RuntimeError(
                "Cairo Allocation returned no eligible accounts."
            )

        cairo_snapshot, _details = fetch_full_snapshot(
            cairo_client,
            cairo_accounts,
            max_workers=config.max_workers,
            spend_date=generated_at.date(),
        )

        if cairo_snapshot.empty:
            raise RuntimeError(
                "Cairo Allocation snapshot is empty."
            )

        return [(
            "Cairo Allocation",
            build_cairo_allocation_report(
                cairo_snapshot,
                generated_at,
            ),
        )]

    # Taher Allocation path below is intentionally unchanged.
    print(
        "Fetching current Allocation / Balance snapshot..."
    )

    client = AllocationMetaClient(
        access_token=config.meta_access_token,
        api_version=config.api_version,
    )

    accounts = client.get_ad_accounts()

    if accounts.empty:
        raise RuntimeError(
            "Allocation Meta client returned no eligible accounts."
        )

    snapshot_df, _details = fetch_full_snapshot(
        client,
        accounts,
        max_workers=config.max_workers,
        spend_date=generated_at.date(),
    )

    if snapshot_df.empty:
        raise RuntimeError(
            "Allocation snapshot is empty."
        )

    if agent_code == "ALL":
        return [(
            "Allocation",
            build_allocation_overall(
                snapshot_df,
                generated_at,
            ),
        )]

    selected = filter_snapshot_agent(
        snapshot_df,
        agent_code,
    )

    return [
        (
            "Allocation Balance",
            build_allocation_agent_balance(
                selected,
                agent_code,
                generated_at,
            ),
        ),
        (
            "Allocation Recharge",
            build_allocation_recharge(
                selected,
                agent_code,
            ),
        ),
    ]


# ============================================================
# WhatsApp send
# ============================================================

def split_message(
    message: str,
    max_chars: int = 3500,
) -> list[str]:
    if len(message) <= max_chars:
        return [message]

    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in message.splitlines():
        added = len(line) + 1

        if current and size + added > max_chars:
            chunks.append(
                "\n".join(current).strip()
            )
            current = []
            size = 0

        current.append(line)
        size += added

    if current:
        chunks.append(
            "\n".join(current).strip()
        )

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [
            f"Report Part {index}/{total}\n\n{chunk}"
            for index, chunk in enumerate(chunks, 1)
        ]

    return chunks


def send_whatsapp(
    config: Config,
    body: str,
) -> None:
    url = (
        f"{BASE_URL}/{config.api_version}/"
        f"{config.whatsapp_phone_number_id}/messages"
    )

    request_json(
        "POST",
        url,
        headers={
            "Authorization": (
                f"Bearer {config.whatsapp_access_token}"
            ),
            "Content-Type": "application/json",
        },
        payload={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": config.recipient,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": body,
            },
        },
    )


def send_report_messages(
    config: Config,
    messages: list[tuple[str, str]],
) -> None:
    for label, message in messages:
        for chunk in split_message(message):
            send_whatsapp(config, chunk)
            time.sleep(0.35)

        print(
            f"SENT {label} -> "
            f"{mask_phone(config.recipient)}"
        )


# ============================================================
# Runner
# ============================================================

def build_requested_messages(
    config: Config,
    period: Period,
    team_key: str,
    agent_code: str,
    report_types: list[str],
    generated_at: datetime,
) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []

    meta_types = [
        report_type
        for report_type in report_types
        if report_type
        in {"spend", "age", "governorate"}
    ]

    account_results: list[dict[str, Any]] = []

    if meta_types:
        account_results, _errors = fetch_report_data(
            config,
            period,
            team_key,
            agent_code,
            meta_types,
        )

    if "spend" in report_types:
        metrics = aggregate_spend(
            account_results
        )

        if team_key == "cairo":
            spend_message = build_cairo_spend_report(
                period,
                metrics,
                generated_at,
            )
        else:
            spend_message = build_spend_report(
                period,
                agent_code,
                metrics,
                generated_at,
            )

        messages.append((
            "Spend",
            spend_message,
        ))

    if "age" in report_types:
        metrics = aggregate_age(
            account_results
        )

        if team_key == "cairo":
            age_message = build_cairo_age_report(
                period,
                metrics,
                generated_at,
            )
        else:
            age_message = build_age_report(
                period,
                agent_code,
                metrics,
                generated_at,
            )

        messages.append((
            "Age",
            age_message,
        ))

    if "governorate" in report_types:
        metrics = aggregate_governorate(
            account_results
        )

        if team_key == "cairo":
            governorate_message = build_cairo_governorate_report(
                period,
                metrics,
                generated_at,
            )
        else:
            governorate_message = build_governorate_report(
                period,
                agent_code,
                metrics,
                generated_at,
            )

        messages.append((
            "Governorate",
            governorate_message,
        ))

    if "allocation" in report_types:
        messages.extend(
            build_allocation_messages(
                config,
                agent_code,
                generated_at,
                team_key=team_key,
            )
        )

    return messages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Flexible WhatsApp Meta Ads report runner."
        )
    )

    parser.add_argument(
        "--range",
        dest="range_value",
        required=True,
    )
    parser.add_argument(
        "--team",
        default="taher",
    )
    parser.add_argument(
        "--agent",
        default="all",
    )
    parser.add_argument(
        "--reports",
        default="spend",
    )
    parser.add_argument(
        "--recipient",
        required=True,
    )
    parser.add_argument(
        "--raw-command",
        default="",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    team_key = normalize_team_key(
        args.team
    )

    agent_code = normalize_agent_code(
        args.agent
    )

    # Cairo/Qaoud is intentionally Overall only.
    if team_key == "cairo":
        agent_code = "ALL"

    report_types = parse_report_types(
        args.reports
    )

    config = load_config(
        args.recipient,
        dry_run=args.dry_run,
    )

    generated_at, periods, daily_split = build_periods(
        config.timezone,
        args.range_value,
    )

    range_description = (
        f"Each Day: {periods[0].since} -> {periods[-1].until}"
        if daily_split
        else (
            periods[0].label
            if periods[0].since == periods[0].until
            else f"{periods[0].since} -> {periods[0].until}"
        )
    )

    print("=" * 80)
    print("Smart Meta WhatsApp Report")
    print(f"Build: {REPORT_BUILD}")
    print(f"Command: {args.raw_command}")
    print(f"Range: {range_description}")
    print(f"Team: {team_label(team_key)}")
    print(f"Scope: {request_scope_label(team_key, agent_code)}")
    print(f"Reports: {', '.join(report_types)}")
    print(
        f"Recipient: {mask_phone(config.recipient)}"
    )
    print("=" * 80)

    messages: list[tuple[str, str]] = []

    if daily_split:
        # Spend / Age / Governorate are generated independently for each day.
        # Allocation/Balance is intentionally current-state and is sent ONCE.
        daily_report_types = [
            report_type
            for report_type in report_types
            if report_type != "allocation"
        ]

        for day_period in periods:
            if not daily_report_types:
                break

            day_messages = build_requested_messages(
                config,
                day_period,
                team_key,
                agent_code,
                daily_report_types,
                generated_at,
            )
            messages.extend(
                (f"{label} {day_period.since}", body)
                for label, body in day_messages
            )

        if "allocation" in report_types:
            messages.extend(
                build_allocation_messages(
                    config,
                    agent_code,
                    generated_at,
                    team_key=team_key,
                )
            )
    else:
        messages = build_requested_messages(
            config,
            periods[0],
            team_key,
            agent_code,
            report_types,
            generated_at,
        )

    if not messages:
        raise RuntimeError(
            "No report messages were generated."
        )

    for label, message in messages:
        print("\n" + "=" * 80)
        print(f"PREVIEW: {label}")
        print("=" * 80)
        print(message)

    if args.dry_run:
        print(
            "\nDRY RUN complete. "
            "No WhatsApp messages sent."
        )
        return 0

    send_report_messages(
        config,
        messages,
    )

    if len(messages) > 1:
        completion = "\n".join([
            "━━━━━━━━━━━━━━━━━━━━",
            "✅ *REQUEST COMPLETED*",
            f"Reports sent: {len(messages)}",
            f"Scope: {request_scope_label(team_key, agent_code)}",
            "━━━━━━━━━━━━━━━━━━━━",
        ])
        send_whatsapp(config, completion)

    print("DONE")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"FATAL: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
