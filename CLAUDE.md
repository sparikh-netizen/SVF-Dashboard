# Spice Village Assistant - Project Context

## What we're building
A Telegram bot that acts as an executive assistant for Spice Village. 
It connects to Shopify, Flour Cloud, Gmail, and Google Calendar.
Runs 24/7 on Railway. Interface is Telegram only.

## Business Context
- Spice Village is a South Asian grocery business based in Dublin
- Two sales channels: Shopify (online) + Flour Cloud (retail POS)
- Owner: Harsh (sparikh-netizen on Github)

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

## Current priorities
1. Morning briefing (revenue + low stock + calendar)
2. Picker workflow (order replacements + refunds via Telegram)