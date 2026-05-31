"""
Central configuration: paths, constants, and reference maps.

One source of truth for things used across the pipeline. Kept free of heavy
imports so any module (and tests) can import it cheaply.
"""
import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = Path(os.getenv("CONDUIT_DB", DATA_DIR / "conduit.db"))
OUTPUT_HTML = REPO_ROOT / "gas_dashboard.html"

# ── Unit conversions ───────────────────────────────────────────────────────
TWH_TO_BCM = 1 / 10.55
GWH_TO_MCM = 0.0948
KWH_TO_GWH = 1 / 1e6

# ── EU storage mandate (Reg 2022/1032) ─────────────────────────────────────
STORAGE_TARGET_PCT = 90.0
STORAGE_TARGET_MONTH = 11
STORAGE_TARGET_DAY = 1

# ── Seasonal baseline ──────────────────────────────────────────────────────
# Years used to define the "normal" day-of-year band. 2022 was the crisis
# year; we keep it but winsorise in analytics so it doesn't dominate.
SEASONAL_LOOKBACK_YEARS = 6
SEASONAL_DOY_WINDOW = 3  # +/- days pooled around each day-of-year

# ── Price symbols (Yahoo Finance) ──────────────────────────────────────────
# TTF=F is the ICE Dutch TTF front-month future (EUR/MWh).
PRICE_SYMBOLS = {
    "TTF": "TTF=F",   # Dutch TTF front-month (EUR/MWh) — the EU benchmark
    "NG": "NG=F",     # Henry Hub front-month (USD/MMBtu) — US benchmark
}

# ── QCI news ───────────────────────────────────────────────────────────────
QCI_API_URL = "https://www.qcintel.com/api/v2/articles"
QCI_PRODUCTS = ["gas", "lng"]  # filtered server-side; falls back gracefully

# ── Country reference (lowercase, ISO2, name, flag) ─────────────────────────
COUNTRIES = [
    ("at", "AT", "Austria", "🇦🇹"), ("be", "BE", "Belgium", "🇧🇪"),
    ("bg", "BG", "Bulgaria", "🇧🇬"), ("hr", "HR", "Croatia", "🇭🇷"),
    ("cz", "CZ", "Czechia", "🇨🇿"), ("dk", "DK", "Denmark", "🇩🇰"),
    ("fr", "FR", "France", "🇫🇷"), ("de", "DE", "Germany", "🇩🇪"),
    ("hu", "HU", "Hungary", "🇭🇺"), ("it", "IT", "Italy", "🇮🇹"),
    ("lv", "LV", "Latvia", "🇱🇻"), ("nl", "NL", "Netherlands", "🇳🇱"),
    ("pl", "PL", "Poland", "🇵🇱"), ("pt", "PT", "Portugal", "🇵🇹"),
    ("ro", "RO", "Romania", "🇷🇴"), ("sk", "SK", "Slovakia", "🇸🇰"),
    ("es", "ES", "Spain", "🇪🇸"), ("se", "SE", "Sweden", "🇸🇪"),
    ("gb", "GB", "United Kingdom", "🇬🇧"), ("ua", "UA", "Ukraine", "🇺🇦"),
]
COUNTRY_NAME = {uc: nm for _, uc, nm, _ in COUNTRIES}
COUNTRY_FLAG = {uc: fl for _, uc, _, fl in COUNTRIES}

# ── Cross-border corridor classification ───────────────────────────────────
# Maps an ENTSOG point-label keyword -> (corridor, source_region, is_supply).
# is_supply=False marks domestic demand/virtual points that must NOT be summed
# into cross-border supply (the old code conflated these).
CORRIDORS = {
    "Dornum": ("Norwegian (North Sea)", "Norway", True),
    "Emden": ("Norwegian (North Sea)", "Norway", True),
    "NETRA": ("Norwegian (North Sea)", "Norway", True),
    "Easington": ("Norwegian (Langeled)", "Norway", True),
    "St. Fergus": ("Norwegian (FLAGS/Vesterled)", "Norway", True),
    "Mazara del Vallo": ("North African (Transmed)", "North Africa", True),
    "Gela": ("North African (Greenstream)", "North Africa", True),
    "Melendugno": ("Azeri (TAP)", "Azerbaijan", True),
    "Strandzha": ("Turkish (TurkStream)", "Russia/Turkey", True),
    "Kipi": ("Azeri/Caspian (IGB)", "Azerbaijan", True),
    "Mallnow": ("Eastern (Yamal)", "Russia", True),
    "Velke Kapusany": ("Eastern (Ukraine transit)", "Russia/Ukraine", True),
    "Tarvisio": ("Central (TAG)", "transit", True),
    "Waidhaus": ("Central (ex-CZ)", "transit", True),
    "Milford Haven": ("LNG (UK)", "LNG", True),
    "Zeebrugge": ("LNG / IUK", "LNG", True),
    "Dunkerque": ("LNG (FR)", "LNG", True),
    "Montoir": ("LNG (FR)", "LNG", True),
    "Fos": ("LNG (FR)", "LNG", True),
    "Gate Terminal": ("LNG (NL)", "LNG", True),
    "Swinoujscie": ("LNG (PL)", "LNG", True),
    "Revithoussa": ("LNG (GR)", "LNG", True),
    "Klaipeda": ("LNG (LT)", "LNG", True),
    # Domestic demand / virtual points — NOT cross-border supply
    "Distribution": ("Domestic Demand", "demand", False),
    "Power Stations": ("Power-gen Demand", "demand", False),
    "Thermal Plants": ("Power-gen Demand", "demand", False),
    "Industrial": ("Industrial Demand", "demand", False),
    "GRTgaz aggregated": ("Domestic Demand", "demand", False),
    "VIP": ("Virtual Trading Point", "virtual", False),
    "TVB": ("Virtual Balancing", "virtual", False),
    "THE": ("Virtual Trading Point", "virtual", False),
}


def classify_point(label: str):
    """Return (corridor, region, is_supply) for an ENTSOG point label."""
    low = (label or "").lower()
    for kw, meta in CORRIDORS.items():
        if kw.lower() in low:
            return meta
    return ("", "", True)  # unknown points assumed cross-border by default
