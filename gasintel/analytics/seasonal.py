"""
Seasonal-normal math: how unusual is today's value for *this time of year*?

The old dashboard only ever compared storage fill to a raw 5-year min/max band,
and only in winter. These helpers generalise that to any series — express a
current value as a percentile / z-score against the pooled distribution of
historical values near the same day-of-year, with winsorising so the 2022 crisis
year doesn't blow out the "normal" band.
"""
from __future__ import annotations

import math
from statistics import mean, pstdev


def winsorize(values: list[float], p: float = 0.10) -> list[float]:
    """Clip the lowest/highest p fraction to the p / 1-p quantiles."""
    xs = sorted(v for v in values if v is not None)
    if len(xs) < 5:
        return xs
    lo = xs[int(p * (len(xs) - 1))]
    hi = xs[int((1 - p) * (len(xs) - 1))]
    return [min(max(v, lo), hi) for v in xs]


def percentile_of(value: float, dist: list[float]) -> float | None:
    """Percentile rank (0–100) of `value` within `dist`."""
    xs = [v for v in dist if v is not None]
    if not xs or value is None:
        return None
    below = sum(1 for v in xs if v < value)
    equal = sum(1 for v in xs if v == value)
    return round((below + 0.5 * equal) / len(xs) * 100, 1)


def zscore(value: float, dist: list[float], winsor: bool = True) -> float | None:
    xs = winsorize(dist) if winsor else [v for v in dist if v is not None]
    if len(xs) < 5 or value is None:
        return None
    mu = mean(xs)
    sd = pstdev(xs)
    if sd < 1e-9:
        return 0.0
    return round((value - mu) / sd, 2)


def band(dist: list[float]) -> dict | None:
    xs = sorted(v for v in dist if v is not None)
    if not xs:
        return None
    n = len(xs)
    def q(p): return xs[min(n - 1, max(0, int(p * (n - 1))))]
    return {"min": round(xs[0], 1), "p10": round(q(0.10), 1),
            "avg": round(mean(xs), 1), "p90": round(q(0.90), 1),
            "max": round(xs[-1], 1), "n": n}


# Human labels for a percentile rank — used in signals and the UI.
def label_for_percentile(pct: float | None) -> str:
    if pct is None:
        return "no baseline"
    if pct >= 95:
        return "exceptionally high"
    if pct >= 85:
        return "well above normal"
    if pct >= 65:
        return "above normal"
    if pct > 35:
        return "around normal"
    if pct > 15:
        return "below normal"
    if pct > 5:
        return "well below normal"
    return "exceptionally low"


def assess(value: float, dist: list[float]) -> dict:
    """Full seasonal assessment of a value against a day-of-year distribution."""
    return {
        "value": round(value, 2) if value is not None else None,
        "percentile": percentile_of(value, dist),
        "z": zscore(value, dist),
        "band": band(dist),
        "label": label_for_percentile(percentile_of(value, dist)),
        "n": len([v for v in dist if v is not None]),
    }


def is_extreme(assessment: dict, pct_lo: float = 12, pct_hi: float = 88) -> bool:
    p = assessment.get("percentile")
    return p is not None and (p <= pct_lo or p >= pct_hi) and assessment.get("n", 0) >= 8
