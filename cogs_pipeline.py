"""
SVF Daily COGS Pipeline
- Pulls yesterday's Shopify orders (Berlin local date)
- Fetches cost via inventory_item_id — locked as cost_at_sale in SQLite
- Appends one row per product to Google Sheet "COGS Daily" tab
- Returns a Telegram summary message string

Called by bot.py at 08:00 Berlin time via job_queue.run_daily.
SQLite DB path: env var COGS_DB_PATH, default ./cogs.db
Requires a Railway persistent volume mounted at that path for durability.
"""

import os
import sqlite3
import time
from datetime import datetime, timedelta

import pytz
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build

# ── Constants ──────────────────────────────────────────────────────────────

SHOPIFY_STORE  = os.getenv("SHOPIFY_STORE", "spice-village-eu.myshopify.com")
SHOPIFY_TOKEN  = os.getenv("SHOPIFY_ACCESS_TOKEN")
COGS_SHEET_ID  = "1vmL9PXQMgwxEioHAIydtOvRbPwBaF4gQbUsIbfG2Y_A"
COGS_TAB       = "COGS Daily"
COGS_DB_PATH   = os.getenv("COGS_DB_PATH", "cogs.db")
BERLIN_TZ      = pytz.timezone("Europe/Berlin")
API_VERSION    = "2024-10"
SH             = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}

_SERVICE_ACCOUNT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "service_account.json"
)
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ── SQLite ─────────────────────────────────────────────────────────────────

