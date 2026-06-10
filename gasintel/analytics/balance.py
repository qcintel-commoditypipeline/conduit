"""
Cross-border supply balance from ENTSOG flows: where is gas physically coming
from, by corridor and source region, and how has that shifted week-on-week.

w/w methodology (the old code compared "latest distinct day" vs the
"8th-most-recent distinct day", which was wrong on both ends):

  * The newest ENTSOG gas-day is frequently *partial* — operators report on a
    lag, so the maximum date often has only a fraction of the points filed.
    We exclude the trailing day when its row count falls below the median
    daily row count of the prior week, and use the latest COMPLETE day.
  * The comparison day is the one exactly 7 calendar days before the chosen
    latest day, falling back to the nearest day with data. If that nearest
    day is more than 2 days from the 7-day target we keep the comparison but
    flag it (``prev_gap_days``) and print a warning, so a sparse history never
    silently masquerades as a clean w/w.

Degrades gracefully — returns an empty structure until flow history is
backfilled, so the rest of the pipeline never depends on it being present.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from .. import store

# The trailing gas-day is treated as incomplete when it has fewer rows than
# this fraction of the median daily row count over the prior week.
PARTIAL_ROW_FRACTION = 0.9
WOW_TARGET_DAYS = 7      # compare vs exactly one week earlier
WOW_MAX_GAP_DAYS = 2     # warn when the nearest data day is further than this


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


def _day_counts(conn, limit: int = 10) -> list[tuple[str, int]]:
    """(gas_day, row_count) for the most recent distinct gas-days, newest first."""
    return [(r["gas_day"], r["n"]) for r in conn.execute(
        "SELECT gas_day, COUNT(*) n FROM flow_daily "
        "GROUP BY gas_day ORDER BY gas_day DESC LIMIT ?", (limit,)).fetchall()]


def _pick_latest_complete(day_counts: list[tuple[str, int]]):
    """Choose the latest *complete* gas-day.

    The newest day (the maximum date) is excluded when it reports materially
    fewer rows than the recent norm — i.e. fewer than PARTIAL_ROW_FRACTION of
    the median row count across the prior (up to) 7 reporting days. Returns
    (chosen_day, excluded_day_or_None).
    """
    if not day_counts:
        return None, None
    if len(day_counts) == 1:
        return day_counts[0][0], None
    newest_day, newest_n = day_counts[0]
    prior = [n for _, n in day_counts[1:8]]
    if newest_n < PARTIAL_ROW_FRACTION * median(prior):
        return day_counts[1][0], newest_day
    return newest_day, None


def _pick_prev_day(conn, latest: str):
    """The comparison day: exactly 7 calendar days before `latest`, or the
    nearest earlier-than-latest day with data. Returns (prev_day, gap_days)
    where gap_days is the distance from the exact 7-day target."""
    target = (datetime.strptime(latest, "%Y-%m-%d")
              - timedelta(days=WOW_TARGET_DAYS)).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT gas_day FROM flow_daily WHERE gas_day < ? "
        "GROUP BY gas_day "
        "ORDER BY ABS(julianday(gas_day) - julianday(?)) ASC, gas_day ASC "
        "LIMIT 1", (latest, target)).fetchone()
    if not row:
        return None, None
    prev = row["gas_day"]
    gap = abs((datetime.strptime(prev, "%Y-%m-%d")
               - datetime.strptime(target, "%Y-%m-%d")).days)
    return prev, gap


def compute(conn) -> dict:
    lo, hi = store.day_range(conn, "flow_daily")
    if not hi:
        return {"available": False, "by_corridor": [], "by_region": []}

    # ASCII-only prints here: this module also runs under plain Windows
    # consoles (cp1252) where emoji raise UnicodeEncodeError.
    latest, excluded = _pick_latest_complete(_day_counts(conn))
    if excluded:
        print(f"  - balance: trailing gas-day {excluded} looks partial; using {latest}")
    prev, gap = _pick_prev_day(conn, latest)
    if prev is None:
        prev, gap = latest, None  # single-day store: w/w degenerates to 0
    elif gap is not None and gap > WOW_MAX_GAP_DAYS:
        print(f"  ! balance: no data near {latest} -7d; comparing vs {prev} "
              f"({gap}d off the 7-day target)")

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
            "partial_day_excluded": excluded, "prev_gap_days": gap,
            "by_corridor": by_corridor, "by_region": by_region,
            "total_supply_gwh": round(sum(max(0, c["net_gwh"]) for c in by_corridor), 1)}
