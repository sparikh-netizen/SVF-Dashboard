# Spice Village Assistant - Project Context

## Shopify API Rules (apply to ALL Shopify scripts)
- **Costs**: always fetch via `GET /inventory_items/{id}.json` → `cost` field. Never read cost from the product or variant object.
- **Pagination**: always use cursor-based `page_info` pagination with `limit=250`. Never use page-number pagination.
- **Rate limiting**: always add `time.sleep(1)` between batch API calls (Shopify REST = 2 req/s). Always implement 429 retry with `Retry-After` header before continuing.
- **Batch cost fetch**: fetch inventory costs in batches of 100 IDs via `/inventory_items.json?ids=...`. 1s delay between batches, retry on 429.
- **Script execution**: run synchronously. No background tasks in standalone scripts.
- **Record counts**: always log/confirm variant count, order count, and cost coverage before processing.

## What we're building
A Telegram bot that acts as an executive assistant for Spice Village. 
It connects to Shopify, Flour Cloud, Gmail, and Google Calendar.
Runs 24/7 on Railway. Interface is Telegram only.

## Business Context
- SVF Products GmbH — South Asian grocery business based in Berlin, Germany
- Two sales channels: Shopify (online) + Flour Cloud (retail POS)
- Owner: Harsh (sparikh-netizen on Github)

## Company Details
- Legal name: SVF Products GmbH
- Address: Tempelhofer Damm 206, 12099 Berlin
- Website: www.spicevillage.eu
- Email: svfproducts@spicevillage.eu
- Invoices email: invoices@spicevillage.eu
- Phone: +49 30 8965 7586
- Tax Number: 29/553/32289
- VAT Number: DE363532317
- Handelsregister: Charlottenburg HRB 256768 B
- EORI: DE260532672959166
- Managing Directors: Nikunj Patel, Alpa Parikh
- IBAN: DE38100101237197421588
- BIC: QNTODEB2XXX
- PayPal: svfproducts@spicevillage.eu

## Tech Stack
- Python (backend)
- Telegram bot (user interface)
- Railway (hosting, always-on)
- Github repo: https://github.com/sparikh-netizen/SVF-Dashboard

## API Credentials (use from .env file, never hardcode)
- SHOPIFY_STORE: spice-village-eu.myshopify.com
- SHOPIFY_ACCESS_TOKEN: in .env
- TELEGRAM_BOT_TOKEN: in .env
- ANTHROPIC_API_KEY: in .env
- FLOUR_CLOUD_TOKEN: in .env
- GOOGLE_API_KEY: in .env
- GOOGLE_SERVICE_ACCOUNT_JSON: set as Railway env var (contents of service_account.json)

## Shopify Details
- API Version: 2024-10
- Location ID: 65313800346

## Business Context — Sales Channels
- **Online**: Shopify (spice-village-eu.myshopify.com)
- **Retail**: Flour Cloud POS (flour.host) — Berlin timezone dates
- **Restaurant**: separate venue, tracked in Google Sheets (monthly cash flow workbook)

## What's already built (Google Apps Scripts in Sheets)
- Monthly Shopify order sync
- Same day delivery tracking
- GA4 device + city sync
- Flour Cloud annual sales by product
- Product inventory + average sales tracker
- AI product tagging
- Shopify product sync (prices + inventory)
- Bulk tag updater

## Bot behaviour
- Only respond to whitelisted Telegram user IDs
- Always on, hosted on Railway
- Natural language understanding via Claude API
- Query Shopify and other APIs in real time when asked

## What's built so far

### Bot infrastructure
- `bot.py` — single-file Telegram bot, deployed on Railway (project: cooperative-laughter)
- `requirements.txt` — python-telegram-bot, requests, python-dotenv, anthropic, pytz, google-auth, google-api-python-client
- Natural language intent parsing via Claude Haiku (claude-haiku-4-5-20251001)
- Whitelist support via ALLOWED_USER_IDS env var