def _get_db():
    conn = sqlite3.connect(COGS_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_line_items (
            order_id       TEXT NOT NULL,
            line_item_id   TEXT NOT NULL,
            order_date     TEXT NOT NULL,
            sku            TEXT,
            title          TEXT,
            variant_id     INTEGER,
            gross_qty      INTEGER DEFAULT 0,
            gross_revenue  REAL    DEFAULT 0,
            refund_qty     INTEGER DEFAULT 0,
            refund_revenue REAL    DEFAULT 0,
            cost_at_sale   REAL,
            PRIMARY KEY (order_id, line_item_id)
        )
    """)
    conn.commit()
    return conn


def upsert_line_items(rows: list):
    """Insert rows into SQLite. Skips duplicates (idempotent on re-run)."""
    conn = _get_db()
    conn.executemany("""
        INSERT OR IGNORE INTO order_line_items
            (order_id, line_item_id, order_date, sku, title, variant_id,
             gross_qty, gross_revenue, refund_qty, refund_revenue, cost_at_sale)
        VALUES
            (:order_id, :line_item_id, :order_date, :sku, :title, :variant_id,
             :gross_qty, :gross_revenue, :refund_qty, :refund_revenue, :cost_at_sale)
    """, rows)
    conn.commit()
    conn.close()


def query_summary(date_from: str, date_to: str) -> dict:
    """Return {revenue, cogs, gross_profit, cogs_pct} for a date range (inclusive)."""
    conn = _get_db()
    row = conn.execute("""
        SELECT
            SUM(gross_revenue - refund_revenue)                      AS revenue,
            SUM(gross_qty * COALESCE(cost_at_sale, 0))               AS cogs,
            SUM(gross_revenue - refund_revenue
                - gross_qty * COALESCE(cost_at_sale, 0))             AS gross_profit
        FROM order_line_items
        WHERE order_date >= ? AND order_date <= ?
          AND cost_at_sale IS NOT NULL
    """, (date_from, date_to)).fetchone()
    conn.close()
    rev  = row["revenue"]  or 0
    cogs = row["cogs"]     or 0
    gp   = row["gross_profit"] or 0
    return {
        "revenue":      rev,
        "cogs":         cogs,
        "gross_profit": gp,
        "cogs_pct":     cogs / rev * 100 if rev else 0,
    }


def query_problem_products(date_str: str, min_rev: float = 50, max_cogs_pct: float = 60) -> list:
    """Return products on date_str with net revenue > min_rev and COGS% > max_cogs_pct."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT
            title,
            SUM(gross_revenue - refund_revenue)                AS net_revenue,
            SUM(gross_qty * cost_at_sale)                      AS cogs_eur,
            SUM(gross_qty * cost_at_sale)
                / NULLIF(SUM(gross_revenue - refund_revenue), 0) * 100  AS cogs_pct
        FROM order_line_items
        WHERE order_date = ?
          AND cost_at_sale IS NOT NULL
        GROUP BY title
        HAVING net_revenue > ? AND cogs_pct > ?
        ORDER BY cogs_pct DESC
    """, (date_str, min_rev, max_cogs_pct)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_daily_product_rows(date_str: str) -> list:
    """Return per-product rows for a given date — for Google Sheet append."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT
            sku,
            title,
            SUM(gross_qty - refund_qty)                        AS net_qty,
            SUM(gross_revenue - refund_revenue)                AS net_revenue,
            SUM(gross_qty * COALESCE(cost_at_sale, 0))         AS cogs_eur,
            SUM(gross_qty * COALESCE(cost_at_sale, 0))
                / NULLIF(SUM(gross_revenue - refund_revenue), 0) * 100  AS cogs_pct,
            SUM(gross_revenue - refund_revenue
                - gross_qty * COALESCE(cost_at_sale, 0))       AS gross_profit
        FROM order_line_items
        WHERE order_date = ?
          AND (gross_qty - refund_qty) >= 1
        GROUP BY sku, title
        ORDER BY net_revenue DESC
    """, (date_str,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Shopify helpers ────────────────────────────────────────────────────────

def _paginate(url: str, key: str) -> list:
    """Cursor-based pagination. 1s delay between pages. 429 retry."""
    results = []
    while url:
        while True:
            r = requests.get(url, headers=SH, timeout=30)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        results.extend(r.json().get(key, []))
        link = r.headers.get("Link", "")
        url  = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split("<")[1].split(">")[0]
        time.sleep(1)
    return results


def _fetch_inventory_costs(inv_ids: list) -> dict:
    """Fetch cost per inventory_item_id. 1s delay between batches. 429 retry."""
    costs  = {}
    unique = list(set(inv_ids))
    for i in range(0, len(unique), 100):
        batch = unique[i:i + 100]
        while True:
            r = requests.get(
                f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}/inventory_items.json"
                f"?ids={','.join(str(x) for x in batch)}&limit=100",
                headers=SH, timeout=30
            )
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        for item in r.json().get("inventory_items", []):
            if item.get("cost"):
                costs[item["id"]] = float(item["cost"])
        time.sleep(1)
    return costs


def _build_variant_map() -> dict:
    """Scan all products (active + archived + draft), return {variant_id: inventory_item_id}."""
    vm = {}
    for status in ("active", "archived", "draft"):
        url = (f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}"
               f"/products.json?limit=250&status={status}&fields=id,variants")
        for p in _paginate(url, "products"):
            for v in p["variants"]:
                vm[v["id"]] = v["inventory_item_id"]
    return vm


# ── Core pipeline ──────────────────────────────────────────────────────────

def run_daily_pipeline(date_str: str = None) -> str:
    """
    Run the full COGS pipeline for `date_str` (YYYY-MM-DD Berlin local).
    Defaults to yesterday Berlin time.
    Returns the Telegram message string.
    """
    now_berlin = datetime.now(BERLIN_TZ)
    if date_str is None:
        date_str = (now_berlin - timedelta(days=1)).strftime("%Y-%m-%d")

    # Berlin midnight boundaries in UTC for Shopify query
    berlin_start = BERLIN_TZ.localize(
        datetime.strptime(date_str, "%Y-%m-%d")
    )
    berlin_end = berlin_start + timedelta(days=1) - timedelta(seconds=1)
    utc_start  = berlin_start.astimezone(pytz.utc).isoformat()
    utc_end    = berlin_end.astimezone(pytz.utc).isoformat()

    # ── 1. Pull orders ─────────────────────────────────────────────────────
    url = (f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}/orders.json"
           f"?limit=250&status=any"
           f"&created_at_min={utc_start}&created_at_max={utc_end}"
           f"&fields=id,name,created_at,cancelled_at,line_items,refunds")
    all_orders = [o for o in _paginate(url, "orders") if not o.get("cancelled_at")]

    # ── 2. Build variant map + fetch costs ─────────────────────────────────
    variant_map = _build_variant_map()
    all_inv_ids = list(variant_map.values())
    inv_costs   = _fetch_inventory_costs(all_inv_ids)

    # ── 3. Aggregate line items ────────────────────────────────────────────
    # keyed by (order_id, line_item_id)
    agg = {}
    for order in all_orders:
        oid = str(order["id"])
        for li in order["line_items"]:
            lid  = str(li["id"])
            vid  = li.get("variant_id") or 0
            key  = (oid, lid)
            inv_id   = variant_map.get(vid)
            cost     = inv_costs.get(inv_id) if inv_id else None
            agg[key] = {
                "order_id":      oid,
                "line_item_id":  lid,
                "order_date":    date_str,
                "sku":           li.get("sku") or "",
                "title":         li.get("title", ""),
                "variant_id":    vid,
                "gross_qty":     li.get("quantity", 0),
                "gross_revenue": li.get("quantity", 0) * float(li.get("price", 0)),
                "refund_qty":    0,
                "refund_revenue": 0.0,
                "cost_at_sale":  cost,
            }
        # Refunds: subtract revenue, keep COGS on original qty
        for refund in order.get("refunds", []):
            for ri in refund.get("refund_line_items", []):
                lid = str(ri["line_item_id"])
                key = (oid, lid)
                if key in agg:
                    agg[key]["refund_qty"]     += ri.get("quantity", 0)
                    agg[key]["refund_revenue"] += float(ri.get("subtotal", 0))

    rows = list(agg.values())

    # ── 4. Upsert to SQLite ────────────────────────────────────────────────
    upsert_line_items(rows)

    # ── 5. Append to Google Sheet ──────────────────────────────────────────
    _append_to_sheet(date_str)

    # ── 6. Build Telegram message ──────────────────────────────────────────
    return _build_telegram_message(date_str, now_berlin)


# ── Google Sheets ──────────────────────────────────────────────────────────

def _get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        _SERVICE_ACCOUNT_FILE, scopes=SHEETS_SCOPES
    )
    return google_build("sheets", "v4", credentials=creds)


def _ensure_tab(svc):
    """Create COGS Daily tab with header if it doesn't exist."""
    meta = svc.spreadsheets().get(spreadsheetId=COGS_SHEET_ID).execute()
    names = [s["properties"]["title"] for s in meta["sheets"]]
    if COGS_TAB not in names:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=COGS_SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": COGS_TAB}}}]}
        ).execute()
        # Write header
        svc.spreadsheets().values().update(
            spreadsheetId=COGS_SHEET_ID,
            range=f"{COGS_TAB}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [["Date", "SKU", "Product name", "Units sold",
                              "Net revenue €", "Net COGS €", "COGS%", "Gross profit €"]]}
        ).execute()


