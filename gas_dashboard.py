#!/usr/bin/env python3
"""
European Gas Intelligence Dashboard v6
=======================================
Fetches live data from GIE AGSI (storage), GIE ALSI (LNG), ENTSOG
Transparency Platform, and National Gas (UK) REST API, then generates
a self-contained HTML dashboard.

Key API notes (confirmed working):
  - AGSI/ALSI: ?type=eu for EU aggregate, ?country=XX (uppercase) for countries
  - Pagination: &page=N&size=300 (max 300 per page)
  - Date ranges: &from=YYYY-MM-DD&to=YYYY-MM-DD
  - Rate limit: 60 calls/minute, 60s lockout if exceeded
  - API key in x-key header
  - GB storage: National Gas REST API (primary) → AGSI NE-UA (fallback)
  - National Gas: POST to /publications/gasday, GET /instantaneousflow/sites
  - National Gas: no auth required, JSON, daily refresh for most items
  - ALSI inventory field is an array {lng, gwh} not a string
  - ENTSOG: public, no key, ?limit=-1 for all records, values in kWh/d

Usage:  python gas_dashboard.py
        python gas_dashboard.py --rebuild   (clear cache, full refetch)
Requires: pip install requests
"""

import json, os, sys, time, webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

_env = Path('/opt/scripts/.env')
load_dotenv(_env if _env.exists() else None)

try:
    import requests
except ImportError:
    print("Missing 'requests'. Run:  pip install requests"); sys.exit(1)

# ─── CONFIG ──────────────────────────────────────────────────
AGSI_KEY = os.getenv("AGSI_KEY", "")
AGSI_BASE = "https://agsi.gie.eu/api"
ALSI_BASE = "https://alsi.gie.eu/api"
ENTSOG_BASE = "https://transparency.entsog.eu/api/v1"
NATGAS_BASE = "https://api.nationalgas.com/operationaldata/v1"
OUTPUT_FILE = "gas_dashboard.html"
CACHE_FILE = "gas_cache.json"
NATGAS_CATALOGUE_CACHE = "natgas_catalogue.json"

# Full country list with correct names — single source of truth lives in
# gasintel.config (de-duplicated; the two copies had started to diverge).
# Note: Ireland (IE) has no underground gas storage and is not on AGSI.
#       Serbia (RS) does not report to GIE AGSI.
#       GB returns dashes at country-level — handled via facility-level aggregation.
from gasintel.config import COUNTRIES, CORRIDORS as _CORRIDOR_META

LNG_CC = ["be", "es", "fr", "de", "gr", "it", "lt", "nl", "pl", "pt", "gb", "hr", "fi"]

# Extended country info for LNG-only countries (no AGSI storage but have ALSI terminals)
ALL_COUNTRIES = {c[0]: c for c in COUNTRIES}
ALL_COUNTRIES.update({
    "gr": ("gr", "GR", "Greece",    "🇬🇷"),
    "lt": ("lt", "LT", "Lithuania", "🇱🇹"),
    "fi": ("fi", "FI", "Finland",   "🇫🇮"),
    "ie": ("ie", "IE", "Ireland",   "🇮🇪"),
    "rs": ("rs", "RS", "Serbia",    "🇷🇸"),
})

SEASON_REGIONS = [("eu", "EU Aggregate")] + [(lc, nm) for lc, uc, nm, fl in COUNTRIES]

TWH_TO_BCM = 1 / 10.55
GWH_TO_MCM = 0.0948

HEADERS = {"x-key": AGSI_KEY}


def sf(v, fb=0):
    """Safe float conversion."""
    if v is None: return fb
    if isinstance(v, str) and v.strip() in ("", "-", "–", "N/A"): return fb
    try: return float(v)
    except: return fb


def sfmt(v, fmt=".2f", fb="–"):
    """Safe format."""
    if v is None: return fb
    try: return f"{float(v):{fmt}}"
    except: return fb


def fetch(url, headers=None, params=None, label="", retries=3):
    """Fetch with retries and rate-limit awareness."""
    for a in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=60)
            if r.status_code == 429:
                print(f"  ⚠ {label} rate limited — waiting 65s...")
                time.sleep(65)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (502, 503, 504):
                wait = 15 * (a + 1)
                print(f"  ⚠ {label} HTTP {r.status_code} — gateway error, waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"  ⚠ {label} HTTP {r.status_code}" + (f" (retry {a+1})" if a else ""))
        except requests.exceptions.Timeout:
            print(f"  ⏱ {label} timeout" + (f" (retry {a+1})" if a else ""))
        except Exception as e:
            print(f"  ✗ {label} {e}" + (f" (retry {a+1})" if a else ""))
        if a < retries - 1:
            time.sleep(2 * (a + 1))
    return None


def agsi(params, label="AGSI"):
    return fetch(AGSI_BASE, HEADERS, params, label)


def alsi_f(params, label="ALSI"):
    return fetch(ALSI_BASE, HEADERS, params, label)


def entsog(ep, params, label="ENTSOG"):
    return fetch(f"{ENTSOG_BASE}/{ep}", params=params, label=label)


def natgas_post(endpoint, body, label="NatGas"):
    """POST to National Gas REST API (no auth required)."""
    url = f"{NATGAS_BASE}/{endpoint}"
    for a in range(3):
        try:
            r = requests.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=45)
            if r.status_code == 200:
                return r.json()
            err_detail = ""
            try:
                err_detail = f" — {r.text[:200]}"
            except:
                pass
            print(f"  ⚠ {label} HTTP {r.status_code}{err_detail}" + (f" (retry {a+1})" if a else ""))
        except requests.exceptions.Timeout:
            print(f"  ⏱ {label} timeout" + (f" (retry {a+1})" if a else ""))
        except Exception as e:
            print(f"  ✗ {label} {e}" + (f" (retry {a+1})" if a else ""))
        if a < 2:
            time.sleep(2 * (a + 1))
    return None


def natgas_get(endpoint, label="NatGas"):
    """GET from National Gas REST API (no auth required)."""
    url = f"{NATGAS_BASE}/{endpoint}"
    return fetch(url, label=label)


def fetch_ttf_price():
    """Fetch TTF natural gas front-month futures price from Yahoo Finance."""
    print("\n💰 TTF gas price...")
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/TTF=F"
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        r = requests.get(url, headers=hdrs, params={"range": "30d", "interval": "1d"}, timeout=15)
        if r.status_code == 200:
            d = r.json()
            res = d.get("chart", {}).get("result", [{}])[0]
            meta = res.get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            ts = res.get("timestamp", [])
            closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            hist = [{"t": t, "p": round(c, 2)} for t, c in zip(ts, closes) if c is not None]
            chg = round((price - prev) / prev * 100, 2) if price and prev and prev > 0 else None
            chg_abs = round(price - prev, 2) if price and prev else None
            if price:
                print(f"  ✓ TTF: {price:.2f} EUR/MWh" + (f" ({chg:+.1f}%)" if chg else ""))
            else:
                print("  ⚠ TTF: no price in response")
            return {"price": round(price, 2) if price else None,
                    "prev": round(prev, 2) if prev else None,
                    "chg": chg, "chg_abs": chg_abs,
                    "hist": hist[-30:]}
        else:
            print(f"  ⚠ TTF HTTP {r.status_code}")
    except Exception as e:
        print(f"  ⚠ TTF price fetch failed: {e}")
    return {}


def fetch_natgas_catalogue():
    """
    Fetch and cache the National Gas publication catalogue.
    Returns a dict mapping lowercased publication names to their IDs.
    """
    cache_path = Path(__file__).parent / NATGAS_CATALOGUE_CACHE
    # Use cached catalogue if less than 24h old
    if cache_path.exists():
        try:
            age_h = (time.time() - cache_path.stat().st_mtime) / 3600
            if age_h < 24:
                cat = json.loads(cache_path.read_text(encoding="utf-8"))
                if cat:
                    print(f"  ✓ National Gas catalogue: cached ({len(cat)} items)")
                    return cat
        except:
            pass

    print("  📋 Fetching National Gas catalogue...")
    raw = natgas_get("publications/catalogue", "NG Catalogue")
    if not raw or not isinstance(raw, dict):
        print("  ⚠ Catalogue fetch failed")
        # Try loading stale cache
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except:
                pass
        return {}

    # Flatten the nested catalogue into {name_lower: {id, name}} 
    cat = {}
    data = raw.get("data", [])
    for category in data:
        for sub in category.get("subCategory", []):
            for entry in sub.get("catalogueEntries", []):
                name = entry.get("name", "")
                pub_id = entry.get("publicationId", "")
                if name and pub_id:
                    cat[name.lower()] = {"id": pub_id, "name": name}

    if cat:
        try:
            cache_path.write_text(json.dumps(cat), encoding="utf-8")
        except:
            pass
        print(f"  ✓ Catalogue: {len(cat)} items discovered")
    return cat


# ─── National Gas storage publication ID mapping ──────────────
# Confirmed from catalogue dump (747 items, March 2026).
# Hardcoded IDs are authoritative; catalogue discovery is backup.
NATGAS_STORAGE_PUBS = {
    # Aggregate storage (D+1 = end-of-day reports)
    "storage_stocks":      ("storage, daily aggregated stock level, d+1",   "PUBOBJ330"),
    "storage_inflows":     ("storage, daily aggregated inflows, d+1",       "PUBOBJ331"),
    "storage_outflows":    ("storage, daily aggregated outflows, d+1",      "PUBOBJ332"),
    "storage_avail_cap":   ("storage, daily aggregated available capacity, d+1", "PUBOBJ333"),
    "storage_inj_actual":  ("storage injection, actual",                    "PUBOB4423"),
    "storage_wdr_actual":  ("storage withdrawal, actual",                   "PUBOB4424"),
    # Combined stock levels
    "storage_med_long":    ("storage, medium and long range, stock levels", "PUBOBJ2350"),
    "storage_med_stock":   ("storage, medium range, stock levels",          "PUBOB1558"),
    "storage_long_stock":  ("storage, long range, stock levels",            "PUBOB1565"),
    "storage_short_stock": ("storage, short range, stock levels",           "PUBOB1551"),
    # Days of supply
    "storage_long_days":   ("storage, long range, days left at ave flow",   "PUBOB1570"),
    "storage_med_days":    ("storage, medium range, days left at ave flow", "PUBOB1563"),
    # System-level
    "demand_forecast":     ("demand forecast, nts, hourly update",          "PUBOB28"),
    "linepack_open":       ("linepack, opening",                            "PUBOBJ0079"),
    "linepack_close":      ("linepack, closing",                            "PUBOBJ0080"),
}

# Per-facility opening stock publication IDs (confirmed from catalogue)
NATGAS_FACILITY_PUBS = {
    "Aldbrough":      "PUBOBJ2367",
    "Rough":          "PUBOBJ2364",
    "Stublach":       "PUBOBJ2370",
    "Holford":        "PUBOBJ2368",
    "Hornsea":        "PUBOBJ2362",
    "Humbly Grove":   "PUBOBJ2361",
    "Hatfield Moor":  "PUBOBJ2365",
    "Hill Top":       "PUBOBJ2369",
    "Holehouse Farm": "PUBOBJ2366",
}

# Per-facility inflow (injection) and outflow (withdrawal) IDs
NATGAS_FACILITY_INFLOW = {
    "Aldbrough": "PUBOBJ2407", "Rough": "PUBOBJ2404", "Stublach": "PUBOBJ2410",
    "Holford": "PUBOBJ2408", "Hornsea": "PUBOBJ2402", "Humbly Grove": "PUBOBJ2401",
    "Hatfield Moor": "PUBOBJ2405", "Hill Top": "PUBOBJ2409", "Holehouse Farm": "PUBOBJ2406",
}
NATGAS_FACILITY_OUTFLOW = {
    "Aldbrough": "PUBOBJ2419", "Rough": "PUBOBJ2416", "Stublach": "PUBOBJ2422",
    "Holford": "PUBOBJ2420", "Hornsea": "PUBOBJ2414", "Humbly Grove": "PUBOBJ2413",
    "Hatfield Moor": "PUBOBJ2417", "Hill Top": "PUBOBJ2421", "Holehouse Farm": "PUBOBJ2418",
}


def resolve_natgas_pub_ids(catalogue):
    """
    Resolve storage publication IDs from the catalogue.
    Returns a dict of {our_key: publication_id}.
    """
    resolved = {}
    for key, (name_pattern, fallback_id) in NATGAS_STORAGE_PUBS.items():
        pattern = name_pattern.lower()
        # Try exact match first
        if pattern in catalogue:
            resolved[key] = catalogue[pattern]["id"]
        else:
            # Fuzzy: find entries containing all words in the pattern
            words = pattern.split()
            for cat_name, cat_entry in catalogue.items():
                if all(w in cat_name for w in words):
                    resolved[key] = cat_entry["id"]
                    break
            else:
                if fallback_id:
                    resolved[key] = fallback_id
    return resolved


def fetch_gb_nationalgas():
    """
    Fetch GB storage data from National Gas REST API (primary source).
    
    Uses hardcoded publication IDs (confirmed from catalogue) to fetch
    aggregate storage stocks, injection, withdrawal, and per-facility
    opening stocks in a single POST request.
    
    Returns an AGSI-compatible dict for drop-in replacement, or None on failure.
    """
    print("  🇬🇧 National Gas (primary)...")
    
    # Build list of all IDs to fetch in one request
    fetch_ids = []
    id_to_key = {}
    
    # Core aggregate storage IDs — ONLY confirmed working from test_natgas.py
    # Excluded: linepack_open/close (PUBOBJ0079/0080 — unverified, cause 400)
    # Excluded: storage_long_days/med_days (PUBOB1570/1563 — untested)
    core_keys = ["storage_stocks", "storage_avail_cap",
                 "storage_inflows", "storage_outflows",
                 "storage_med_long", "storage_med_stock", "storage_long_stock"]
    for key in core_keys:
        _, pid = NATGAS_STORAGE_PUBS.get(key, (None, None))
        if pid:
            fetch_ids.append(pid)
            id_to_key[pid] = key
    
    # Per-facility opening stock IDs
    fac_id_to_name = {}
    for fac_name, pid in NATGAS_FACILITY_PUBS.items():
        fetch_ids.append(pid)
        fac_id_to_name[pid] = fac_name
    
    # Per-facility inflow/outflow IDs (for injection/withdrawal)
    inflow_id_to_name = {}
    outflow_id_to_name = {}
    for fac_name, pid in NATGAS_FACILITY_INFLOW.items():
        fetch_ids.append(pid)
        inflow_id_to_name[pid] = fac_name
    for fac_name, pid in NATGAS_FACILITY_OUTFLOW.items():
        fetch_ids.append(pid)
        outflow_id_to_name[pid] = fac_name
    
    print(f"    Requesting {len(fetch_ids)} publication IDs...")
    
    today = datetime.now().strftime("%Y-%m-%d")
    three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    
    body = {
        "fromDate": three_days_ago,
        "toDate": today,
        "publicationIds": fetch_ids,
        "latestValue": "Y"
    }
    
    data = natgas_post("publications/gasday", body, "NG Storage")
    if not data or not isinstance(data, list):
        print("    ⚠ No data from National Gas")
        return None
    
    # Parse results
    values = {}
    fac_values = {}
    fac_inflows = {}
    fac_outflows = {}
    gas_day = None
    
    for item in data:
        pub_id = item.get("publicationId", "")
        pub_name = item.get("publicationName", "")
        pubs = item.get("publications", [])
        if not pubs:
            continue
        val = sf(pubs[0].get("value"))
        applicable_for = pubs[0].get("applicableFor", "")
        if not gas_day and applicable_for:
            gas_day = applicable_for
        
        if pub_id in id_to_key:
            key = id_to_key[pub_id]
            values[key] = val
            print(f"    ✓ {key}: {val/1e9:.3f} TWh — {pub_name}")
        
        if pub_id in fac_id_to_name:
            fac_values[fac_id_to_name[pub_id]] = val
        if pub_id in inflow_id_to_name:
            fac_inflows[inflow_id_to_name[pub_id]] = val
        if pub_id in outflow_id_to_name:
            fac_outflows[outflow_id_to_name[pub_id]] = val
    
    # Determine total stocks (values from National Gas are in kWh)
    # Confirmed: PUBOBJ330 = 8,334,511,463 kWh = 8.335 TWh (March 2026)
    stocks_kwh = values.get("storage_stocks", 0)
    if stocks_kwh <= 0:
        stocks_kwh = values.get("storage_med_long", 0)
    if stocks_kwh <= 0:
        stocks_kwh = (values.get("storage_med_stock", 0) +
                      values.get("storage_long_stock", 0))
    if stocks_kwh <= 0 and fac_values:
        stocks_kwh = sum(fac_values.values())
        print(f"    → Using sum of {len(fac_values)} facility stocks: {stocks_kwh/1e9:.3f} TWh")
    
    # Available capacity — dynamically compute WGV as stock + avail cap
    # Confirmed: PUBOBJ333 = 27,127,922,794 kWh = 27.128 TWh
    avail_cap_kwh = values.get("storage_avail_cap", 0)
    
    # Injection/withdrawal — prefer per-facility sums (more reliable),
    # fall back to D+1 aggregated inflows/outflows
    total_inflow_kwh = sum(fac_inflows.values()) if fac_inflows else 0
    total_outflow_kwh = sum(fac_outflows.values()) if fac_outflows else 0
    
    inj_kwh = total_inflow_kwh or values.get("storage_inflows", 0)
    wdr_kwh = total_outflow_kwh or values.get("storage_outflows", 0)
    
    if fac_inflows or fac_outflows:
        print(f"    ✓ Per-facility inflows: {total_inflow_kwh/1e6:.1f} GWh ({len(fac_inflows)} sites)")
        print(f"    ✓ Per-facility outflows: {total_outflow_kwh/1e6:.1f} GWh ({len(fac_outflows)} sites)")
    
    if stocks_kwh <= 0:
        print("    ⚠ No storage stocks data from any publication")
        return None
    
    # Convert from kWh to TWh/GWh (AGSI uses TWh for stocks, GWh/d for flows)
    gas_twh = stocks_kwh / 1e9   # kWh → TWh
    inj_gwh = inj_kwh / 1e6     # kWh → GWh (daily flow)
    wdr_gwh = wdr_kwh / 1e6
    
    # WGV = current stock + available capacity (both from National Gas D+1 aggregates)
    # This is the authoritative figure — no hardcoding needed
    if avail_cap_kwh > 0:
        wgv_twh = (stocks_kwh + avail_cap_kwh) / 1e9
    else:
        # Fallback: hardcoded approximate WGV if avail cap not available
        wgv_twh = 35.5  # approximate, based on March 2026 data
    
    fill_pct = (gas_twh / wgv_twh * 100) if wgv_twh > 0 else 0
    
    n_facs = len(fac_values) if fac_values else len(NATGAS_FACILITY_PUBS)
    
    print(f"    → GB (National Gas): {fill_pct:.1f}% | {gas_twh:.3f} TWh / {wgv_twh:.3f} TWh WGV")
    
    result = {
        "gasDayStart": gas_day or today,
        "gasInStorage": str(round(gas_twh, 2)),
        "workingGasVolume": str(round(wgv_twh, 2)),
        "injection": str(round(inj_gwh, 2)),
        "withdrawal": str(round(wdr_gwh, 2)),
        "full": str(round(fill_pct, 2)),
        "trend": "0",
        "_c": "GB",
        "_n": "United Kingdom",
        "_f": "🇬🇧",
        "_facilities": n_facs,
        "_source": "nationalgas",
        # Extra National Gas data (in original kWh for reference)
        "_ng_stocks_kwh": stocks_kwh,
        "_ng_avail_cap_kwh": avail_cap_kwh,
        "_ng_inj_kwh": inj_kwh,
        "_ng_wdr_kwh": wdr_kwh,
        "_ng_facilities": fac_values,
    }
    
    return result


