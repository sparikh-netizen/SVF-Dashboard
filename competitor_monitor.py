"""
SVF Competitor Price Monitor
- Loads top 40 products from price_monitor_products.csv
- Fetches our live prices from Shopify
- Searches jamoona.com and eu.dookan.com via Shopify suggest.json API
- Matches by product type + weight; prefers same-brand match when available
- Sends HTML email to sparikh@spicevillage.eu + info@spicevillage.eu
- Runs every alternate day at 07:00 Berlin time (called by bot.py)
- On error: retries 3x, then emails full error details to sparikh@spicevillage.eu
"""

import os
import csv
import time
import base64
import re
import traceback
from collections import defaultdict
from difflib import SequenceMatcher
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build

# ── Constants ──────────────────────────────────────────────────────────────

SHOPIFY_STORE  = os.getenv("SHOPIFY_STORE", "spice-village-eu.myshopify.com")
SHOPIFY_TOKEN  = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION    = "2024-10"
SH             = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}

RECIPIENTS   = ["sparikh@spicevillage.eu", "info@spicevillage.eu"]
SENDER_EMAIL = "sparikh@spicevillage.eu"
BERLIN_TZ    = pytz.timezone("Europe/Berlin")

COMPETITORS = {
    "Jamoona": "https://www.jamoona.com",
    "Dookan":  "https://eu.dookan.com",
}

MAX_RETRIES = 3

_SERVICE_ACCOUNT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "service_account.json"
)
PRODUCTS_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "price_monitor_products.csv"
)


# ── Schedule gate: every alternate day ────────────────────────────────────

def should_run_today() -> bool:
    """Run on odd calendar days (1, 3, 5 … 31). Skips even days."""
    return datetime.now(BERLIN_TZ).day % 2 == 1


# ── Load product list ──────────────────────────────────────────────────────

def load_products() -> list:
    products = []
    with open(PRODUCTS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            products.append(row)
    return products


# ── Shopify: fetch our live prices ─────────────────────────────────────────

def fetch_our_prices(skus: list) -> dict:
    """Returns {sku: {"price": float, "title": str}} for all our SKUs."""
    prices = {}
    url = (f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}"
           f"/variants.json?limit=250&fields=sku,price,title")
    while url:
        while True:
            r = requests.get(url, headers=SH, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)))
                continue
            r.raise_for_status()
            break
        for v in r.json().get("variants", []):
            if v.get("sku") in skus:
                prices[v["sku"]] = {
                    "price": float(v.get("price", 0)),
                    "title": v.get("title", ""),
                }
        link = r.headers.get("Link", "")
        url = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split("<")[1].split(">")[0]
        time.sleep(0.5)
    return prices


# ── Competitor search ──────────────────────────────────────────────────────

def _parse_weight_g(weight_str: str) -> int:
    """Convert weight string to grams. '5kg'→5000, '500gm'→500, '1l'→1000."""
    m = re.match(r"([\d.]+)\s*(kg|gm|g|l|ltr|ml|pack)", weight_str.lower().strip())
    if not m:
        return 0
    val, unit = float(m.group(1)), m.group(2)
    return int({"kg": val*1000, "l": val*1000, "ltr": val*1000,
                "gm": val, "g": val, "ml": val, "pack": val*100}.get(unit, val))


def _weight_in_title(title: str, target_g: int) -> bool:
    """Check if title contains a weight within ±20% of target_g."""
    for m in re.finditer(r"([\d.]+)\s*(kg|gm|g|l|ltr|ml)", title, re.IGNORECASE):
        w = _parse_weight_g(m.group(0))
        if w and abs(w - target_g) / max(target_g, 1) <= 0.20:
            return True
    return False


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _extract_brand(our_title: str) -> str:
    """Extract the likely brand (first meaningful word) from our product title."""
    # Strip weight and common suffixes
    cleaned = re.sub(r'\d+\s*(kg|gm|g|l|ml|ltr)', '', our_title, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(export|pack|fresh|frozen|pure|desi|indian|organic)\b', '',
                     cleaned, flags=re.IGNORECASE)
    words = cleaned.strip().split()
    return words[0] if words else ""


