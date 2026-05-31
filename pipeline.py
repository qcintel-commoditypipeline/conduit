#!/usr/bin/env python3
"""
Conduit pipeline — the new entrypoint.

    fetch (reused gas_dashboard fetchers)
      -> persist (SQLite store)
      -> analytics (seasonal deviations, refill trajectory, spreads, balance)
      -> brief (Claude narrative -> Telegram)
      -> render (inject Sitrep hero into the existing dashboard)

Every optional stage is wrapped: prices, flows, news, brief and the Sitrep
render can each fail without stopping the daily page from being written.
Run:  python pipeline.py
"""
from __future__ import annotations

import sys
import time
import traceback
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


def _try(label, fn, default=None):
    """Run an optional stage; log a clean traceback on failure, keep going."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        print(f"  ⚠ {label} failed:")
        traceback.print_exc()
        return default


def main() -> int:
    t0 = time.time()
    run_date = datetime.utcnow().strftime("%Y-%m-%d")
    print("=" * 60)
    print("  Conduit pipeline —", run_date, "UTC")
    print("=" * 60)

    _step("Fetch (AGSI/ALSI/ENTSOG/National Gas/TTF/weather)")
    D = gas_dashboard.fetch_all()

    _step("Persist")
    a = {}
    ttf_series, hh_last = [], None
    with store.session() as conn:
        print("  snapshot:", _try("persist", lambda: persist.persist_snapshot(conn, D, run_date)))
        _try("price refresh", lambda: print("  prices:", store.upsert_prices(conn, sources.fetch_prices(rng="1mo"))))
        f7 = (datetime.utcnow() - timedelta(days=8)).strftime("%Y-%m-%d")
        _try("flow refresh", lambda: print("  flows:", store.upsert_flows(conn, sources.fetch_flows(f7, run_date))))

        _step("Analytics")
        a = _try("analytics", lambda: analytics.build(conn, run_date), default={}) or {}
        if a.get("signals"):
            store.write_signals(conn, run_date, a["signals"])
        print(f"  signals={len(a.get('signals', []))} trajectory={len(a.get('trajectory', {}))}")
        ttf_series = [dict(r) for r in store.price_series(conn, "TTF", 200)]
        hh = store.price_series(conn, "NG", 2)
        hh_last = hh[-1]["price"] if hh else None

    _step("News + Brief")
    headlines = _try("news", news.fetch_headlines, default=[]) or []
    brief_text = _try("brief", lambda: brief.generate(a, headlines))
    if brief_text:
        _try("brief delivery", lambda: brief.deliver_telegram(brief_text))

    _step("Render")
    base = gas_dashboard.gen_html(D)
    base = _try("retire intel tab", lambda: render.retire_intel_tab(base), default=base) or base
    sitrep = _try("sitrep render",
                  lambda: render.build_sitrep_html(a, ttf_series, hh_last, headlines, brief_text),
                  default="")
    html = base.replace('<div class="main">', '<div class="main">\n' + sitrep, 1) if sitrep else base
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"  ✓ wrote {OUTPUT_HTML} ({len(html):,} bytes; sitrep={'yes' if sitrep else 'no'})")

    print(f"\n⏱ {time.time() - t0:.1f}s — done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
