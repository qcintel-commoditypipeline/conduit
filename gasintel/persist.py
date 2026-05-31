"""
Adapter: map the reused `gas_dashboard.fetch_all()` output (the `D` dict) into
normalised store rows.

This is the bridge between the old in-memory snapshot and the new time-series
store. It also lifts the seasonality fill history (years of daily fill % already
fetched for the charts) straight into `storage_daily` — a free multi-year
backfill for the seasonal engine, no extra API calls.
"""
from __future__ import annotations


def _sf(v, fb=None):
    if v is None:
        return fb
    if isinstance(v, str) and v.strip() in ("", "-", "–", "N/A"):
        return fb
    try:
        return float(v)
    except (TypeError, ValueError):
        return fb


def storage_rows(D: dict) -> list[dict]:
    rows = []
    eu = D.get("eu") or {}
    if eu.get("gasDayStart"):
        rows.append({
            "country": "EU", "gas_day": eu["gasDayStart"],
            "fill": _sf(eu.get("full")), "gas_twh": _sf(eu.get("gasInStorage")),
            "wgv_twh": _sf(eu.get("workingGasVolume")),
            "injection": _sf(eu.get("injection")), "withdrawal": _sf(eu.get("withdrawal")),
            "trend": _sf(eu.get("trend")), "source": "agsi",
        })
    for c in D.get("countries", []):
        if not c.get("gasDayStart"):
            continue
        rows.append({
            "country": c.get("_c"), "gas_day": c["gasDayStart"],
            "fill": _sf(c.get("full")), "gas_twh": _sf(c.get("gasInStorage")),
            "wgv_twh": _sf(c.get("workingGasVolume")),
            "injection": _sf(c.get("injection")), "withdrawal": _sf(c.get("withdrawal")),
            "trend": _sf(c.get("trend")), "source": c.get("_source", "agsi"),
        })
    return rows


def lng_rows(D: dict) -> list[dict]:
    rows = []
    el = D.get("eu_lng") or {}
    inv = el.get("inventory")
    if isinstance(inv, dict):
        inv = inv.get("lng") or inv.get("gwh")
    if el.get("gasDayStart"):
        rows.append({
            "country": "EU", "gas_day": el["gasDayStart"],
            "send_out": _sf(el.get("sendOut")), "inventory": _sf(inv),
            "dtrs": _sf(el.get("dtrs")), "fullness": _sf(el.get("full")),
        })
    for l in D.get("lng", []):
        if not l.get("gasDayStart"):
            continue
        inv = l.get("inventory")
        if isinstance(inv, dict):
            inv = inv.get("lng") or inv.get("gwh")
        rows.append({
            "country": l.get("_c"), "gas_day": l["gasDayStart"],
            "send_out": _sf(l.get("sendOut")), "inventory": _sf(inv),
            "dtrs": _sf(l.get("dtrs")), "fullness": _sf(l.get("full")),
        })
    return rows


def seasonality_fill_rows(D: dict) -> list[dict]:
    """Lift D['s_*'] seasonality history into storage fill rows.

    Region key 'eu' -> country 'EU'; ISO2 region keys -> uppercase country.
    """
    rows = []
    for key, series in D.items():
        if not key.startswith("s_") or not isinstance(series, dict):
            continue
        region = key[2:]
        country = "EU" if region == "eu" else region.upper()
        for _year, entries in series.items():
            for e in entries or []:
                if e.get("d") and e.get("f") is not None:
                    rows.append({"country": country, "gas_day": e["d"],
                                 "fill": _sf(e.get("f"))})
    return rows


def weather_rows(D: dict, run_date: str) -> list[dict]:
    rows = []
    for cc, w in (D.get("weather") or {}).items():
        dates = w.get("dates") or []
        temps = w.get("avg_temp") or []
        hdds = w.get("avg_hdd") or []
        for i, day in enumerate(dates):
            rows.append({
                "country": cc, "forecast_day": day, "run_date": run_date,
                "temp_mean": temps[i] if i < len(temps) else None,
                "hdd": hdds[i] if i < len(hdds) else None,
            })
    return rows


def persist_snapshot(conn, D: dict, run_date: str) -> dict:
    """Write a full fetch_all() result into the store. Returns row counts."""
    from . import store
    # seasonality first (fill-only), then richer snapshot overwrites today's row
    n_season = store.upsert_storage_fill(conn, seasonality_fill_rows(D))
    n_stor = store.upsert_storage(conn, storage_rows(D))
    n_lng = store.upsert_lng(conn, lng_rows(D))
    n_wx = store.upsert_weather(conn, weather_rows(D, run_date))
    return {"seasonality_fill": n_season, "storage": n_stor,
            "lng": n_lng, "weather": n_wx}
