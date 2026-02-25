import asyncio
import json
import logging
import os
from datetime import datetime, time as dt_time, timedelta, timezone
from email.utils import parsedate_to_datetime

import anthropic
import pytz
import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

load_dotenv()

SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
FLOUR_CLOUD_TOKEN = os.getenv("FLOUR_CLOUD_TOKEN")
RESTAURANT_SHEET_ID = "1BiiLjs30NF_O6xZz_O-G4bfgcbQnGP6GuRW12vgJJKo"
SUPPLIER_SHEET_ID   = "1JMfhCB-af8DNnbYNe2Any2oakbNRJHOFvdFZXDMq1qg"

_raw = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in _raw.split(",") if uid.strip()]

_raw_chat_id = os.getenv("DAILY_REPORT_CHAT_ID", "")
DAILY_REPORT_CHAT_ID = int(_raw_chat_id.strip()) if _raw_chat_id.strip() else None

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Flour Cloud POS system uses Berlin local dates
BERLIN_TZ = pytz.timezone("Europe/Berlin")

# Gmail inboxes to search
GMAIL_INBOXES = [
    "invoices@spicevillage.eu",
    "svfproducts@spicevillage.eu",
    "info@spicevillage.eu",
    "sparikh@spicevillage.eu",
]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_account.json")


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
    all_docs = []
    skip = 0
    PAGE = 1000

    while True:
        params = {"limit": PAGE, "type": "R", "sort": "-date", "skip": skip}
        response = requests.get(
            "https://flour.host/v3/documents",
            headers=headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        page = data if isinstance(data, list) else (data.get("docs") or data.get("documents") or data.get("data") or [])

        if not page:
            break

        all_docs.extend(page)

        # Stop early if the oldest doc on this page is before our start date
        oldest_date_str = str(page[-1].get("date", ""))[:10]
        try:
            if datetime.fromisoformat(oldest_date_str).date() < start_date:
                break
        except ValueError:
            pass

        if len(page) < PAGE:
            break

        skip += PAGE

    logger.info("Flour Cloud: fetched %d raw docs (paginated), filtering %s → %s (Berlin)", len(all_docs), start_date, end_date)

    filtered = []
    for doc in all_docs:
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
# Restaurant sales (Google Sheets)
# ---------------------------------------------------------------------------

def _fetch_restaurant_tab(tab_name: str, find_date_str: str = None) -> dict:
    """
    Fetch restaurant sales from one monthly tab.
    Returns {"mtd": float, "daily": float|None}
    find_date_str: date in DD/MM/YYYY format to look up the daily column.
    """
    info = _load_service_account_info()
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    svc = google_build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Locate the column for the requested date
    date_col = None
    if find_date_str:
        row3 = svc.spreadsheets().values().get(
            spreadsheetId=RESTAURANT_SHEET_ID,
            range=f"'{tab_name}'!A3:AZ3",
        ).execute().get("values", [[]])[0]
        for i, val in enumerate(row3):
            if val == find_date_str:
                date_col = i
                break
        if date_col is None:
            logger.warning("Restaurant sheet: date %s not found in row 3 of %s", find_date_str, tab_name)

    # Fetch all rows in the data area (wide enough to cover daily columns)
    rows = svc.spreadsheets().values().get(
        spreadsheetId=RESTAURANT_SHEET_ID,
        range=f"'{tab_name}'!A200:AZ350",
    ).execute().get("values", [])

    def _parse(val):
        try:
            return float(str(val).replace("€", "").replace(",", "").strip())
        except ValueError:
            return None

    for row in rows:
        if len(row) > 3 and "restaurant sales" in str(row[3]).lower():
            mtd   = _parse(row[4]) if len(row) > 4 else None
            daily = _parse(row[date_col]) if (date_col is not None and date_col < len(row)) else None
            return {"mtd": mtd or 0.0, "daily": daily}

    return {"mtd": 0.0, "daily": None}


def restaurant_sales_all() -> dict:
    """Return {"yesterday": float, "mtd": float} fetching from the sheet."""
    now_berlin = datetime.now(BERLIN_TZ)
    yesterday  = now_berlin - timedelta(days=1)
    yday_tab   = yesterday.strftime("%B %Y")
    mtd_tab    = now_berlin.strftime("%B %Y")
    yday_str   = yesterday.strftime("%d/%m/%Y")

    if yday_tab == mtd_tab:
        result = _fetch_restaurant_tab(yday_tab, yday_str)
        logger.info("Restaurant sales — yesterday: €%.2f  MTD: €%.2f", result["daily"] or 0, result["mtd"])
        return {"yesterday": result["daily"] or 0.0, "mtd": result["mtd"]}
    else:
        # Month boundary: yesterday was in a different month
        yday_result = _fetch_restaurant_tab(yday_tab, yday_str)
        mtd_result  = _fetch_restaurant_tab(mtd_tab)
        logger.info("Restaurant sales (cross-month) — yesterday: €%.2f  MTD: €%.2f",
                    yday_result["daily"] or 0, mtd_result["mtd"])
        return {"yesterday": yday_result["daily"] or 0.0, "mtd": mtd_result["mtd"]}


# ---------------------------------------------------------------------------
# Supplier outstanding (Google Sheets)
# ---------------------------------------------------------------------------

def _supplier_sheets_svc():
    info = _load_service_account_info()
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return google_build("sheets", "v4", credentials=creds, cache_discovery=False)


def _find_supplier_tab(svc, query: str):
    """Fuzzy-match a supplier name to the closest tab name."""
    meta = svc.spreadsheets().get(spreadsheetId=SUPPLIER_SHEET_ID).execute()
    tab_names = [s["properties"]["title"] for s in meta["sheets"]]
    q = query.lower().strip()
    # Exact match first, then prefix, then substring
    for tab in tab_names:
        if tab.lower() == q:
            return tab
    for tab in tab_names:
        if tab.lower().startswith(q) or q.startswith(tab.lower()):
            return tab
    for tab in tab_names:
        if q in tab.lower() or tab.lower() in q:
            return tab
    return None


def _parse_eur(val: str) -> float:
    try:
        return float(str(val).replace("€", "").replace(",", "").replace(" ", "").strip())
    except ValueError:
        return 0.0


def fetch_supplier_outstanding(supplier_name: str) -> dict:
    svc = _supplier_sheets_svc()
    tab = _find_supplier_tab(svc, supplier_name)
    if not tab:
        return {"error": f"No supplier tab found matching '{supplier_name}'"}

    rows = svc.spreadsheets().values().get(
        spreadsheetId=SUPPLIER_SHEET_ID,
        range=f"'{tab}'!A1:O60",
    ).execute().get("values", [])

    # Row 2 (idx 1): summary totals at col G (idx 6) and col J (idx 9)
    summary_row = rows[1] if len(rows) > 1 else []
    total_due     = _parse_eur(summary_row[6])  if len(summary_row) > 6  else 0.0
    total_balance = _parse_eur(summary_row[9])  if len(summary_row) > 9  else 0.0

    # Find header row (contains "Invoice Date") then parse data rows below it
    header_idx = next((i for i, r in enumerate(rows) if any("Invoice Date" in str(c) for c in r)), 7)

    invoices = []
    for row in rows[header_idx + 1:]:
        if len(row) < 15:
            continue
        balance = _parse_eur(row[14])
        if balance == 0.0:
            continue
        invoices.append({
            "date":    row[1] if len(row) > 1 else "",
            "invoice": row[2] if len(row) > 2 else "",
            "amount":  _parse_eur(row[4]) if len(row) > 4 else 0.0,
            "due":     row[5] if len(row) > 5 else "",
            "balance": balance,
        })

    logger.info("Supplier '%s': balance=€%.2f  due=€%.2f  invoices=%d", tab, total_balance, total_due, len(invoices))
    return {
        "supplier":      tab,
        "total_balance": total_balance,
        "total_due":     total_due,
        "invoices":      invoices,
    }


def fmt_supplier_outstanding(data: dict) -> str:
    if "error" in data:
        return data["error"]

    lines = [
        f"{data['supplier']} — Outstanding",
        f"",
        f"Total balance:  €{data['total_balance']:,.2f}",
        f"Overdue:        €{data['total_due']:,.2f}",
        f"",
    ]

    outstanding = [inv for inv in data["invoices"] if inv["balance"] > 0]
    credits     = [inv for inv in data["invoices"] if inv["balance"] < 0]

    if outstanding:
        lines.append("Unpaid invoices:")
        for inv in outstanding:
            lines.append(
                f"  {inv['invoice']:<14}  {inv['date']}  "
                f"Due {inv['due']}  Balance: €{inv['balance']:,.2f}"
            )

    if credits:
        lines.append("")
        lines.append("Unapplied credit notes:")
        for inv in credits:
            lines.append(
                f"  {inv['invoice']:<14}  {inv['date']}  €{inv['balance']:,.2f}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def _load_service_account_info() -> dict:
    """Load service account creds from file (local) or env var (Railway)."""
    if os.path.exists(_SERVICE_ACCOUNT_FILE):
        with open(_SERVICE_ACCOUNT_FILE) as f:
            return json.load(f)
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if raw:
        return json.loads(raw)
    raise RuntimeError("No service account credentials found — set GOOGLE_SERVICE_ACCOUNT_JSON env var on Railway")


def _gmail_service(email: str):
    info = _load_service_account_info()
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=GMAIL_SCOPES
    ).with_subject(email)
    return google_build("gmail", "v1", credentials=creds, cache_discovery=False)


def _fmt_email_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).strftime("%d %b %Y %H:%M")
    except Exception:
        return raw


