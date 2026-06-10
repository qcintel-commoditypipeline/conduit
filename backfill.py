#!/usr/bin/env python3
"""
One-time / repeatable history backfill into the SQLite store.

  python backfill.py --all
  python backfill.py --seasonality --prices       # fast, no heavy fetching
  python backfill.py --flows --years 2

Seasonality is lifted from the existing gas_cache.json (free). Prices come from
Yahoo (2y default). Flows come from ENTSOG in monthly chunks.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gasintel import store, sources
from gasintel.persist import seasonality_fill_rows

CACHE_CANDIDATES = [
    Path(__file__).parent / "gas_cache.json",
    Path("/opt/scripts/conduit/gas_cache.json"),
]


def backfill_seasonality(conn) -> None:
    cache = next((p for p in CACHE_CANDIDATES if p.exists()), None)
    if not cache:
        print("  ⚠ no gas_cache.json found — skipping seasonality backfill")
        return
    D = json.loads(cache.read_text(encoding="utf-8"))
    rows = seasonality_fill_rows(D)
    n = store.upsert_storage_fill(conn, rows)
    conn.commit()
    lo, hi = store.day_range(conn, "storage_daily")
    print(f"  ✓ seasonality: {n} fill rows from {cache} ({lo} → {hi})")


def backfill_prices(conn, rng: str) -> None:
    rows = sources.fetch_prices(rng=rng)
    n = store.upsert_prices(conn, rows)
    conn.commit()
    print(f"  ✓ prices: {n} rows")


def _date_chunks(years: int, chunk_days: int = 7):
    """Walk the period in small windows.

    ENTSOG's operationaldatas endpoint silently returns 0 records when a single
    (range x points) query with limit=-1 is too large — a ~30-day window comes
    back empty, while ~8-day windows return thousands of records reliably, even
    years back. So we chunk by a week.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(years * 365.25))
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=chunk_days), end + timedelta(days=1))
        yield cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")
        cur = nxt


def backfill_flows(conn, years: int) -> None:
    total = 0
    for lo, hi in _date_chunks(years):
        rows = sources.fetch_flows(lo, hi)
        n = store.upsert_flows(conn, rows)
        conn.commit()
        total += n
        print(f"    {lo}..{hi}: {n} flow rows (total {total})")
        time.sleep(0.5)
    print(f"  ✓ flows: {total} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--seasonality", action="store_true")
    ap.add_argument("--prices", action="store_true")
    ap.add_argument("--flows", action="store_true")
    ap.add_argument("--range", default="2y", help="Yahoo price range (e.g. 2y, 5y)")
    ap.add_argument("--years", type=float, default=2, help="flow backfill years")
    a = ap.parse_args()
    if not any([a.all, a.seasonality, a.prices, a.flows]):
        ap.error("nothing to do — pass --all or specific parts")

    with store.session() as conn:
        if a.all or a.seasonality:
            print("📈 seasonality backfill..."); backfill_seasonality(conn)
        if a.all or a.prices:
            print("💰 price backfill..."); backfill_prices(conn, a.range)
        if a.all or a.flows:
            print("🔀 flow backfill..."); backfill_flows(conn, a.years)
        print("\nStore summary:")
        for t in ("storage_daily", "lng_daily", "flow_daily", "price_daily"):
            print(f"  {t}: {store.table_count(conn, t)}")


if __name__ == "__main__":
    sys.exit(main())
