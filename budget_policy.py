"""Current live daily budgets plus today's spend of stopped budget owners."""
def closing_budgets(campaigns, adsets, ads, campaign_spend, adset_spend, money):
    campaigns = {str(x['id']): x for x in campaigns}
    active_campaigns, active_sets = set(), set()
    for ad in ads:
        if ad.get('effective_status') == 'ACTIVE':
            active_campaigns.add(str(ad.get('campaign_id') or ''))
            active_sets.add(str(ad.get('adset_id') or ''))
    owners = {}
    for cid, campaign in campaigns.items():
        daily = money(campaign.get('daily_budget')) or 0
        lifetime = money(campaign.get('lifetime_budget')) or 0
        if daily <= 0 and lifetime <= 0:
            continue
        live = campaign.get('effective_status') == 'ACTIVE' and cid in active_campaigns
        amount = daily if live else float(campaign_spend.get(cid, 0) or 0)
        if amount > 0:
            owners[('CBO', cid)] = dict(amount=amount, mode='live' if live else 'spent', label=campaign.get('name') or cid)
    for adset in adsets:
        sid, cid = str(adset['id']), str(adset.get('campaign_id') or '')
        campaign = campaigns.get(cid)
        if campaign is None:
            raise ValueError('Missing campaign metadata for budget calculation: ' + cid)
        if (money(campaign.get('daily_budget')) or 0) > 0 or (money(campaign.get('lifetime_budget')) or 0) > 0:
            continue  # Campaign owns this budget: never add its child budgets/spend twice.
        live = (campaign.get('effective_status') == 'ACTIVE' and
                adset.get('effective_status') == 'ACTIVE' and sid in active_sets)
        daily = money(adset.get('daily_budget')) or 0
        amount = daily if live else float(adset_spend.get(sid, 0) or 0)
        if amount > 0:
            owners[('ABO', sid)] = dict(amount=amount, mode='live' if live else 'spent', label=adset.get('name') or sid)
    return owners
