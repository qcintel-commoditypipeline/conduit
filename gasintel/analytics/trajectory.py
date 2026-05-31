"""
Refill trajectory vs the 90% / Nov-1 mandate (EU Reg 2022/1032) and the 5-yr
seasonal path. This is the question the old dashboard never answered and the one
that actually matters in injection season:

    "At the current pace, where does storage land by Nov 1 — and is that ahead
     of or behind the seasonal norm?"
"""
from __future__ import annotations

from datetime import datetime, date
from statistics import median

from .. import store
from ..config import (COUNTRIES, STORAGE_TARGET_PCT, STORAGE_TARGET_MONTH,
                      STORAGE_TARGET_DAY, SEASONAL_DOY_WINDOW)
from . import seasonal

PACE_WINDOW_DAYS = 14


def _next_target_date(today: date) -> date:
    t = date(today.year, STORAGE_TARGET_MONTH, STORAGE_TARGET_DAY)
    return t if today <= t else date(today.year + 1, STORAGE_TARGET_MONTH, STORAGE_TARGET_DAY)


def _seasonal_gain(series, cur_year: int, doy_now: int, doy_tgt: int,
                   window: int) -> float | None:
    """Median historical fill gain from `doy_now` to `doy_tgt`, per past year.

    This is the realistic refill expectation: how much fill % storage has
    typically *added* over this calendar stretch in prior years. Captures the
    natural tapering of injection as tanks fill — unlike a linear extrapolation
    of the current daily pace, which overshoots badly over a 5-month horizon.
    """
    by_year: dict[int, list[tuple[int, float]]] = {}
    for r in series:
        if r["fill"] is None:
            continue
        d = datetime.strptime(r["gas_day"], "%Y-%m-%d")
        if d.year == cur_year:
            continue
        by_year.setdefault(d.year, []).append((d.timetuple().tm_yday, r["fill"]))

    def nearest(points, doy):
        best, bestd = None, 1e9
        for pd, pf in points:
            dist = min(abs(pd - doy), 365 - abs(pd - doy))
            if dist < bestd:
                best, bestd = pf, dist
        return best if bestd <= window else None

    gains = []
    for pts in by_year.values():
        f_now = nearest(pts, doy_now)
        f_tgt = nearest(pts, doy_tgt)
        if f_now is not None and f_tgt is not None:
            gains.append(f_tgt - f_now)
    return round(median(gains), 1) if gains else None


def _entity_trajectory(conn, entity: str, run_date: str) -> dict | None:
    series = [r for r in store.storage_series(conn, entity) if r["fill"] is not None]
    if len(series) < 2:
        return None
    last = series[-1]
    cur_day = datetime.strptime(last["gas_day"], "%Y-%m-%d").date()
    cur_fill = last["fill"]
    if cur_fill is None:
        return None

    # pace: fill %/day over the trailing window
    ref = None
    for r in reversed(series[:-1]):
        if r["fill"] is None:
            continue
        d = datetime.strptime(r["gas_day"], "%Y-%m-%d").date()
        if (cur_day - d).days >= PACE_WINDOW_DAYS:
            ref = r
            break
    ref = ref or series[0]
    if ref["fill"] is None:
        return None
    ref_day = datetime.strptime(ref["gas_day"], "%Y-%m-%d").date()
    span = (cur_day - ref_day).days or 1
    pace = (cur_fill - ref["fill"]) / span  # pp per day, recent

    target_date = _next_target_date(cur_day)
    days_to_target = (target_date - cur_day).days
    doy_now = cur_day.timetuple().tm_yday
    doy_tgt = target_date.timetuple().tm_yday

    # Realistic projection: current fill + typical historical gain over this
    # calendar stretch. Falls back to (capped) linear pace only if there's no
    # seasonal history to lean on.
    gain = _seasonal_gain(series, cur_day.year, doy_now, doy_tgt, SEASONAL_DOY_WINDOW)
    if gain is not None:
        projected = cur_fill + gain
        proj_method = "seasonal"
    else:
        projected = cur_fill + pace * days_to_target if pace > 0 else cur_fill
        proj_method = "pace"
    projected = max(0.0, min(100.0, projected))

    # seasonal context: normal fill now and at the target date
    dist_now = store.doy_distribution(conn, "storage_daily", "fill", "country",
                                      entity, doy_now, SEASONAL_DOY_WINDOW,
                                      exclude_year=cur_day.year)
    dist_tgt = store.doy_distribution(conn, "storage_daily", "fill", "country",
                                      entity, doy_tgt, SEASONAL_DOY_WINDOW)
    normal_now = seasonal.band(dist_now)
    normal_tgt = seasonal.band(dist_tgt)
    vs_normal_now = (round(cur_fill - normal_now["avg"], 1)
                     if normal_now else None)

    return {
        "entity": entity,
        "as_of": last["gas_day"],
        "current_fill": round(cur_fill, 1),
        "pace_pp_per_day": round(pace, 3),
        "seasonal_gain_pp": gain,
        "proj_method": proj_method,
        "days_to_target": days_to_target,
        "target_date": target_date.isoformat(),
        "target_pct": STORAGE_TARGET_PCT,
        "projected_fill": round(projected, 1),
        "on_track": projected >= STORAGE_TARGET_PCT,
        "shortfall_pp": round(max(0.0, STORAGE_TARGET_PCT - projected), 1),
        "normal_now_avg": normal_now["avg"] if normal_now else None,
        "normal_at_target_avg": normal_tgt["avg"] if normal_tgt else None,
        "vs_normal_now_pp": vs_normal_now,
        "percentile_now": seasonal.percentile_of(cur_fill, dist_now),
    }


def compute(conn, run_date: str) -> dict:
    out = {}
    for entity in ["EU"] + [uc for _, uc, _, _ in COUNTRIES]:
        try:
            t = _entity_trajectory(conn, entity, run_date)
        except Exception as e:  # noqa: BLE001 — one bad entity must not kill the run
            print(f"  ⚠ trajectory {entity} skipped: {e}")
            t = None
        if t:
            out[entity] = t
    return out