### Shopify (online channel)
- Sales by period: today, yesterday, last 7 days, this week, last week, this month, last month
- Sales by product keyword (searches line item titles)
- Fetches all orders excluding refunded/voided; paginated with Link header support
- Timeout: 30s

### Flour Cloud (retail POS channel)
- Base URL: https://flour.host/v3/documents?limit=1000&type=R&sort=-date
- Date filtering in Europe/Berlin timezone (store POS timezone)
- Skips cancelled items
- Item fields: title (name), amount (qty), totalIncVat (revenue)

### Cross-channel queries
- "retail sales yesterday" → Flour Cloud only
- "online sales last week" → Shopify only
- "total sales this month" → combined figure
- "compare online and retail" → side-by-side breakdown
- All of the above work for product-level queries too

### Gmail search
- Searches 4 inboxes: invoices@, svfproducts@, info@, sparikh@spicevillage.eu
- Auth: service_account.json locally / GOOGLE_SERVICE_ACCOUNT_JSON env var on Railway
- Domain-wide delegation on spicevillage.eu Google Workspace
- Returns top 3 results per inbox with subject, sender, date, direct Gmail link

### Restaurant sales (Google Sheets)
- Spreadsheet ID: `1BiiLjs30NF_O6xZz_O-G4bfgcbQnGP6GuRW12vgJJKo` (stored as `RESTAURANT_SHEET_ID`)
- One tab per month, named "Month YYYY" e.g. "February 2026"
- Row 3: date headers in DD/MM/YYYY format, starting column I (idx 8), one column per day
- "Restaurant sales" row found by searching col D for label (row number varies per month — ~267-276)
- Col E (idx 4) = MTD Actual total; daily value = column matching date in row 3
- Auth: same service account as Gmail (spreadsheets.readonly scope)
- Key functions: `_fetch_restaurant_tab(tab_name, find_date_str)` → `restaurant_sales_all()` → `{"yesterday": float, "mtd": float}`
- Handles month-boundary edge case (yesterday in different tab from current month)

### Daily 4am Briefing
- Scheduled at 04:00 CET/CEST (Europe/Berlin) via `job_queue.run_daily`
- Sends to chat ID configured via `DAILY_REPORT_CHAT_ID` env var
- Reports yesterday's revenue + MTD revenue for: Retail (Flour Cloud), Online (Shopify), Restaurant (Sheets)
- Each source fetched independently — unavailable shown if one source fails
- Retry logic: 3 attempts with 5-minute `asyncio.sleep` between retries (non-blocking)
- Enabled via `python-telegram-bot[job-queue]==20.7` (APScheduler dependency)

### Supplier outstanding (Google Sheets)
- Spreadsheet ID: `1JMfhCB-af8DNnbYNe2Any2oakbNRJHOFvdFZXDMq1qg` (stored as `SUPPLIER_SHEET_ID`)
- One tab per supplier (Transfood, Smart Elite, Shalamar, Swagat, AR Food, IPS, Om Food, Das Vegarma, GFT, Bonesca, Asia Express, Deilght Food, Crown, Kumar Ayurveda, Sona Food, Umer, Aayush, Taya, Aheco, Bakery, Desi Megamart)
- Row 2: col G (idx 6) = TOTAL PAYMENT DUE (overdue), col J (idx 9) = TOTAL PAYMENT BALANCE (all outstanding)
- Row 8: column headers — Invoice Date (B), Invoice No (C), Total Invoice Amount (E), Due by Date (F), Payment 1-4 + dates, Payment Balance (O, idx 14)
- Rows 9+: one row per invoice/credit note; RE... = invoices (positive), GS... = credit notes (negative)
- Outstanding = any row where Payment Balance ≠ 0
- Tab matched by fuzzy search: exact → prefix → substring
- Key functions: `_find_supplier_tab(svc, query)`, `fetch_supplier_outstanding(supplier_name)`, `fmt_supplier_outstanding(data)`
- Intent: `supplier_outstanding` — triggered by "what do we owe X", "outstanding for X", "unpaid invoices X"