def fetch_gb_seasonality_nationalgas(year, cache_entry=None):
    """
    Fetch GB storage seasonality from National Gas REST API.
    
    Uses the aggregated stock level (PUBOBJ330) and available capacity (PUBOBJ333)
    to compute daily fill % dynamically. WGV = stock + available capacity.
    
    Falls back to AGSI NE-UA method if National Gas fails.
    """
    today = datetime.now()
    cy = today.year
    fd = f"{year}-01-01"
    td = today.strftime("%Y-%m-%d") if year == cy else f"{year}-12-31"
    
    stocks_id = NATGAS_STORAGE_PUBS.get("storage_stocks", (None, None))[1]  # PUBOBJ330
    avail_id = NATGAS_STORAGE_PUBS.get("storage_avail_cap", (None, None))[1]  # PUBOBJ333
    
    if not stocks_id:
        print(f"    ⚠ No storage stocks ID — falling back to AGSI")
        return fetch_gb_seasonality_agsi(year, cache_entry)
    
    pub_ids = [stocks_id]
    if avail_id:
        pub_ids.append(avail_id)
    
    body = {
        "fromDate": fd,
        "toDate": td,
        "publicationIds": pub_ids,
        "latestValue": "N"  # get all values, not just latest
    }
    
    data = natgas_post("publications/gasday", body, f"NG Stocks {year}")
    if not data or not isinstance(data, list):
        print(f"    ⚠ National Gas seasonality failed — falling back to AGSI")
        return fetch_gb_seasonality_agsi(year, cache_entry)
    
    # Parse stock and available capacity by date
    stocks_by_date = {}
    avail_by_date = {}
    
    for item in data:
        pid = item.get("publicationId", "")
        for p in item.get("publications", []):
            d = p.get("applicableFor", "")
            v = sf(p.get("value"))
            if d and v >= 0:
                if pid == stocks_id:
                    stocks_by_date[d] = v
                elif pid == avail_id:
                    avail_by_date[d] = v
    
    if not stocks_by_date:
        print(f"    ⚠ No stock data — falling back to AGSI")
        return fetch_gb_seasonality_agsi(year, cache_entry)
    
    result = []
    for d, stock_kwh in sorted(stocks_by_date.items()):
        avail_kwh = avail_by_date.get(d, 0)
        wgv_kwh = stock_kwh + avail_kwh
        if wgv_kwh > 0:
            fill = stock_kwh / wgv_kwh * 100
        elif stock_kwh > 0:
            # No avail cap data — use fallback WGV
            fill = (stock_kwh / 1e9) / 35.5 * 100
        else:
            continue
        result.append({"d": d, "f": round(fill, 2)})
    
    if result:
        print(f"✓ {len(result)} days (National Gas)")
        return result
    
    print(f"    ⚠ No National Gas seasonality data — falling back to AGSI")
    return fetch_gb_seasonality_agsi(year, cache_entry)


def paginate(base, p_base, mx=12, delay=0.4, label=""):
    """Paginate through AGSI/ALSI API."""
    all_d = []
    for pg in range(1, mx + 1):
        p = {**p_base, "page": pg, "size": 300}
        d = fetch(base, HEADERS, p, f"{label} p{pg}")
        if d and d.get("data"):
            all_d.extend(d["data"])
            if pg >= d.get("last_page", 1):
                break
        else:
            break
        time.sleep(delay)
    return all_d


def fetch_gb_agsi_fallback(date_str=None):
    """
    FALLBACK: Fetch GB storage data from AGSI facility-level aggregation.
    
    Used when National Gas REST API is unavailable.
    AGSI returns dashes for ?country=GB at country level (post-Brexit).
    The /api/about?show=table endpoint returns a list of all SSOs and facilities.
    GB facilities have type="ASF" (Actual Storage Facility). SSO entries are
    operator-level aggregates that would double-count, so we skip those.
    
    Each facility entry has a full URL like:
      https://agsi.gie.eu/api?country=GB&company=XXX&facility=YYY
    We query each one and sum gasInStorage/workingGasVolume.
    
    Fallback: if facility discovery fails, use Non-EU aggregate minus Ukraine.
    """
    print("  🇬🇧 GB facility-level aggregation...")
    
    # Step 1: Discover GB facility URLs from the metadata endpoint
    about = fetch(AGSI_BASE + "/about", HEADERS, {"show": "table"}, "about?show=table")
    
    facility_urls = []
    if about and isinstance(about, list):
        for entry in about:
            if entry.get("country") == "GB" and entry.get("type") in ("ASF", ""):
                # ASF = Actual Storage Facility, "" also appears for facilities
                url = entry.get("url", "")
                name = entry.get("name", "?")
                if url and "facility=" in url:
                    facility_urls.append({"url": url, "name": name})
        print(f"    Found {len(facility_urls)} GB facilities")
    else:
        print("    ⚠ about/table failed or unexpected format")
    
    # Step 2: Query each facility and aggregate
    total_gas = 0
    total_wgv = 0
    total_inj = 0
    total_wdr = 0
    facility_count = 0
    gas_day = None
    
    for fac in facility_urls:
        url = fac["url"]
        params = {}
        if date_str:
            params["from"] = date_str
            params["to"] = date_str
        d = fetch(url, HEADERS, params, f"  {fac['name']}")
        if d and d.get("data"):
            rec = d["data"][0] if isinstance(d["data"], list) else d["data"]
            gas = sf(rec.get("gasInStorage"))
            wgv = sf(rec.get("workingGasVolume"))
            if gas > 0 or wgv > 0:
                total_gas += gas
                total_wgv += wgv
                total_inj += sf(rec.get("injection"))
                total_wdr += sf(rec.get("withdrawal"))
                facility_count += 1
                if not gas_day:
                    gas_day = rec.get("gasDayStart")
                print(f"    ✓ {fac['name']}: {rec.get('full', '?')}% ({gas:.2f} TWh)")
            else:
                print(f"    - {fac['name']}: no data")
        time.sleep(0.4)
    
    # Fallback: Non-EU aggregate minus Ukraine
    if facility_count == 0:
        print("    Fallback: NE aggregate minus UA...")
        ne = agsi({"type": "ne"}, "NE agg")
        ua = agsi({"country": "UA"}, "UA")
        if ne and ne.get("data") and ua and ua.get("data"):
            ne_rec = ne["data"][0]
            ua_rec = ua["data"][0]
            total_gas = sf(ne_rec.get("gasInStorage")) - sf(ua_rec.get("gasInStorage"))
            total_wgv = sf(ne_rec.get("workingGasVolume")) - sf(ua_rec.get("workingGasVolume"))
            total_inj = sf(ne_rec.get("injection")) - sf(ua_rec.get("injection"))
            total_wdr = sf(ne_rec.get("withdrawal")) - sf(ua_rec.get("withdrawal"))
            gas_day = ne_rec.get("gasDayStart")
            if total_gas > 0 and total_wgv > 0:
                facility_count = -1  # Mark as estimated
                fill = total_gas / total_wgv * 100
                print(f"    ✓ GB (NE-UA): {fill:.1f}% ({total_gas:.2f} TWh)")
            else:
                print(f"    ✗ NE-UA gave invalid result")
    
    if total_gas <= 0 and total_wgv <= 0:
        print("    ✗ Could not get GB data")
        return None
    
    fill_pct = (total_gas / total_wgv * 100) if total_wgv > 0 else 0
    method = f"{facility_count} facilities" if facility_count > 0 else "NE-UA"
    print(f"    → GB total ({method}): {fill_pct:.1f}% | {total_gas:.2f} TWh / {total_wgv:.2f} TWh")
    
    return {
        "gasDayStart": gas_day or datetime.now().strftime("%Y-%m-%d"),
        "gasInStorage": str(round(total_gas, 2)),
        "workingGasVolume": str(round(total_wgv, 2)),
        "injection": str(round(total_inj, 2)),
        "withdrawal": str(round(total_wdr, 2)),
        "full": str(round(fill_pct, 2)),
        "trend": "0",
        "_c": "GB",
        "_n": "United Kingdom",
        "_f": "🇬🇧",
        "_facilities": abs(facility_count),
    }


def fetch_gb_seasonality_agsi(year, cache_entry=None):
    """
    FALLBACK: Fetch GB seasonality data from AGSI using NE-UA method.
    
    For historical fill%, querying each facility individually with date ranges
    would be too slow (5 facilities × 365 days). Instead we use:
      NE (Non-EU) aggregate minus UA (Ukraine) = GB
    
    Both NE and UA return proper daily data via the standard paginated API.
    """
    today = datetime.now()
    cy = today.year
    fd = f"{year}-01-01"
    td = today.strftime("%Y-%m-%d") if year == cy else f"{year}-12-31"

    # Fetch NE aggregate for the year
    ne_data = paginate(AGSI_BASE, {"type": "ne", "from": fd, "to": td},
                       mx=6, delay=1.1, label=f"NE {year}")
    if not ne_data:
        return cache_entry or []

    # Fetch UA for the same period
    ua_data = paginate(AGSI_BASE, {"country": "UA", "from": fd, "to": td},
                       mx=6, delay=1.1, label=f"UA {year}")

    # Index UA by date for fast lookup
    ua_by_date = {}
    if ua_data:
        for r in ua_data:
            d = r.get("gasDayStart")
            if d:
                ua_by_date[d] = {
                    "gas": sf(r.get("gasInStorage")),
                    "wgv": sf(r.get("workingGasVolume"))
                }

    # Subtract UA from NE to get GB
    result = []
    for r in ne_data:
        d = r.get("gasDayStart")
        if not d:
            continue
        ne_gas = sf(r.get("gasInStorage"))
        ne_wgv = sf(r.get("workingGasVolume"))
        ua = ua_by_date.get(d, {"gas": 0, "wgv": 0})
        gb_gas = ne_gas - ua["gas"]
        gb_wgv = ne_wgv - ua["wgv"]
        if gb_wgv > 0:
            fill = gb_gas / gb_wgv * 100
            result.append({"d": d, "f": round(fill, 2)})

    if result:
        print(f"✓ {len(result)} days (NE-UA method)")
        return result
    else:
        return cache_entry or []


