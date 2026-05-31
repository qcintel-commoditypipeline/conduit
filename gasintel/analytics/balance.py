"""
Cross-border supply balance from ENTSOG flows: where is gas physically coming
from, by corridor and source region, and how has that shifted week-on-week.

Degrades gracefully — returns an empty structure until flow history is
backfilled, so the rest of the pipeline never depends on it being present.
"""
from __future__ import annotations

from collections import defaultdict

from .. import store


def _net_by(conn, gas_day: str):
    rows = conn.execute(
        "SELECT corridor, region, direction, SUM(value_gwh) v "
        "FROM flow_daily WHERE gas_day=? AND is_supply=1 "
        "GROUP BY corridor, region, direction", (gas_day,)).fetchall()
    by_corridor = defaultdict(float)
    by_region = defaultdict(float)
    for r in rows:
        sign = 1 if r["direction"] == "entry" else -1
        by_corridor[r["corridor"]] += sign * (r["v"] or 0)
        by_region[r["region"]] += sign * (r["v"] or 0)
    return by_corridor, by_region


def compute(conn) -> dict:
    lo, hi = store.day_range(conn, "flow_daily")
    if not hi:
        return {"available": False, "by_corridor": [], "by_region": []}

    # latest day with data, and ~7 days prior for w/w comparison
    days = [r["gas_day"] for r in conn.execute(
        "SELECT DISTINCT gas_day FROM flow_daily ORDER BY gas_day DESC LIMIT 8"
    ).fetchall()]
    latest = days[0]
    prev = days[-1] if len(days) > 1 else latest

    cur_corr, cur_reg = _net_by(conn, latest)
    prv_corr, _ = _net_by(conn, prev)

    by_corridor = sorted(
        ({"corridor": k, "net_gwh": round(v, 1),
          "wow_gwh": round(v - prv_corr.get(k, 0), 1)}
         for k, v in cur_corr.items()),
        key=lambda x: x["net_gwh"], reverse=True)
    by_region = sorted(
        ({"region": k, "net_gwh": round(v, 1)} for k, v in cur_reg.items()),
        key=lambda x: x["net_gwh"], reverse=True)

    return {"available": True, "latest_day": latest, "prev_day": prev,
            "by_corridor": by_corridor, "by_region": by_region,
            "total_supply_gwh": round(sum(max(0, c["net_gwh"]) for c in by_corridor), 1)}
