# Smart Meta WhatsApp Reports

A separate WhatsApp command bot for flexible Meta Ads reporting.

It does **not** modify the older scheduled report bot.

## Architecture

```text
WhatsApp message
      ↓
Meta WhatsApp Webhook
      ↓
Cloudflare Worker
  - understands English / Arabic
  - detects range
  - detects agent
  - detects one or many report types
      ↓
GitHub workflow_dispatch
      ↓
smart_report.py
      ↓
Meta Marketing API
      ↓
WhatsApp report to the same sender
```

---

## 1. WhatsApp Menu

Any exact message below opens the menu:

```text
Hi
Hello
Report
Menu
Help
Start
ابدأ
ابدا
تقرير
القائمة
```

The menu contains:

### Range

```text
1. Today
2. Yesterday
3. Last 7 Days
4. Last 30 Days
5. This Month
6. Last Month
```

### Reports

```text
Spend
Age
Government
Allocation
Full
```

`Government` is interpreted as the Governorate / Meta `region` report.

### Agents

```text
AA — Abdallah Adel
HM — Ahmed Hesham
BM — Bassem Shalawy
EK — Esraa Kamal
MA — Mahmoud
AF — Amr Fathy
SQ — Ahmed Sharkawy
OS — Osama Serwe
MM — Mohamed Mahmoud
NB — Mohamed Nabih
```

---

## 2. Flexible Commands

### English / keywords

```text
7day EK Age
7day - Esraa - Age
Today AA
Allocation
3 EK Age Government
Full AA
Yesterday HM
30day BM Government
```

Rules:

- No Agent = All Agents.
- No Report Type = Spend report.
- No Range = Today.
- More than one Report Type can be requested in one message.
- `Full` = Spend + Age + Governorate + Allocation.
- Allocation always uses the **current balance / current daily budget / Spend Today** snapshot. A 7-day range does not turn Allocation into a 7-day balance report.

### Natural Arabic

Examples supported by the built-in deterministic parser:

```text
عايز تقرير لعبدالله انهاردة للصرف وال AGE
عايز المحافظات لاسراء اخر 7 ايام
عايز الالوكيشن بتاع كل الناس
تقرير احمد هشام امبارح
عايز السن لاسراء الشهر ده
```

The parser handles common Arabic prefixes such as:

```text
لعبدالله
للصرف
والمحافظات
```

No external AI API or paid LLM is needed.

---

## 3. Report behavior

### Spend

For All Agents:

- Overall Spend
- Overall Leads
- Overall CPL
- Highest CPL
- Lowest CPL
- Every Agent's Spend / Leads / CPL
- Male Spend details

For one Agent:

- Agent Spend
- Agent Leads
- Agent CPL
- Male Spend only for that Agent's ad accounts

### Age

For the requested scope/range:

- 18-24
- 25-34
- 35-44
- 45-54
- 55-64
- 65+
- Spend
- Leads
- CPL
- % of total spend

If an Agent is selected, only that Agent's ad accounts are used.

### Government / Governorate

Meta `region` breakdown:

- Spend
- Leads
- CPL
- % of total spend

Rows are sorted by Spend descending.

If an Agent is selected, only that Agent's ad accounts are used.

### Allocation

Allocation keeps the balance project's current-state logic.

`Allocation` with no Agent:

- Spend Today
- Daily Budget
- Allocation Budget
- Spend vs Allocation
- Remaining Allocation
- Allocation note
- Agent performance

`Allocation AA`, `Allocation EK`, etc.:

Message 1:
- each Ad Account
- Daily Budget
- Balance
- Balance Coverage
- Alarm
- Overall Agent Daily Budget
- Overall Agent Balance

Message 2:
- recharge invoice for accounts below the 3-day target
- recharge amount = `Daily Budget × 3 - Current Balance`
- recharge value is rounded to the nearest 100 using `.50 => up`

---

# GitHub Setup

## 4. Create a new repository

Recommended repository name:

```text
meta_whatsapp_smart_reports
```

Upload all files from this package to the repository root.

Final structure:

```text
meta_whatsapp_smart_reports/
├── smart_report.py
├── allocation_meta.py
├── allocation_config.py
├── cloudflare-worker.js
├── requirements.txt
├── README.md
├── MENU_PREVIEW.txt
├── .env.example
├── .gitignore
└── .github/
    └── workflows/
        └── smart_report.yml
```

---

## 5. GitHub Secrets

Open:

```text
Repository
→ Settings
→ Secrets and variables
→ Actions
→ Secrets
```

Add:

```text
META_ACCESS_TOKEN
WHATSAPP_ACCESS_TOKEN
WHATSAPP_PHONE_NUMBER_ID
```

Optional:

```text
MANUAL_BALANCE_OVERRIDES_JSON
```

Example:

```json
{"123456789":50000}
```

Only use the override if the Meta balance source for a prepaid account does not represent the actual usable funds.

---

## 6. GitHub Variables

Open:

```text
Repository
→ Settings
→ Secrets and variables
→ Actions
→ Variables
```

Add:

```text
META_API_VERSION
```

Value:

```text
v26.0
```

### Performance / Age / Governorate Business IDs

```text
REPORT_BUSINESS_IDS
```

Default:

```text
1935536750225128,751488620224306
```

### Allocation Business IDs

```text
ALLOCATION_BUSINESS_IDS
```