## Dashboard (index.html + config.js)

### Google Sheets structure
- `spreadsheetIdPro` (`1fa694tBLbrJbRPQVh0Y9TLKinjSWsLrpOoJININmjXY`) — operational data sheet, tabs:
  - `Pro. Monthly` — monthly P&L summary (Month, Total Sales, Sales_Online[2], Sales_Retail[3], Sales_Restaurant[4], COGS_Online[6], COGS_Retail[7], Net Profit[10], Salary[13], Shipping[14])
  - `Shopify_Orders_Monthly` — Shopify orders by month (Month[0], Total Orders[2], Net Sales[5])
  - `Retail Monthly Sales` — Flour Cloud retail by month (Date[0], Total Sales[2])
  - `Restaurant Monthly Sales` — 3 rows per month: Physical / Wolt / Uber (Date[0], Channel[1], Total Sales[3])
  - `Shopify_SameDay_Monthly` — same-day delivery (Month[0], Total[1], SameDay[2], %[3])
  - `GA4_Device_Weekly`, `GA4_Top_Cities`
- `spreadsheetIdPur` (`1wpntBkyUwS16kkJijmCjgwArD7P-6x3djx-ikBB0BzE`) — purchase data, tab `Dashboard` (rows from idx 4, col 0=date, col 1=amount)

### Annual profitability workbooks (separate from operational sheet)
- 2025: `1_X_YBpjiATBFosxKZxB-FLjSkvQDSjRVedqRjNLGNWs` — monthly P&L tabs + Summary (months as columns, metrics as rows)
- 2026: `17Fg-VINGcFmpIMoqmyizsQQR-fNn55QON_9D5UVSI3I` — currently returns 403 (needs "Anyone with link can view" sharing setting)

### Known data gaps (as of 2026-03)
- `Pro. Monthly` only has Jan–Dec 2025 rows — need to add 2026 monthly rows for Net Profit YTD tile to show
- `Retail Monthly Sales` only has through Jan 2026 (Feb+ missing)
- `Restaurant Monthly Sales` only has through Jan 2026 (Feb+ missing)
- `Shopify_Orders_Monthly` and `Shopify_SameDay_Monthly` are up to Feb 2026

### Security
- Google API key in config.js must be restricted to GitHub Pages referrer in Google Cloud Console
- Shopify token was removed from config.js (was exposed in public repo) — do NOT re-add to any committed file

## Current priorities
1. Picker workflow (order replacements + refunds via Telegram)

---

## COGS Pipeline

### Scripts
- `cogs_pipeline.py` — daily COGS pipeline module (imported by bot.py)
- `weekly_cogs_report.py` — ad-hoc weekly COGS report, run manually from terminal

### Daily pipeline (`cogs_pipeline.py`)
- Runs at **08:00 Berlin time** via `job_queue.run_daily` in bot.py (`send_cogs_report`)
- Pulls yesterday's Shopify orders (Berlin local date, UTC boundaries for API)
- Excludes cancelled orders; refunds remove revenue but keep COGS (damaged goods rule)
- Fetches costs via `inventory_item_id` → locked as `cost_at_sale` in SQLite at time of processing
- Appends one row per product (≥1 unit sold) to Google Sheet — never overwrites
- Sends Telegram summary to `DAILY_REPORT_CHAT_ID`

### SQLite database
- Path: env var `COGS_DB_PATH`, default `./cogs.db`
- **Requires Railway persistent volume** — ephemeral filesystem will lose data on redeploy
- Table: `order_line_items` — primary key `(order_id, line_item_id)`, idempotent on re-run
- Columns: `order_id`, `line_item_id`, `order_date` (Berlin YYYY-MM-DD), `sku`, `title`, `variant_id`, `gross_qty`, `gross_revenue`, `refund_qty`, `refund_revenue`, `cost_at_sale`
- Used for rolling 7-day and MTD summaries without extra Shopify API calls