def search_inbox(email: str, query: str, max_results: int = 3) -> list:
    svc = _gmail_service(email)
    res = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages = res.get("messages", [])
    results = []
    for msg in messages:
        detail = svc.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        hdrs = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        results.append({
            "subject": hdrs.get("Subject", "(no subject)"),
            "from":    hdrs.get("From", ""),
            "date":    _fmt_email_date(hdrs.get("Date", "")),
            "link":    f"https://mail.google.com/mail/u/0/#all/{msg['id']}",
        })
    return results


def gmail_search_all(query: str) -> dict:
    """Search all inboxes. Returns {email: [message, ...]}."""
    results = {}
    for email in GMAIL_INBOXES:
        try:
            results[email] = search_inbox(email, query)
        except Exception as exc:
            logger.error("Gmail error for %s: %s", email, exc)
            results[email] = []
    return results


def fmt_gmail_results(results: dict, query: str) -> str:
    any_found = any(msgs for msgs in results.values())
    if not any_found:
        return f"No emails found matching \"{query}\" across all inboxes."

    lines = [f"Gmail search: \"{query}\"\n"]
    for email, messages in results.items():
        if not messages:
            continue
        lines.append(email)
        for msg in messages:
            lines.append(f"  {msg['date']}  {msg['from']}")
            lines.append(f"  {msg['subject']}")
            lines.append(f"  {msg['link']}")
            lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Claude intent parsing
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You parse messages for SVF Products GmbH, trading as Spice Village (South Asian grocery, Berlin, Germany).
It has two sales channels: Shopify (online orders) and Flour Cloud (retail/in-store POS).
It has 4 Gmail inboxes: invoices@spicevillage.eu, svfproducts@spicevillage.eu, info@spicevillage.eu, sparikh@spicevillage.eu.
It tracks supplier invoices and outstanding payments in a Google Sheet (suppliers include: Transfood, Smart Elite, Shalamar, Swagat, AR Food, IPS, Om Food, Das Vegarma, GFT, Bonesca, Asia Express, Deilght Food, Crown, Kumar Ayurveda, Sona Food, Umer, Aayush, Taya, Aheco, Bakery, Desi Megamart).

