import json
import logging
import os
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

load_dotenv()

SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

_raw = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in _raw.split(",") if uid.strip()]

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Date ranges
# ---------------------------------------------------------------------------

def get_date_range(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        return today_start, now

    if period == "yesterday":
        start = today_start - timedelta(days=1)
        end = today_start - timedelta(seconds=1)
        return start, end

    if period == "last_7_days":
        return now - timedelta(days=7), now

    if period == "this_week":
        monday = today_start - timedelta(days=today_start.weekday())
        return monday, now

    if period == "last_week":
        this_monday = today_start - timedelta(days=today_start.weekday())
        last_monday = this_monday - timedelta(days=7)
        last_sunday_end = this_monday - timedelta(seconds=1)
        return last_monday, last_sunday_end

    if period == "this_month":
        return today_start.replace(day=1), now

    if period == "last_month":
        first_of_this_month = today_start.replace(day=1)
        last_month_end = first_of_this_month - timedelta(seconds=1)
        last_month_start = last_month_end.replace(day=1)
        return last_month_start, last_month_end

    # fallback
    return today_start, now


PERIOD_LABELS = {
    "today": "today",
    "yesterday": "yesterday",
    "last_7_days": "the last 7 days",
    "this_week": "this week (Mon → now)",
    "last_week": "last week",
    "this_month": "this month",
    "last_month": "last month",
}


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------

def fetch_orders(start: datetime, end: datetime) -> list:
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-10/orders.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
    params = {
        "status": "any",
        "financial_status": "paid",
        "created_at_min": start.isoformat(),
        "created_at_max": end.isoformat(),
        "limit": 250,
    }
    response = requests.get(url, headers=headers, params=params, timeout=10)
    response.raise_for_status()
    return response.json().get("orders", [])


def sales_by_period(period: str) -> dict:
    start, end = get_date_range(period)
    orders = fetch_orders(start, end)
    revenue = sum(float(o["total_price"]) for o in orders)
    return {"revenue": revenue, "order_count": len(orders), "period": period}


def sales_by_product(period: str, product: str) -> dict:
    start, end = get_date_range(period)
    orders = fetch_orders(start, end)

    needle = product.lower()
    total_qty = 0
    total_revenue = 0.0

    for order in orders:
        for item in order.get("line_items", []):
            if needle in item["title"].lower():
                total_qty += item["quantity"]
                total_revenue += float(item["price"]) * item["quantity"]

    return {
        "product": product,
        "quantity": total_qty,
        "revenue": total_revenue,
        "period": period,
    }


# ---------------------------------------------------------------------------
# Claude intent parsing
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You parse sales questions for a Shopify store called Spice Village (South Asian grocery, Dublin, Ireland).

Return ONLY valid JSON — no explanation, no markdown fences.

Schema:
{
  "intent": "sales_by_period" | "sales_by_product" | "unknown",
  "period": "today" | "yesterday" | "last_7_days" | "this_week" | "last_week" | "this_month" | "last_month" | null,
  "product": "<product name>" | null
}

Rules:
- "last week" → last_week, "this week" → this_week, "past 7 days" → last_7_days
- "this month" → this_month, "last month" → last_month
- If a product is named, intent = sales_by_product
- If no period is mentioned, default period = today
- If the message is not a sales/revenue/orders question, intent = unknown
"""


def parse_intent(message: str) -> dict:
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )
    text = response.content[0].text.strip()
    # Strip accidental markdown code fences
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------

def format_period_response(data: dict) -> str:
    label = PERIOD_LABELS.get(data["period"], data["period"])
    return (
        f"Shopify sales — {label}\n"
        f"Revenue: €{data['revenue']:,.2f}\n"
        f"Orders:  {data['order_count']}"
    )


def format_product_response(data: dict) -> str:
    label = PERIOD_LABELS.get(data["period"], data["period"])
    if data["quantity"] == 0:
        return f"No paid sales found for \"{data['product']}\" {label}."
    return (
        f"\"{data['product']}\" — {label}\n"
        f"Revenue: €{data['revenue']:,.2f}\n"
        f"Units sold: {data['quantity']}"
    )


HELP_TEXT = (
    "I can answer questions like:\n"
    "• What were my sales today?\n"
    "• Revenue yesterday?\n"
    "• Sales last week / this month?\n"
    "• How much basmati rice did I sell this week?"
)


# ---------------------------------------------------------------------------
# Telegram handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logger.info("Ignoring unauthorised user %s", user_id)
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # Parse intent with Claude
    try:
        parsed = parse_intent(text)
    except Exception as exc:
        logger.error("Intent parse error: %s", exc)
        await update.message.reply_text(
            "Sorry, I had trouble understanding that.\n\n" + HELP_TEXT
        )
        return

    intent = parsed.get("intent", "unknown")
    period = parsed.get("period") or "today"
    product = parsed.get("product")

    logger.info("intent=%s period=%s product=%s", intent, period, product)

    if intent == "unknown":
        await update.message.reply_text(HELP_TEXT)
        return

    await update.message.reply_text("Checking Shopify...")

    try:
        if intent == "sales_by_product" and product:
            data = sales_by_product(period, product)
            reply = format_product_response(data)
        else:
            data = sales_by_period(period)
            reply = format_period_response(data)
    except Exception as exc:
        logger.error("Shopify error: %s", exc)
        reply = f"Couldn't fetch Shopify data: {exc}"

    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