### Google Sheet — COGS Shopify
- **Sheet ID:** `1vmL9PXQMgwxEioHAIydtOvRbPwBaF4gQbUsIbfG2Y_A`
- **Tab:** `COGS Daily`
- **Columns:** Date | SKU | Product name | Units sold | Net revenue € | Net COGS € | COGS% | Gross profit €
- Tab auto-created with header if missing. Date-deduplication prevents double-writes.
- Service account: `spice-village-bot@spice-village-bot.iam.gserviceaccount.com`

### Telegram message format (08:00 daily)
```
📊 COGS Report — DD Mon YYYY
📅 Yesterday — Revenue: €X,XXX | COGS: XX.X% | Gross Profit: €X,XXX
📆 Last 7 Days — Revenue: €X,XXX | COGS: XX.X% | Gross Profit: €X,XXX
🗓 Month to Date — Revenue: €X,XXX | COGS: XX.X% | Gross Profit: €X,XXX
⚠️ Problem Products (revenue >€50, COGS >60%)
🔍 Full detail: https://docs.google.com/spreadsheets/d/1vmL9PXQMgwxEioHAIydtOvRbPwBaF4gQbUsIbfG2Y_A
```

### Ad-hoc report (`weekly_cogs_report.py`)
- Run manually: `python3 weekly_cogs_report.py 2026-03-18 2026-03-25`
- Fetches costs fresh from Shopify each run (no SQLite)
- Prints to terminal only — does not write to Sheet or Telegram

---

## Financial Sheets — Full Context (as of Mar 2026)

Five core Google Sheets track SVF's finances. All accessible via the spice-village-bot service account.

---

### 1. SVF Supplier Ledger
**Sheet ID:** `1JMfhCB-af8DNnbYNe2Any2oakbNRJHOFvdFZXDMq1qg`
**Tabs:** Master Sheet, Opening Balance 25, then one tab per supplier (22 suppliers)

**Structure per supplier tab:**
- Row 2: col G (idx 6) = TOTAL PAYMENT DUE (overdue invoices only), col J (idx 9) = TOTAL PAYMENT BALANCE (all outstanding)
- Row 8: column headers — Invoice Date (B), Invoice No (C), Total Invoice Amount (E), Due by Date (F), Payments 1–4 + dates, Payment Balance (O)
- Rows 9+: one row per invoice. RE... = invoices (positive), GS... = credit notes (negative)
- Negative DUE = supplier owes us credit

**Outstanding balances (Mar 2026):**
| Supplier | Overdue (DUE) | Total Outstanding |
|----------|---------------|-------------------|
| Transfood | -€427.59 (credit) | €36,961.32 |
| GFT (Global Food) | €8,078.90 | €42,706.90 |
| Smart Elite | €4,838.73 | €11,487.33 |
| Umer | €5,526.40 | €11,593.10 |
| AR Food | €0 | €8,819.51 |
| Swagat | €1,291.81 | €7,261.91 |
| Taya | €4,725.75 | €5,676.55 |
| Sona Food | €1,190.35 | €3,584.23 |
| Om Food | €275.50 | €3,015.79 |
| Das Vegarma | €469.37 | €2,217.02 |
| Asia Express | -€216.50 (credit) | €1,936.94 |
| Kumar Ayurveda | €0 | €1,447.90 |
| Delight Food | €0 | €1,330.92 |
| Aayush | €1,341.58 | €1,341.58 |
| Aheco | €550.40 | €550.40 |
| Desi Megamart | €300.50 | €300.50 |
| Bakery | €74.90 | €74.90 |
| Shalamar, IPS, Bonesca, Crown | €0 | €0 |

**Totals (Mar 2026):** ~€28,227 overdue (excl. credits), ~€140,306 total outstanding

---

### 2. SVF Cash Book
**Sheet ID:** `1BiiLjs30NF_O6xZz_O-G4bfgcbQnGP6GuRW12vgJJKo`
**Key tabs:** Month wise summary, one tab per month (Oct 2023 → Mar 2026), Qonto Loan 2026, Qonto bill discount, New Master Sheet

