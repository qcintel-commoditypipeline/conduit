#!/usr/bin/env python3
"""
Conduit pipeline — the new entrypoint.

    fetch (reused gas_dashboard fetchers)
      -> persist (SQLite store)
      -> analytics (seasonal deviations, refill trajectory, spreads, balance)
      -> brief (Claude narrative -> Telegram)
      -> render (inject Sitrep hero into the existing dashboard)

The live page keeps updating even if any optional stage (prices, flows, news,
brief) fails — each is wrapped defensively. Run:  python pipeline.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# UTF-8 console (the old script crashed on Windows cp1252; harmless on Linux)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from dotenv import load_dotenv
_env = Path("/opt/scripts/.env")
load_dotenv(_env if _env.exists() else None)

import gas_dashboard  # reused, proven fetchers + base HTML
from gasintel import store, persist, sources, analytics, news, brief, render
from gasintel.config import OUTPUT_HTML


def _step(label):
    print(f"\n=== {label} ===", flush=True)


def main() -> int:
    t0 = time.time()
    run_date = datetime.utcnow().strftime("%Y-%m-%d")
    print("=" * 60)
    print("  Conduit pipeline —", run_date, "UTC")
    print("=" * 60)

    _step("Fetch (AGSI/ALSI/ENTSOG/National Gas/TTF/weather)")
    D = gas_dashboard.fetch_all()

    _step("Persist")
    with store.session() as conn:
        counts = persist.persist_snapshot(conn, D, run_date)
        print("  storage/lng/seasonality/weather:", counts)
        try:
            pr = sources.fetch_prices(rng="1mo")
            print("  prices upserted:", store.upsert_prices(conn, pr))
        except Exception as e:  # noqa: BLE001
            print("  ⚠ price refresh failed:", e)
        try:
            f7 = (datetime.utcnow() - timedelta(days=8)).strftime("%Y-%m-%d")
            fl = sources.fetch_flows(f7, run_date)
            print("  flows upserted:", store.upsert_flows(conn, fl))
        except Exception as e:  # noqa: BLE001
            print("  ⚠ flow refresh failed:", e)

        _step("Analytics")
        a = analytics.build(conn, run_date)
        store.write_signals(conn, run_date, a["signals"])
        print(f"  signals={len(a['signals'])} trajectory={len(a['trajectory'])}")
        ttf_series = [dict(r) for r in store.price_series(conn, "TTF", 200)]
        hh = store.price_series(conn, "NG", 2)
        hh_last = hh[-1]["price"] if hh else None

    _step("News + Brief")
    headlines = news.fetch_headlines()
    brief_text = brief.generate(a, headlines)
    if brief_text:
        brief.deliver_telegram(brief_text)

    _step("Render")
    base = gas_dashboard.gen_html(D)
    sitrep = render.build_sitrep_html(a, ttf_series, hh_last, headlines, brief_text)
    html = base.replace('<div class="main">', '<div class="main">\n' + sitrep, 1)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"  ✓ wrote {OUTPUT_HTML} ({len(html):,} bytes)")

    print(f"\n⏱ {time.time() - t0:.1f}s — done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
