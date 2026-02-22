import os
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

load_dotenv()

SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Optional whitelist — add your Telegram user ID to .env as ALLOWED_USER_IDS=123456,789012
_raw = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in _raw.split(",") if uid.strip()]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def get_shopify_sales_today() -> tuple[float, int]:
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-10/orders.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
    params = {
        "status": "any",
        "financial_status": "paid",
        "created_at_min": today_start.isoformat(),
        "limit": 250,
    }

    response = requests.get(url, headers=headers, params=params, timeout=10)
    response.raise_for_status()

    orders = response.json().get("orders", [])
    total_revenue = sum(float(order["total_price"]) for order in orders)
    return total_revenue, len(orders)


def _is_asking_about_sales(text: str) -> bool:
    text = text.lower()
    triggers = [
        "sales today",
        "revenue today",
        "today's sales",
        "today's revenue",
        "today sales",
        "today revenue",
        "how much today",
        "sales so far",
        "revenue so far",
        "shopify today",
    ]
    return any(t in text for t in triggers)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logger.info("Ignoring message from unauthorised user %s", user_id)
        return

    text = update.message.text or ""

    if not _is_asking_about_sales(text):
        return

    await update.message.reply_text("Checking Shopify...")

    try:
        revenue, count = get_shopify_sales_today()
        reply = (
            f"Today's Shopify sales (UTC midnight → now):\n"
            f"Revenue: €{revenue:,.2f}\n"
            f"Orders:  {count}"
        )
    except Exception as exc:
        logger.error("Shopify error: %s", exc)
        reply = f"Couldn't fetch Shopify data: {exc}"

    await update.message.reply_text(reply)


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
