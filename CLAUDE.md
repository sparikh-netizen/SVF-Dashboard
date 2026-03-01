# Spice Village Assistant - Project Context

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