def fetch_all():
    D = {}
    today = datetime.now().strftime("%Y-%m-%d")
    cy = datetime.now().year

    # ─── EU AGGREGATE ───
    print("\n📦 EU storage aggregate...")
    eu = agsi({"type": "eu"}, "EU agg")
    D["eu"] = eu["data"][0] if eu and eu.get("data") else {}
    if D["eu"]:
        print(f"  ✓ EU: {D['eu'].get('full', '?')}% | {D['eu'].get('gasInStorage', '?')} TWh")
    else:
        print("  ✗ No EU data")

    D["ttf"] = fetch_ttf_price()

    # ─── COUNTRIES ───
    print("\n🗺  Countries...")
    cc = []

    def _fetch_one_country(args):
        lc, uc, nm, fl = args
        if lc == "gb":
            gb_data = fetch_gb_nationalgas()
            if not gb_data:
                print("    ⚠ National Gas failed — trying AGSI facility aggregation...")
                gb_data = fetch_gb_agsi_fallback()
            if gb_data:
                return gb_data
            else:
                print(f"  ✗ {fl} {nm} (no data from any source)")
                return None
        else:
            d = agsi({"country": uc}, f"AGSI {uc}")
            if d and d.get("data") and d["data"]:
                e = d["data"][0]
                e["_c"] = uc
                e["_n"] = nm
                e["_f"] = fl
                print(f"  ✓ {fl} {nm}: {e.get('full', '?')}%")
                return e
            else:
                print(f"  ✗ {fl} {nm}")
                return None

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fetch_one_country, row): row for row in COUNTRIES}
        results = []
        for fut in as_completed(futures):
            entry = fut.result()
            if entry is not None:
                results.append(entry)
        cc = results

    D["countries"] = sorted(cc, key=lambda x: sf(x.get("gasInStorage")), reverse=True)

    # ─── SEASONALITY WITH CACHING ───
    print("\n📈 Seasonality...")
    cache_path = Path(__file__).parent / CACHE_FILE
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"  ✓ Loaded cache ({cache_path.name})")
        except:
            print("  ⚠ Cache corrupted, rebuilding")
            cache = {}

    yrs = list(range(cy, cy - 6, -1))
    total_regions = len(SEASON_REGIONS)

    for idx, (rk, rn) in enumerate(SEASON_REGIONS):
        cache_key = f"s_{rk}"
        existing = cache.get(cache_key, {})
        print(f"\n  [{idx + 1}/{total_regions}] {rn}:")

        s = {}
        is_gb = (rk == "gb")

        for y in yrs:
            y_str = str(y)
            fd = f"{y}-01-01"
            td = today if y == cy else f"{y}-12-31"

            # Check cache
            if y < cy and y_str in existing and len(existing[y_str]) >= 300:
                s[y] = existing[y_str]
                print(f"    {y}: cached ({len(s[y])} days)")
                continue

            if y == cy and y_str in existing and len(existing[y_str]) > 0:
                cached_dates = {e["d"] for e in existing[y_str]}
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                if yesterday in cached_dates:
                    s[y] = existing[y_str]
                    recent_from = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
                    print(f"    {y}: cached ({len(s[y])} days), refreshing last 3 days...", end=" ", flush=True)

                    if is_gb:
                        # GB: National Gas primary, AGSI NE-UA fallback for recent days
                        new_data = fetch_gb_seasonality_nationalgas(y, existing[y_str])
                        if new_data:
                            # Merge recent
                            merged = {e["d"]: e["f"] for e in s[y]}
                            for e in new_data:
                                if e["d"] >= recent_from:
                                    merged[e["d"]] = e["f"]
                            s[y] = [{"d": d, "f": f} for d, f in sorted(merged.items())]
                        print(f"✓ now {len(s[y])} days")
                    else:
                        p = {"type": "eu", "from": recent_from, "to": td} if rk == "eu" else {"country": rk.upper(), "from": recent_from, "to": td}
                        recs = paginate(AGSI_BASE, p, mx=1, delay=0.3, label=f"{rn} {y} update")
                        if recs:
                            new_entries = {r["gasDayStart"]: sf(r.get("full")) for r in recs if r.get("gasDayStart")}
                            merged = {e["d"]: e["f"] for e in s[y]}
                            merged.update(new_entries)
                            s[y] = [{"d": d, "f": f} for d, f in sorted(merged.items())]
                            print(f"✓ now {len(s[y])} days")
                        else:
                            print("✓ (no new)")
                    time.sleep(0.3)
                    continue

            # Full fetch needed
            print(f"    {y}: fetching...", end=" ", flush=True)

            if is_gb:
                result = fetch_gb_seasonality_nationalgas(y, existing.get(y_str))
                if result:
                    s[y] = result
                    print(f"✓ {len(s[y])} days")
                else:
                    print("✗")
                    if y_str in existing:
                        s[y] = existing[y_str]
                        print(f"      (keeping cached: {len(s[y])} days)")
            else:
                p = {"type": "eu", "from": fd, "to": td} if rk == "eu" else {"country": rk.upper(), "from": fd, "to": td}
                recs = paginate(AGSI_BASE, p, mx=6, delay=1.1, label=f"{rn} {y}")
                if recs:
                    s[y] = [{"d": r["gasDayStart"], "f": sf(r.get("full"))} for r in recs if r.get("gasDayStart")]
                    print(f"✓ {len(s[y])} days")
                else:
                    print("✗")
                    if y_str in existing:
                        s[y] = existing[y_str]
                        print(f"      (keeping cached: {len(s[y])} days)")
            time.sleep(1.5)

        D[f"s_{rk}"] = s
        cache[cache_key] = {str(k): v for k, v in s.items()}

    # Save cache
    try:
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        print(f"\n  💾 Cache saved ({cache_path.name})")
    except Exception as e:
        print(f"\n  ⚠ Could not save cache: {e}")

    # ─── LNG TERMINALS ───
    print("\n🚢 LNG terminals...")
    lng = []

    def _fetch_one_lng(lc):
        ci = ALL_COUNTRIES.get(lc, (lc, lc.upper(), lc.upper(), "🏳️"))
        d = alsi_f({"country": lc.upper()}, f"ALSI {lc.upper()}")
        if d and d.get("data") and d["data"]:
            e = d["data"][0]
            e["_c"] = ci[1]
            e["_n"] = ci[2]
            e["_f"] = ci[3]
            inv = e.get("inventory")
            if isinstance(inv, dict):
                e["inventory"] = inv.get("lng") or inv.get("gwh") or "0"
            print(f"  ✓ {ci[3]} {ci[2]}: {e.get('sendOut', '?')} GWh/d")
            return e
        else:
            print(f"  ✗ {ci[3]} {ci[2]}")
            return None

    with ThreadPoolExecutor(max_workers=5) as ex:
        lng_futures = {ex.submit(_fetch_one_lng, lc): lc for lc in LNG_CC}
        lng_results = []
        for fut in as_completed(lng_futures):
            entry = fut.result()
            if entry is not None:
                lng_results.append(entry)
        lng = lng_results

    D["lng"] = sorted(lng, key=lambda x: sf(x.get("sendOut")), reverse=True)

    el = alsi_f({"type": "eu"}, "ALSI EU")
    D["eu_lng"] = el["data"][0] if el and el.get("data") else {}

    # ─── ENTSOG FLOWS ───
    print("\n🔀 ENTSOG flows...")
    f7 = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    ed = entsog("operationaldatas",
                {"indicator": "Physical Flow", "periodType": "day",
                 "from": f7, "to": today, "limit": -1},
                "ENTSOG")
    raw = []
    if ed:
        raw = ed.get("operationaldatas", ed.get("operationaldata", []))
        if not isinstance(raw, list):
            raw = []
        print(f"  ✓ {len(raw)} records")
    else:
        print("  ✗ Failed")

    # Group records per (point, direction) and per gas-day. The old code keyed
    # by point only (mixing entry+exit values into one bucket) and took v[0] as
    # "latest" — ENTSOG does not return records date-ordered, so that was an
    # arbitrary day in the window. Multiple operators can report the same
    # point/day; we average them per day to keep one comparable daily value.
    pts = {}
    for f in raw:
        k = f.get("pointLabel", f.get("pointKey", ""))
        day = (f.get("periodFrom") or "")[:10]
        if not k or not day:
            continue
        try:
            v = float(f["value"])
        except:
            continue
        dirn = f.get("directionKey", "")
        pk = (k, dirn)
        if pk not in pts:
            pts[pk] = {"l": f.get("pointLabel", k), "o": f.get("operatorLabel", ""),
                       "d": dirn, "u": f.get("unit", "kWh/d"), "days": {}}
        pts[pk]["days"].setdefault(day, []).append(v)

    # Display labels come from the shared corridor map in gasintel.config —
    # the stale local copy (which still said "South Stream") is gone.
    CORRIDORS = {kw: meta[0] for kw, meta in _CORRIDOR_META.items()}

    fl = []
    for p in pts.values():
        if not p["days"]:
            continue
        daily = sorted((day, sum(vs) / len(vs)) for day, vs in p["days"].items())
        latest = daily[-1][1]                                # true latest gas-day
        avg = sum(v for _, v in daily) / len(daily)          # mean of daily values
        unit_out = p["u"]
        if "kWh/d" in p["u"]:
            latest = latest / 1e6
            avg = avg / 1e6
            unit_out = "GWh/d"
        corridor = ""
        for keyword, label in CORRIDORS.items():
            if keyword.lower() in p["l"].lower():
                corridor = label
                break
        fl.append({"label": p["l"], "operator": p["o"], "direction": p["d"],
                    "unit": unit_out, "latest": latest, "avg": avg,
                    "corridor": corridor, "latest_day": daily[-1][0]})
    D["flows"] = sorted(fl, key=lambda x: x["latest"], reverse=True)[:40]
    print(f"  ✓ {len(D['flows'])} points")

    # ─── NATIONAL GAS (UK) — supplement with instantaneous flows ───
    print("\n🇬🇧 National Gas instantaneous flows...")
    ng_data = {}
    # Carry over any National Gas data already fetched for GB storage
    gb_entry = next((c for c in cc if c.get("_c") == "GB"), None)
    if gb_entry and gb_entry.get("_source") == "nationalgas":
        ng_data["storage_stocks_kwh"] = gb_entry.get("_ng_stocks_kwh", 0)
        ng_data["storage_avail_cap_kwh"] = gb_entry.get("_ng_avail_cap_kwh", 0)
        ng_data["storage_injection_kwh"] = gb_entry.get("_ng_inj_kwh", 0)
        ng_data["storage_withdrawal_kwh"] = gb_entry.get("_ng_wdr_kwh", 0)
        ng_data["facility_stocks"] = gb_entry.get("_ng_facilities", {})
        ng_data["source"] = "nationalgas"
    try:
        # Instantaneous flows — real-time entry point data
        ng_flows = fetch("https://api.nationalgas.com/operationaldata/v1/instantaneousflow/sites",
                         label="NG Instant Flow", retries=2)
        if ng_flows and isinstance(ng_flows, dict):
            ng_sites = {}
            for group in ng_flows.get("instantaneousFlow", []):
                for site in group.get("sites", []):
                    name = site.get("siteName", "")
                    details = site.get("siteGasDetail", [])
                    if details:
                        latest = details[0]
                        flow = sf(latest.get("flowRate"))
                        ng_sites[name] = flow
            ng_data["sites"] = ng_sites
            ng_data["gasDay"] = ng_flows.get("currentGasDay", "")
            total_supply = sum(v for v in ng_sites.values() if v > 0)
            print(f"  ✓ {len(ng_sites)} sites, total supply: {total_supply:.1f} mcm/d")
        else:
            print(f"  ⚠ Instantaneous flow: unexpected response")

    except Exception as e:
        print(f"  ⚠ National Gas fetch failed: {e}")

    D["ng"] = ng_data

    # ─── WEATHER / HEATING DEGREE DAYS ───
    print("\n🌡  Weather forecast (Open-Meteo)...")
    # Major population-weighted cities per country for HDD estimation
    WEATHER_CITIES = {
        "DE": [("Berlin", 52.52, 13.41), ("Munich", 48.14, 11.58)],
        "FR": [("Paris", 48.86, 2.35), ("Lyon", 45.76, 4.84)],
        "IT": [("Rome", 41.90, 12.50), ("Milan", 45.46, 9.19)],
        "NL": [("Amsterdam", 52.37, 4.90)],
        "AT": [("Vienna", 48.21, 16.37)],
        "PL": [("Warsaw", 52.23, 21.01)],
        "ES": [("Madrid", 40.42, -3.70)],
        "BE": [("Brussels", 50.85, 4.35)],
        "CZ": [("Prague", 50.08, 14.44)],
        "HU": [("Budapest", 47.50, 19.04)],
        "SK": [("Bratislava", 48.15, 17.11)],
        "RO": [("Bucharest", 44.43, 26.10)],
        "BG": [("Sofia", 42.70, 23.32)],
        "GB": [("London", 51.51, -0.13)],
        "DK": [("Copenhagen", 55.68, 12.57)],
        "HR": [("Zagreb", 45.81, 15.98)],
        "LV": [("Riga", 56.95, 24.11)],
        "SE": [("Stockholm", 59.33, 18.07)],
        "UA": [("Kyiv", 50.45, 30.52)],
        "PT": [("Lisbon", 38.72, -9.14)],
    }
    HDD_BASE = 15.5  # base temperature for heating degree days

    weather = {}
    # Batch all cities into one API call (Open-Meteo supports multi-location)
    all_lats = []
    all_lons = []
    city_map = []  # (country_code, city_name, index)
    for cc_code, cities in WEATHER_CITIES.items():
        for cname, lat, lon in cities:
            all_lats.append(str(lat))
            all_lons.append(str(lon))
            city_map.append((cc_code, cname))

    try:
        wx_url = "https://api.open-meteo.com/v1/forecast"
        wx_params = {
            "latitude": ",".join(all_lats),
            "longitude": ",".join(all_lons),
            "daily": "temperature_2m_mean",
            "timezone": "Europe/Berlin",
            "forecast_days": 7,
        }
        wx = fetch(wx_url, params=wx_params, label="Open-Meteo", retries=2)
        if wx and isinstance(wx, list):
            for i, city_wx in enumerate(wx):
                cc_code, cname = city_map[i]
                daily = city_wx.get("daily", {})
                temps = daily.get("temperature_2m_mean", [])
                dates = daily.get("time", [])
                if temps and dates:
                    hdds = [max(0, HDD_BASE - t) for t in temps if t is not None]
                    avg_temp = sum(t for t in temps if t is not None) / len([t for t in temps if t is not None]) if temps else None
                    if cc_code not in weather:
                        weather[cc_code] = {"cities": [], "hdds": [], "temps": [], "dates": dates}
                    weather[cc_code]["cities"].append(cname)
                    weather[cc_code]["hdds"].append(hdds)
                    weather[cc_code]["temps"].append(temps)
            # Average HDD across cities per country
            for cc_code in weather:
                w = weather[cc_code]
                n_cities = len(w["hdds"])
                if n_cities > 0:
                    n_days = len(w["hdds"][0])
                    avg_hdds = []
                    avg_temps = []
                    for d in range(n_days):
                        hdd_vals = [w["hdds"][c][d] for c in range(n_cities) if d < len(w["hdds"][c])]
                        tmp_vals = [w["temps"][c][d] for c in range(n_cities) if d < len(w["temps"][c]) and w["temps"][c][d] is not None]
                        avg_hdds.append(round(sum(hdd_vals) / len(hdd_vals), 1) if hdd_vals else 0)
                        avg_temps.append(round(sum(tmp_vals) / len(tmp_vals), 1) if tmp_vals else None)
                    w["avg_hdd"] = avg_hdds
                    w["avg_temp"] = avg_temps
                    w["total_hdd_7d"] = round(sum(avg_hdds), 1)
            print(f"  ✓ {len(weather)} countries, 7-day HDD forecast")
        elif wx and isinstance(wx, dict) and "daily" in wx:
            # Single location response (shouldn't happen with multi but handle it)
            print(f"  ⚠ Single response format — check API")
        else:
            print(f"  ⚠ Unexpected response format")
    except Exception as e:
        print(f"  ⚠ Weather fetch failed: {e}")

    D["weather"] = weather

    return D


# ─── HTML GENERATION ───────────────────────────────────────────

def ta(t):
    try:
        n = float(t)
        return "▲" if n > 0.01 else ("▼" if n < -0.01 else "◆")
    except:
        return "–"

def tc(t):
    try:
        n = float(t)
        return "green" if n > 0.01 else ("red" if n < -0.01 else "orange")
    except:
        return "dim"

def fc(v):
    f = sf(v)
    return "green" if f > 80 else ("accent" if f > 50 else ("orange" if f > 30 else "red"))

def cb(v):
    return sfmt(sf(v) * TWH_TO_BCM, ".2f")

def cm(v):
    return sfmt(sf(v) * GWH_TO_MCM, ".1f")