**Month wise summary columns:** Month | Vendor purchase | Vendor Payments | Shop Sales | Online Sales | Cafe Sales | Total sale | Salary/OT | Shop profit | Online Profit | Cafe Profit | Total Profit | % Profit | Berlin no of orders

**Monthly cash book structure (each tab):**
- Sections: Bank Book (top), then Cash Book (below closing bank balance)
- Bank inflow: Retail (Concardis/SumUp), Restaurant (Wolt/Stripe/Lifferando), Online (Shopify + PayPal), Others
- Loan Schedule: Loan Taken (Qonto, Wayflyer, Shopify) and Loan Repayments (subtracted)
- Purchases-Bank: Restaurant purchases + vendor payments (Transfood, GFT, AR Foods, etc.)
- Bank-Salary, Bank-Packaging, Bank-Shipping (DPD, Charif same-day, DHL)
- Bank-Operating Exp: Rent, Electricity, Legal, Sales Promotion, IT, Shopify, Qonto, SevDesk, etc.
- Bank-Compliance: BKK, TK, AOK (health insurance)
- Closing Bank Balance = Opening + Inflow - Outflow

**March 2026 actuals (complete month):**
- Bank inflow: €147,947.61 (Retail €40,779 + Restaurant €8,926 + Online €98,173 + Other €70)
- Loan taken: €14,739.12 (Qonto bill discount only)
- Loan repayments: -€49,942.79 (DV Parikh €1,250 + Wayflyer €17,004 + Shopify €10,718 + Qonto €20,971)
- Vendor purchases: €94,112.11 (Restaurant €1,951 + Vendors €92,161)
- Salary: €4,309.22 (Shivam €553, DVP €2,030, NP €871, Upwork €300, Harsh Basia €555)
- Packaging: €1,113.02
- Shipping: €18,427.06 (Charif same-day €13,031 + DPD €3,946 + DHL €1,450)
- Operating expenses: €13,954.63 (Rent €8,755, Electricity €1,962, Legal €726, IT €1,202, etc.)
- Compliance: €4,155.47 (health insurance)
- Total Bank Outflow: €136,542.94
- **Closing Bank Balance: €91,286.58**
- Cash (Retail €11,849 + Restaurant €2,083)

**Active loan products:**
- **Qonto Bill Discount:** Opening balance €75,000; €90,916.58 total payable; €29,766.56 paid; €61,150.02 due; €13,849.98 available. Used to discount supplier invoices (pay supplier immediately via Qonto, repay Qonto later + interest ~2.3% per invoice). See `Qonto Loan 2026` tab.
- **Wayflyer:** Revenue-based advance. Repaying ~€15,940–17,004/month. Expires mid-2026.
- **Shopify Capital:** Repaying ~€10,718–11,000/month.
- **DV Parikh Targo Bank:** €1,250/month personal loan repayment.
- **Alpa Parikh loan:** €1,000/month principal + €1,500/month interest.

---

### 3. Profitability 2025
**Sheet ID:** `1_X_YBpjiATBFosxKZxB-FLjSkvQDSjRVedqRjNLGNWs`
**Key tabs:** Monthly report + raw tabs for each month (Jan–Dec 2025), Summary, Check

**Summary tab structure:** Months as column groups (3 cols each: Shop / Online / Restaurant). Rows: Sales, COGS, Gross Profit, Misc Expenses, Packing, Restaurant Purchases, Logistics, Fixed Costs, Salary, OT, Total Exp, Net Profit. Then Total Profit per quarter.

**2025 Annual Results:**
- **Total Net Profit: €55,708.12**
- Approximate full-year revenue: ~€1.7M (Shop ~€530K + Online ~€1M + Restaurant ~€170K)
- Net margin: ~3.3%

