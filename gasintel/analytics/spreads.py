"""
Price context. Yahoo gives front-month only (TTF=F, NG=F), so we compute what's
robust from that: level, daily/30-day momentum, and where the current price sits
in its own trailing-year range. A full forward curve (summer-winter spread,
TTF-JKM) needs a curve source (e.g. Barchart) — left as a clean extension point.
"""
from __future__ import annotations

from .. import store
from . import seasonal

# Henry Hub USD/MMBtu -> USD/MWh (1 MMBtu = 0.293071 MWh)
MMBTU_PER_MWH = 1 / 0.293071


def _changes(series):
    if len(series) < 2:
        return None
    last = series[-1]["price"]
    prev = series[-2]["price"]
    d30 = series[-22]["price"] if len(series) >= 22 else series[0]["price"]
    pct = lambda a, b: round((a - b) / b * 100, 1) if b else None
    return {
        "last": round(last, 2),
        "chg_1d_pct": pct(last, prev),
        "chg_1d_abs": round(last - prev, 2),
        "chg_30d_pct": pct(last, d30),
        "year_percentile": seasonal.percentile_of(
            last, [r["price"] for r in series[-260:]]),
        "as_of": series[-1]["trade_day"],
    }


def compute(conn) -> dict:
    ttf = store.price_series(conn, "TTF")
    ng = store.price_series(conn, "NG")
    out = {"ttf": _changes(ttf) if ttf else None,
           "hh": _changes(ng) if ng else None}
    # transatlantic context: TTF premium to Henry Hub, both in USD/MWh
    # (TTF is EUR/MWh; without live FX we flag it as approximate EUR vs USD)
    if out["ttf"] and out["hh"]:
        hh_usd_mwh = out["hh"]["last"] * MMBTU_PER_MWH
        out["transatlantic"] = {
            "ttf_eur_mwh": out["ttf"]["last"],
            "hh_usd_mwh": round(hh_usd_mwh, 2),
            "ttf_premium_approx": round(out["ttf"]["last"] - hh_usd_mwh, 1),
            "note": "TTF in EUR/MWh vs HH in USD/MWh — premium is FX-approx",
        }
    return out