def gen_html(D):
    eu = D.get("eu", {})
    cc = D.get("countries", [])
    lng = D.get("lng", [])
    el = D.get("eu_lng", {})
    fl = D.get("flows", [])
    cy = datetime.now().year
    ns = datetime.now().strftime("%d %b %Y %H:%M")
    gd = eu.get("gasDayStart", "–")

    # ── Seasonality JS data ──
    def sjs(k):
        s = D.get(f"s_{k}", {})
        r = {}
        for y, ents in s.items():
            p = []
            for e in sorted(ents, key=lambda x: x["d"]):
                try:
                    d = datetime.strptime(e["d"], "%Y-%m-%d")
                    p.append({"x": d.timetuple().tm_yday, "y": round(e["f"], 2)})
                except:
                    pass
            r[str(y)] = p
        return json.dumps(r)

    # Build 5-year band data
    def band_js(k):
        s = D.get(f"s_{k}", {})
        hist = {y: ents for y, ents in s.items() if str(y) != str(cy)}
        by_doy = {}
        for y, ents in hist.items():
            for e in ents:
                try:
                    d = datetime.strptime(e["d"], "%Y-%m-%d")
                    doy = d.timetuple().tm_yday
                    if doy not in by_doy:
                        by_doy[doy] = []
                    by_doy[doy].append(e["f"])
                except:
                    pass
        if not by_doy:
            return "null"
        band = {"min": [], "max": [], "avg": []}
        for doy in sorted(by_doy.keys()):
            vals = by_doy[doy]
            band["min"].append({"x": doy, "y": round(min(vals), 2)})
            band["max"].append({"x": doy, "y": round(max(vals), 2)})
            band["avg"].append({"x": doy, "y": round(sum(vals) / len(vals), 2)})
        return json.dumps(band)

    sd_js = "{\n" + ",\n".join(f'    "{k}": {sjs(k)}' for k, _ in SEASON_REGIONS) + "\n  }"
    bands_js = "{\n" + ",\n".join(f'    "{k}": {band_js(k)}' for k, _ in SEASON_REGIONS) + "\n  }"
    so_html = "\n".join(f'      <option value="{k}">{l}</option>' for k, l in SEASON_REGIONS)

    # ── LNG rows ──
    lr = ""
    for c in lng:
        so = sf(c.get("sendOut"))
        dt = sf(c.get("dtrs"))
        ut = (so / dt * 100) if dt > 0 else 0
        uc = "green" if ut > 70 else ("orange" if ut > 40 else "dim")
        inv_val = c.get("inventory", "0")
        if isinstance(inv_val, dict):
            inv_val = inv_val.get("lng", "0")
        lr += f'''<tr><td class="cl"><span class="fl">{c.get("_f","")}</span> {c.get("_n","")}</td>
<td class="cr blue">{sfmt(c.get("sendOut"),".1f")} GWh/d</td><td class="cr">{sfmt(inv_val,".0f")}</td>
<td class="cr dim">{sfmt(c.get("dtrs"),".1f")} GWh/d</td>
<td class="cr"><div class="fb-w"><div class="fb-bg" style="width:50px"><div class="fb {uc}" style="width:{min(ut,100):.0f}%"></div></div><span class="{uc}">{ut:.0f}%</span></div></td></tr>'''

    # ── Flow rows (grouped by corridor) ──
    corridors_grouped = {}
    ungrouped = []
    for f in fl:
        corr = f.get("corridor", "")
        if corr:
            if corr not in corridors_grouped:
                corridors_grouped[corr] = []
            corridors_grouped[corr].append(f)
        else:
            ungrouped.append(f)

    corridor_html = ""
    all_flows = list(corridors_grouped.items())
    if ungrouped:
        all_flows.append(("Other", ungrouped))

    for corr_name, flows in all_flows:
        total = sum(f.get("latest", 0) for f in flows)
        corridor_html += f'<div class="pnl" style="margin-bottom:12px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><div class="st" style="font-size:14px">{corr_name}</div><div style="font-family:\'IBM Plex Mono\',monospace;font-size:13px;font-weight:600;color:var(--ac)">{total:,.0f} GWh/d</div></div>'
        corridor_html += '<table><thead><tr><th class="cl">Point</th><th class="cl">Operator</th><th class="cr">Direction</th><th class="cr">Latest</th><th class="cr">7d Avg</th><th class="cr">vs Avg</th></tr></thead><tbody>'
        for i, f in enumerate(flows):
            dc = "green" if f["direction"] == "entry" else "orange"
            dev_pct = (f["latest"] - f["avg"]) / f["avg"] * 100 if f["avg"] else 0
            if dev_pct > 10:
                dev_color = "green"
            elif dev_pct < -10:
                dev_color = "red"
            elif dev_pct < 0:
                dev_color = "orange"
            else:
                dev_color = "dim"
            dev_str = f"{dev_pct:+.1f}%"
            corridor_html += f'''<tr class="flow-row" data-dir="{f['direction']}"><td class="cl">{f["label"]}</td>
<td class="cl dim" style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{f["operator"]}</td>
<td class="cr"><span class="db {dc}">{f["direction"]}</span></td>
<td class="cr accent bold">{f["latest"]:,.1f}</td><td class="cr dim">{f["avg"]:,.1f}</td><td class="cr"><span class="{dev_color}">{dev_str}</span></td></tr>'''
        corridor_html += '</tbody></table></div>'

    # ── Metric values ──
    eg = sfmt(eu.get("gasInStorage"))
    ew = sfmt(eu.get("workingGasVolume"))
    ei = sfmt(eu.get("injection"), ".1f")
    ewd = sfmt(eu.get("withdrawal"), ".1f")
    egb = cb(eu.get("gasInStorage"))
    ewb = cb(eu.get("workingGasVolume"))
    eim = cm(eu.get("injection"))
    ewm = cm(eu.get("withdrawal"))
    ef = sfmt(eu.get("full"), ".1f")
    eff = sf(eu.get("full"))
    efc = fc(eu.get("full"))
    etc_ = tc(eu.get("trend"))
    el_inv = el.get("inventory")
    if isinstance(el_inv, dict):
        el_inv = el_inv.get("lng") or el_inv.get("gwh") or "0"
    li = sfmt(el_inv, ".0f")
    ls = sfmt(el.get("sendOut"), ".1f")
    ld = sfmt(el.get("dtrs"), ".1f")
    lu = "–"
    try:
        lu = f"{sf(el['sendOut']) / sf(el['dtrs']) * 100:.1f}%"
    except:
        pass

    # ══════════════════════════════════════════════════════
    # INTELLIGENCE ANALYTICS
    # ══════════════════════════════════════════════════════

    # 1. Days-of-demand remaining (storage ÷ net withdrawal)
    eu_gas = sf(eu.get("gasInStorage"))
    eu_wdr = sf(eu.get("withdrawal"))
    eu_inj = sf(eu.get("injection"))
    eu_net = eu_wdr - eu_inj  # net draw on storage
    eu_days_net = int(eu_gas * 1000 / eu_net) if eu_net > 0.1 else 999
    eu_days_gross = int(eu_gas * 1000 / eu_wdr) if eu_wdr > 0.1 else 999
    eu_days = eu_days_net if eu_days_net < 999 else eu_days_gross
    eu_days_str = f"{eu_days}" if eu_days < 999 else "n/a"
    eu_days_color = "green" if eu_days > 90 else ("accent" if eu_days > 60 else ("orange" if eu_days > 30 else "red"))

    # 7-day rolling implied draw from seasonality data
    eu_s_cy = D.get("s_eu", {}).get(str(cy), [])
    eu_s_sorted = sorted(eu_s_cy, key=lambda x: x["d"])
    eu_wgv_val = sf(eu.get("workingGasVolume"))
    eu_implied_draw_7d = None
    if len(eu_s_sorted) >= 8 and eu_wgv_val > 0:
        fill_7d_ago = eu_s_sorted[-8]["f"]
        fill_latest = eu_s_sorted[-1]["f"]
        eu_implied_draw_7d = round((fill_7d_ago - fill_latest) / 7 * eu_wgv_val * 1000 / 100, 0)

    country_intel = []
    for c in cc:
        gas = sf(c.get("gasInStorage"))
        wdr = sf(c.get("withdrawal"))
        inj = sf(c.get("injection"))
        fill = sf(c.get("full"))
        net = wdr - inj
        # Days of supply: use NET withdrawal when drawing, else n/a
        # But also compute days at gross withdrawal rate for comparison
        days_net = int(gas * 1000 / net) if net > 0.5 else 999  # gas TWh, rates GWh/d
        days_gross = int(gas * 1000 / wdr) if wdr > 0.5 else 999
        # Use the more meaningful number: net if clearly drawing, gross if balanced
        days = days_net if days_net < 999 else days_gross

        # Consumption-based days (Stock/Consumption) — annual consumption from AGSI
        consumption = sf(c.get("consumption"))  # TWh/year
        days_cons = int(gas / (consumption / 365)) if consumption > 1 else None

        code = c.get("_c", "")

        # 2. Compare fill to 5-year range for this day-of-year
        doy = datetime.now().timetuple().tm_yday
        s_data = D.get(f"s_{code.lower()}", {})
        hist_fills = []
        for yr, ents in s_data.items():
            if str(yr) == str(cy):
                continue
            for e in ents:
                try:
                    ed = datetime.strptime(e["d"], "%Y-%m-%d")
                    if abs(ed.timetuple().tm_yday - doy) <= 3:
                        hist_fills.append(e["f"])
                except:
                    pass
        hist_min = min(hist_fills) if hist_fills else None
        hist_max = max(hist_fills) if hist_fills else None
        hist_avg = sum(hist_fills) / len(hist_fills) if hist_fills else None
        below_min = fill < hist_min if hist_min is not None else False
        vs_avg = fill - hist_avg if hist_avg is not None else None

        # YoY: compare to same day-of-year last year
        ly_str = str(cy - 1)
        vs_ly = None
        if ly_str in s_data:
            for e in s_data[ly_str]:
                try:
                    ed = datetime.strptime(e["d"], "%Y-%m-%d")
                    if abs(ed.timetuple().tm_yday - doy) <= 1:
                        vs_ly = round(fill - e["f"], 1)
                        break
                except:
                    pass

        country_intel.append({
            "code": code, "name": c["_n"], "flag": c["_f"],
            "fill": fill, "gas": gas, "wdr": wdr, "inj": inj,
            "net": net, "days": days, "days_cons": days_cons,
            "hist_min": hist_min, "hist_max": hist_max, "hist_avg": hist_avg,
            "below_min": below_min, "vs_avg": vs_avg, "vs_ly": vs_ly,
        })

    # 3. Injection season readiness — is net flow positive (injecting) or negative (withdrawing)?
    injecting_countries = [ci for ci in country_intel if ci["net"] < 0]  # inj > wdr
    drawing_countries = [ci for ci in country_intel if ci["net"] > 0.1]

    # 4. Alert flags
    alerts = []
    for ci in country_intel:
        if ci["below_min"]:
            alerts.append({"type": "below_min", "severity": "red", "icon": "▼",
                           "msg": f'{ci["flag"]} {ci["name"]} storage at {ci["fill"]:.1f}% — below 5-year minimum ({ci["hist_min"]:.1f}%) for this date'})
        if ci["days"] < 30 and ci["net"] > 0.5 and ci["gas"] > 0.5:
            d_word = "day" if ci["days"] == 1 else "days"
            alerts.append({"type": "low_days", "severity": "red", "icon": "⏱",
                           "msg": f'{ci["flag"]} {ci["name"]} — {ci["days"]} {d_word} of supply at current net withdrawal rate'})
        elif ci["days"] < 60 and ci["net"] > 0.5 and ci["gas"] > 0.5:
            alerts.append({"type": "low_days", "severity": "orange", "icon": "⏱",
                           "msg": f'{ci["flag"]} {ci["name"]} — {ci["days"]} days of supply remaining at current pace'})

    for lrec in lng:
        so = sf(lrec.get("sendOut"))
        dt = sf(lrec.get("dtrs"))
        if dt > 0 and so / dt > 0.90:
            alerts.append({"type": "lng_maxed", "severity": "orange", "icon": "🚢",
                           "msg": f'{lrec.get("_f","")} {lrec.get("_n","")} LNG at {so/dt*100:.0f}% utilisation — near capacity'})

    alerts.sort(key=lambda a: {"red": 0, "orange": 1}.get(a["severity"], 2))

    # ── Country table rows ── (built here so country_intel is already populated)
    ci_by_code = {ci["code"]: ci for ci in country_intel}
    cr = ""
    for c in cc:
        fp = sf(c.get("full"))
        fcc = fc(c.get("full"))
        tcc = tc(c.get("trend"))
        gt = sfmt(c.get("gasInStorage"))
        wt = sfmt(c.get("workingGasVolume"))
        it = sfmt(c.get("injection"), ".1f")
        wdt = sfmt(c.get("withdrawal"), ".1f")
        gb = cb(c.get("gasInStorage"))
        wb = cb(c.get("workingGasVolume"))
        im = cm(c.get("injection"))
        wm = cm(c.get("withdrawal"))
        fac_note = ""
        if c.get("_facilities"):
            src_tag = "National Gas" if c.get("_source") == "nationalgas" else "AGSI"
            fac_note = f' <span style="font-size:9px;color:var(--t3)" title="Source: {src_tag}, {c["_facilities"]} facilities">({src_tag})</span>'
        vs_ly_val = ci_by_code.get(c.get("_c", ""), {}).get("vs_ly")
        if vs_ly_val is not None:
            if vs_ly_val > 0:
                vs_ly_color = "green"
            elif vs_ly_val < -3:
                vs_ly_color = "red"
            else:
                vs_ly_color = "orange"
            vs_ly_cell = f'<span class="{vs_ly_color}">{vs_ly_val:+.1f}pp</span>'
        else:
            vs_ly_cell = '<span class="dim">–</span>'
        cr += f'''<tr><td class="cl"><span class="fl">{c["_f"]}</span> {c["_n"]}{fac_note}</td>
<td class="cr"><div class="fb-w"><div class="fb-bg"><div class="fb {fcc}" style="width:{min(fp,100):.0f}%"></div></div><span class="fp {fcc}">{sfmt(c.get("full"),".1f")}%</span></div></td>
<td class="cr"><span class="uv" data-t="{gt}" data-b="{gb}">{gt} TWh</span></td>
<td class="cr dim"><span class="uv" data-t="{wt}" data-b="{wb}">{wt} TWh</span></td>
<td class="cr green"><span class="uf" data-g="{it}" data-m="{im}">{it} GWh/d</span></td>
<td class="cr orange"><span class="uf" data-g="{wdt}" data-m="{wm}">{wdt} GWh/d</span></td>
<td class="cr {tcc}" style="font-size:18px">{ta(c.get("trend"))}</td>
<td class="cr">{vs_ly_cell}</td></tr>'''

    # ── Build Intelligence tab HTML ──
    # Days-of-demand table
    ci_sorted = sorted(country_intel, key=lambda x: x["days"] if x["days"] < 500 else 9999)
    dod_rows = ""
    for ci in ci_sorted:
        if ci["gas"] < 0.1:
            continue
        d = ci["days"]
        d_str = str(d) if d < 999 else "n/a"
        d_col = "green" if d > 90 else ("accent" if d > 60 else ("orange" if d > 30 else "red"))
        # Show withdrawal and injection separately
        wdr_str = f'{ci["wdr"]:.1f}' if ci["wdr"] > 0.1 else "–"
        inj_str = f'{ci["inj"]:.1f}' if ci["inj"] > 0.1 else "–"
        vs = ""
        if ci["vs_avg"] is not None:
            vs_val = ci["vs_avg"]
            vs_col = "green" if vs_val > 0 else ("red" if vs_val < -5 else "orange")
            vs = f'<span class="{vs_col}">{vs_val:+.1f}pp</span>'
        # Stock/Consumption days
        dc = ci.get("days_cons")
        dc_str = str(dc) if dc is not None else "–"
        dc_col = "dim" if dc is None else ("green" if dc > 60 else ("accent" if dc > 30 else ("orange" if dc > 15 else "red")))
        bm = ' <span class="red" style="font-size:9px">⚠ BELOW MIN</span>' if ci["below_min"] else ""
        dod_rows += f'''<tr><td class="cl"><span class="fl">{ci["flag"]}</span> {ci["name"]}{bm}</td>
<td class="cr {d_col} bold">{d_str}</td>
<td class="cr {dc_col}">{dc_str}</td>
<td class="cr">{ci["fill"]:.1f}%</td>
<td class="cr">{ci["gas"]:.1f}</td>
<td class="cr orange">{wdr_str}</td>
<td class="cr green">{inj_str}</td>
<td class="cr">{vs}</td></tr>'''

    # Alerts HTML
    alerts_html = ""
    if alerts:
        for a in alerts:
            alerts_html += f'<div style="display:flex;gap:12px;align-items:flex-start;padding:12px 16px;background:rgba({"248,113,113" if a["severity"]=="red" else "251,191,36"},.06);border:1px solid rgba({"248,113,113" if a["severity"]=="red" else "251,191,36"},.15);border-radius:8px;margin-bottom:8px;font-size:12px;font-family:\'IBM Plex Mono\',monospace"><span style="font-size:16px">{a["icon"]}</span><span class="{a["severity"]}">{a["msg"]}</span></div>'
    else:
        alerts_html = '<div style="padding:20px;text-align:center;color:var(--t3);font-family:\'IBM Plex Mono\',monospace;font-size:12px">No alerts — all indicators within normal ranges</div>'

    # ── Weather outlook for intelligence tab ──
    weather = D.get("weather", {})
    wx_rows = ""
    if weather:
        wx_sorted = sorted(weather.items(), key=lambda x: x[1].get("total_hdd_7d", 0), reverse=True)
        for cc_code, w in wx_sorted:
            ci = next((x for x in country_intel if x["code"] == cc_code), None)
            if not ci:
                continue
            hdd7 = w.get("total_hdd_7d", 0)
            avg_temps = w.get("avg_temp", [])
            avg_hdds = w.get("avg_hdd", [])
            dates = w.get("dates", [])
            # Mini sparkline of HDD as a simple bar chart
            max_hdd = max(avg_hdds) if avg_hdds else 1
            spark = '<div style="display:flex;gap:2px;align-items:end;height:24px">'
            for h in avg_hdds:
                pct = min(100, h / max(max_hdd, 0.1) * 100) if max_hdd > 0 else 0
                col = "#f87171" if h > 12 else ("#fbbf24" if h > 6 else "#34d399")
                spark += f'<div style="width:8px;height:{max(2, pct * 0.24):.0f}px;background:{col};border-radius:1px" title="{h:.1f} HDD"></div>'
            spark += '</div>'
            # Demand pressure indicator
            if hdd7 > 80:
                pressure = '<span class="red bold">HIGH</span>'
            elif hdd7 > 40:
                pressure = '<span class="orange bold">MODERATE</span>'
            else:
                pressure = '<span class="green bold">LOW</span>'
            avg_t = avg_temps[0] if avg_temps and avg_temps[0] is not None else "–"
            avg_t_str = f"{avg_t:.0f}°C" if isinstance(avg_t, (int, float)) else "–"
            flag = ci["flag"]
            wx_rows += f'''<tr><td class="cl"><span class="fl">{flag}</span> {ci["name"]}</td>
<td class="cr">{avg_t_str}</td>
<td class="cr">{hdd7:.0f}</td>
<td class="cr">{spark}</td>
<td class="cr">{pressure}</td></tr>'''

    wx_section = ""
    if wx_rows:
        wx_section = f'''<div class="pnl">
<div class="st">7-Day Weather & Heating Demand Outlook</div>
<div class="ss">Open-Meteo forecast \u2014 HDD = heating degree days (base 15.5\u00b0C) \u2014 higher HDD = more gas demand</div>
<table><thead><tr><th class="cl">Country</th><th class="cr">Today</th><th class="cr">7d HDD</th><th class="cr">Daily HDD</th><th class="cr">Demand Pressure</th></tr></thead>
<tbody>{wx_rows}</tbody></table>
</div>'''
    else:
        wx_section = '<div class="pnl"><div class="st">Weather Outlook</div><div class="ss">7-day heating demand forecast \u2014 requires internet connection to Open-Meteo</div><div style="padding:30px;text-align:center;color:var(--t3);font-family:\'IBM Plex Mono\',monospace;font-size:12px">Weather data not available \u2014 will populate on next live run</div></div>'

    ttf = D.get("ttf", {})
    ttf_price = ttf.get("price")
    ttf_chg = ttf.get("chg")
    ttf_price_str = f"{ttf_price:.2f}" if ttf_price else "–"
    ttf_chg_str = (f"{ttf_chg:+.1f}%" if ttf_chg else "")
    ttf_chg_color = ("green" if ttf_chg and ttf_chg > 0 else "red") if ttf_chg is not None else "dim"
    eu_draw_str = f"{eu_implied_draw_7d:,.0f}" if eu_implied_draw_7d is not None else "–"
    eu_lng_so = sfmt(el.get("sendOut"), ".1f")

    intel_html = f'''<div id="tab-intel" class="tc">

<div class="pnl" style="margin-bottom:20px">
<div class="st">Alerts & Watchlist</div>
<div class="ss">Automated flags \u2014 storage below 5-year minimum, low days-of-supply, LNG near capacity</div>
<div style="margin-top:14px">{alerts_html}</div>
</div>

<div class="mg" style="grid-template-columns:repeat(4,1fr)">
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--{eu_days_color}),transparent)"></div><div class="mc-lbl">EU Days of Supply</div><div class="mc-val {eu_days_color}">{eu_days_str}</div><div class="mc-sub">At current net withdrawal ({eu_net:.0f} GWh/d)</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--or),transparent)"></div><div class="mc-lbl">Countries Drawing</div><div class="mc-val orange">{len(drawing_countries)}</div><div class="mc-sub">Net withdrawal from storage</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--gn),transparent)"></div><div class="mc-lbl">Countries Injecting</div><div class="mc-val green">{len(injecting_countries)}</div><div class="mc-sub">Net injection into storage</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--or),transparent)"></div><div class="mc-lbl">TTF Front Month</div><div class="mc-val accent">{ttf_price_str}</div><div class="mc-sub">EUR/MWh <span class="{ttf_chg_color}">{ttf_chg_str}</span></div></div>
</div>

<div class="mg" style="grid-template-columns:repeat(3,1fr);margin-top:0">
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--bl),transparent)"></div><div class="mc-lbl">EU 7d Implied Draw</div><div class="mc-val blue">{eu_draw_str}</div><div class="mc-sub">GWh/d (from fill % trend)</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--tl),transparent)"></div><div class="mc-lbl">EU LNG Send-Out</div><div class="mc-val teal">{eu_lng_so} GWh/d</div><div class="mc-sub">Total EU LNG terminal output</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--ac),transparent)"></div><div class="mc-lbl">EU Fill Level</div><div class="mc-val {efc}">{ef}%</div><div class="mc-sub">of {ew} TWh WGV</div></div>
</div>

{wx_section}

<div class="pnl">
<div class="st">Days of Supply by Country</div>
<div class="ss">Sorted by urgency \u2014 W/D Days = at current withdrawal rate \u2014 S/C Days = stock \u00f7 annual consumption</div>
<table><thead><tr><th class="cl">Country</th><th class="cr">W/D Days</th><th class="cr">S/C Days</th><th class="cr">Fill %</th><th class="cr">Gas (TWh)</th><th class="cr">W/D (GWh/d)</th><th class="cr">Inj (GWh/d)</th><th class="cr">vs 5yr Avg</th></tr></thead>
<tbody>{dod_rows}</tbody></table>
</div>

</div>'''

    # ── Country deep-dive data (for JS panel) ──
    # FACILITIES dict is defined below in the geo section but needed here too
    FACILITIES = {"DE":[{"name":"Rehden","lat":52.60,"lon":8.48,"wgv":44},{"name":"Jemgum","lat":53.27,"lon":7.38,"wgv":10},{"name":"Etzel","lat":53.59,"lon":7.88,"wgv":14},{"name":"Bierwang","lat":48.18,"lon":12.28,"wgv":7.5},{"name":"Wolfersberg","lat":48.15,"lon":12.10,"wgv":3.8},{"name":"Bernburg","lat":51.79,"lon":11.73,"wgv":5.3},{"name":"Katharina","lat":52.95,"lon":11.02,"wgv":4.1},{"name":"Epe","lat":52.19,"lon":7.08,"wgv":6},{"name":"Breitbrunn","lat":47.93,"lon":12.37,"wgv":5.2},{"name":"Kraak","lat":53.55,"lon":11.58,"wgv":2.8},{"name":"Bad Lauchstädt","lat":51.38,"lon":11.88,"wgv":6.8},{"name":"Xanten","lat":51.66,"lon":6.45,"wgv":2.7},{"name":"Dötlingen","lat":52.93,"lon":8.39,"wgv":2.2}],"IT":[{"name":"Sergnano","lat":45.43,"lon":9.70,"wgv":30},{"name":"Settala","lat":45.38,"lon":9.37,"wgv":18},{"name":"Minerbio","lat":44.63,"lon":11.48,"wgv":15},{"name":"Fiume Treste","lat":44.60,"lon":11.95,"wgv":12},{"name":"Sabbioncello","lat":44.82,"lon":11.52,"wgv":8},{"name":"Ripalta","lat":45.27,"lon":9.72,"wgv":5},{"name":"Collalto","lat":45.90,"lon":12.03,"wgv":5.5}],"FR":[{"name":"Chémery","lat":47.34,"lon":1.46,"wgv":22},{"name":"Cerville","lat":48.64,"lon":6.26,"wgv":12},{"name":"Beynes","lat":48.83,"lon":1.86,"wgv":7.5},{"name":"Germigny","lat":48.18,"lon":3.30,"wgv":6.5},{"name":"Lussagnet","lat":43.72,"lon":-0.25,"wgv":8.5},{"name":"Etrez","lat":46.33,"lon":5.38,"wgv":5},{"name":"Manosque","lat":43.83,"lon":5.78,"wgv":4}],"NL":[{"name":"Norg","lat":53.06,"lon":6.46,"wgv":50},{"name":"Grijpskerk","lat":53.27,"lon":6.28,"wgv":24},{"name":"Bergermeer","lat":52.62,"lon":4.89,"wgv":43},{"name":"Alkmaar","lat":52.64,"lon":4.76,"wgv":4.5}],"AT":[{"name":"Haidach","lat":47.95,"lon":13.03,"wgv":29},{"name":"7Fields","lat":48.33,"lon":16.67,"wgv":16},{"name":"Puchkirchen","lat":48.03,"lon":13.78,"wgv":13},{"name":"Tallesbrunn","lat":48.50,"lon":16.82,"wgv":9}],"HU":[{"name":"Szőreg","lat":46.22,"lon":20.18,"wgv":21},{"name":"Zsana","lat":46.58,"lon":19.60,"wgv":15},{"name":"Hajdúszoboszló","lat":47.45,"lon":21.38,"wgv":10},{"name":"Kardoskút","lat":46.48,"lon":20.68,"wgv":7.5}],"ES":[{"name":"Gaviota","lat":43.38,"lon":-2.68,"wgv":14},{"name":"Marismas","lat":37.28,"lon":-6.68,"wgv":6.5},{"name":"Yela","lat":40.95,"lon":-2.77,"wgv":7.5}],"PL":[{"name":"Wierzchowice","lat":51.30,"lon":16.72,"wgv":12},{"name":"Husów","lat":49.97,"lon":22.28,"wgv":5.5},{"name":"Mogilno","lat":52.65,"lon":17.92,"wgv":6},{"name":"Kosakowo","lat":54.55,"lon":18.52,"wgv":3.5}],"CZ":[{"name":"Dolní Dunajovice","lat":48.85,"lon":16.60,"wgv":8},{"name":"Tvrdonice","lat":48.75,"lon":17.02,"wgv":5},{"name":"Štramberk","lat":49.58,"lon":18.12,"wgv":4},{"name":"Háje","lat":50.10,"lon":14.50,"wgv":5.5}],"RO":[{"name":"Depomureș","lat":46.40,"lon":24.75,"wgv":13},{"name":"Bilciurești","lat":44.77,"lon":25.52,"wgv":5}],"SK":[{"name":"Láb","lat":48.38,"lon":16.97,"wgv":20},{"name":"Gajary-Baden","lat":48.42,"lon":16.90,"wgv":10}],"BG":[{"name":"Chiren","lat":43.17,"lon":23.48,"wgv":6}],"HR":[{"name":"Okoli","lat":45.58,"lon":16.85,"wgv":5}],"LV":[{"name":"Inčukalns","lat":57.10,"lon":24.70,"wgv":24}],"DK":[{"name":"Stenlille","lat":55.55,"lon":11.55,"wgv":3.5},{"name":"Lille Torup","lat":56.72,"lon":9.30,"wgv":4.5}],"PT":[{"name":"Carriço","lat":39.70,"lon":-8.85,"wgv":3}],"BE":[{"name":"Loenhout","lat":51.38,"lon":4.62,"wgv":7}],"SE":[{"name":"Skallen","lat":56.68,"lon":12.85,"wgv":0.1}],"GB":[{"name":"Stublach","lat":53.18,"lon":-2.55,"wgv":4.4},{"name":"Aldbrough","lat":53.83,"lon":-0.18,"wgv":3.2},{"name":"Hornsea","lat":53.91,"lon":-0.17,"wgv":3.4},{"name":"Holford","lat":53.22,"lon":-2.42,"wgv":2.6},{"name":"Humbly Grove","lat":51.10,"lon":-1.05,"wgv":3.1},{"name":"Rough","lat":53.83,"lon":0.80,"wgv":16.6},{"name":"Hatfield Moor","lat":53.50,"lon":-0.95,"wgv":1.3},{"name":"Hill Top","lat":53.70,"lon":-2.95,"wgv":0.6},{"name":"Holehouse Farm","lat":53.15,"lon":-2.40,"wgv":0.2}],"UA":[{"name":"Bilche-Volytsko","lat":49.32,"lon":23.72,"wgv":170},{"name":"Bohorodchany","lat":48.80,"lon":24.52,"wgv":23},{"name":"Dashava","lat":49.05,"lon":23.95,"wgv":20}]}

    country_deep = {}
    for ci in country_intel:
        code = ci["code"]
        # Find facilities for this country
        fac_list = FACILITIES.get(code, [])
        # Find LNG terminals
        lng_terminals = [l for l in lng if l.get("_c") == code]
        # Find pipeline flows
        pipe_flows = [f for f in fl if any(kw.lower() in f.get("label", "").lower() for kw in
            {"DE":["Dornum","NETRA","Mallnow","Waidhaus","Greifswald","Lubmin","Ellund","Emden"],
             "IT":["Tarvisio","Mazara","Gela"],
             "FR":["Obergailbach","Dunkerque","Montoir","Fos"],
             "NL":["Bunde","Oude","Gate Terminal"],
             "GB":["Easington","St. Fergus","Milford Haven","Bacton"],
             "AT":["Baumgarten","Oberkappel"],
             "HU":["Csanadpalota","Beregdaroc"],
             "BG":["Strandzha","Kulata"],
             "PL":["Mallnow","Kondratki"],
             "SK":["Velke Kapusany","Lanzhot"],
             "ES":["TVB"],
             "BE":["Zeebrugge","Eynatten"],
             "RO":["Negru Voda","Mediesu"],
             "CZ":["Waidhaus","Lanzhot"],
             "HR":["Rogatec"],
             "DK":["Nybro","Ellund"],
            }.get(code, [code]))]
        # Weather
        wx = weather.get(code, {})
        country_deep[code.lower()] = {
            "code": code, "name": ci["name"], "flag": ci["flag"],
            "fill": ci["fill"], "gas": ci["gas"],
            "wgv": sf(next((c.get("workingGasVolume") for c in cc if c.get("_c") == code), 0)),
            "wdr": ci["wdr"], "inj": ci["inj"], "net": round(ci["net"], 1),
            "days": ci["days"] if ci["days"] < 500 else None,
            "vs_avg": round(ci["vs_avg"], 1) if ci["vs_avg"] is not None else None,
            "below_min": ci["below_min"],
            "hist_min": round(ci["hist_min"], 1) if ci["hist_min"] is not None else None,
            "hist_avg": round(ci["hist_avg"], 1) if ci["hist_avg"] is not None else None,
            "facilities": [f["name"] for f in fac_list],
            "lng": [{"name": l.get("_n", ""), "so": sf(l.get("sendOut"))} for l in lng_terminals],
            "pipes": [{"label": f.get("label", ""), "flow": f.get("latest", 0), "dir": f.get("direction", "")} for f in pipe_flows[:5]],
            "hdd_7d": wx.get("total_hdd_7d"),
            "temps": wx.get("avg_temp", []),
        }
    country_deep_js = json.dumps(country_deep)

    yc = json.dumps({str(cy - i): ["#00d4aa", "#60a5fa", "#a78bfa", "#fbbf24", "#f87171", "#94a3b8"][i] for i in range(6)})
    cj = json.dumps([{"name": c["_n"], "fill": sf(c.get("full")), "code": c["_c"], "flag": c["_f"]} for c in cc])

    # ── Map data ──
    map_data = json.dumps({c["_c"].lower(): {"fill": sf(c.get("full")), "name": c["_n"]} for c in cc})

    # ── Facility-level storage geocoding ──
    FACILITIES = {"DE":[{"name":"Rehden","lat":52.60,"lon":8.48,"wgv":44},{"name":"Jemgum","lat":53.27,"lon":7.38,"wgv":10},{"name":"Etzel","lat":53.59,"lon":7.88,"wgv":14},{"name":"Bierwang","lat":48.18,"lon":12.28,"wgv":7.5},{"name":"Wolfersberg","lat":48.15,"lon":12.10,"wgv":3.8},{"name":"Bernburg","lat":51.79,"lon":11.73,"wgv":5.3},{"name":"Katharina","lat":52.95,"lon":11.02,"wgv":4.1},{"name":"Epe","lat":52.19,"lon":7.08,"wgv":6},{"name":"Breitbrunn","lat":47.93,"lon":12.37,"wgv":5.2},{"name":"Kraak","lat":53.55,"lon":11.58,"wgv":2.8},{"name":"Bad Lauchstädt","lat":51.38,"lon":11.88,"wgv":6.8},{"name":"Xanten","lat":51.66,"lon":6.45,"wgv":2.7},{"name":"Dötlingen","lat":52.93,"lon":8.39,"wgv":2.2}],"IT":[{"name":"Sergnano","lat":45.43,"lon":9.70,"wgv":30},{"name":"Settala","lat":45.38,"lon":9.37,"wgv":18},{"name":"Minerbio","lat":44.63,"lon":11.48,"wgv":15},{"name":"Fiume Treste","lat":44.60,"lon":11.95,"wgv":12},{"name":"Sabbioncello","lat":44.82,"lon":11.52,"wgv":8},{"name":"Ripalta","lat":45.27,"lon":9.72,"wgv":5},{"name":"Collalto","lat":45.90,"lon":12.03,"wgv":5.5}],"FR":[{"name":"Chémery","lat":47.34,"lon":1.46,"wgv":22},{"name":"Cerville","lat":48.64,"lon":6.26,"wgv":12},{"name":"Beynes","lat":48.83,"lon":1.86,"wgv":7.5},{"name":"Germigny","lat":48.18,"lon":3.30,"wgv":6.5},{"name":"Lussagnet","lat":43.72,"lon":-0.25,"wgv":8.5},{"name":"Etrez","lat":46.33,"lon":5.38,"wgv":5},{"name":"Manosque","lat":43.83,"lon":5.78,"wgv":4}],"NL":[{"name":"Norg","lat":53.06,"lon":6.46,"wgv":50},{"name":"Grijpskerk","lat":53.27,"lon":6.28,"wgv":24},{"name":"Bergermeer","lat":52.62,"lon":4.89,"wgv":43},{"name":"Alkmaar","lat":52.64,"lon":4.76,"wgv":4.5}],"AT":[{"name":"Haidach","lat":47.95,"lon":13.03,"wgv":29},{"name":"7Fields","lat":48.33,"lon":16.67,"wgv":16},{"name":"Puchkirchen","lat":48.03,"lon":13.78,"wgv":13},{"name":"Tallesbrunn","lat":48.50,"lon":16.82,"wgv":9}],"HU":[{"name":"Szőreg","lat":46.22,"lon":20.18,"wgv":21},{"name":"Zsana","lat":46.58,"lon":19.60,"wgv":15},{"name":"Hajdúszoboszló","lat":47.45,"lon":21.38,"wgv":10},{"name":"Kardoskút","lat":46.48,"lon":20.68,"wgv":7.5}],"ES":[{"name":"Gaviota","lat":43.38,"lon":-2.68,"wgv":14},{"name":"Marismas","lat":37.28,"lon":-6.68,"wgv":6.5},{"name":"Yela","lat":40.95,"lon":-2.77,"wgv":7.5}],"PL":[{"name":"Wierzchowice","lat":51.30,"lon":16.72,"wgv":12},{"name":"Husów","lat":49.97,"lon":22.28,"wgv":5.5},{"name":"Mogilno","lat":52.65,"lon":17.92,"wgv":6},{"name":"Kosakowo","lat":54.55,"lon":18.52,"wgv":3.5}],"CZ":[{"name":"Dolní Dunajovice","lat":48.85,"lon":16.60,"wgv":8},{"name":"Tvrdonice","lat":48.75,"lon":17.02,"wgv":5},{"name":"Štramberk","lat":49.58,"lon":18.12,"wgv":4},{"name":"Háje","lat":50.10,"lon":14.50,"wgv":5.5}],"RO":[{"name":"Depomureș","lat":46.40,"lon":24.75,"wgv":13},{"name":"Bilciurești","lat":44.77,"lon":25.52,"wgv":5}],"SK":[{"name":"Láb","lat":48.38,"lon":16.97,"wgv":20},{"name":"Gajary-Baden","lat":48.42,"lon":16.90,"wgv":10}],"BG":[{"name":"Chiren","lat":43.17,"lon":23.48,"wgv":6}],"HR":[{"name":"Okoli","lat":45.58,"lon":16.85,"wgv":5}],"LV":[{"name":"Inčukalns","lat":57.10,"lon":24.70,"wgv":24}],"DK":[{"name":"Stenlille","lat":55.55,"lon":11.55,"wgv":3.5},{"name":"Lille Torup","lat":56.72,"lon":9.30,"wgv":4.5}],"PT":[{"name":"Carriço","lat":39.70,"lon":-8.85,"wgv":3}],"BE":[{"name":"Loenhout","lat":51.38,"lon":4.62,"wgv":7}],"SE":[{"name":"Skallen","lat":56.68,"lon":12.85,"wgv":0.1}],"GB":[{"name":"Stublach","lat":53.18,"lon":-2.55,"wgv":4.4},{"name":"Aldbrough","lat":53.83,"lon":-0.18,"wgv":3.2},{"name":"Hornsea","lat":53.91,"lon":-0.17,"wgv":3.4},{"name":"Holford","lat":53.22,"lon":-2.42,"wgv":2.6},{"name":"Humbly Grove","lat":51.10,"lon":-1.05,"wgv":3.1},{"name":"Rough","lat":53.83,"lon":0.80,"wgv":16.6},{"name":"Hatfield Moor","lat":53.50,"lon":-0.95,"wgv":1.3},{"name":"Hill Top","lat":53.70,"lon":-2.95,"wgv":0.6},{"name":"Holehouse Farm","lat":53.15,"lon":-2.40,"wgv":0.2}],"UA":[{"name":"Bilche-Volytsko","lat":49.32,"lon":23.72,"wgv":170},{"name":"Bohorodchany","lat":48.80,"lon":24.52,"wgv":23},{"name":"Dashava","lat":49.05,"lon":23.95,"wgv":20}]}
    storage_geo = []
    # Build per-facility fill% for GB from National Gas data
    gb_fac_fills = {}
    gb_entry = next((c for c in cc if c.get("_c") == "GB"), None)
    if gb_entry and gb_entry.get("_ng_facilities"):
        ng_facs = gb_entry["_ng_facilities"]  # {name: stock_kwh}
        ng_avail = gb_entry.get("_ng_avail_cap_kwh", 0)
        # We have per-facility stocks; compute fill from stock/(stock+avail_cap_share)
        # Available capacity per facility comes from the NATGAS_FACILITY_PUBS data
        # For now, compute fill as stock/wgv using FACILITIES wgv (in TWh)
        gb_facs_geo = FACILITIES.get("GB", [])
        for fac_geo in gb_facs_geo:
            fname = fac_geo["name"]
            stock_kwh = ng_facs.get(fname, 0)
            stock_twh = stock_kwh / 1e9 if stock_kwh > 1000 else stock_kwh  # handle already-converted
            wgv_twh = fac_geo["wgv"]
            if wgv_twh > 0:
                gb_fac_fills[fname] = round(stock_twh / wgv_twh * 100, 1)
    
    for c in cc:
        code = c.get("_c", "")
        facs = FACILITIES.get(code, [])
        fill = sf(c.get("full"))
        gas = sf(c.get("gasInStorage"))
        wgv = sf(c.get("workingGasVolume"))
        inj = sf(c.get("injection"))
        wdr = sf(c.get("withdrawal"))
        if facs:
            total_wgv = sum(f["wgv"] for f in facs)
            for fac in facs:
                share = fac["wgv"] / total_wgv if total_wgv > 0 else 0
                # Use per-facility fill% for GB if available
                fac_fill = gb_fac_fills.get(fac["name"], fill) if code == "GB" else fill
                storage_geo.append({
                    "name": fac["name"], "country": c["_n"],
                    "lat": fac["lat"], "lon": fac["lon"],
                    "fill": fac_fill, "gas": round(gas * share, 2),
                    "wgv": fac["wgv"], "inj": round(inj * share, 1),
                    "wdr": round(wdr * share, 1),
                })
    storage_geo = json.dumps(storage_geo)

    # ── LNG terminal coordinates from xlsx ──
    lng_geo = []
    try:
        import openpyxl
        xlsx_path = Path(__file__).parent / "oil_terminal_global.xlsx"
        if xlsx_path.exists():
            wb = openpyxl.load_workbook(str(xlsx_path), read_only=True)
            ws = wb["Worksheet"]
            headers = None
            eu_set = {"Belgium","Netherlands","Germany","France","Spain","Portugal",
                      "Italy","Greece","Croatia","Poland","Lithuania","Finland",
                      "United Kingdom","Sweden","Norway","Denmark","Turkey","Ireland"}
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    headers = row
                    continue
                d = dict(zip(headers, row))
                country = d.get("Terminal Country", "")
                if country in eu_set and d.get("LNG") == "Y" and d.get("Latitude") and d.get("Longitude"):
                    cap = d.get("Total Cubic Capacity(m3)")
                    lng_geo.append({
                        "name": str(d["Terminal Name"])[:50],
                        "country": country, "port": str(d.get("Terminal Port", "") or ""),
                        "lat": round(float(d["Latitude"]), 4),
                        "lon": round(float(d["Longitude"]), 4),
                        "cap": int(cap) if cap else 0,
                    })
            wb.close()
    except Exception as e:
        print(f"  ⚠ Could not load xlsx for LNG geo: {e}")
    if not lng_geo:
        lng_geo = [{"name":"Zeebrugge LNG","country":"Belgium","lat":51.35,"lon":3.22,"cap":566000},{"name":"Gate Terminal","country":"Netherlands","lat":51.97,"lon":4.05,"cap":540000},{"name":"Dunkerque LNG","country":"France","lat":51.03,"lon":2.19,"cap":600000},{"name":"Grain LNG","country":"United Kingdom","lat":51.45,"lon":0.69,"cap":1000000},{"name":"South Hook LNG","country":"United Kingdom","lat":51.69,"lon":-5.05,"cap":775000},{"name":"Barcelona LNG","country":"Spain","lat":41.34,"lon":2.15,"cap":760000},{"name":"Sines LNG","country":"Portugal","lat":37.94,"lon":-8.84,"cap":390000},{"name":"Swinoujscie LNG","country":"Poland","lat":53.91,"lon":14.29,"cap":320000},{"name":"Revithoussa LNG","country":"Greece","lat":37.96,"lon":23.40,"cap":225000},{"name":"Klaipeda FSRU","country":"Lithuania","lat":55.66,"lon":21.14,"cap":170000}]
    lng_geo = json.dumps(lng_geo)

    # ── Pipeline point geocoding ──
    PIPE_COORDS = {"Dornum":(53.64,7.43),"NETRA":(52.50,10.40),"Emden":(53.37,7.21),"Easington":(53.65,0.12),"St. Fergus":(57.58,-1.83),"Milford Haven":(51.70,-5.05),"Bacton":(52.87,1.45),"Mazara del Vallo":(37.65,12.59),"Tarvisio":(46.50,13.58),"Waidhaus":(49.64,12.50),"Mallnow":(52.46,14.50),"Zeebrugge":(51.33,3.20),"Dunkerque":(51.03,2.38),"Montoir":(47.30,-2.14),"Fos":(43.43,4.87),"Gate Terminal":(51.97,4.05),"Bunde":(53.18,7.25),"Oude":(53.15,7.20),"Obergailbach":(49.14,7.14),"Strandzha":(41.82,27.75),"VIP THE-ZTP":(51.00,10.00),"Baumgarten":(48.34,16.92),"Oberkappel":(48.55,13.83),"Velke Kapusany":(48.68,22.07),"Lanzhot":(48.72,16.97),"Greifswald":(54.10,13.38),"Lubmin":(54.14,13.67),"Nybro":(56.75,15.90),"Ellund":(54.83,9.33),"Eynatten":(50.68,6.08),"Tegelen":(51.34,6.16),"TVB":(40.5,-3.7),"Distribution":(42.0,12.5),"Power Stations":(52.0,-1.0),"GRTgaz":(48.5,2.5)}
    pipe_geo = []
    for f in fl:
        label = f.get("label", "")
        matched = None
        for kw, coords in PIPE_COORDS.items():
            if kw.lower() in label.lower():
                matched = coords
                break
        if matched:
            pipe_geo.append({"label":label,"lat":matched[0],"lon":matched[1],"dir":f.get("direction",""),"flow":f.get("latest",0),"avg":f.get("avg",0),"corridor":f.get("corridor",""),"op":f.get("operator","")})
    pipe_geo = json.dumps(pipe_geo)

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>European Gas Intelligence Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#06090f;--bg2:#0c1018;--bg3:#121820;--bd:#1a2332;--bd2:#243044;--tx:#e8ecf2;--t2:#9aa8bc;--t3:#5e6e84;--ac:#00d4aa;--acd:#00a888;--ac2:rgba(0,212,170,.08);--gn:#34d399;--rd:#f87171;--or:#fbbf24;--bl:#60a5fa;--pr:#a78bfa;--tl:#2dd4bf;--cy:#22d3ee;--r:10px}}
*{{margin:0;padding:0;box-sizing:border-box}}body{{background:var(--bg);color:var(--tx);font-family:'DM Sans',sans-serif;line-height:1.6}}::selection{{background:var(--ac);color:var(--bg)}}
.hdr{{padding:28px 32px 0;background:linear-gradient(180deg,var(--bg2),var(--bg));border-bottom:1px solid var(--bd);position:relative}}.hdr::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--ac),var(--cy),var(--ac));opacity:.6}}
.hdr-top{{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:16px}}
.hdr-lbl{{font-size:10px;color:var(--ac);text-transform:uppercase;letter-spacing:4px;font-family:'IBM Plex Mono',monospace;font-weight:600;margin-bottom:6px}}
.hdr-ttl{{font-size:28px;font-weight:700;letter-spacing:-.5px;background:linear-gradient(135deg,var(--tx) 30%,var(--ac));-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.hdr-meta{{text-align:right;font-size:10px;color:var(--t3);font-family:'IBM Plex Mono',monospace;line-height:1.8}}.hdr-meta span{{color:var(--t2)}}
.tabs{{display:flex;gap:2px;padding:20px 0 0;overflow-x:auto}}.tab{{background:transparent;border:1px solid transparent;border-bottom:none;border-radius:var(--r) var(--r) 0 0;padding:10px 22px;cursor:pointer;color:var(--t3);font-size:12px;font-family:'IBM Plex Mono',monospace;font-weight:500;transition:all .25s;white-space:nowrap}}.tab:hover{{color:var(--t2);background:var(--bg2)}}.tab.active{{background:var(--bg);color:var(--ac);border-color:var(--bd);font-weight:600}}
.tc{{display:none}}.tc.active{{display:block}}.main{{padding:28px 32px 48px;max-width:1440px;margin:0 auto}}
.mg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:28px}}.mc{{background:var(--bg2);border-radius:var(--r);padding:18px 22px;border:1px solid var(--bd);position:relative;overflow:hidden;transition:border-color .2s}}.mc:hover{{border-color:var(--bd2)}}.mc-accent{{position:absolute;top:0;left:0;right:0;height:2px}}.mc-lbl{{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;font-family:'IBM Plex Mono',monospace;font-weight:500}}.mc-val{{font-size:28px;font-weight:700;line-height:1.1}}.mc-sub{{font-size:11px;color:var(--t3);margin-top:6px;font-family:'IBM Plex Mono',monospace}}
.gauge{{position:relative;height:48px;background:var(--bg);border-radius:var(--r);overflow:visible;border:1px solid var(--bd)}}.gauge-f{{position:absolute;left:0;top:0;bottom:0;border-radius:var(--r);transition:width 1.5s cubic-bezier(.16,1,.3,1)}}.gauge-t{{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--rd);opacity:.7;z-index:2}}.gauge-tl{{position:absolute;top:-20px;font-size:9px;color:var(--rd);transform:translateX(-50%);font-family:'IBM Plex Mono',monospace;font-weight:600}}.gauge-v{{position:absolute;right:16px;top:50%;transform:translateY(-50%);font-size:20px;font-weight:700;color:var(--tx);text-shadow:0 2px 8px rgba(0,0,0,.8)}}
.pnl{{background:var(--bg2);border-radius:var(--r);padding:22px;border:1px solid var(--bd);margin-bottom:24px;overflow-x:auto}}.st{{font-size:17px;font-weight:700;color:var(--tx);margin-bottom:4px}}.ss{{font-size:11px;color:var(--t3);margin-bottom:18px;font-family:'IBM Plex Mono',monospace}}
.cw{{background:var(--bg2);border-radius:var(--r);padding:22px;border:1px solid var(--bd);margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;font-size:12px;font-family:'IBM Plex Mono',monospace}}thead tr{{border-bottom:1px solid var(--bd)}}th{{padding:10px 14px;color:var(--t3);font-size:9px;text-transform:uppercase;letter-spacing:2px;font-weight:600}}td{{padding:11px 14px;border-bottom:1px solid rgba(26,35,50,.4)}}.cl{{text-align:left}}.cr{{text-align:right}}tr:hover td{{background:rgba(0,212,170,.02)}}tr.alt{{background:rgba(10,14,23,.3)}}
.fb-w{{display:flex;align-items:center;justify-content:flex-end;gap:10px}}.fb-bg{{width:64px;height:5px;background:var(--bg);border-radius:3px;overflow:hidden;border:1px solid var(--bd)}}.fb{{height:100%;border-radius:3px;transition:width .8s}}.fp{{min-width:52px;text-align:right;font-weight:600}}
.accent{{color:var(--ac)}}.green{{color:var(--gn)}}.red{{color:var(--rd)}}.orange{{color:var(--or)}}.blue{{color:var(--bl)}}.purple{{color:var(--pr)}}.teal{{color:var(--tl)}}.dim{{color:var(--t3)}}
.fb.green{{background:var(--gn)}}.fb.accent{{background:var(--ac)}}.fb.orange{{background:var(--or)}}.fb.red{{background:var(--rd)}}.fb.dim{{background:var(--td)}}
.db{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}}.db.green{{background:rgba(16,185,129,.12);color:var(--gn);border:1px solid rgba(16,185,129,.2)}}.db.orange{{background:rgba(245,158,11,.12);color:var(--or);border:1px solid rgba(245,158,11,.2)}}
.sel{{background:var(--bg2);color:var(--tx);border:1px solid var(--bd);border-radius:6px;padding:7px 14px;font-size:12px;font-family:'IBM Plex Mono',monospace;cursor:pointer}}.ut{{display:inline-flex;border-radius:6px;overflow:hidden;border:1px solid var(--bd)}}.ub{{background:transparent;border:none;color:var(--t3);padding:7px 16px;font-size:10px;cursor:pointer;font-family:'IBM Plex Mono',monospace;font-weight:500;transition:all .2s;letter-spacing:.5px}}.ub:hover{{color:var(--t2)}}.ub.active{{background:var(--ac);color:var(--bg);font-weight:600}}
.cl-w{{display:flex;gap:18px;justify-content:center;flex-wrap:wrap;margin-top:14px}}.cl-i{{display:flex;align-items:center;gap:7px;font-size:11px;font-family:'IBM Plex Mono',monospace}}.cl-s{{width:22px;height:3px;border-radius:2px}}
.band-toggle{{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--t2);font-family:'IBM Plex Mono',monospace;cursor:pointer;user-select:none}}.band-toggle input{{accent-color:var(--ac)}}
.fl{{margin-right:8px}}.bold{{font-weight:600}}
.map-grid{{display:grid;grid-template-columns:1fr 320px;gap:24px;margin-bottom:24px}}.map-svg-wrap{{background:var(--bg2);border-radius:var(--r);border:1px solid var(--bd);padding:20px;display:flex;align-items:center;justify-content:center;min-height:480px}}.map-legend{{background:var(--bg2);border-radius:var(--r);border:1px solid var(--bd);padding:20px;overflow-y:auto;max-height:540px}}
.map-layer-btn{{background:var(--bg2);border:1px solid var(--bd);color:var(--t3);padding:7px 16px;border-radius:8px;font-size:11px;font-family:'IBM Plex Mono',monospace;cursor:pointer;transition:all .25s;font-weight:500;letter-spacing:.3px}}.map-layer-btn:hover{{border-color:var(--ac);color:var(--t2);background:var(--bg3)}}.map-layer-btn.active{{background:linear-gradient(135deg,rgba(0,212,170,.12),rgba(0,212,170,.06));border-color:var(--ac);color:var(--ac);font-weight:600;box-shadow:0 0 12px rgba(0,212,170,.1)}}
.map-row{{display:flex;align-items:center;gap:12px;padding:8px 6px;border-bottom:1px solid rgba(26,35,50,.3);cursor:pointer;transition:background .15s;border-radius:4px}}.map-row:hover{{background:var(--ac2)}}.map-fill-bar{{flex:1;height:4px;background:var(--bg);border-radius:2px;overflow:hidden;min-width:60px}}.map-fill-val{{height:100%;border-radius:2px;transition:width .6s}}.map-pct{{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;min-width:42px;text-align:right}}.map-name{{font-size:12px;min-width:90px;white-space:nowrap}}
.note{{padding:16px;background:var(--bg2);border-radius:10px;border:1px solid var(--bd);font-size:12px;color:var(--t3);font-family:'IBM Plex Mono',monospace;margin-top:16px}}
.ftr{{padding:20px 32px;border-top:1px solid var(--bd);font-size:9px;color:var(--t3);font-family:'IBM Plex Mono',monospace;text-align:center;letter-spacing:.5px;line-height:1.8}}
@media(max-width:900px){{.map-grid{{grid-template-columns:1fr}}.hdr,.main{{padding-left:16px;padding-right:16px}}.hdr-ttl{{font-size:20px}}.mg{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:600px){{.mg{{grid-template-columns:1fr}}}}
</style></head><body>

<div class="hdr"><div class="hdr-top"><div><div class="hdr-lbl">\u26a1 European Gas Intelligence</div><div class="hdr-ttl">Storage \u00b7 Flows \u00b7 LNG Monitor</div></div><div class="hdr-meta">DATA SOURCES: <span>GIE AGSI \u00b7 GIE ALSI \u00b7 ENTSOG TP \u00b7 National Gas</span><br>Generated: <span>{ns}</span></div></div>
<div class="tabs"><button class="tab active" onclick="stab('intel')">\U0001f6a8 Intelligence</button><button class="tab" onclick="stab('overview')">\u26fd Storage Overview</button><button class="tab" onclick="stab('season')">\U0001f4c8 Seasonality</button><button class="tab" onclick="stab('map')">\U0001f5fa\ufe0f Map</button><button class="tab" onclick="stab('lng')">\U0001f6a2 LNG Terminals</button><button class="tab" onclick="stab('flows')">\U0001f500 Pipeline Flows</button></div></div>

<div class="main">

<!-- ── INTELLIGENCE ── -->
{intel_html}

<!-- ── STORAGE OVERVIEW ── -->
<div id="tab-overview" class="tc">
<div style="display:flex;justify-content:flex-end;margin-bottom:16px"><div class="ut"><button class="ub active" id="btn-twh" onclick="su('twh')">TWh / GWh</button><button class="ub" id="btn-bcm" onclick="su('bcm')">bcm / mcm</button></div></div>
<div class="mg">
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--ac),transparent)"></div><div class="mc-lbl">EU Gas in Storage</div><div class="mc-val accent"><span class="uv" data-t="{eg}" data-b="{egb}">{eg} TWh</span></div><div class="mc-sub">of <span class="uv" data-t="{ew}" data-b="{ewb}">{ew} TWh</span> WGV</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--{efc}),transparent)"></div><div class="mc-lbl">Fill Level</div><div class="mc-val {efc}">{ef}% <span style="font-size:14px;margin-left:8px" class="{etc_}">{ta(eu.get("trend"))}</span></div><div class="mc-sub">Target: 90% by Nov 1</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--or),transparent)"></div><div class="mc-lbl">Withdrawal</div><div class="mc-val orange"><span class="uf" data-g="{ewd}" data-m="{ewm}">{ewd} GWh/d</span></div><div class="mc-sub">During gas day</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--gn),transparent)"></div><div class="mc-lbl">Injection</div><div class="mc-val green"><span class="uf" data-g="{ei}" data-m="{eim}">{ei} GWh/d</span></div><div class="mc-sub">During gas day</div></div>
</div>
<div class="pnl"><div class="st">EU Aggregate Storage Level</div><div class="ss">Gas day: {gd}</div><div class="gauge"><div class="gauge-f" style="width:{min(eff,100):.0f}%;background:linear-gradient(90deg,var(--acd),var(--ac))"></div><div class="gauge-t" style="left:90%"><div class="gauge-tl">90% target</div></div><div class="gauge-v">{ef}%</div></div></div>
<div class="cw"><div class="st">Storage Fill by Country</div><div class="ss">Current fill % \u2014 colour coded by level</div><div style="height:400px"><canvas id="oc"></canvas></div></div>
<div class="pnl"><div class="st">Country Breakdown</div><div class="ss">Sorted by gas in storage \u2014 GB via National Gas REST API</div><table><thead><tr><th class="cl">Country</th><th class="cr">Fill %</th><th class="cr">Gas in Storage</th><th class="cr">WGV</th><th class="cr">Injection</th><th class="cr">Withdrawal</th><th class="cr">Trend</th><th class="cr">vs LY</th></tr></thead><tbody>{cr}</tbody></table></div>
</div>