**Monthly net profit 2025:**
| Month | Shop | Online | Restaurant | Total |
|-------|------|--------|------------|-------|
| Jan | -€907 | €329 | -€1,803 | **-€2,381** |
| Feb | €268 | €1,038 | €339 | **€1,645** |
| Mar | €1,318 | €2,864 | €2,218 | **€6,400** |
| Apr | €6,401 | €339 | -€1,297 | **€5,443** |
| May | €2,433 | ~€0 | ~€0 | **~€2,433** |
| Jul | -€201 | €3,070 | €1,555 | **€4,424** |
| Aug | €1,586 | €10,053 | €1,259 | **€12,898** |
| Sep | -€623 | -€450 | €8,504 | **€7,431** |
| Oct | €3,033 | -€1,119 | €4,522 | **€6,436** |
| Nov | -€633 | ~€2,800 | ~€2,800 | **~€6,686** |

**Key cost structure (Shop channel, typical month):**
- COGS: ~60% of sales
- Logistics: €11–16K/month (all online)
- Fixed costs (rent split): ~€5,700–8,300/month per channel
- Salary: ~€6,500–7,700/month per channel

---

### 4. Purchase Forecast
**Sheet ID:** `1myt2haBSqERKMARiPPt9NSMSZd4lKeKSt6mUSS7P8nA`
**Tabs:** January–December (2025 monthly), Summary 2025, Jan-26, Feb 26, Mar 26

**Structure per month tab:** Party Name | Week 1 (Project / Actual) | Week 2 | Week 3 | Week 4 | Total Project | Total Actual
**Summary 2025 tab:** Party Name | Jan–Dec with Project vs Actual columns | (no annual total column)

**Top suppliers by annual 2025 purchase volume (approximate actuals):**
- Transfood: ~€215K (biggest supplier; 2x/month large orders €9–29K each)
- AR Foods: ~€95K
- GFT (Global Food): ~€85K (also uses alias "Exodeen" for Nirala Foods?)
- Smart Elite: ~€75K
- Nirala Foods: ~€70K
- Swagat: ~€28K
- Asia Express: ~€56K
- Shalamar: ~€7K (dropped to zero mid-2025)
- Kumar Ayurveda: ~€25K
- Sona Food: started May 2025, growing
- Taya: started Jan 2026
- Om Food, Das Vegas, Delight Food, Crown, Bonesca: smaller (~€1–5K/month each)

**2026 monthly purchase actuals vs target:**
| Month | Target | Actual | Variance |
|-------|--------|--------|---------|
| Jan 2026 | €81,100 | €81,483 | On track |
| Feb 2026 | €81,100 | €69,361 | -€11,739 (some deferred) |
| Mar 2026 (partial) | €81,100 | €54,762 | Partial month |

**Key pattern:** Transfood and GFT are bi-weekly large orders (~€8–16K each). Weekly suppliers: Nirala Foods, Taya, Sona Food, AR Foods, Om Food.

---

### 5. Cash Flow Projection
**Sheet ID:** `10UxZLVwIgg0DFurZ-syXktRCHqfM37wPV1VmoJ3nsXE`
**Tabs:** Two main scenarios: `Mar to Dec 2026` (with big loans) and `Mar to Dec 2026 without loan` (Qonto bill discounting only)

**Revenue assumptions (same in both scenarios):**
- Online: €90–100K/month (€80K Dec)
- Retail: €45–50K/month (€40K Dec)
- Restaurant: €15–16K/month (€14K Dec)
- **Total: €150–166K/month, €1,586,000 for the period**

#### Scenario A — With big loans (Wayflyer + Shopify Capital):
- Outflow: €1,839,368 total
- Net operating cash flow: +€65,530 (before debt service)
- Loan repayments: €318,898 (Wayflyer €123K + Shopify €107K + Qonto €51K + others)
- Cash flow after creditors: **-€253,368**
- Loan injections: €220,000 (Shopify €120K in Mar, Wayflyer €100K in Jun)
- Running balance: €81,863 → €45,648 → drops to **-€33,368 in Dec** (goes negative)

