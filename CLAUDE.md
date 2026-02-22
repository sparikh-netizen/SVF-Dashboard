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
- `bot.py` — Telegram bot, runs with `python3 bot.py`
- `requirements.txt` — pip dependencies (python-telegram-bot, requests, python-dotenv)
- Answers Shopify sales queries: "sales today", "revenue today", "shopify today", etc.
- Queries Shopify Orders API (paid orders since UTC midnight) and returns revenue + order count

## Current priorities
1. Deploy to Railway (always-on)
2. Morning briefing (revenue + low stock + calendar)
3. Gmail invoice finder
4. Picker workflow (order replacements + refunds via Telegram)