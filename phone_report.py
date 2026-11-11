"""WhatsApp destination reporting. Never infer a phone from an account/campaign name."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from allocation_meta import api_money_to_major

DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
PHONE_KEYS = {'whatsapp_phone_number', 'whatsapp_number', 'whats_app_business_phone_number'}
ID_KEYS = {'whats_app_business_phone_number_id', 'whatsapp_phone_number_id'}
MESSAGE_KEYS = ('onsite_conversion.messaging_conversation_started_7d', 'onsite_conversion.messaging_conversation_started')


def normalize_destination(value):
    digits = re.sub(r'\D', '', str(value or '').translate(DIGITS))
    if digits.startswith('00'):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith('01'):
        digits = '2' + digits
    elif len(digits) == 10 and digits.startswith(('10', '11', '12', '15')):
        digits = '20' + digits
    return digits if 8 <= len(digits) <= 15 else ''


def extract_destinations(value):
    """Only explicit destination fields and WhatsApp URL hosts; never caption digits or IDs."""
    numbers, ids = set(), set()
    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key in PHONE_KEYS and isinstance(item, (str, int)):
                    number = normalize_destination(item)
                    if number:
                        numbers.add(number)
                elif key in ID_KEYS and item:
                    ids.add(str(item))
                elif isinstance(item, (dict, list)):
                    walk(item)
                elif key in {'link', 'url', 'website_url', 'link_url'} and isinstance(item, str):
                    parsed = urlparse(item)
                    if parsed.hostname in {'wa.me', 'www.wa.me'}:
                        number = normalize_destination(parsed.path.strip('/'))
                        if number:
                            numbers.add(number)
                    elif parsed.hostname in {'api.whatsapp.com', 'web.whatsapp.com', 'www.whatsapp.com'}:
                        number = normalize_destination(parse_qs(parsed.query).get('phone', [''])[0])
                        if number:
                            numbers.add(number)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(value)
    return numbers, ids


def message_count(actions):
    values = {a.get('action_type'): float(a.get('value') or 0) for a in (actions or []) if isinstance(a, dict)}
    # These are aliases, not independent results: never add them together.
    return next((values[key] for key in MESSAGE_KEYS if key in values), 0.0)


def matches_phone(number, query):
    if not query:
        return True
    if query.startswith('suffix:'):
        return number.endswith(query.split(':', 1)[1])
    return number == normalize_destination(query)


def graph_read(api, config, path, fields, preferred='token_1', many=True):
    errors = []
    for key in [preferred] + [k for k in config.meta_tokens if k != preferred]:
        token = config.meta_tokens.get(key)
        if not token:
            continue
        try:
            params = {'fields': fields, 'access_token': token, 'limit': 500}
            url = f'{api.BASE_URL}/{config.api_version}/{path}'
            return api.fetch_all_pages(url, params) if many else api.request_json('GET', url, params=params)
        except Exception as exc:
            detail = str(exc)
            for secret in config.meta_tokens.values():
                if secret:
                    detail = detail.replace(secret, '[REDACTED]')
            errors.append(f'{key}: {detail}')
    raise RuntimeError(' | '.join(errors))


def collect_account(api, config, account, period, today=None, yesterday=None):
    account_id = api.normalize_account_id(account.get('id'))
    preferred = account.get('_token_key', 'token_1')
    issues = []
    today = today or period
    yesterday = yesterday or period
    cached = {}
    by_period = {}
    for key, wanted in [('selected', period), ('today', today), ('yesterday', yesterday)]:
        dates = (wanted.since, wanted.until)
        if dates not in cached:
            cached[dates] = api.fetch_insights(config, account_id, wanted,
                fields='ad_id,adset_id,campaign_id,campaign_name,spend,actions', level='ad', token_key=preferred)
        # Never silently overwrite two positive-spend rows (or a row without
        # an ad ID). That used to make money disappear before attribution.
        indexed = {}
        for metric in cached[dates]:
            ad_id = str(metric.get('ad_id') or '')
            if not ad_id:
                if float(metric.get('spend') or 0) > 0:
                    raise ValueError(f'{account_id}: positive-spend Insights row has no ad_id')
                continue
            if ad_id in indexed:
                raise ValueError(f'{account_id}: duplicate ad_id {ad_id} in Insights {dates}')
            indexed[ad_id] = metric
        by_period[key] = indexed
    insights = list({str(row.get('ad_id')): row for rows in cached.values() for row in rows}.values())
    # For CTWA, the destination phone is sometimes stored at the TOP LEVEL of
    # AdCreative rather than in object_story_spec or the ad set. Without this
    # field many real WhatsApp ads show as destination-unknown.
    fallback_ad_fields = 'id,adset_id,campaign_id,effective_status,creative{object_story_spec,asset_feed_spec,call_to_action,link_url}'
    ad_fields = 'id,adset_id,campaign_id,effective_status,creative{object_story_spec,asset_feed_spec,call_to_action,link_url,whats_app_business_phone_number}'
    ad_inventory_complete = True
    try:
        try:
            ads = graph_read(api, config, f'act_{account_id}/ads', ad_fields, preferred)
        except Exception as exc:
            # Older tokens/API versions may reject this optional creative
            # field; fall back ONLY for a genuine unsupported-field error.
            detail = str(exc).lower()
            unsupported = ('whats_app_business_phone_number' in detail and
                           any(k in detail for k in ('unknown', 'nonexisting', 'does not exist',
                                                     'cannot query', 'unsupported field')))
            if not unsupported:
                raise
            ads = graph_read(api, config, f'act_{account_id}/ads', fallback_ad_fields, preferred)
    except Exception as exc:
        ads = []
        ad_inventory_complete = False
        issues.append(f'{account_id}: ad metadata unavailable: {exc}')
    try:
        adsets = graph_read(api, config, f'act_{account_id}/adsets',
            'id,campaign_id,destination_type,promoted_object,effective_status,daily_budget,lifetime_budget', preferred)
    except Exception as exc:
        adsets = []
        issues.append(f'{account_id}: ad set metadata unavailable: {exc}')
    try:
        campaigns = graph_read(api, config, f'act_{account_id}/campaigns',
            'id,name,effective_status,daily_budget,lifetime_budget', preferred)
    except Exception as exc:
        campaigns = []
        issues.append(f'{account_id}: campaign budgets unavailable: {exc}')
    campaign_map = {str(c['id']): c for c in campaigns}
    admap = {str(a['id']): a for a in ads}
    setmap = {str(a['id']): a for a in adsets}
    # Include historical ads missing from the account edge when still readable.
    for row in insights:
        ad_id = str(row.get('ad_id') or '')
        if ad_id and ad_id not in admap:
            try:
                admap[ad_id] = graph_read(api, config, ad_id, ad_fields, preferred, False)
            except Exception:
                admap[ad_id] = {'id': ad_id, 'adset_id': row.get('adset_id'), 'campaign_id': row.get('campaign_id')}
    # Use the SAME audited calculation as Allocation.  The old
    # closing_budgets() silently differed from the corrected budget policy.
    from budget_policy import allocation_budget_breakdown
    campaign_spend, adset_spend = defaultdict(float), defaultdict(float)
    today_rows = list(by_period['today'].values())
    for metric in today_rows:
        campaign_spend[str(metric.get('campaign_id') or '')] += float(metric.get('spend') or 0)
        adset_spend[str(metric.get('adset_id') or '')] += float(metric.get('spend') or 0)
        # A historical ad may be absent from /ads and its single-object lookup
        # may also fail.  Under still-active parents, missing ad status cannot
        # be interpreted as PAUSED: that would inflate stopped spend.
        ad_id = str(metric.get('ad_id') or '')
        ad = admap.get(ad_id, {})
        cid = str(metric.get('campaign_id') or '')
        sid = str(metric.get('adset_id') or '')
        if (float(metric.get('spend') or 0) > 0
                and not ad.get('effective_status')
                and str(campaign_map.get(cid, {}).get('effective_status') or '') == 'ACTIVE'
                and str(setmap.get(sid, {}).get('effective_status') or '') == 'ACTIVE'):
            issues.append(f'{account_id}: status unavailable for spent ad {ad_id}')

    live_owners = {}
    stopped_ads = {}
    budget_inventory_complete = ad_inventory_complete and not issues
    budget_details = []
    if budget_inventory_complete:
        try:
            _live, _stopped, budget_details = allocation_budget_breakdown(
                campaigns, adsets, list(admap.values()),
                campaign_spend, adset_spend,
                lambda value: api_money_to_major(value, account.get('currency') or 'EGP'),
                ad_spend_rows=today_rows,
            )
            for item in budget_details:
                level, entity_id = item['budget_level'], str(item['entity_id'])
                mode = item['calculation_mode']
                owner = dict(
                    amount=float(item['daily_budget']), mode=mode,
                    label=item.get('entity_name') or entity_id,
                    # Spent entries have a unique ad ID.  Live entries have a
                    # unique CBO Campaign / ABO Ad Set ID.
                    key=(account_id, mode, level, entity_id),
                )
                if mode == 'live':
                    live_owners[(level, entity_id)] = owner
                else:
                    stopped_ads[entity_id] = owner
        except (ValueError, TypeError, KeyError) as exc:
            # Never present a partial or uncertain budget as an exact zero.
            budget_inventory_complete = False
            issues.append(f'{account_id}: budget reconciliation unavailable: {exc}')
            budget_details = []
            live_owners.clear()
            stopped_ads.clear()

    print('PHONE BUDGET CLOSING: ' + json.dumps({
        'account_id': account_id,
        'live': sum(v['amount'] for v in live_owners.values()),
        'stopped': sum(v['amount'] for v in stopped_ads.values()),
        'owners': budget_details,
        'complete': budget_inventory_complete,
    }, ensure_ascii=False))
    resolved_ids = {}
    records = []
    # Prefer the period actually requested; do not let a yesterday-only row
    # overwrite today's actions when classifying an unlabelled creative.
    by_ad = {ad_id: (by_period['selected'].get(ad_id)
                     or by_period['today'].get(ad_id)
                     or by_period['yesterday'].get(ad_id, {}))
             for ad_id in {str(row.get('ad_id')) for row in insights}}
    for ad_id in sorted(set(by_ad) | set(admap)):
        ad = admap.get(ad_id, {})
        row = by_ad.get(ad_id, {})
        active = ad.get('effective_status') == 'ACTIVE'
        if not row and not active:
            continue
        adset_id = str(ad.get('adset_id') or row.get('adset_id') or '')
        if adset_id and adset_id not in setmap:
            try:
                setmap[adset_id] = graph_read(api, config, adset_id,
                    'id,campaign_id,destination_type,promoted_object,effective_status,daily_budget,lifetime_budget', preferred, False)
            except Exception:
                setmap[adset_id] = {}
        adset = setmap.get(adset_id, {})
        metadata = {'creative': ad.get('creative') or {}, 'promoted_object': adset.get('promoted_object') or {}}
        numbers, phone_ids = extract_destinations(metadata)
        for phone_id in phone_ids:
            if phone_id not in resolved_ids:
                try:
                    obj = graph_read(api, config, phone_id, 'display_phone_number', preferred, False)
                    resolved_ids[phone_id] = normalize_destination(obj.get('display_phone_number'))
                except Exception:
                    resolved_ids[phone_id] = ''
            if resolved_ids[phone_id]:
                numbers.add(resolved_ids[phone_id])
        destination = str(adset.get('destination_type') or '').upper()
        metadata_text = json.dumps(metadata).upper()
        has_messages = any(a.get('action_type') in MESSAGE_KEYS for a in (row.get('actions') or []) if isinstance(a, dict))
        is_whatsapp = bool(numbers or phone_ids or 'WHATSAPP' in destination or 'WHATSAPP' in metadata_text)
        unknown_metadata = not adset or not ad.get('creative')
        reportable = is_whatsapp or (unknown_metadata and has_messages)
        # A non-WhatsApp ad can still spend, and that spend is part of the
        # account-level total. Keep it in the reconciliation ledger, even if it
        # is inactive now or its spend belongs to a selected historical range.
        if (not reportable and not active and not any(
            float(mapping.get(ad_id, {}).get('spend') or 0) > 0
            for mapping in by_period.values()
        )):
            continue
        mixed = bool(is_whatsapp and ('MESSENGER' in destination or 'INSTAGRAM' in destination or destination == 'MULTIPLE'))
        # An ad with multiple destinations cannot have its full spend assigned to each.
        number = next(iter(numbers)) if len(numbers) == 1 and not mixed and not any(not resolved_ids[i] for i in phone_ids) else ''
        reason = ('multiple destinations' if mixed or len(numbers) > 1 else
                  'destination not returned by Meta' if reportable else
                  'WhatsApp destination not verified') if not number else ''
        campaign_id = str(ad.get('campaign_id') or row.get('campaign_id') or adset.get('campaign_id') or '')
        currency = account.get('currency') or 'EGP'
        budget = None
        budget_missing = False
        lifetime = False
        campaign = campaign_map.get(campaign_id, {})
        is_cbo = (api_money_to_major(campaign.get('daily_budget'), currency) or 0) > 0 or (
            api_money_to_major(campaign.get('lifetime_budget'), currency) or 0) > 0
        owner_key = ('CBO', campaign_id) if is_cbo else ('ABO', adset_id)
        # A stopped ad inside a live owner has already consumed part of that
        # owner's daily limit.  Never add its spend above the live budget.
        budget = live_owners.get(owner_key) or stopped_ads.get(ad_id)
        budget_missing = not budget_inventory_complete
        lifetime = bool(adset.get('lifetime_budget') or campaign_map.get(campaign_id, {}).get('lifetime_budget'))
        record = {'phone': number if reportable else '', 'reason': reason, 'currency': currency,
            'account_id': account_id, 'ad_id': ad_id,
            'buyer': account.get('buyer_code') or api.extract_buyer_code(account.get('name', '')),
            'campaign_id': campaign_id, 'adset_id': adset_id,
            'destination_type': destination, 'active': active, 'budget': budget,
            'budget_missing': budget_missing, 'lifetime': lifetime, 'budget_only': not reportable,
            'candidate_phones': numbers, 'budget_targets_incomplete': not ad_inventory_complete}
        for key, mapping in by_period.items():
            # Always retain the source Insights values: dropping the spend of
            # ads with an unknown/non-WA destination made the visible total
            # smaller than the media buyer's real account-level spend.
            metric = mapping.get(ad_id, {})
            suffix = '' if key == 'selected' else '_' + key
            record['spend' + suffix] = float(metric.get('spend') or 0)
            record['messages' + suffix] = message_count(metric.get('actions'))
        records.append(record)

    for mapping_name in ('selected', 'today'):
        source_total = sum(float(x.get('spend') or 0) for x in by_period[mapping_name].values())
        record_field = 'spend' if mapping_name == 'selected' else 'spend_today'
        record_total = sum(float(x.get(record_field) or 0) for x in records)
        if abs(source_total - record_total) > 0.011:
            raise ValueError(f'{account_id}: {mapping_name} spend dropped between Insights '
                             f'and phone records: {source_total:.2f} != {record_total:.2f}')

    # Every budget owner must also appear in the reconciliation ledger, even
    # if none of its ad creatives exposes a resolvable WhatsApp destination.
    attached = {tuple(row['budget']['key']) for row in records if row.get('budget')}
    for owner in list(live_owners.values()) + list(stopped_ads.values()):
        if tuple(owner['key']) in attached:
            continue
        records.append({
            'phone': '', 'reason': 'budget destination not returned by Meta',
            'currency': account.get('currency') or 'EGP',
            'account_id': account_id,
            'ad_id': '__unassigned_owner__' + ':'.join(map(str, owner['key'])),
            'buyer': account.get('buyer_code') or api.extract_buyer_code(account.get('name', '')),
            'campaign_id': '', 'active': False, 'budget': owner,
            'budget_missing': False, 'lifetime': False,
            'budget_only': True, 'owner_only': True,
            'candidate_phones': set(), 'budget_targets_incomplete': True,
            **{key: 0.0 for key in METRICS},
        })
    return records, issues


def build_phone_report(api, config, period, team_key, agent_code, phone_filter=''):
    if phone_filter and not (re.fullmatch(r'suffix:\d{3,8}', phone_filter) or normalize_destination(phone_filter)):
        raise ValueError('Phone filter must be a full phone number or suffix: followed by 3–8 digits.')
    accounts, errors = api.get_report_accounts(config)
    accounts = api.filter_accounts_for_team(accounts, team_key)
    if team_key == 'taher':
        accounts = api.filter_accounts_for_agent(accounts, agent_code)
    if not accounts:
        raise RuntimeError('No accounts found for the requested phone-report scope.')
    now = datetime.now(ZoneInfo(getattr(config, 'timezone', 'Africa/Cairo')))
    today_date = now.date()
    yesterday_date = today_date - timedelta(days=1)
    today = SimpleNamespace(since=today_date.isoformat(), until=today_date.isoformat())
    yesterday = SimpleNamespace(since=yesterday_date.isoformat(), until=yesterday_date.isoformat())
    records = []
    successful = 0
    with ThreadPoolExecutor(max_workers=min(config.max_workers, len(accounts))) as pool:
        futures = {pool.submit(collect_account, api, config, a, period, today, yesterday): a for a in accounts}
        for future in as_completed(futures):
            try:
                rows, issues = future.result()
                records.extend(rows)
                errors.extend(issues)
                successful += 1
            except Exception as exc:
                errors.append(f"{futures[future].get('id')}: {exc}")
    if not successful:
        raise RuntimeError('All phone-report account queries failed; no total was generated.')
    for error in errors:
        safe = str(error)
        for secret in config.meta_tokens.values():
            if secret:
                safe = safe.replace(secret, '[REDACTED]')
        print('PHONE REPORT WARNING: ' + safe)
    return render_phone_report(api, records, errors, period, today, yesterday,
                               team_key, agent_code, phone_filter)


METRICS = ('spend', 'messages', 'spend_today', 'messages_today', 'spend_yesterday', 'messages_yesterday')


def aggregate_phone_budget_components(records):
    """Allocate each unique owner once; distinguish today's closing vs live.

    When multiple destinations share one CBO/ABO budget, or an ad destination
    is unknown, do NOT pretend that the full budget belongs to either number.
    """
    budgets = {}
    for row in records:
        budget = row.get('budget')
        if not budget:
            continue
        key = tuple(budget['key'])
        item = budgets.setdefault(key, {**budget, 'currency': row['currency'], 'phones': set(), 'candidates': set()})
        item['phones'].add(row['phone'])
        if row.get('budget_targets_incomplete'):
            item['phones'].add('')
        item['candidates'].update(row.get('candidate_phones', set()))
    daily = defaultdict(float)
    live = defaultdict(float)
    shared = []
    for item in budgets.values():
        if len(item['phones']) == 1 and '' not in item['phones']:
            key = (next(iter(item['phones'])), item['currency'])
            daily[key] += item['amount']
            if item.get('mode') == 'live':
                live[key] += item['amount']
        else:
            shared.append(item)
    return daily, live, shared


def aggregate_phone_budgets(records):
    """Backward-compatible accessor for the existing closing budget total."""
    daily, _live, shared = aggregate_phone_budget_components(records)
    return daily, shared


def reconcile_phone_totals(records, exclusive, live_exclusive, shared):
    """Full-scope audit; no cost or budget is invented for an unknown WA ID.

    Phone allocations are only the provable exclusive part of each owner.
    The remaining owner budgets MUST remain visible as a separate shared or
    unattributed total instead of disappearing from the report.
    """
    summary = defaultdict(lambda: defaultdict(float))
    seen_owners = set()
    for row in records:
        currency = row.get('currency') or 'EGP'
        if not row.get('owner_only'):
            today_spend = float(row.get('spend_today') or 0)
            summary[currency]['all_spend_today'] += today_spend
            if row.get('phone') and not row.get('budget_only'):
                summary[currency]['assigned_spend_today'] += today_spend
            elif row.get('budget_only'):
                summary[currency]['other_unverified_spend_today'] += today_spend
            else:
                summary[currency]['wa_unattributed_spend_today'] += today_spend
        owner = row.get('budget')
        if owner:
            owner_key = tuple(owner['key'])
            if owner_key not in seen_owners:
                seen_owners.add(owner_key)
                summary[currency]['overall_daily_budget'] += float(owner['amount'])
                if owner.get('mode') == 'live':
                    summary[currency]['overall_live_budget'] += float(owner['amount'])
    for (phone, currency), value in exclusive.items():
        summary[currency]['assigned_daily_budget'] += value
    for (phone, currency), value in live_exclusive.items():
        summary[currency]['assigned_live_budget'] += value
    for owner in shared:
        currency = owner['currency']
        summary[currency]['shared_daily_budget'] += float(owner['amount'])
        if owner.get('mode') == 'live':
            summary[currency]['shared_live_budget'] += float(owner['amount'])

    for currency, values in summary.items():
        if abs(values['overall_daily_budget'] - values['assigned_daily_budget'] - values['shared_daily_budget']) > 0.011:
            raise ValueError(f'Daily budget attribution not reconciled for {currency}')
        if abs(values['overall_live_budget'] - values['assigned_live_budget'] - values['shared_live_budget']) > 0.011:
            raise ValueError(f'Live budget attribution not reconciled for {currency}')
        unknown_sum = (values['assigned_spend_today'] + values['wa_unattributed_spend_today']
                       + values['other_unverified_spend_today'])
        if abs(values['all_spend_today'] - unknown_sum) > 0.011:
            raise ValueError(f'Spend attribution not reconciled for {currency}')
    return summary


def render_phone_report(api, records, errors, period, today, yesterday, team_key, agent_code, phone_filter=''):
    unique = {(r['account_id'], r['ad_id']): r for r in records}
    records = list(unique.values())
    exclusive, live_exclusive, shared = aggregate_phone_budget_components(records)
    reconciliation = reconcile_phone_totals(records, exclusive, live_exclusive, shared)
    groups = {}
    unknown = defaultdict(lambda: {k: 0.0 for k in METRICS})
    missing_budget = 0
    for row in records:
        if row.get('budget_only'):
            continue
        if row.get('budget_missing'):
            missing_budget += 1
        if not row['phone']:
            for k in METRICS:
                unknown[row['currency']][k] += row.get(k, 0)
            continue
        if not matches_phone(row['phone'], phone_filter):
            continue
        key = (row['phone'], row['currency'])
        group = groups.setdefault(key, {**{k: 0.0 for k in METRICS}, 'buyers': set(), 'campaigns': set(), 'active': 0, 'lifetime': False, 'budget_missing': False})
        for k in METRICS:
            group[k] += row.get(k, 0)
        group['buyers'].add(row['buyer'])
        if row['campaign_id']:
            group['campaigns'].add((row['account_id'], row['campaign_id']))
        group['active'] += int(row['active'])
        group['lifetime'] |= row.get('lifetime', False)
        group['budget_missing'] |= row.get('budget_missing', False)
    label = 'Today' if (period.since, period.until) == (today.since, today.until) else (
        'Yesterday' if (period.since, period.until) == (yesterday.since, yesterday.until) else
        (period.since if period.since == period.until else f'{period.since} → {period.until}'))
    names = {'AA': 'Abdallah', 'HM': 'Hesham', 'BM': 'Bassem', 'AF': 'Amr',
             'EK': 'Esraa', 'MA': 'Mahmoud', 'MM': 'Mohamed Mahmoud', 'NB': 'Mohamed Nabih',
             'SQ': 'Ahmed Sharkawy', 'OS': 'Osama'}
    title = 'Overall' if agent_code == 'ALL' else names.get(agent_code, api.buyer_name(agent_code))
    if team_key == 'cairo':
        title = 'Cairo Overall'
    lines = [f'*{title} WA Numbers*']
    if errors:
        lines.append('⚠️ Partial data')
    for (phone, currency), group in sorted(groups.items(), key=lambda kv: (-kv[1]['spend'], kv[0])):
        budget = exclusive[(phone, currency)]
        live_budget = live_exclusive[(phone, currency)]
        shares = [b for b in shared if b['currency'] == currency and phone in (b['phones'] | b['candidates'])]
        # Shared CBO/ABO budgets must not be repeated under multiple numbers.
        budget_text = f'{budget:,.2f} {currency}'
        live_text = f'{live_budget:,.2f} {currency}'
        if shares or group['budget_missing']:
            budget_text = f'{budget:,.2f} {currency} (confirmed only)' if budget else 'N/A (shared or unavailable)'
            live_text = f'{live_budget:,.2f} {currency} (confirmed only)' if live_budget else 'N/A (shared or unavailable)'
        lines += ['', f'WA Phonenumber : +{phone}',
            f"Spend ( {label} ) : {group['spend']:,.2f} {currency}",
            f"Messages : {group['messages']:,.0f}",
            f'Daily budget : {budget_text}',
            f'Live daily budget : {live_text}']
    if not groups:
        lines.append('No matching WA numbers found.')
    if not phone_filter and (period.since, period.until) == (today.since, today.until):
        # Leave the original per-number layout untouched, and reconcile the
        # missing values explicitly beneath it. The two totals can be compared
        # with the matching balance report at the SAME timestamp/date/scope.
        incomplete_totals = bool(errors or missing_budget)
        for currency, data in sorted(reconciliation.items()):
            fmt = lambda key: f"{data[key]:,.2f} {currency}"
            qualifier = ' (confirmed only)' if incomplete_totals else ''
            lines += [
                '', '──────────────',
                '*WA Reconciliation (Today)*',
                f"*Total Spend Today (all ads):* {fmt('all_spend_today')}{qualifier}",
                f"Assigned to WA numbers: {fmt('assigned_spend_today')}",
                f"WA destination unknown: {fmt('wa_unattributed_spend_today')}",
                f"Other / unverified destination: {fmt('other_unverified_spend_today')}",
                f"*Overall Daily Budget (all owners):* {fmt('overall_daily_budget')}{qualifier}",
                f"*Overall Live Budget (all owners):* {fmt('overall_live_budget')}{qualifier}",
                f"Budget assigned to WA numbers: {fmt('assigned_daily_budget')}",
                f"Budget shared / unattributed: {fmt('shared_daily_budget')}",
            ]
            print('PHONE RECONCILIATION AUDIT: ' + json.dumps({
                'currency': currency, **{k: round(v, 2) for k, v in data.items()}
            }, ensure_ascii=False))
    # Diagnostics belong in GitHub logs, never in the WhatsApp report body.
    for item in shared:
        print('PHONE BUDGET DETAIL: ' + json.dumps(item, default=lambda v: sorted(v) if isinstance(v, set) else str(v), ensure_ascii=False))
    for currency, values in unknown.items():
        print('PHONE UNMAPPED DETAIL: ' + json.dumps({'currency': currency, **values}, ensure_ascii=False))
    for row in records:
        if not row.get('owner_only') and not row.get('phone') and float(row.get('spend_today') or 0) > 0:
            print('PHONE UNMAPPED AD: ' + json.dumps({
                'account_id': row.get('account_id'), 'ad_id': row.get('ad_id'),
                'adset_id': row.get('adset_id'), 'campaign_id': row.get('campaign_id'),
                'spend_today': round(float(row.get('spend_today') or 0), 2),
                'destination_type': row.get('destination_type'),
                'reason': row.get('reason'), 'possible_phones': sorted(row.get('candidate_phones') or []),
            }, ensure_ascii=False))
    if missing_budget:
        print(f'PHONE BUDGET WARNING: {missing_budget} ads have unavailable budget metadata')
    return '\n'.join(lines)
