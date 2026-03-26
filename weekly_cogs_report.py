"""
SVF Weekly COGS Report
Usage: python3 weekly_cogs_report.py [YYYY-MM-DD] [YYYY-MM-DD]
       python3 weekly_cogs_report.py              # defaults to last 7 days
"""

import os
import requests
import time
import sys
from collections import defaultdict
from datetime import datetime, timedelta

STORE = 'spice-village-eu.myshopify.com'
TOKEN = os.environ['SHOPIFY_ACCESS_TOKEN']
H = {'X-Shopify-Access-Token': TOKEN}


# ── Helpers ────────────────────────────────────────────────────────────────

def paginate(url, key):
    """Cursor-based pagination. Yields items from `key` across all pages."""
    while url:
        r = requests.get(url, headers=H)
        if r.status_code == 429:
            wait = float(r.headers.get('Retry-After', 2))
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        yield from r.json().get(key, [])
        link = r.headers.get('Link', '')
        url = None
        if 'rel="next"' in link:
            for part in link.split(','):
                if 'rel="next"' in part:
                    url = part.split('<')[1].split('>')[0]
        time.sleep(1)


def fetch_inventory_costs(inv_ids):
    """Fetch cost field for a list of inventory_item_ids. Returns {id: cost}."""
    costs = {}
    unique_ids = list(set(inv_ids))
    batches = [unique_ids[i:i+100] for i in range(0, len(unique_ids), 100)]
    for batch in batches:
        while True:
            r = requests.get(
                f'https://{STORE}/admin/api/2024-01/inventory_items.json'
                f'?ids={",".join(str(x) for x in batch)}',
                headers=H
            )
            if r.status_code == 429:
                wait = float(r.headers.get('Retry-After', 2))
                print(f"  Rate limited on inventory batch, waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            for item in r.json().get('inventory_items', []):
                if item.get('cost'):
                    costs[item['id']] = float(item['cost'])
            time.sleep(1)
            break
    return costs


# ── Step 1: Build variant_id → inventory_item_id map ──────────────────────

def build_variant_map():
    print("Building variant → inventory_item map...")
    variant_to_inv = {}
    url = f'https://{STORE}/admin/api/2024-01/products.json?limit=250&fields=id,variants'
    page = 0
    for p in paginate(url, 'products'):
        for v in p['variants']:
            variant_to_inv[v['id']] = v['inventory_item_id']
        page += 1
    print(f"  ✓ {len(variant_to_inv):,} variants mapped")
    return variant_to_inv


# ── Step 2: Pull orders ────────────────────────────────────────────────────

def pull_orders(date_from, date_to):
    print(f"Fetching orders {date_from} → {date_to}...")
    orders = []
    url = (f'https://{STORE}/admin/api/2024-01/orders.json'
           f'?limit=250&status=any'
           f'&created_at_min={date_from}T00:00:00Z'
           f'&created_at_max={date_to}T23:59:59Z'
           f'&fields=id,name,created_at,cancelled_at,line_items,refunds')
    for o in paginate(url, 'orders'):
        if not o.get('cancelled_at'):
            orders.append(o)
    print(f"  ✓ {len(orders)} non-cancelled orders")
    return orders


# ── Step 3: Aggregate line items + refunds ────────────────────────────────

def aggregate_lines(orders):
    lines = defaultdict(lambda: {
        'title': '', 'sku': '', 'variant_id': None,
        'gross_qty': 0, 'gross_revenue': 0.0,
        'refund_qty': 0, 'refund_revenue': 0.0
    })
    for order in orders:
        for li in order['line_items']:
            vid = li.get('variant_id') or 0
            key = (vid, li.get('title', ''), li.get('sku', '') or '')
            d = lines[key]
            d['title']         = li.get('title', '')
            d['sku']           = li.get('sku', '') or ''
            d['variant_id']    = vid
            d['gross_qty']     += li.get('quantity', 0)
            d['gross_revenue'] += li.get('quantity', 0) * float(li.get('price', 0))
        # Refunds: subtract revenue only — COGS stays on original qty (damaged goods rule)
        for refund in order.get('refunds', []):
            for ri in refund.get('refund_line_items', []):
                oli = ri.get('line_item', {})
                vid = oli.get('variant_id') or 0
                key = (vid, oli.get('title', ''), oli.get('sku', '') or '')
                if key in lines:
                    lines[key]['refund_qty']     += ri.get('quantity', 0)
                    lines[key]['refund_revenue'] += float(ri.get('subtotal', 0))
    print(f"  ✓ {len(lines)} unique line items")
    return lines


# ── Step 4: Build report rows ──────────────────────────────────────────────

def build_rows(lines, variant_to_inv, inv_costs, min_revenue=20):
    costed, unknown = [], []
    for key, d in lines.items():
        net_revenue = d['gross_revenue'] - d['refund_revenue']
        if net_revenue < min_revenue:
            continue
        net_qty = d['gross_qty'] - d['refund_qty']
        vid     = d['variant_id']
        inv_id  = variant_to_inv.get(vid) if vid else None
        cost    = inv_costs.get(inv_id) if inv_id else None

        row = {
            'title':       d['title'],
            'sku':         d['sku'],
            'gross_qty':   d['gross_qty'],
            'refund_qty':  d['refund_qty'],
            'net_qty':     net_qty,
            'net_revenue': net_revenue,
        }
        if cost:
            cogs_eur     = d['gross_qty'] * cost   # COGS on original qty
            gross_profit = net_revenue - cogs_eur
            cogs_pct     = cogs_eur / net_revenue * 100 if net_revenue else 0
            row.update({'cost': cost, 'cogs_eur': cogs_eur,
                        'gross_profit': gross_profit, 'cogs_pct': cogs_pct})
            costed.append(row)
        else:
            unknown.append(row)

    costed.sort(key=lambda x: x['cogs_pct'], reverse=True)
    unknown.sort(key=lambda x: x['net_revenue'], reverse=True)
    return costed, unknown


# ── Step 5: Print report ───────────────────────────────────────────────────

def print_report(costed, unknown, date_from, date_to, n_orders):
    W   = 118
    HDR = (f"  {'Product':<42} {'SKU':<14} {'Sold':>5} {'Rfnd':>5} "
           f"{'NetRev€':>8} {'COGS€':>8} {'COGS%':>7} {'GP€':>8}")
    SEP = "─" * W

    print(f"\n{'═'*W}")
    print(f"  SVF ONLINE — COGS REPORT  |  {date_from} → {date_to}  |  {n_orders} orders")
    print(f"{'═'*W}")
    print(HDR)
    print(SEP)

    total_rev = total_cogs = total_gp = warn_count = 0

    for row in costed:
        flag = ' ⚠️' if row['cogs_pct'] > 60 else ''
        warn_count  += 1 if row['cogs_pct'] > 60 else 0
        total_rev   += row['net_revenue']
        total_cogs  += row['cogs_eur']
        total_gp    += row['gross_profit']
        rfnd = f"-{row['refund_qty']}" if row['refund_qty'] else ''
        print(f"  {row['title'][:40]:<40}  {row['sku'][:12]:<12}  "
              f"{row['gross_qty']:>4}  {rfnd:>4}  "
              f"€{row['net_revenue']:>6.0f}  €{row['cogs_eur']:>6.0f}  "
              f"{row['cogs_pct']:>5.1f}%  €{row['gross_profit']:>6.0f}{flag}")

    print(SEP)
    blended = total_cogs / total_rev * 100 if total_rev else 0
    print(f"  {'TOTAL — costed products':<58}  "
          f"€{total_rev:>6.0f}  €{total_cogs:>6.0f}  "
          f"{blended:>5.1f}%  €{total_gp:>6.0f}")
    print(f"\n  ► {warn_count} products above 60% COGS  |  "
          f"Blended gross margin: {100-blended:.1f}%  |  "
          f"Gross profit: €{total_gp:,.0f}")

    if unknown:
        unknown_rev  = sum(r['net_revenue'] for r in unknown)
        grand_total  = total_rev + unknown_rev
        blind_pct    = unknown_rev / grand_total * 100 if grand_total else 0
        print(f"\n{'─'*W}")
        print(f"  ❓ COST UNKNOWN — {len(unknown)} products  "
              f"(€{unknown_rev:,.0f} = {blind_pct:.1f}% of total revenue)")
        print(f"{'─'*W}")
        print(f"  {'Product':<42} {'SKU':<14} {'Sold':>5}  {'NetRev€':>8}")
        for row in unknown:
            print(f"  {row['title'][:40]:<40}  {row['sku'][:12]:<12}  "
                  f"{row['gross_qty']:>4}   €{row['net_revenue']:>6.0f}"
                  f"  ← enter cost in Shopify")
        print(f"\n  Revenue blind spot: €{unknown_rev:,.0f} / €{grand_total:,.0f} total")

    print(f"\n{'═'*W}\n")


# ── Main ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) == 3:
        date_from, date_to = sys.argv[1], sys.argv[2]
    else:
        today     = datetime.now().date()
        date_to   = str(today)
        date_from = str(today - timedelta(days=7))
        print(f"No dates provided — defaulting to last 7 days: {date_from} → {date_to}")

    variant_to_inv = build_variant_map()
    all_inv_ids    = list(variant_to_inv.values())

    print(f"Fetching costs for {len(all_inv_ids):,} inventory items...")
    inv_costs = fetch_inventory_costs(all_inv_ids)
    print(f"  ✓ {len(inv_costs):,} items have cost set "
          f"({len(inv_costs)/len(all_inv_ids)*100:.1f}% coverage)")

    orders         = pull_orders(date_from, date_to)
    lines          = aggregate_lines(orders)
    costed, unknown = build_rows(lines, variant_to_inv, inv_costs, min_revenue=20)

    print_report(costed, unknown, date_from, date_to, len(orders))