<!-- ── SEASONALITY ── -->
<div id="tab-season" class="tc">
<div style="display:flex;align-items:center;gap:16px;margin-bottom:22px;flex-wrap:wrap">
<div><div class="st">Storage Seasonality</div><div class="ss">Current year vs 5-year historical band</div></div>
<select class="sel" id="ss" onchange="updateSeason()">{so_html}</select>
<label class="band-toggle"><input type="checkbox" id="bandToggle" checked onchange="updateSeason()"> Show 5-year band</label>
</div>
<div class="cw"><div style="height:480px"><canvas id="sc"></canvas></div><div class="cl-w" id="sl"></div></div>
</div>

<!-- ── MAP ── -->
<div id="tab-map" class="tc">
<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:16px">
<div><div class="st">European Gas Infrastructure Map</div>
<div class="ss">94 storage facilities · LNG terminals · Pipeline interconnection points</div></div>
<div style="display:flex;gap:6px;flex-wrap:wrap">
<button class="map-layer-btn active" id="ml-storage" onclick="toggleLayer('storage')">⛽ Storage</button>
<button class="map-layer-btn active" id="ml-lng" onclick="toggleLayer('lng')">🚢 LNG</button>
<button class="map-layer-btn active" id="ml-pipes" onclick="toggleLayer('pipes')">🔀 Pipelines</button>
</div></div>
<div class="map-grid" style="grid-template-columns:1fr 280px">
<div style="background:var(--bg2);border-radius:var(--r);border:1px solid var(--bd);overflow:hidden;min-height:560px"><div id="leaflet-map" style="width:100%;height:560px;background:#06090f"></div></div>
<div class="map-legend" style="max-height:560px">
<div style="margin-bottom:14px"><div class="st" style="font-size:14px">Country Rankings</div><div class="ss" style="margin-bottom:8px">Sorted by fill level</div>
<div style="display:flex;gap:4px;align-items:center;margin-bottom:12px"><div style="flex:1;height:6px;border-radius:3px;background:linear-gradient(90deg,var(--rd),var(--or),var(--gn))"></div><span style="font-size:9px;color:var(--t3);font-family:'IBM Plex Mono',monospace">0% → 100%</span></div>
</div><div id="map-rankings"></div>
<div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--bd)">
<div class="st" style="font-size:12px;margin-bottom:10px">Legend</div>
<div style="display:flex;flex-direction:column;gap:6px;font-size:10px;font-family:'IBM Plex Mono',monospace;color:var(--t3)">
<div style="display:flex;align-items:center;gap:8px"><div style="width:12px;height:12px;border-radius:50%;border:2px solid #00d4aa;background:rgba(0,212,170,0.3)"></div> Storage facility</div>
<div style="display:flex;align-items:center;gap:8px"><div style="width:12px;height:12px;border-radius:50%;border:2px solid #60a5fa;background:rgba(96,165,250,0.3)"></div> LNG terminal</div>
<div style="display:flex;align-items:center;gap:8px"><div style="width:12px;height:12px;border-radius:50%;border:2px solid #a78bfa;background:rgba(167,139,250,0.3)"></div> Pipeline entry</div>
<div style="display:flex;align-items:center;gap:8px"><div style="width:12px;height:12px;border-radius:50%;border:2px solid #f87171;background:rgba(248,113,113,0.3)"></div> Pipeline exit</div>
</div></div></div></div></div>