#### Scenario B — Without big loans (Qonto bill discounting only):
- Outflow: €1,886,728 (higher due to Qonto interest on each invoice)
- Net operating cash flow: +€55,530
- Loan repayments: €356,258 (Qonto monthly ~€23–35K replaces Wayflyer/Shopify)
- Cash flow after creditors: **-€300,728**
- Loan injection: €311,000 (all Qonto bill discount, monthly €25–40K)
- Running balance: **-€13,137 in Mar** → turns positive May → ends at **+€10,272 in Dec**
- Requires existing cash buffer of at least €15,500 to cover March opening negative

**Why Scenario B is preferred:** No large-principal loans means no lump-sum interest charges. Qonto bill discount charges interest only on the specific invoices discounted (~2.3%), not on a fixed loan amount. Year-end balance is positive vs negative. Total loan injections (€311K) are lower than Scenario A's effective commitment.

**⚠️ Known data error in both scenarios:** "Other Expenses" line (€3,350) and its sub-items (Dooplepack €800 + Sales promotion €1,300 + Amazon €750 + Misc €500 = €3,350) are both counted in Outflow Total, causing **double-counting of €3,350/month = €33,500 overstated for the period**. The actual outflow and deficit is lower than shown.

**Running balance summary — Scenario B:**
| Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |
|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|
| -€13,137 | -€5,352 | +€7,851 | +€7,054 | +€8,257 | +€18,960 | +€32,663 | +€18,866 | +€33,069 | +€10,272 |

---

---

### 6. Profitability Projection 2026
**Sheet ID:** `1NJWM0iwMURJCtVOx5I86i0VIYo-gJeM-WQy49NVJ26c`
**Tab:** Sheet1
**Purpose:** Live forward-looking P&L projections for Mar–Dec 2026 by channel (Shop/Retail, Online, Restaurant)

**Structure:** 10 months (Mar–Dec), three channel columns per month. Rows include: Sales, COGS, COGS%, Gross Profit, Misc Expenses, Packing, Restaurant Purchases, Logistics, Fixed Costs, Salary, OT, Promotion, Commission, Interest, Depreciation, Net Profit, Total Profit per month.

**Interest structure (rows 46–51):**
- Shopify interest: €1,650/month (Mar–Dec)
- Qonto interest: €500/month (Apr–Jun only)
- 3rd Party interest: €2,500/month (Mar–Dec)
- Wayflyer/Qonto additional: €1,000/month (Oct, Nov, Dec)

**Monthly projected net profit:**
| Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |
|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|
| €1,376 | -€3,131 | €1,772 | €3,090 | -€1,412 | €3,727 | -€389 | €1,643 | -€6,639 | -€8,551 |

**Full year projected net profit: -€8,514.63** (loss — driven by high Nov/Dec costs + interest burden)

**Key sales assumptions (examples):**
- Aug: Shop €53K, Online €100K, Restaurant €15K
- Oct: Shop €60K, Online €100K, Restaurant €14K
- Nov: Shop €50K, Online €80K, Restaurant €12K (seasonally lower)
- Dec: Shop €65K, Online €90K, Restaurant €12K

---

### Financial Sheet IDs — Quick Reference
| Sheet | ID |
|-------|----|
| Supplier Ledger | `1JMfhCB-af8DNnbYNe2Any2oakbNRJHOFvdFZXDMq1qg` |
| Cash Book | `1BiiLjs30NF_O6xZz_O-G4bfgcbQnGP6GuRW12vgJJKo` |
| Profitability 2025 | `1_X_YBpjiATBFosxKZxB-FLjSkvQDSjRVedqRjNLGNWs` |
| Purchase Forecast | `1myt2haBSqERKMARiPPt9NSMSZd4lKeKSt6mUSS7P8nA` |
| Cash Flow Projection | `10UxZLVwIgg0DFurZ-syXktRCHqfM37wPV1VmoJ3nsXE` |
| Profitability Projection 2026 | `1NJWM0iwMURJCtVOx5I86i0VIYo-gJeM-WQy49NVJ26c` |