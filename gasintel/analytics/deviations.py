"""
The deviation engine: scan every series for what's abnormal *for this time of
year* and emit ranked signals. Replaces the old winter-only threshold alerts
that fell silent all summer.

Signals are scored (higher = more noteworthy) and tagged red/amber so the brief
and the Sitrep tab can lead with what matters. Baselines that don't exist yet
(e.g. injection history before the store has accumulated it) are skipped
gracefully — fill (years of history) and price (2y) work from day one.
"""
from __future__ import annotations

from datetime import datetime

from .. import store
from ..config import COUNTRIES, COUNTRY_NAME, COUNTRY_FLAG, SEASONAL_DOY_WINDOW
from . import seasonal

ENTITIES = ["EU"] + [uc for _, uc, _, _ in COUNTRIES]


def _name(entity):
    return "EU aggregate" if entity == "EU" else COUNTRY_NAME.get(entity, entity)


def _flag(entity):
    return "🇪🇺" if entity == "EU" else COUNTRY_FLAG.get(entity, "")


def _fill_signals(conn, skip_entities=frozenset()):
    """Storage unusually low/high vs seasonal norm.

    Low-side is the risk signal (red/amber). High-side is informational only and
    additionally requires the tank to be genuinely full in absolute terms (>55%)
    — without that guard, a market that's merely less-drawn than its crisis-era
    history reads as a misleading "exceptionally high" headline (e.g. GB ~9%,
    whose history also mixes measurement bases). Entities already covered by a
    refill signal are skipped to avoid saying the same thing twice.
    """
    out = []
    for e in ENTITIES:
        if e in skip_entities:
            continue
        last = store.latest_storage(conn, e)
        if not last or last["fill"] is None:
            continue
        d = datetime.strptime(last["gas_day"], "%Y-%m-%d")
        dist = store.doy_distribution(conn, "storage_daily", "fill", "country",
                                      e, d.timetuple().tm_yday,
                                      SEASONAL_DOY_WINDOW, exclude_year=d.year)
        a = seasonal.assess(last["fill"], dist)
        if not seasonal.is_extreme(a):
            continue
        pct, fill, b = a["percentile"], a["value"], a["band"]
        low_side = pct is not None and pct <= 12
        if not low_side and fill < 55:
            continue  # suppress dubious "high" flags on near-empty storage
        out.append({
            "category": "storage_fill", "entity": e,
            "severity": ("red" if pct <= 5 else "amber") if low_side else "amber",
            "score": (abs(a["z"] or 0) + 1) * (1.0 if low_side else 0.5),
            "headline": f"{_flag(e)} {_name(e)} storage {fill:.0f}% — {a['label']}",
            "detail": (f"{pct:.0f}th percentile for this date "
                       f"(5yr {b['min']:.0f}–{b['max']:.0f}%, avg {b['avg']:.0f}%)"
                       if b else a["label"]),
        })
    return out


def _trajectory_signals(traj, top_n: int = 5):
    """Worst refill shortfalls only — the 'Countries Behind' card and the refill
    table carry the full list, so we surface just the most acute here (EU always
    included) to keep the signal panel from flooding in a low-storage year."""
    cand = [(e, t) for e, t in (traj or {}).items()
            if not t.get("on_track") and t.get("shortfall_pp", 0) >= 2]
    cand.sort(key=lambda kv: -kv[1]["shortfall_pp"])
    keep = [kv for kv in cand if kv[0] == "EU"][:1]
    keep += [kv for kv in cand if kv[0] != "EU"][:top_n]
    out = []
    for e, t in keep:
        basis = ("following the typical seasonal refill"
                 if t.get("proj_method") == "seasonal" else "at current pace")
        out.append({
            "category": "refill", "entity": e,
            "severity": "red" if t["shortfall_pp"] >= 8 else "amber",
            "score": 1 + t["shortfall_pp"] / 10,
            "headline": (f"{_flag(e)} {_name(e)} projected {t['projected_fill']:.0f}% "
                         f"by Nov 1 — {t['shortfall_pp']:.0f}pp short of target"),
            "detail": (f"{basis}; now {t['current_fill']:.0f}%"
                       + (f" vs ~{t['normal_now_avg']:.0f}% seasonal norm"
                          if t.get("normal_now_avg") else "")),
        })
    return out


def _price_signals(spreads):
    out = []
    ttf = (spreads or {}).get("ttf")
    if not ttf:
        return out
    if ttf.get("chg_1d_pct") is not None and abs(ttf["chg_1d_pct"]) >= 5:
        up = ttf["chg_1d_pct"] > 0
        out.append({
            "category": "price", "entity": "TTF",
            "severity": "red" if abs(ttf["chg_1d_pct"]) >= 8 else "amber",
            "score": abs(ttf["chg_1d_pct"]) / 5,
            "headline": f"TTF {ttf['last']:.1f} €/MWh, {ttf['chg_1d_pct']:+.1f}% on the day",
            "detail": f"{'+' if up else ''}{ttf['chg_1d_abs']:.2f} €/MWh; "
                      f"{ttf['year_percentile']:.0f}th percentile of the past year",
        })
    yp = ttf.get("year_percentile")
    if yp is not None and (yp >= 92 or yp <= 8):
        out.append({
            "category": "price", "entity": "TTF",
            "severity": "amber", "score": 1.5,
            "headline": f"TTF {ttf['last']:.1f} €/MWh — "
                        f"{'near 1-yr highs' if yp >= 92 else 'near 1-yr lows'}",
            "detail": f"{yp:.0f}th percentile of the trailing year",
        })
    return out


def build_signals(conn, run_date: str, analytics: dict) -> list[dict]:
    refill = _trajectory_signals(analytics.get("trajectory"))
    covered = {s["entity"] for s in refill}
    sigs = []
    sigs += refill
    sigs += _fill_signals(conn, skip_entities=covered)
    sigs += _price_signals(analytics.get("spreads"))
    sev_rank = {"red": 0, "amber": 1}
    sigs.sort(key=lambda s: (sev_rank.get(s["severity"], 2), -s["score"]))
    return sigs
