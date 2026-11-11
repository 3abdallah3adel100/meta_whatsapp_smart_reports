"""Allocation collection ported from balance-main/run_reports.py.

Cairo uses only its business accounts; Taher keeps the reference /me scope.
No messaging or performance-report code is imported from the reference.
"""
from __future__ import annotations
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
from allocation_meta import MetaClient, fetch_full_snapshot

# Business IDs for Taher. The GitHub Variable overrides this default.
# No tokens are ever saved to a dataframe or written to the report.
DEFAULT_TAHER_BUSINESS_IDS = (
    "751488620224306",
    "1178859133269743",
    "1370772291128896",
)
ACCOUNT_NAME_PREFIXES = ("OK-FB-HR-", "OK-FB-NF-", "US-FB-HR-", "US-BO-HR-")
META_ACCOUNT_FIELDS = (
    "id,account_id,name,account_status,currency,balance,amount_spent,"
    "spend_cap,funding_source_details,timezone_name,timezone_offset_hours_utc"
)


def configured_business_ids(raw: str) -> list[str]:
    values = [x.strip() for x in re.split(r"[,;\n]+", raw or "") if x.strip()]
    if not values:
        values = list(DEFAULT_TAHER_BUSINESS_IDS)
    invalid = [value for value in values if not re.fullmatch(r"\d+", value)]
    if invalid:
        raise RuntimeError("TAHER_BUSINESS_IDS must contain numeric IDs separated by commas")
    return list(dict.fromkeys(values))


def redact_meta_error(exc: Exception, tokens: dict[str, str]) -> str:
    message = str(exc)
    for token in tokens.values():
        if token:
            message = message.replace(token, "[REDACTED]")
    return message[:600]


def graph_accounts(token: str, api_version: str, path: str, fields: str,
                   client: MetaClient | None = None) -> list[dict]:
    """Read all pages with the same rate-limit policy as other Meta requests."""
    graph_client = client or MetaClient(access_token=token, api_version=api_version)
    url = f"https://graph.facebook.com/{api_version}/{path}"
    params = {"access_token": token, "fields": fields, "limit": 500}
    rows: list[dict] = []
    while url:
        data = graph_client._get(url, params)
        rows.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if next_url and not next_url.startswith("https://graph.facebook.com/"):
            raise RuntimeError("Unexpected pagination host returned by Meta")
        url, params = next_url, None
    return rows


def account_key(row: dict) -> str:
    value = str(row.get("id") or row.get("account_id") or "").strip()
    return value.replace("act_", "")


def suitable_baseline_account(row: dict, business_ids: list[str]) -> bool:
    source = str(row.get("source") or "")
    # Preserve existing /me discovery rules, but don't add unrelated businesses
    # that a repository's older static config.py may include.
    if source.startswith("business/"):
        return any(f"business/{business_id}/" in source for business_id in business_ids)
    name = str(row.get("name") or "").strip().upper()
    return any(prefix in name for prefix in ACCOUNT_NAME_PREFIXES)


def discover_all_accounts(clients: dict[str, MetaClient], business_ids: list[str],
                          api_version: str, include_me_accounts: bool = True) -> tuple[dict[str, list[tuple[str, dict]]], list[str]]:
    """Return one account ID with its available token candidates (never token values)."""
    discovered: dict[str, dict[str, dict]] = {}
    warnings: list[str] = []
    business_counts: Counter = Counter()
    business_edge_success: Counter = Counter()
    all_tokens = {label: client.access_token for label, client in clients.items()}

    def add(row: dict, token_label: str, source: str) -> None:
        value = dict(row)
        key = account_key(value)
        if not key:
            return
        value["id"] = f"act_{key}"
        value["account_id"] = key
        value["source"] = source
        per_token = discovered.setdefault(key, {})
        if token_label in per_token:
            # Keep fields already provided by meta_api.get_ad_accounts(); enrich
            # only if the explicit Business API supplies a real value.
            old = per_token[token_label]
            old.update({k: v for k, v in value.items() if v is not None and v != ""})
        else:
            per_token[token_label] = value

    for label, client in clients.items():
        # Do not call get_ad_accounts() here: it re-queries these same
        # business edges (plus /me) and doubles discovery work. The explicit
        # queries below cover the selected businesses and preserve full
        # account metadata, including balances.

        # Explicitly query ALL selected businesses with EACH available token.
        # This ensures a new business is included even if config.py is unchanged.
        for business_id in business_ids:
            found_here = set()
            for edge in ("owned_ad_accounts", "client_ad_accounts"):
                source = f"business/{business_id}/{edge}"
                try:
                    try:
                        rows = graph_accounts(client.access_token, api_version,
                                              f"{business_id}/{edge}", META_ACCOUNT_FIELDS, client=client)
                    except RuntimeError as exc:
                        # A restricted optional account field should not hide
                        # otherwise accessible accounts. A rate limit must NOT
                        # trigger another identical request using fewer fields.
                        detail = str(exc).lower()
                        if ('code=17' in detail or 'code=4' in detail or
                                'code=32' in detail or 'code=613' in detail or
                                'request limit' in detail or 'rate limit' in detail):
                            raise
                        rows = graph_accounts(client.access_token, api_version,
                                              f"{business_id}/{edge}",
                                              "id,account_id,name,account_status,currency", client=client)
                    business_edge_success[business_id] += 1
                    for row in rows:
                        add(row, label, source)
                        if account_key(row):
                            found_here.add(account_key(row))
                except Exception as exc:
                    warnings.append(f"{label} | {source}: {redact_meta_error(exc, all_tokens)}")
            business_counts[(label, business_id)] += len(found_here)

        if not include_me_accounts:
            continue

        # Preserve the existing name-coded accounts that the token can access
        # outside the configured businesses. Do not include unrelated names.
        try:
            try:
                rows = graph_accounts(client.access_token, api_version, "me/adaccounts",
                                      META_ACCOUNT_FIELDS, client=client)
            except RuntimeError as exc:
                # Only retry with basic fields for a field/permission issue,
                # not when Meta is already limiting this token.
                detail = str(exc).lower()
                if any(x in detail for x in ("code=17", "code=4", "code=32", "code=613", "rate limit", "request limit")):
                    raise
                rows = graph_accounts(client.access_token, api_version, "me/adaccounts",
                                      "id,account_id,name,account_status,currency", client=client)
            for row in rows:
                name = str(row.get("name") or "").upper()
                if any(prefix in name for prefix in ACCOUNT_NAME_PREFIXES):
                    add(row, label, "me/adaccounts")
        except Exception as exc:
            warnings.append(f"{label} | me/adaccounts: {redact_meta_error(exc, all_tokens)}")

    unverified = [bid for bid in business_ids if not business_edge_success[bid]]
    if unverified:
        raise RuntimeError(
            "Neither token could access the owned/client edges of Business ID(s): "
            + ", ".join(unverified)
            + ". Reports were NOT sent to avoid an incomplete total."
        )

    for business_id in business_ids:
        summary = " | ".join(f"{label}={business_counts[(label, business_id)]}"
                             for label in clients)
        print(f"Business {business_id} discovered: {summary}")
    print(f"Unique ad accounts: {len(discovered)}; configured tokens: {len(clients)}")
    # Prefer token_1 on overlaps; fallback to token_2 if fetching fails.
    return {key: [(label, rows[label]) for label in clients if label in rows]
            for key, rows in discovered.items()}, warnings


