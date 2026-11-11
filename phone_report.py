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
        by_period[key] = {str(row.get('ad_id')): row for row in cached[dates]}
    insights = list({str(row.get('ad_id')): row for rows in cached.values() for row in rows}.values())
    ad_fields = 'id,adset_id,campaign_id,effective_status,creative{object_story_spec,asset_feed_spec,call_to_action,link_url}'
    ad_inventory_complete = True
    try:
        ads = graph_read(api, config, f'act_{account_id}/ads', ad_fields, preferred)
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
    resolved_ids = {}
    records = []
    by_ad = {str(row.get('ad_id')): row for row in insights}
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
        if not reportable and not active:
            continue
        mixed = bool(is_whatsapp and ('MESSENGER' in destination or 'INSTAGRAM' in destination or destination == 'MULTIPLE'))
        # An ad with multiple destinations cannot have its full spend assigned to each.
        number = next(iter(numbers)) if len(numbers) == 1 and not mixed and not any(not resolved_ids[i] for i in phone_ids) else ''
        reason = ('multiple destinations' if mixed or len(numbers) > 1 else 'destination not returned by Meta') if not number else ''
        campaign_id = str(ad.get('campaign_id') or row.get('campaign_id') or adset.get('campaign_id') or '')
        currency = account.get('currency') or 'EGP'
        budget = None
        budget_missing = False
        lifetime = False
        if active:
            campaign = campaign_map.get(campaign_id)
            if campaign is None and campaign_id:
                try:
                    campaign = graph_read(api, config, campaign_id,
                        'id,name,effective_status,daily_budget,lifetime_budget', preferred, False)
                except Exception:
                    campaign = {}
                campaign_map[campaign_id] = campaign
            campaign = campaign or {}
            if not campaign or not adset:
                budget_missing = True
            elif campaign.get('effective_status') == 'ACTIVE' and adset.get('effective_status') == 'ACTIVE':
                campaign_daily = api_money_to_major(campaign.get('daily_budget'), currency) or 0.0
                campaign_lifetime = api_money_to_major(campaign.get('lifetime_budget'), currency) or 0.0
                adset_daily = api_money_to_major(adset.get('daily_budget'), currency) or 0.0
                if campaign_daily > 0:
                    budget = {'key': (account_id, 'CBO', campaign_id), 'amount': campaign_daily,
                              'label': campaign.get('name') or campaign_id}
                elif campaign_lifetime > 0:
                    lifetime = True
                elif adset_daily > 0:
                    budget = {'key': (account_id, 'ABO', adset_id), 'amount': adset_daily,
                              'label': adset_id}
                elif (api_money_to_major(adset.get('lifetime_budget'), currency) or 0) > 0:
                    lifetime = True
            elif not campaign.get('effective_status') or not adset.get('effective_status'):
                budget_missing = True
        record = {'phone': number if reportable else '', 'reason': reason, 'currency': currency,
            'account_id': account_id, 'ad_id': ad_id,
            'buyer': account.get('buyer_code') or api.extract_buyer_code(account.get('name', '')),
            'campaign_id': campaign_id, 'active': active, 'budget': budget,
            'budget_missing': budget_missing, 'lifetime': lifetime, 'budget_only': not reportable,
            'candidate_phones': numbers, 'budget_targets_incomplete': not ad_inventory_complete}
        for key, mapping in by_period.items():
            metric = mapping.get(ad_id, {}) if reportable else {}
            suffix = '' if key == 'selected' else '_' + key
            record['spend' + suffix] = float(metric.get('spend') or 0)
            record['messages' + suffix] = message_count(metric.get('actions'))
        records.append(record)
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


def aggregate_phone_budgets(records):
    """Each budget owner counts once, regardless of ads or number filters."""
    budgets = {}
    for row in records:
        budget = row.get('budget')
        if not budget or not row['active']:
            continue
        key = tuple(budget['key'])
        item = budgets.setdefault(key, {**budget, 'currency': row['currency'], 'phones': set(), 'candidates': set()})
        item['phones'].add(row['phone'])
        if row.get('budget_targets_incomplete'):
            item['phones'].add('')
        item['candidates'].update(row.get('candidate_phones', set()))
    exclusive = defaultdict(float)
    shared = []
    for item in budgets.values():
        if len(item['phones']) == 1 and '' not in item['phones']:
            exclusive[(next(iter(item['phones'])), item['currency'])] += item['amount']
        else:
            shared.append(item)
    return exclusive, shared


