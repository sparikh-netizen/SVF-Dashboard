import json
import logging
import os
from datetime import datetime, timedelta, timezone

import anthropic
import pytz
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

load_dotenv()

SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
FLOUR_CLOUD_TOKEN = os.getenv("FLOUR_CLOUD_TOKEN")

_raw = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in _raw.split(",") if uid.strip()]

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Flour Cloud POS system uses Berlin local dates
BERLIN_TZ = pytz.timezone("Europe/Berlin")


# ---------------------------------------------------------------------------
# Date ranges
# ---------------------------------------------------------------------------

def get_date_range(period: str):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        return today_start, now
    if period == "yesterday":
        return today_start - timedelta(days=1), today_start - timedelta(seconds=1)
    if period == "last_7_days":
        return now - timedelta(days=7), now
    if period == "this_week":
        monday = today_start - timedelta(days=today_start.weekday())
        return monday, now
    if period == "last_week":
        this_monday = today_start - timedelta(days=today_start.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(seconds=1)
    if period == "this_month":
        return today_start.replace(day=1), now
    if period == "last_month":
        first_of_this_month = today_start.replace(day=1)
        last_month_end = first_of_this_month - timedelta(seconds=1)
        return last_month_end.replace(day=1), last_month_end
    return today_start, now  # fallback


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

def _parse_next_link(link_header: str):
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


def fetch_shopify_orders(start: datetime, end: datetime) -> list:
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-10/orders.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
    params = {
        "status": "any",
        "created_at_min": start.isoformat(),
        "created_at_max": end.isoformat(),
        "limit": 250,
    }
    all_orders = []
    while True:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        page = response.json().get("orders", [])
        all_orders.extend(page)
        if len(page) < 250:
            break
        next_url = _parse_next_link(response.headers.get("Link", ""))
        if not next_url:
            break
        url = next_url
        params = {}
    return [o for o in all_orders if o.get("financial_status") not in ("refunded", "voided")]


def shopify_sales(period: str) -> dict:
    start, end = get_date_range(period)
    orders = fetch_shopify_orders(start, end)
    revenue = sum(float(o["total_price"]) for o in orders)
    return {"revenue": revenue, "order_count": len(orders), "period": period}


def shopify_product_sales(period: str, product: str) -> dict:
    start, end = get_date_range(period)
    orders = fetch_shopify_orders(start, end)
    needle = product.lower()
    total_qty, total_rev = 0, 0.0
    for order in orders:
        for item in order.get("line_items", []):
            if needle in item["title"].lower():
                total_qty += item["quantity"]
                total_rev += float(item["price"]) * item["quantity"]
    return {"product": product, "quantity": total_qty, "revenue": total_rev, "period": period}


# ---------------------------------------------------------------------------
# Flour Cloud (retail POS)
# ---------------------------------------------------------------------------

def _berlin_date_range(period: str):
    """Return (start_date, end_date) as date objects in Europe/Berlin local time.

    Flour Cloud document dates are Berlin-local calendar dates, so we compute
    the range directly in that timezone rather than converting UTC boundaries.
    """
    from datetime import date as _date
    today = datetime.now(BERLIN_TZ).date()

    if period == "today":
        return today, today
    if period == "yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if period == "last_7_days":
        return today - timedelta(days=7), today
    if period == "this_week":
        monday = today - timedelta(days=today.weekday())
        return monday, today
    if period == "last_week":
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        last_sunday = this_monday - timedelta(days=1)
        return last_monday, last_sunday
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        first_of_this_month = today.replace(day=1)
        last_month_last = first_of_this_month - timedelta(days=1)
        return last_month_last.replace(day=1), last_month_last
    return today, today  # fallback


def fetch_flour_cloud_docs(start_date, end_date) -> list:
    headers = {"Authorization": f"Bearer {FLOUR_CLOUD_TOKEN}"}
    params = {"limit": 1000, "type": "R", "sort": "-date"}
    response = requests.get(
        "https://flour.host/v3/documents",
        headers=headers,
        params=params,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    if isinstance(data, list):
        docs = data
    else:
        docs = data.get("docs") or data.get("documents") or data.get("data") or []

    logger.info("Flour Cloud: fetched %d raw docs, filtering %s → %s (Berlin)", len(docs), start_date, end_date)

    filtered = []
    for doc in docs:
        raw_date = str(doc.get("date", ""))[:10]
        try:
            if start_date <= datetime.fromisoformat(raw_date).date() <= end_date:
                filtered.append(doc)
        except ValueError:
            continue
    return filtered


def flour_cloud_sales(period: str) -> dict:
    start_date, end_date = _berlin_date_range(period)
    docs = fetch_flour_cloud_docs(start_date, end_date)
    total_rev = 0.0
    for doc in docs:
        for item in doc.get("items", []):
            if item.get("cancelled"):
                continue
            total_rev += float(item.get("totalIncVat", 0))
    return {"revenue": total_rev, "transaction_count": len(docs), "period": period}


def flour_cloud_product_sales(period: str, product: str) -> dict:
    start_date, end_date = _berlin_date_range(period)
    docs = fetch_flour_cloud_docs(start_date, end_date)
    needle = product.lower()
    total_qty, total_rev = 0, 0.0
    for doc in docs:
        for item in doc.get("items", []):
            if item.get("cancelled"):
                continue
            if needle in str(item.get("title", "")).lower():
                total_qty += int(item.get("amount", 0))
                total_rev += float(item.get("totalIncVat", 0))
    return {"product": product, "quantity": total_qty, "revenue": total_rev, "period": period}


# ---------------------------------------------------------------------------
# Claude intent parsing
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You parse sales questions for Spice Village (South Asian grocery, Dublin).
It has two sales channels: Shopify (online orders) and Flour Cloud (retail/in-store POS).

Return ONLY valid JSON — no explanation, no markdown fences.

Schema:
{
  "intent": "sales_by_period" | "sales_by_product" | "unknown",
  "period": "today" | "yesterday" | "last_7_days" | "this_week" | "last_week" | "this_month" | "last_month" | null,
  "channel": "online" | "retail" | "total" | "compare" | null,
  "product": "<product name>" | null
}

Channel rules:
- "online", "shopify", "website", "web orders" → online
- "retail", "in store", "in-store", "shop", "flour cloud", "pos", "walk-in" → retail
- "total", "overall", "combined", "all channels", "all" → total
- "compare", "vs", "versus", "online and retail", "retail and online" → compare
- If no channel mentioned → null (defaults to online/Shopify)
- Channel rules apply equally when a product is named

Period rules:
- "last week" → last_week, "this week" → this_week, "past 7 days" → last_7_days
- "this month" → this_month, "last month" → last_month
- If no period mentioned → today

Other rules:
- If a product name is mentioned, intent = sales_by_product
- If not a sales/revenue/orders question, intent = unknown
"""


def parse_intent(message: str) -> dict:
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------

def fmt_period(data: dict, channel_label: str) -> str:
    label = PERIOD_LABELS.get(data["period"], data["period"])
    count_key = "order_count" if "order_count" in data else "transaction_count"
    count_label = "Orders" if "order_count" in data else "Transactions"
    return (
        f"{channel_label} — {label}\n"
        f"Revenue: €{data['revenue']:,.2f}\n"
        f"{count_label}: {data[count_key]}"
    )


def fmt_product(data: dict, channel_label: str = "") -> str:
    label = PERIOD_LABELS.get(data["period"], data["period"])
    prefix = f"{channel_label} — " if channel_label else ""
    if data["quantity"] == 0:
        channel_note = f" on {channel_label}" if channel_label else ""
        return f"No sales found for \"{data['product']}\" {label}{channel_note}."
    return (
        f"{prefix}\"{data['product']}\" — {label}\n"
        f"Revenue: €{data['revenue']:,.2f}\n"
        f"Units sold: {data['quantity']}"
    )


def fmt_product_cross_channel(shopify: dict, fc: dict) -> str:
    product = shopify["product"]
    label = PERIOD_LABELS.get(shopify["period"], shopify["period"])
    combined_rev = shopify["revenue"] + fc["revenue"]
    combined_qty = shopify["quantity"] + fc["quantity"]

    if combined_qty == 0 and combined_rev == 0:
        return f"No sales found for \"{product}\" {label} on either channel."

    return (
        f"\"{product}\" — {label}\n"
        f"\n"
        f"Online (Shopify):     €{shopify['revenue']:,.2f}  |  Units: {shopify['quantity']}\n"
        f"Retail (Flour Cloud): €{fc['revenue']:,.2f}  |  Units: {fc['quantity']}\n"
        f"\n"
        f"Combined: €{combined_rev:,.2f}  |  Units: {combined_qty}"
    )


def fmt_compare(shopify: dict, fc: dict) -> str:
    label = PERIOD_LABELS.get(shopify["period"], shopify["period"])
    total = shopify["revenue"] + fc["revenue"]
    return (
        f"Sales comparison — {label}\n"
        f"\n"
        f"Online (Shopify)\n"
        f"  Revenue: €{shopify['revenue']:,.2f}  |  Orders: {shopify['order_count']}\n"
        f"\n"
        f"Retail (Flour Cloud)\n"
        f"  Revenue: €{fc['revenue']:,.2f}  |  Transactions: {fc['transaction_count']}\n"
        f"\n"
        f"Combined total: €{total:,.2f}"
    )


def fmt_total(shopify: dict, fc: dict) -> str:
    label = PERIOD_LABELS.get(shopify["period"], shopify["period"])
    total = shopify["revenue"] + fc["revenue"]
    return (
        f"Total sales — {label}\n"
        f"Combined: €{total:,.2f}\n"
        f"  Online: €{shopify['revenue']:,.2f} ({shopify['order_count']} orders)\n"
        f"  Retail: €{fc['revenue']:,.2f} ({fc['transaction_count']} transactions)"
    )


HELP_TEXT = (
    "I can answer questions like:\n"
    "• What were my sales today?\n"
    "• Retail sales yesterday?\n"
    "• Compare online and retail last week\n"
    "• Total sales this month?\n"
    "• How much basmati rice did I sell this week?\n"
    "• Mishti sales yesterday online and retail?"
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

    try:
        parsed = parse_intent(text)
    except Exception as exc:
        logger.error("Intent parse error: %s", exc)
        await update.message.reply_text("Sorry, I had trouble understanding that.\n\n" + HELP_TEXT)
        return

    intent = parsed.get("intent", "unknown")
    period = parsed.get("period") or "today"
    channel = parsed.get("channel")  # online | retail | total | compare | None
    product = parsed.get("product")

    logger.info("intent=%s period=%s channel=%s product=%s", intent, period, channel, product)

    if intent == "unknown":
        await update.message.reply_text(HELP_TEXT)
        return

    # Determine which sources to fetch based on channel
    needs_shopify = channel in (None, "online", "total", "compare")
    needs_flour = channel in ("retail", "total", "compare")

    await update.message.reply_text("Checking...")

    try:
        if intent == "sales_by_product":
            if channel in ("total", "compare"):
                shopify_data = shopify_product_sales(period, product)
                fc_data = flour_cloud_product_sales(period, product)
                reply = fmt_product_cross_channel(shopify_data, fc_data)
            elif channel == "retail":
                fc_data = flour_cloud_product_sales(period, product)
                reply = fmt_product(fc_data, "Retail (Flour Cloud)")
            else:
                # Default: online only
                shopify_data = shopify_product_sales(period, product)
                reply = fmt_product(shopify_data)

        elif channel == "compare":
            shopify_data = shopify_sales(period)
            fc_data = flour_cloud_sales(period)
            reply = fmt_compare(shopify_data, fc_data)

        elif channel == "total":
            shopify_data = shopify_sales(period)
            fc_data = flour_cloud_sales(period)
            reply = fmt_total(shopify_data, fc_data)

        elif channel == "retail":
            fc_data = flour_cloud_sales(period)
            reply = fmt_period(fc_data, "Retail (Flour Cloud)")

        else:
            # Default: Shopify / online
            shopify_data = shopify_sales(period)
            reply = fmt_period(shopify_data, "Online (Shopify)")

    except Exception as exc:
        logger.error("Data fetch error: %s", exc)
        reply = f"Couldn't fetch data: {exc}"

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