Default matching the allocation reference project:

```text
751488620224306,1178859133269743
```

### Overall Allocation

```text
OVERALL_ALLOCATION_BUDGET
```

Put the same daily allocation value used by your current Allocation system.

Example only:

```text
250000
```

If it stays `0`, the report will show:

```text
Allocation Budget: NOT CONFIGURED
```

Agent balance / recharge calculations do not use this overall allocation value.

---

## 7. Test GitHub before Cloudflare

Go to:

```text
Actions
→ Smart WhatsApp Meta Reports
→ Run workflow
```

Test 1:

```text
range_key: today
agent_code: AA
report_types: spend
recipient: 201280871971
raw_command: Today AA
```

Test 2:

```text
range_key: 7d
agent_code: EK
report_types: age,governorate
recipient: 201280871971
raw_command: 7day EK Age Government
```

Test 3:

```text
range_key: today
agent_code: ALL
report_types: allocation
recipient: 201280871971
raw_command: Allocation
```

Do not continue until the manual GitHub tests work.

---

# Cloudflare Setup

## 8. Create a new Worker

Cloudflare:

```text
Workers & Pages
→ Create application
→ Start with Hello World
→ Deploy
→ Edit code
```

Delete the Hello World code.

Paste the complete content of:

```text
cloudflare-worker.js
```

Save and Deploy.

---

## 9. GitHub Fine-Grained Token for the Worker

Create a new token specifically for this new repository.

GitHub:

```text
Settings
→ Developer settings
→ Personal access tokens
→ Fine-grained tokens
```

Repository access:

```text
Only select repositories
→ meta_whatsapp_smart_reports
```

Repository permissions:

```text
Actions → Read and write
Contents → Read-only
```

Store the result in Cloudflare as:

```text
GITHUB_TOKEN
```

Never place the token in JavaScript source code.

---

## 10. Cloudflare Secrets

Worker:

```text
Settings
→ Variables and Secrets
```

Create these as **Secret**:

```text
WHATSAPP_VERIFY_TOKEN
WHATSAPP_ACCESS_TOKEN
GITHUB_TOKEN
META_APP_SECRET
```

`META_APP_SECRET` must be the App Secret of the same Meta App whose webhook is connected to this Worker.

---

## 11. Cloudflare Variables

Create:

```text
WHATSAPP_PHONE_NUMBER_ID
META_API_VERSION
GITHUB_OWNER
GITHUB_REPO
GITHUB_WORKFLOW
GITHUB_REF
ALLOWED_NUMBERS
```

Recommended:

```text
META_API_VERSION = v26.0
GITHUB_OWNER = 3abdallah3adel100
GITHUB_REPO = meta_whatsapp_smart_reports
GITHUB_WORKFLOW = smart_report.yml
GITHUB_REF = main
```

Example allowed numbers:

```text
201280871971,201098320008,201012354080
```

Use international format without `+`.

---

# Meta Webhook Setup

## 12. Create / configure the Meta App

Use a separate Meta App if you want this bot fully isolated from the previous command bot.

Configure WhatsApp and set the Worker URL as the app's Webhook Callback URL.

Example:

```text
https://YOUR-WORKER.YOUR-SUBDOMAIN.workers.dev/
```

Verify Token must exactly equal the Cloudflare secret:

```text
WHATSAPP_VERIFY_TOKEN
```

Subscribe to:

```text
messages
```

Move the App to Live when production messages are required.

---

## 13. Subscribe the new Meta App to the WABA

Get the WhatsApp Business Account ID.

Graph API:

```text
GET /WABA_ID/subscribed_apps
```

Confirm the new App ID is present.

If not:

```text
POST /WABA_ID/subscribed_apps
```

Expected:

```json
{"success": true}
```

Then GET again and confirm the App appears.

---

# Final tests

## 14. Menu

Send:

```text
Menu
```

You should receive the full menu.

## 15. Agent Spend

Send:

```text
Today AA
```

Expected:

```text
Today
Abdallah Adel (AA)
Spend / Performance
```

Then the report arrives after GitHub completes.

## 16. Age

Send:

```text
7day EK Age
```

Expected:

```text
Last 7 Days
Esraa Kamal (EK)
Age
```

## 17. Multi-report

Send:

```text
3 EK Age Government
```

GitHub sends:

1. Age report
2. Governorate report
3. Request Completed separator

## 18. Arabic

Send:

```text
عايز تقرير لعبدالله انهاردة للصرف وال AGE
```

Expected interpretation:

```text
Range: Today
Agent: Abdallah Adel (AA)
Reports: Spend + Age
```

---

# Debugging

Cloudflare:

```text
Worker
→ Observability
→ Live
```

Useful errors:

```text
Ignored unauthorized sender
```

Fix `ALLOWED_NUMBERS`.

```text
GitHub dispatch 401
```

Replace `GITHUB_TOKEN`.

```text
GitHub dispatch 403
```

Give the Fine-Grained token `Actions: Read and write`.

```text
GitHub dispatch 404
```

Check:

```text
GITHUB_OWNER
GITHUB_REPO
GITHUB_WORKFLOW
```

```text
WhatsApp API 401
```

Check `WHATSAPP_ACCESS_TOKEN`.

GitHub failures should be diagnosed from:

```text
Actions
→ failed run
→ Generate requested WhatsApp report
```
