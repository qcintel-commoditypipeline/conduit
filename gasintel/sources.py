"""
Fresh data sources owned by the framework.

Storage/LNG snapshot + seasonality are reused from the proven `gas_dashboard`
fetchers (see persist.py). Here we own the two sources the old code handled
poorly or not as time series:

  * prices  — Yahoo Finance daily history (TTF, Henry Hub), full curve-ready.
  * flows   — ENTSOG physical flows, correctly date-stamped per gas-day and
              classified by corridor (the old code took v[0] as "latest" and
              mixed demand points into supply).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from .config import PRICE_SYMBOLS, classify_point

ENTSOG_URL = "https://transparency.entsog.eu/api/v1/operationaldatas"
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}


# ── Prices ───────────────────────────────────────────────────────────────────

def fetch_prices(rng: str = "2y", symbols: dict | None = None) -> list[dict]:
    """Daily close history from Yahoo. Returns price_daily rows.

    symbols maps our label -> Yahoo ticker (default config.PRICE_SYMBOLS).
    """
    symbols = symbols or PRICE_SYMBOLS
    out: list[dict] = []
    for label, ticker in symbols.items():
        try:
            r = requests.get(YF_URL.format(sym=ticker), headers=_UA,
                             params={"range": rng, "interval": "1d"}, timeout=20)
            if r.status_code != 200:
                print(f"  ⚠ price {label} ({ticker}) HTTP {r.status_code}")
                continue
            res = r.json().get("chart", {}).get("result", [{}])[0]
            ts = res.get("timestamp", []) or []
            closes = (res.get("indicators", {}).get("quote", [{}])[0]
                      .get("close", []) or [])
            n = 0
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                day = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
                out.append({"symbol": label, "trade_day": day, "price": round(c, 4)})
                n += 1
            print(f"  ✓ price {label}: {n} days")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ price {label} failed: {e}")
        time.sleep(0.3)
    return out


# ── Flows ────────────────────────────────────────────────────────────────────

def fetch_flows(date_from: str, date_to: str,
                only_classified: bool = True) -> list[dict]:
    """ENTSOG physical flows per (point, gas-day, direction).

    Returns flow_daily rows in GWh/d. By default keeps only points that map to a
    known corridor (config.CORRIDORS) so the table stays focused on the
    cross-border + LNG supply story rather than thousands of internal points.
    """
    try:
        r = requests.get(ENTSOG_URL, params={
            "indicator": "Physical Flow", "periodType": "day",
            "from": date_from, "to": date_to, "limit": -1}, timeout=120)
        if r.status_code != 200:
            print(f"  ⚠ ENTSOG {date_from}..{date_to} HTTP {r.status_code}")
            return []
        recs = r.json().get("operationaldatas") or []
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ ENTSOG {date_from}..{date_to} failed: {e}")
        return []

    rows: list[dict] = []
    for f in recs:
        label = f.get("pointLabel") or f.get("pointKey") or ""
        pfrom = f.get("periodFrom") or ""
        if not label or not pfrom:
            continue
        try:
            val = float(f["value"])
        except (TypeError, ValueError, KeyError):
            continue
        corridor, region, is_supply = classify_point(label)
        if only_classified and not corridor:
            continue
        rows.append({
            "point": label,
            "gas_day": pfrom[:10],
            "direction": (f.get("directionKey") or "").lower(),
            "operator": f.get("operatorLabel") or "",
            "corridor": corridor,
            "region": region,
            "is_supply": 1 if is_supply else 0,
            "value_gwh": val / 1e6,  # kWh/d -> GWh/d
        })
    return rows