def _date_already_written(svc, date_str: str) -> bool:
    """Check if rows for this date already exist in the sheet."""
    result = svc.spreadsheets().values().get(
        spreadsheetId=COGS_SHEET_ID,
        range=f"{COGS_TAB}!A:A"
    ).execute()
    values = result.get("values", [])
    return any(row and row[0] == date_str for row in values)


def _append_to_sheet(date_str: str):
    svc = _get_sheets_service()
    _ensure_tab(svc)

    if _date_already_written(svc, date_str):
        return  # Idempotent: never write the same date twice

    product_rows = query_daily_product_rows(date_str)
    if not product_rows:
        return

    sheet_rows = []
    for r in product_rows:
        net_rev = r["net_revenue"] or 0
        cogs_e  = r["cogs_eur"]   or 0
        gp      = r["gross_profit"] or 0
        cogs_p  = cogs_e / net_rev * 100 if net_rev else 0
        sheet_rows.append([
            date_str,
            r["sku"]   or "",
            r["title"] or "",
            r["net_qty"],
            round(net_rev, 2),
            round(cogs_e,  2),
            round(cogs_p,  2),
            round(gp,      2),
        ])

    svc.spreadsheets().values().append(
        spreadsheetId=COGS_SHEET_ID,
        range=f"{COGS_TAB}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": sheet_rows}
    ).execute()


# ── Telegram message ───────────────────────────────────────────────────────

def _build_telegram_message(date_str: str, now_berlin: datetime) -> str:
    display_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y")
    mtd_start    = now_berlin.replace(day=1).strftime("%Y-%m-%d")
    week_start   = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")

    yday = query_summary(date_str,    date_str)
    week = query_summary(week_start,  date_str)
    mtd  = query_summary(mtd_start,   date_str)
    probs = query_problem_products(date_str, min_rev=50, max_cogs_pct=60)

    def _fmt(d: dict) -> str:
        return (f"Revenue: €{d['revenue']:,.0f} | "
                f"COGS: {d['cogs_pct']:.1f}% | "
                f"Gross Profit: €{d['gross_profit']:,.0f}")

    lines = [
        f"📊 COGS Report — {display_date}",
        "",
        f"📅 Yesterday",
        f"{_fmt(yday)}",
        "",
        f"📆 Last 7 Days (rolling)",
        f"{_fmt(week)}",
        "",
        f"🗓 Month to Date",
        f"{_fmt(mtd)}",
    ]

    if probs:
        lines += ["", "⚠️ Problem Products (revenue >€50, COGS >60%)"]
        for p in probs[:8]:
            lines.append(
                f"{p['title'][:35]} | €{p['net_revenue']:.0f} rev | {p['cogs_pct']:.1f}% COGS"
            )

    lines += [
        "",
        f"🔍 Full detail: https://docs.google.com/spreadsheets/d/{COGS_SHEET_ID}",
    ]

    return "\n".join(lines)