def snapshot_failed(frame: pd.DataFrame, expected_account_id: str) -> bool:
    if frame.empty:
        return True
    row = frame.iloc[0]
    actual = str(row.get("account_id") or "").replace("act_", "")
    if actual and actual != expected_account_id:
        return True
    return str(row.get("fetch_status") or "").upper() == "ERROR"


def fetch_account_with_fallback(account_id: str, candidates: list[tuple[str, dict]],
                                clients: dict[str, MetaClient], spend_date) -> tuple[pd.DataFrame, list[dict], str, list[str]]:
    errors: list[str] = []
    first_partial = None
    all_tokens = {label: client.access_token for label, client in clients.items()}
    attempts = list(candidates)
    known_labels = {label for label, _ in attempts}
    if attempts:
        attempts.extend((label, dict(attempts[0][1])) for label in clients if label not in known_labels)
    for label, row in attempts:
        try:
            frame, details = fetch_full_snapshot(
                clients[label], pd.DataFrame([row]), max_workers=1, spend_date=spend_date
            )
            if not snapshot_failed(frame, account_id):
                row_error = frame.iloc[0].get("error")
                if row_error is None or pd.isna(row_error) or str(row_error).strip() == "":
                    return frame, details, label, errors
                if first_partial is None:
                    first_partial = (frame, details, label)
                errors.append(f"{label}: partial snapshot: {redact_meta_error(Exception(row_error), all_tokens)}")
                continue
            raw_errors = frame['error'].dropna().astype(str).tolist() if 'error' in frame.columns else []
            reason = '; '.join(raw_errors) or 'account fetch_status=ERROR or no matching row'
            safe_reason = redact_meta_error(Exception(reason), all_tokens)
            errors.append(f"{label}: {safe_reason}")
            print(f"ALLOCATION FETCH ERROR act_{account_id} via {label}: {safe_reason}")
        except Exception as exc:
            errors.append(f"{label}: {redact_meta_error(exc, all_tokens)}")
    if first_partial is not None:
        frame, details, label = first_partial
        return frame, details, label, errors
    raise RuntimeError(f"Account act_{account_id} failed for all available tokens: {'; '.join(errors)}")


def collect_multi_token_snapshot(clients: dict[str, MetaClient], business_ids: list[str],
                                 api_version: str, spend_date,
                                 max_workers: int, include_me_accounts: bool = True) -> tuple[pd.DataFrame, list[dict], list[str]]:
    discovered, warnings = discover_all_accounts(clients, business_ids, api_version, include_me_accounts)
    if not discovered:
        raise RuntimeError("No eligible ad accounts discovered from either token. Reports were NOT sent.")
    frames: list[pd.DataFrame] = []
    details: list[dict] = []
    failed: list[str] = []
    token_counts: Counter = Counter()
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 6))) as executor:
        futures = {executor.submit(fetch_account_with_fallback, account_id, candidates,
                                   clients, spend_date): account_id
                   for account_id, candidates in discovered.items()}
        for future in as_completed(futures):
            account_id = futures[future]
            try:
                frame, result_details, label, issues = future.result()
                frames.append(frame)
                details.extend(result_details)
                token_counts[label] += 1
                print(f"Fetched act_{account_id} via {label}")
                for item in issues:
                    warnings.append(f"act_{account_id}: {item}")
            except Exception as exc:
                failed.append(redact_meta_error(exc, {k: c.access_token for k, c in clients.items()}))
    if failed:
        # Never send an incomplete budget/coverage total as an overall total.
        raise RuntimeError(f"{len(failed)} accounts could not be fetched. Reports NOT sent. "
                           + " | ".join(failed[:12]))
    print("Snapshot by token: " + ", ".join(f"{label}={token_counts[label]}" for label in clients))
    return pd.concat(frames, ignore_index=True), details, warnings