def _search_suggest(base_url: str, query: str, weight_g: int) -> list:
    """Hit Shopify suggest.json and return weight-filtered, scored results."""
    url = (f"{base_url}/search/suggest.json"
           f"?q={requests.utils.quote(query)}"
           f"&resources[type]=product&resources[limit]=10")
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 429:
            time.sleep(3)
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception:
        return []

    results = []
    for p in (r.json().get("resources", {}).get("results", {}).get("products", [])):
        title = p.get("title", "")
        if weight_g and not _weight_in_title(title, weight_g):
            continue
        try:
            price = float(str(p.get("price", "0")).replace(",", ".").replace("€", "").strip())
        except ValueError:
            continue
        if price <= 0:
            continue
        results.append({
            "title": title,
            "price": price,
            "brand": p.get("vendor", ""),
            "url":   base_url + p.get("url", ""),
            "score": _similarity(query, title),
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def search_competitor(base_url: str, our_title: str, search_term: str,
                      weight_g: int):
    """
    Search a competitor store. Strategy:
    1. Try brand-specific search first (e.g. "Anjappar sona masoori rice")
       If the brand exists on their site with matching weight → use it (exact match).
    2. Fall back to generic search_term (e.g. "sona masoori rice")
       → use best weight-matching result regardless of brand.
    Returns single best match dict or None.
    """
    brand = _extract_brand(our_title)

    # Step 1: brand-specific search — only accept if competitor actually carries this brand
    if brand:
        brand_results = _search_suggest(base_url, f"{brand} {search_term}", weight_g)
        for r in brand_results:
            if brand.lower() in r["title"].lower() or brand.lower() in r["brand"].lower():
                return r  # genuine brand match

    # Step 2: generic fallback
    time.sleep(0.3)
    generic_results = _search_suggest(base_url, search_term, weight_g)
    return generic_results[0] if generic_results else None


# ── Core runner ────────────────────────────────────────────────────────────

def _run_once() -> tuple[str, list]:
    """Execute one full price monitor run. Returns (status_msg, rows)."""
    products   = load_products()
    skus       = [p["sku"] for p in products]
    our_prices = fetch_our_prices(skus)

    rows = []
    for p in products:
        sku        = p["sku"]
        our_title  = p["our_title"]
        search_term = p["search_term"]
        weight_g   = int(p.get("weight_g", 0))

        our_price_info = our_prices.get(sku)
        our_price      = our_price_info["price"] if our_price_info else None

        comp_results = {}
        for name, base_url in COMPETITORS.items():
            comp_results[name] = search_competitor(
                base_url, our_title, search_term, weight_g
            )
            time.sleep(0.5)

        rows.append({
            "sku":         sku,
            "our_title":   our_title,
            "search_term": search_term,
            "weight":      p["weight"],
            "our_price":   our_price,
            "competitors": comp_results,
        })

    found = sum(1 for r in rows if any(v for v in r["competitors"].values()))
    return f"Price monitor sent. {found}/{len(rows)} products matched.", rows


def run_price_monitor() -> str:
    """
    Public entry point called by bot.py.
    Skips on even days. Retries up to MAX_RETRIES times.
    Sends error email if all retries fail.
    """
    if not should_run_today():
        return "Price monitor skipped (even day)"

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            status, rows = _run_once()
            html = _build_html(rows)
            _send_email(
                subject=f"SVF Price Monitor — {datetime.now(BERLIN_TZ).strftime('%d %b %Y')}",
                html_body=html,
                recipients=RECIPIENTS,
            )
            return status
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(30 * attempt)  # 30s, 60s between retries

    # All retries failed — send error email
    _send_error_email(last_exc)
    return f"Price monitor failed after {MAX_RETRIES} attempts: {last_exc}"


# ── HTML email builder ─────────────────────────────────────────────────────

def _status_icon(our: float, theirs: float) -> str:
    if our is None or theirs is None:
        return ""
    diff = (our - theirs) / theirs * 100
    return "🔴" if diff > 5 else ("🟢" if diff < -5 else "🟡")


def _build_html(rows: list) -> str:
    date_str = datetime.now(BERLIN_TZ).strftime("%d %b %Y")
    groups   = defaultdict(list)
    for r in rows:
        groups[(r["search_term"], r["weight"])].append(r)

    table_rows = ""
    for (term, weight), group_rows in groups.items():
        table_rows += f"""
        <tr style="background:#1a1a2e;color:#fff;">
          <td colspan="5" style="padding:8px 12px;font-weight:bold;font-size:13px;letter-spacing:.5px;">
            {term.upper()} — {weight}
          </td>
        </tr>"""

        for r in group_rows:
            our_p    = r["our_price"]
            our_fmt  = f"€{our_p:.2f}" if our_p else "—"
            comp_cells = ""
            all_prices = []

            for comp_name in COMPETITORS:
                match = r["competitors"].get(comp_name)
                if match:
                    icon  = _status_icon(our_p, match["price"])
                    brand = match["brand"] or "—"
                    all_prices.append(match["price"])
                    comp_cells += f"""
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;">
                      {icon} €{match['price']:.2f}<br>
                      <span style="font-size:11px;color:#888;">{brand[:30]}</span>
                    </td>"""
                else:
                    comp_cells += '<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#bbb;">—</td>'

            if our_p and all_prices:
                cheapest = min(all_prices)
                diff = (our_p - cheapest) / cheapest * 100
                if diff > 5:
                    status = f'<span style="color:#e74c3c;font-weight:bold;">▲ {diff:+.1f}% vs cheapest</span>'
                elif diff < -5:
                    status = f'<span style="color:#27ae60;font-weight:bold;">▼ {diff:+.1f}% (we\'re cheapest)</span>'
                else:
                    status = f'<span style="color:#f39c12;">≈ {diff:+.1f}% competitive</span>'
            else:
                status = '<span style="color:#bbb;">no data</span>'

            table_rows += f"""
            <tr style="background:#fff;">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;">
                {r['our_title']}<br>
                <span style="color:#aaa;font-size:11px;">{r['sku']}</span>
              </td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;font-weight:bold;">{our_fmt}</td>
              {comp_cells}
              <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;">{status}</td>
            </tr>"""

    comp_headers = "".join(
        f'<th style="padding:10px 12px;text-align:left;font-weight:600;">{n}</th>'
        for n in COMPETITORS
    )
    return f"""
    <html><body style="font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
    <div style="max-width:900px;margin:0 auto;">
      <h2 style="color:#1a1a2e;margin-bottom:4px;">📊 SVF Price Monitor</h2>
      <p style="color:#888;margin-top:0;">{date_str} — Top 40 products vs Jamoona &amp; Dookan</p>
      <table style="width:100%;border-collapse:collapse;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.1);border-radius:6px;overflow:hidden;">
        <thead>
          <tr style="background:#f0f0f0;">
            <th style="padding:10px 12px;text-align:left;font-weight:600;">Our Product</th>
            <th style="padding:10px 12px;text-align:left;font-weight:600;">Our Price</th>
            {comp_headers}
            <th style="padding:10px 12px;text-align:left;font-weight:600;">Status</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
      <p style="color:#aaa;font-size:11px;margin-top:16px;">
        🔴 We're &gt;5% more expensive than cheapest competitor &nbsp;|&nbsp;
        🟡 Within 5% &nbsp;|&nbsp;
        🟢 We're cheapest<br>
        Sent every alternate day at 07:00 CET. Brand-specific match used where competitor stocks same brand.
      </p>
    </div></body></html>"""


# ── Gmail send ─────────────────────────────────────────────────────────────

def _get_gmail_service():
    creds = service_account.Credentials.from_service_account_file(
        _SERVICE_ACCOUNT_FILE, scopes=["https://mail.google.com/"]
    ).with_subject(SENDER_EMAIL)
    return google_build("gmail", "v1", credentials=creds)


def _send_email(subject: str, html_body: str, recipients: list):
    svc = _get_gmail_service()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def _send_error_email(exc: Exception):
    tb = traceback.format_exc()
    html = f"""
    <html><body style="font-family:monospace;padding:20px;">
    <h2 style="color:#c0392b;">⚠️ SVF Price Monitor — Error</h2>
    <p><b>Time:</b> {datetime.now(BERLIN_TZ).strftime('%d %b %Y %H:%M CET')}</p>
    <p><b>Error:</b> {exc}</p>
    <p><b>What to check:</b></p>
    <ul>
      <li>Shopify API token still valid (SHOPIFY_ACCESS_TOKEN env var on Railway)</li>
      <li>Google service account still has Gmail DWD scope (admin.google.com)</li>
      <li>Jamoona / Dookan websites reachable (try opening them manually)</li>
      <li>price_monitor_products.csv still exists on Railway volume</li>
    </ul>
    <pre style="background:#f8f8f8;padding:12px;border-radius:4px;font-size:11px;">{tb}</pre>
    </body></html>"""
    try:
        _send_email(
            subject="⚠️ SVF Price Monitor FAILED — action needed",
            html_body=html,
            recipients=["sparikh@spicevillage.eu"],
        )
    except Exception:
        pass  # If email itself fails, don't crash the bot


# ── Manual test run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    if not force and not should_run_today():
        print("Today is an even day — skipping. Use --force to override.")
    else:
        status, rows = _run_once()
        html = _build_html(rows)
        subject_prefix = "FORCED TEST — " if force else ""
        _send_email(
            subject=f"{subject_prefix}SVF Price Monitor — {datetime.now(BERLIN_TZ).strftime('%d %b %Y')}",
            html_body=html,
            recipients=RECIPIENTS,
        )
        print(status)