<!-- ── LNG ── -->
<div id="tab-lng" class="tc">
<div class="mg">
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--tl),transparent)"></div><div class="mc-lbl">EU LNG Inventory</div><div class="mc-val teal">{li}</div><div class="mc-sub">1000 m³ LNG</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--bl),transparent)"></div><div class="mc-lbl">Total Send-Out</div><div class="mc-val blue">{ls} GWh/d</div><div class="mc-sub">Capacity: {ld} GWh/d</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--pr),transparent)"></div><div class="mc-lbl">Utilization</div><div class="mc-val purple">{lu}</div><div class="mc-sub">Send-out / DTRS</div></div>
</div>
<div class="pnl"><div class="st">LNG Terminal Data by Country</div><div class="ss">Countries with active terminals — inventory in 1000 m³ LNG</div><table><thead><tr><th class="cl">Country</th><th class="cr">Send-Out (GWh/d)</th><th class="cr">Inventory (1000 m³)</th><th class="cr">DTRS Capacity</th><th class="cr">Utilization</th></tr></thead><tbody>{lr}</tbody></table></div>
</div>

<!-- ── FLOWS ── -->
<div id="tab-flows" class="tc">
<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:16px">
<div><div class="st">Cross-Border Pipeline Flows</div>
<div class="ss">ENTSOG physical flows — last 7 days — {len(fl)} interconnection points — GWh/d</div></div>
<div style="display:flex;gap:6px;flex-wrap:wrap" id="flow-filter-btns">
<button class="map-layer-btn active" onclick="filterFlows('all',this)">All</button>
<button class="map-layer-btn" onclick="filterFlows('entry',this)">▶ Entry</button>
<button class="map-layer-btn" onclick="filterFlows('exit',this)">◀ Exit</button>
</div></div>
{corridor_html}
<div class="note">📝 ENTSOG Transparency Platform — public API, no key required. Flows in kWh/d converted to GWh/d.</div>
</div>
</div>