Company details (use when asked):
- Legal name: SVF Products GmbH
- Address: Tempelhofer Damm 206, 12099 Berlin
- Website: www.spicevillage.eu
- Email: svfproducts@spicevillage.eu | Invoices: invoices@spicevillage.eu
- Phone: +49 30 8965 7586
- Tax Number: 29/553/32289 | VAT: DE363532317
- Handelsregister: Charlottenburg HRB 256768 B | EORI: DE260532672959166
- Managing Directors: Nikunj Patel, Alpa Parikh
- IBAN: DE38100101237197421588 | BIC: QNTODEB2XXX
- PayPal: svfproducts@spicevillage.eu

Return ONLY valid JSON — no explanation, no markdown fences.

Schema:
{
  "intent": "sales_by_period" | "sales_by_product" | "gmail_search" | "company_info" | "supplier_outstanding" | "unknown",
  "period": "today" | "yesterday" | "last_7_days" | "this_week" | "last_week" | "this_month" | "last_month" | null,
  "channel": "online" | "retail" | "total" | "compare" | null,
  "product": "<product name>" | null,
  "search_query": "<gmail search terms or supplier name>" | null
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

Gmail rules:
- intent = gmail_search when: "find invoice", "find email", "search email", "any email", "invoice from", "email about", "did we get an email"
- search_query = the supplier name, topic, or keyword to search for (clean Gmail search string)
- period/channel/product = null for gmail_search

Supplier outstanding rules:
- intent = supplier_outstanding when asked about: outstanding balance, what we owe, unpaid invoices, payment due, how much do we owe [supplier]
- search_query = the supplier name exactly as mentioned (e.g. "Transfood", "Smart Elite")
- period/channel/product = null for supplier_outstanding

Company info rules:
- intent = company_info when asked for: address, IBAN, VAT, tax number, EORI, bank details, phone, managing directors, Handelsregister, PayPal, website, company name, legal details
- For company_info, return the relevant detail(s) in search_query field as a short label e.g. "IBAN", "address", "VAT", "all"

Other rules:
- If a product name is mentioned (not an email search), intent = sales_by_product
- If none of the above match, intent = unknown
"""


def parse_intent(message: str) -> dict:
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
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
    "• Mishti sales yesterday online and retail?\n"
    "• Find invoice from TRS\n"
    "• Any email about the Ashoka delivery?"
)


# ---------------------------------------------------------------------------
# Daily scheduled report
# ---------------------------------------------------------------------------

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not DAILY_REPORT_CHAT_ID:
        logger.warning("DAILY_REPORT_CHAT_ID not set — skipping daily report")
        return

    now_berlin  = datetime.now(BERLIN_TZ)
    yday_label  = (now_berlin - timedelta(days=1)).strftime("%d %b %Y")
    month_label = now_berlin.strftime("%B %Y")

    async def _safe_fetch(fn, *args, retries=3, delay=300):
        for attempt in range(1, retries + 1):
            try:
                return fn(*args)
            except Exception as exc:
                logger.warning("Daily report fetch attempt %d/%d failed (%s %s): %s", attempt, retries, fn.__name__, args, exc)
                if attempt < retries:
                    logger.info("Retrying in %ds...", delay)
                    await asyncio.sleep(delay)
        logger.error("Daily report fetch gave up after %d attempts (%s %s)", retries, fn.__name__, args)
        return None

    retail_yday    = await _safe_fetch(flour_cloud_sales, "yesterday")
    retail_mtd     = await _safe_fetch(flour_cloud_sales, "this_month")
    online_yday    = await _safe_fetch(shopify_sales, "yesterday")
    online_mtd     = await _safe_fetch(shopify_sales, "this_month")
    restaurant = await _safe_fetch(restaurant_sales_all)

    def _fmt(data, key="revenue"):
        return f"€{data[key]:>10,.2f}" if data is not None else "      unavailable"

    def _fmt_r(key):
        return f"€{restaurant[key]:>10,.2f}" if restaurant is not None else "      unavailable"

    msg = (
        f"Good morning! Daily Sales Briefing\n"
        f"\n"
        f"Yesterday ({yday_label})\n"
        f"  Retail      {_fmt(retail_yday)}\n"
        f"  Online      {_fmt(online_yday)}\n"
        f"  Restaurant  {_fmt_r('yesterday')}\n"
        f"\n"
        f"Month to Date ({month_label})\n"
        f"  Retail      {_fmt(retail_mtd)}\n"
        f"  Online      {_fmt(online_mtd)}\n"
        f"  Restaurant  {_fmt_r('mtd')}"
    )

    await context.bot.send_message(chat_id=DAILY_REPORT_CHAT_ID, text=msg)


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
    channel = parsed.get("channel")
    product = parsed.get("product")
    search_query = parsed.get("search_query")

    logger.info("intent=%s period=%s channel=%s product=%s search_query=%s", intent, period, channel, product, search_query)

    if intent == "unknown":
        await update.message.reply_text(HELP_TEXT)
        return

    await update.message.reply_text("Checking...")

    try:
        if intent == "company_info":
            reply = (
                "SVF Products GmbH\n"
                "Tempelhofer Damm 206, 12099 Berlin\n"
                "www.spicevillage.eu\n"
                "\n"
                "Email: svfproducts@spicevillage.eu\n"
                "Invoices: invoices@spicevillage.eu\n"
                "Phone: +49 30 8965 7586\n"
                "PayPal: svfproducts@spicevillage.eu\n"
                "\n"
                "Tax Nr: 29/553/32289\n"
                "VAT: DE363532317\n"
                "Handelsregister: Charlottenburg HRB 256768 B\n"
                "EORI: DE260532672959166\n"
                "\n"
                "Managing Directors: Nikunj Patel, Alpa Parikh\n"
                "\n"
                "IBAN: DE38100101237197421588\n"
                "BIC: QNTODEB2XXX"
            )

        elif intent == "supplier_outstanding":
            if not search_query:
                reply = "Which supplier? e.g. \"What do we owe Transfood?\""
            else:
                data = fetch_supplier_outstanding(search_query)
                reply = fmt_supplier_outstanding(data)

        elif intent == "gmail_search":
            if not search_query:
                reply = "What should I search for? Try: \"find invoice from TRS\" or \"email about delivery\"."
            else:
                results = gmail_search_all(search_query)
                reply = fmt_gmail_results(results, search_query)

        elif intent == "sales_by_product":
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

    if DAILY_REPORT_CHAT_ID:
        app.job_queue.run_daily(
            send_daily_report,
            time=dt_time(4, 0, 0, tzinfo=BERLIN_TZ),
        )
        logger.info("Daily report scheduled at 04:00 CET/CEST → chat %s", DAILY_REPORT_CHAT_ID)
    else:
        logger.warning("DAILY_REPORT_CHAT_ID not set — daily report disabled")

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