def render_phone_report(api, records, errors, period, today, yesterday, team_key, agent_code, phone_filter=''):
    unique = {(r['account_id'], r['ad_id']): r for r in records}
    records = list(unique.values())
    exclusive, shared = aggregate_phone_budgets(records)
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
    lines = ['📱 *تقرير أرقام واتساب*', f'النهارده: {today.since} | امبارح: {yesterday.since}',
        f'الفريق: {api.team_label(team_key)}', f'الميديا باير: {api.scope_label(agent_code)}']
    if phone_filter:
        lines.append('البحث: ' + phone_filter.replace('suffix:', 'آخر أرقام: '))
    if errors:
        lines.append(f'⚠️ تقرير جزئي: {len(errors)} خطأ في جلب البيانات.')
    if missing_budget:
        lines.append('⚠️ بيانات بعض البادجت غير متاحة؛ إجمالي البادجت المؤكد قد يكون ناقصا.')
    show_selected = (period.since, period.until) not in {(today.since, today.until), (yesterday.since, yesterday.until)}
    totals = defaultdict(lambda: {**{k: 0.0 for k in METRICS}, 'budget': 0.0})
    for (phone, currency), group in sorted(groups.items(), key=lambda kv: (-kv[1]['spend_today'], kv[0])):
        for k in METRICS:
            totals[currency][k] += group[k]
        budget = exclusive[(phone, currency)]
        totals[currency]['budget'] += budget
        shares = [b for b in shared if b['currency'] == currency and phone in (b['phones'] | b['candidates'])]
        lines += ['', f'*+{phone}*', f"الميديا باير: {', '.join(api.buyer_name(b) for b in sorted(group['buyers']))}",
            f"النهارده: صرف {group['spend_today']:,.2f} {currency} | محادثات {group['messages_today']:,.0f}",
            f"امبارح: صرف {group['spend_yesterday']:,.2f} {currency} | محادثات {group['messages_yesterday']:,.0f}",
            f'Daily Budget مخصص لهذا الرقم: {budget:,.2f} {currency}']
        if shares:
            lines.append('وله بادجت مشتركة؛ قيمتها وتفاصيلها أسفل التقرير، وليست مضافة بالكامل لهذا الرقم.')
        if group['budget_missing']:
            lines.append('⚠️ قيمة البادجت أعلاه هي الجزء المؤكد فقط؛ بعض البيانات غير متاحة.')
        for suffix, title in [('today', 'النهارده'), ('yesterday', 'امبارح')]:
            count = group['messages_' + suffix]
            cost = f"{group['spend_' + suffix] / count:,.2f} {currency}" if count else 'N/A'
            lines.append(f'تكلفة المحادثة {title}: {cost}')
        if group['lifetime']:
            lines.append('يوجد Lifetime Budget؛ لا يدخل ضمن Daily Budget.')
        if show_selected:
            lines.append(f"الفترة المطلوبة {period.since} → {period.until}: صرف {group['spend']:,.2f} {currency} | محادثات {group['messages']:,.0f}")
        if show_selected:
            cost = f"{group['spend'] / group['messages']:,.2f} {currency}" if group['messages'] else 'N/A'
            lines.append(f'تكلفة المحادثة للفترة المطلوبة: {cost}')
        lines.append(f"الحملات: {len(group['campaigns'])} | الإعلانات Active حاليا: {group['active']}")
    if not groups:
        lines.append('لم نجد أرقاما مؤكدة مطابقة؛ راجع تنبيهات الربط أدناه.')
    selected_phones = {phone for phone, _ in groups}
    relevant_shared = [b for b in shared if not phone_filter or selected_phones & (b['phones'] | b['candidates']) or any(matches_phone(n, phone_filter) for n in b['candidates'])]
    for item in sorted(relevant_shared, key=lambda b: tuple(b['key'])):
        known = sorted((item['phones'] | item['candidates']) - {''})
        destinations = ', '.join('+' + n for n in known) or 'غير محددة'
        if '' in item['phones']:
            destinations += ' + وجهة أخرى/غير مؤكدة'
        lines += ['', f"🔀 بادجت مشتركة/غير موزعة — {item['key'][1]} {item['label']}",
            f"القيمة الكاملة: {item['amount']:,.2f} {item['currency']}", f'الوجهات: {destinations}']
        # Whole owner may enter overall only when every target belongs to the selected numbers.
        if '' not in item['phones'] and item['phones'] <= selected_phones:
            totals[item['currency']]['budget'] += item['amount']
        else:
            lines.append('لم تضف إلى إجمالي البادجت المنسوب للأرقام المختارة؛ لا توجد حصة مؤكدة لكل وجهة.')
    for currency, total in sorted(totals.items()):
        lines += ['', f'*الإجمالي للأرقام المطابقة ({currency})*',
            f"النهارده: صرف {total['spend_today']:,.2f} | محادثات {total['messages_today']:,.0f}",
            f"امبارح: صرف {total['spend_yesterday']:,.2f} | محادثات {total['messages_yesterday']:,.0f}",
            f"Daily Budget مؤكد بدون تكرار: {total['budget']:,.2f}"]
        if show_selected:
            lines.append(f"الفترة المطلوبة: صرف {total['spend']:,.2f} | محادثات {total['messages']:,.0f}")
    for currency, vals in sorted(unknown.items()):
        lines += ['', f'⚠️ صرف غير مرتبط برقم مؤكد / متعدد الوجهات ({currency}) — نطاق الحسابات كله:',
            f"النهارده: {vals['spend_today']:,.2f} | محادثات {vals['messages_today']:,.0f}",
            f"امبارح: {vals['spend_yesterday']:,.2f} | محادثات {vals['messages_yesterday']:,.0f}",
            'لم يضف إلى إجماليات الأرقام.']
        if show_selected:
            lines.append(f"الفترة المطلوبة: {vals['spend']:,.2f} | محادثات {vals['messages']:,.0f}")
    lines += ['', 'Daily Budget الحالي للإعلانات Active، حتى لو لم تصرف اليوم؛ لا يساوي الصرف الفعلي.',
        'المحادثات = نتائج الإعلانات، وليست كل رسائل الواتساب.',
        'ربط الرقم حسب الإعدادات الحالية؛ لو الرقم اتغير، توزيع الصرف التاريخي على الرقم القديم غير مؤكد.']
    return '\n'.join(lines)