<div class="ftr">Data: GIE (AGSI/ALSI) · ENTSOG Transparency Platform · National Gas Transmission (UK) · AGSI/ALSI updated daily at 19:30 CET<br>EU Regulation 2022/1032 mandates 90% storage fill by November 1 each year<br>GB data from National Gas REST API (primary) · AGSI NE-UA fallback · 94 storage facilities geocoded</div>

<script>
const CD={cj};const SD={sd_js};const BANDS={bands_js};const YC={yc};const CY=String({cy});const MAPD={map_data};
const LNG_GEO={lng_geo};const PIPE_GEO={pipe_geo};const STORAGE_GEO={storage_geo};
const DEEP={country_deep_js};

function su(u){{document.getElementById('btn-twh').classList.toggle('active',u==='twh');document.getElementById('btn-bcm').classList.toggle('active',u==='bcm');document.querySelectorAll('.uv').forEach(e=>{{e.textContent=u==='bcm'?e.dataset.b+' bcm':e.dataset.t+' TWh'}});document.querySelectorAll('.uf').forEach(e=>{{e.textContent=u==='bcm'?e.dataset.m+' mcm/d':e.dataset.g+' GWh/d'}})}}
function stab(id){{document.querySelectorAll('.tc').forEach(e=>e.classList.remove('active'));document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));document.getElementById('tab-'+id).classList.add('active');const tn={{overview:'Overview',season:'Seasonality',map:'Map',lng:'LNG',flows:'Flows'}};document.querySelectorAll('.tab').forEach(e=>{{if(e.textContent.includes(tn[id]||''))e.classList.add('active')}});if(id==='season'&&!window._si){{setTimeout(updateSeason,200);window._si=true}};if(id==='map'&&!window._mi){{setTimeout(initMap,200);window._mi=true}}}}
function fillColor(f){{return f>80?'#34d399':f>50?'#00d4aa':f>30?'#fbbf24':'#f87171'}}
function initOverviewChart(){{try{{if(typeof Chart==='undefined')return;const filtered=CD.filter(c=>c.fill>0);new Chart(document.getElementById('oc'),{{type:'bar',data:{{labels:filtered.map(c=>c.code),datasets:[{{data:filtered.map(c=>c.fill),backgroundColor:filtered.map(c=>fillColor(c.fill)+'99'),borderColor:filtered.map(c=>fillColor(c.fill)),borderWidth:1,borderRadius:5,borderSkipped:false}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#0c1018',borderColor:'#1a2332',borderWidth:1,callbacks:{{label:i=>filtered[i.dataIndex].name+': '+i.parsed.y.toFixed(1)+'%'}}}}}},scales:{{y:{{min:0,max:100,ticks:{{callback:v=>v+'%',color:'#5e6e84',font:{{family:"'IBM Plex Mono',monospace",size:10}}}},grid:{{color:'#1a2332'}}}},x:{{ticks:{{color:'#9aa8bc',font:{{family:"'IBM Plex Mono',monospace",size:10}}}},grid:{{display:false}}}}}}}}}})}}catch(e){{console.error('Chart:',e)}}}}
let sC=null;function updateSeason(){{try{{if(typeof Chart==='undefined')return;const r=document.getElementById('ss').value;const sd=SD[r]||{{}};const band=BANDS[r];const showBand=document.getElementById('bandToggle').checked;const c=document.getElementById('sc');if(!c)return;if(sC)sC.destroy();const ds=[];if(showBand&&band){{ds.push({{label:'5yr Max',data:band.max,borderColor:'transparent',backgroundColor:'rgba(0,212,170,0.08)',fill:'+1',pointRadius:0,tension:.3,order:10}});ds.push({{label:'5yr Min',data:band.min,borderColor:'transparent',backgroundColor:'rgba(0,212,170,0.08)',fill:false,pointRadius:0,tension:.3,order:10}});ds.push({{label:'5yr Avg',data:band.avg,borderColor:'rgba(0,212,170,0.35)',backgroundColor:'transparent',borderWidth:1.5,borderDash:[6,4],pointRadius:0,tension:.3,order:5}})}};Object.keys(sd).sort().forEach(y=>{{const p=sd[y];if(!p||!p.length)return;ds.push({{label:y,data:p,borderColor:YC[y]||'#5e6e84',backgroundColor:'transparent',borderWidth:y===CY?3:1.2,pointRadius:0,tension:.3,order:y===CY?0:2}})}});if(!ds.length){{c.parentElement.innerHTML='<p style="color:#5e6e84;text-align:center;padding:60px;font-family:monospace">No data</p>';return}};sC=new Chart(c,{{type:'line',data:{{datasets:ds}},options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},scales:{{x:{{type:'linear',min:1,max:365,ticks:{{callback:v=>{{const m=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];const d=[1,32,60,91,121,152,182,213,244,274,305,335];for(let i=0;i<12;i++)if(i<11?v>=d[i]&&v<d[i+1]:v>=d[i])return m[i];return''}},maxTicksLimit:12,color:'#5e6e84',font:{{family:"'IBM Plex Mono',monospace",size:10}}}},grid:{{color:'#1a2332'}}}},y:{{min:0,max:100,ticks:{{callback:v=>v+'%',color:'#5e6e84',font:{{family:"'IBM Plex Mono',monospace",size:10}}}},grid:{{color:'#1a2332'}}}}}},plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#0c1018',borderColor:'#1a2332',borderWidth:1,titleFont:{{family:"'IBM Plex Mono',monospace",size:10}},bodyFont:{{family:"'IBM Plex Mono',monospace",size:11}},filter:item=>!['5yr Max','5yr Min'].includes(item.dataset.label),callbacks:{{title:items=>{{if(!items.length)return'';const doy=items[0].parsed.x;const m=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];const days=[31,28,31,30,31,30,31,31,30,31,30,31];let dd=doy,mi=0;for(mi=0;mi<12;mi++){{if(dd<=days[mi])break;dd-=days[mi]}};return m[mi]+' '+dd}},label:i=>i.dataset.label+': '+i.parsed.y.toFixed(1)+'%'}}}}}}}}}});const lg=document.getElementById('sl');let html='';if(showBand&&band){{html+='<div class="cl-i"><div class="cl-s" style="background:rgba(0,212,170,0.3)"></div><span style="color:var(--t2)">5-year band</span></div>';html+='<div class="cl-i"><div class="cl-s" style="background:rgba(0,212,170,0.35);border-top:1px dashed rgba(0,212,170,0.5)"></div><span style="color:var(--t2)">5yr avg</span></div>'}};ds.filter(d=>!d.label.startsWith('5yr')).forEach(d=>{{html+='<div class="cl-i"><div class="cl-s" style="background:'+d.borderColor+'"></div><span style="color:'+d.borderColor+';font-weight:'+(d.label===CY?700:400)+'">'+d.label+(d.label===CY?' (current)':'')+'</span></div>'}});lg.innerHTML=html}}catch(e){{console.error('Season:',e)}}}}
function filterFlows(dir,btn){{document.querySelectorAll('#flow-filter-btns .map-layer-btn').forEach(b=>b.classList.remove('active'));if(btn)btn.classList.add('active');document.querySelectorAll('.flow-row').forEach(r=>{{r.style.display=(dir==='all'||r.dataset.dir===dir)?'':'none'}})}}
let gMap=null;const mapLayers={{storage:null,lng:null,pipes:null}};
function toggleLayer(key){{const btn=document.getElementById('ml-'+key);if(!btn||!mapLayers[key])return;btn.classList.toggle('active');if(btn.classList.contains('active'))gMap.addLayer(mapLayers[key]);else gMap.removeLayer(mapLayers[key])}}
function popHtml(t,col,lines){{return'<div style="font-family:IBM Plex Mono,monospace;font-size:11px;line-height:1.7;min-width:160px"><div style="font-weight:700;font-size:13px;margin-bottom:2px;color:'+col+'">'+t+'</div>'+lines.join('')+'</div>'}}
function initMap(){{try{{if(typeof L==='undefined')return;const el=document.getElementById('leaflet-map');if(!el)return;gMap=L.map(el,{{zoomControl:true,attributionControl:false,minZoom:3,maxZoom:12}}).setView([50,12],4);L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_nolabels/{{z}}/{{x}}/{{y}}{{r}}.png',{{subdomains:'abcd',maxZoom:18}}).addTo(gMap);L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_only_labels/{{z}}/{{x}}/{{y}}{{r}}.png',{{subdomains:'abcd',maxZoom:18,opacity:0.5}}).addTo(gMap);L.control.attribution({{position:'bottomright',prefix:false}}).addAttribution('© CartoDB · OSM').addTo(gMap);const sg=L.layerGroup();STORAGE_GEO.forEach(s=>{{const f=s.fill||0,col=fillColor(f),r=Math.max(4,Math.min(16,Math.sqrt(s.wgv||1)*1.4));const m=L.circleMarker([s.lat,s.lon],{{radius:r,color:col,fillColor:col,fillOpacity:0.25,weight:2,opacity:0.9}});m.bindPopup(popHtml(s.name,col,['<div style="color:#9aa8bc">'+s.country+'</div>','<div style="margin-top:4px"><b>Fill:</b> <span style="color:'+col+'">'+f.toFixed(1)+'%</span></div>','<div><b>WGV:</b> '+s.wgv+' TWh</div>',s.inj>0?'<div style="color:#34d399">▲ '+s.inj.toFixed(1)+' GWh/d</div>':'',s.wdr>0?'<div style="color:#fbbf24">▼ '+s.wdr.toFixed(1)+' GWh/d</div>':'']),{{className:'dark-popup'}});m.bindTooltip(s.name+': '+f.toFixed(1)+'%',{{permanent:false,direction:'top',offset:[0,-r],className:'dark-tooltip'}});sg.addLayer(m)}});sg.addTo(gMap);mapLayers.storage=sg;const lg2=L.layerGroup();LNG_GEO.forEach(t=>{{const r=Math.max(3,Math.min(10,t.cap>0?Math.sqrt(t.cap/80000):3));const m=L.circleMarker([t.lat,t.lon],{{radius:r,color:'#60a5fa',fillColor:'#60a5fa',fillOpacity:0.25,weight:1.5,opacity:0.8}});m.bindPopup(popHtml(t.name,'#60a5fa',['<div style="color:#9aa8bc">'+t.country+(t.port?' · '+t.port:'')+'</div>',t.cap>0?'<div style="margin-top:4px"><b>Cap:</b> '+Number(t.cap).toLocaleString()+' m³</div>':'',t.so?'<div style="color:#60a5fa"><b>Send-out:</b> '+t.so.toFixed(1)+' GWh/d</div>':'']),{{className:'dark-popup'}});m.bindTooltip(t.name,{{permanent:false,direction:'top',offset:[0,-r],className:'dark-tooltip'}});lg2.addLayer(m)}});lg2.addTo(gMap);mapLayers.lng=lg2;const pg=L.layerGroup();PIPE_GEO.forEach(p=>{{const isE=p.dir==='entry',col=isE?'#a78bfa':'#f87171';const r=Math.max(4,Math.min(12,Math.sqrt(Math.abs(p.flow||1))*0.5));const m=L.circleMarker([p.lat,p.lon],{{radius:r,color:col,fillColor:col,fillOpacity:0.3,weight:2,opacity:0.9}});m.bindPopup(popHtml(p.label,col,[p.corridor?'<div style="color:#9aa8bc;font-size:10px">'+p.corridor+'</div>':'','<div style="margin-top:4px">'+(isE?'→ Entry':'→ Exit')+'</div>','<div><b>Latest:</b> <span style="color:'+col+'">'+p.flow.toFixed(1)+' GWh/d</span></div>','<div><b>7d avg:</b> '+p.avg.toFixed(1)+' GWh/d</div>',p.op?'<div style="color:#5e6e84;font-size:10px;margin-top:2px">'+p.op+'</div>':'']),{{className:'dark-popup'}});m.bindTooltip(p.label+': '+p.flow.toFixed(1),{{permanent:false,direction:'top',offset:[0,-r],className:'dark-tooltip'}});pg.addLayer(m)}});pg.addTo(gMap);mapLayers.pipes=pg;const rankings=document.getElementById('map-rankings');const sorted=CD.filter(c=>c.fill>0).sort((a,b)=>b.fill-a.fill);rankings.innerHTML=sorted.map(c=>'<div class="map-row" onclick="mapClick(\\x27'+c.code.toLowerCase()+'\\x27)"><span class="map-name">'+c.flag+' '+c.name+'</span><div class="map-fill-bar"><div class="map-fill-val" style="width:'+c.fill+'%;background:'+fillColor(c.fill)+'"></div></div><span class="map-pct" style="color:'+fillColor(c.fill)+'">'+c.fill.toFixed(1)+'%</span></div>').join('');setTimeout(()=>gMap.invalidateSize(),100)}}catch(e){{console.error('Map:',e)}}}}
function mapClick(code){{
  const d=DEEP[code];
  if(!d){{stab('season');document.getElementById('ss').value=code;updateSeason();return}}
  const p=document.getElementById('deep-panel');
  const col=fillColor(d.fill);
  const daysStr=d.days?d.days:'n/a';
  const daysCol=d.days?(d.days>90?'#34d399':d.days>60?'#00d4aa':d.days>30?'#fbbf24':'#f87171'):'#5e6e84';
  const netCol=d.net>0?'#fbbf24':'#34d399';
  const netDir=d.net>0?'▼ Drawing':'▲ Injecting';
  let html='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px"><div style="font-size:22px;font-weight:700">'+d.flag+' '+d.name+'</div><button onclick="closeDeep()" style="background:none;border:none;color:var(--t3);font-size:20px;cursor:pointer;padding:4px 8px">\u2715</button></div>';
  // Key metrics
  html+='<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:20px">';
  html+='<div style="background:var(--bg);border-radius:8px;padding:14px;border:1px solid var(--bd)"><div style="font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Fill Level</div><div style="font-size:24px;font-weight:700;color:'+col+'">'+d.fill.toFixed(1)+'%</div></div>';
  html+='<div style="background:var(--bg);border-radius:8px;padding:14px;border:1px solid var(--bd)"><div style="font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Days of Supply</div><div style="font-size:24px;font-weight:700;color:'+daysCol+'">'+daysStr+'</div></div>';
  html+='<div style="background:var(--bg);border-radius:8px;padding:14px;border:1px solid var(--bd)"><div style="font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Gas in Storage</div><div style="font-size:18px;font-weight:600;color:var(--ac)">'+d.gas.toFixed(1)+' TWh</div><div style="font-size:10px;color:var(--t3)">of '+d.wgv.toFixed(1)+' TWh WGV</div></div>';
  html+='<div style="background:var(--bg);border-radius:8px;padding:14px;border:1px solid var(--bd)"><div style="font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Net Flow</div><div style="font-size:18px;font-weight:600;color:'+netCol+'">'+netDir+'</div><div style="font-size:10px;color:var(--t3)">'+Math.abs(d.net).toFixed(1)+' GWh/d</div></div>';
  html+='</div>';
  // vs 5yr
  if(d.vs_avg!==null){{
    const vsCol=d.vs_avg>0?'#34d399':(d.vs_avg<-5?'#f87171':'#fbbf24');
    html+='<div style="padding:10px 14px;background:var(--bg);border-radius:8px;border:1px solid var(--bd);margin-bottom:16px;font-size:12px;font-family:IBM Plex Mono,monospace"><span style="color:var(--t3)">vs 5yr avg:</span> <span style="color:'+vsCol+';font-weight:600">'+((d.vs_avg>0?'+':'')+d.vs_avg.toFixed(1))+'pp</span>';
    if(d.below_min)html+=' <span style="color:#f87171;font-weight:600">\u26a0 Below 5yr minimum ('+d.hist_min+'%)</span>';
    html+='</div>';
  }}
  // Weather
  if(d.temps&&d.temps.length){{
    html+='<div style="margin-bottom:16px"><div style="font-size:12px;font-weight:600;margin-bottom:8px;color:var(--t2)">7-Day Temperature Forecast</div>';
    html+='<div style="display:flex;gap:4px;align-items:end;height:40px">';
    d.temps.forEach(t=>{{
      if(t===null)return;
      const h=Math.max(4,Math.abs(t)*2);
      const c=t<0?'#60a5fa':(t<10?'#a78bfa':'#34d399');
      html+='<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="width:100%;height:'+h+'px;background:'+c+';border-radius:2px;min-height:4px"></div><div style="font-size:8px;color:var(--t3)">'+t.toFixed(0)+'\u00b0</div></div>';
    }});
    html+='</div>';
    if(d.hdd_7d!==null)html+='<div style="font-size:10px;color:var(--t3);margin-top:6px;font-family:IBM Plex Mono,monospace">7d HDD: '+d.hdd_7d.toFixed(0)+' \u2014 '+(d.hdd_7d>80?'<span style=color:#f87171>High heating demand</span>':(d.hdd_7d>40?'<span style=color:#fbbf24>Moderate demand</span>':'<span style=color:#34d399>Low demand</span>'))+'</div>';
    html+='</div>';
  }}
  // Facilities
  if(d.facilities&&d.facilities.length){{
    html+='<div style="margin-bottom:16px"><div style="font-size:12px;font-weight:600;margin-bottom:6px;color:var(--t2)">\u26fd Storage Facilities ('+d.facilities.length+')</div>';
    html+='<div style="font-size:11px;color:var(--t3);font-family:IBM Plex Mono,monospace;line-height:1.8">'+d.facilities.join(' \u00b7 ')+'</div></div>';
  }}
  // LNG
  if(d.lng&&d.lng.length){{
    html+='<div style="margin-bottom:16px"><div style="font-size:12px;font-weight:600;margin-bottom:6px;color:var(--t2)">\U0001f6a2 LNG Terminals</div>';
    d.lng.forEach(l=>{{html+='<div style="font-size:11px;font-family:IBM Plex Mono,monospace;color:var(--t3);padding:3px 0"><span style="color:#60a5fa">'+l.name+'</span>'+(l.so?' \u2014 '+l.so.toFixed(1)+' GWh/d':'')+'</div>'}});
    html+='</div>';
  }}
  // Pipeline flows
  if(d.pipes&&d.pipes.length){{
    html+='<div style="margin-bottom:16px"><div style="font-size:12px;font-weight:600;margin-bottom:6px;color:var(--t2)">\U0001f500 Connected Pipelines</div>';
    d.pipes.forEach(pp=>{{
      const pc=pp.dir==='entry'?'#a78bfa':'#f87171';
      html+='<div style="font-size:11px;font-family:IBM Plex Mono,monospace;color:var(--t3);padding:3px 0"><span style="color:'+pc+'">'+pp.label+'</span> \u2014 '+pp.flow.toFixed(0)+' GWh/d ('+pp.dir+')</div>';
    }});
    html+='</div>';
  }}
  // View seasonality link
  html+='<button onclick="closeDeep();stab(\\x27season\\x27);document.getElementById(\\x27ss\\x27).value=\\x27'+code+'\\x27;updateSeason()" style="width:100%;padding:12px;background:var(--ac2);border:1px solid var(--ac);border-radius:8px;color:var(--ac);font-family:IBM Plex Mono,monospace;font-size:12px;font-weight:600;cursor:pointer;transition:background .2s" onmouseover="this.style.background=\\x27rgba(0,212,170,0.15)\\x27" onmouseout="this.style.background=\\x27var(--ac2)\\x27">\U0001f4c8 View Seasonality Chart \u2192</button>';
  p.innerHTML=html;
  p.classList.add('open');
}}
function closeDeep(){{document.getElementById('deep-panel').classList.remove('open')}}
window.addEventListener('DOMContentLoaded',function(){{stab('intel');initOverviewChart()}});
</script>
<style>.dark-popup .leaflet-popup-content-wrapper{{background:#0c1018;color:#e8ecf2;border:1px solid #1a2332;border-radius:8px;box-shadow:0 8px 32px rgba(0,0,0,.6)}}.dark-popup .leaflet-popup-tip{{background:#0c1018;border:1px solid #1a2332}}.dark-tooltip{{background:#0c1018!important;color:#e8ecf2!important;border:1px solid #1a2332!important;border-radius:6px!important;padding:4px 10px!important;font-family:'IBM Plex Mono',monospace!important;font-size:10px!important;box-shadow:0 4px 16px rgba(0,0,0,.5)!important}}.leaflet-control-zoom a{{background:#0c1018!important;color:#9aa8bc!important;border-color:#1a2332!important}}.leaflet-control-zoom a:hover{{background:#121820!important;color:#e8ecf2!important}}
#deep-panel{{position:fixed;top:0;right:-420px;width:400px;height:100vh;background:var(--bg2);border-left:1px solid var(--bd);padding:28px 24px;overflow-y:auto;z-index:9999;transition:right .35s cubic-bezier(.16,1,.3,1);box-shadow:-8px 0 40px rgba(0,0,0,.5)}}
#deep-panel.open{{right:0}}
#deep-panel::-webkit-scrollbar{{width:4px}}#deep-panel::-webkit-scrollbar-track{{background:transparent}}#deep-panel::-webkit-scrollbar-thumb{{background:var(--bd);border-radius:2px}}
</style>
<div id="deep-panel"></div>
</body></html>'''


def main():
    if "--rebuild" in sys.argv:
        cache_path = Path(__file__).parent / CACHE_FILE
        if cache_path.exists():
            cache_path.unlink()
            print("Cache cleared. Will do a full rebuild.\n")

    print("=" * 60)
    print("  ⚡ European Gas Intelligence Dashboard v6")
    print("  Fetching from AGSI · ALSI · ENTSOG · National Gas")
    print("  GB: National Gas REST API (primary) → AGSI (fallback)")
    print("  Map: 94 storage facilities · LNG terminals · pipeline points")
    print("=" * 60)

    t0 = time.time()
    D = fetch_all()
    print(f"\n⏱ {time.time() - t0:.1f}s")
    print("📝 Generating HTML...")

    h = gen_html(D)
    out = Path(__file__).parent / OUTPUT_FILE
    out.write_text(h, encoding="utf-8")
    print(f"✓ Saved: {out}")
    if "--no-browser" not in sys.argv:
        print("🌐 Opening browser...")
        webbrowser.open(out.as_uri())

    print(f"\n{'=' * 60}")
    print("  Done! Re-run to refresh.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
