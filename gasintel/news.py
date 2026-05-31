"""
QC Intel gas & LNG headlines for the Sitrep and the morning brief.

Reuses the same article API the FLARE app uses for oil, filtered to gas/LNG.
Resilient: tries product filters, falls back to keyword filtering, and returns
an empty list on any failure so the pipeline never breaks on news.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from .config import QCI_API_URL, QCI_PRODUCTS

_GAS_KW = re.compile(
    r"\b(gas|lng|ttf|nbp|pipeline|storage|regas|liquefaction|nord ?stream|"
    r"gazprom|entsog|henry hub|jkm|feedgas|send.?out)\b", re.I)


def _token():
    # The QCI article API wants the JWT bearer (QCINTEL_TOKEN / QCI_JWT, ~1KB,
    # "eyJ..."), NOT the short QCINTEL_API_TOKEN — so prefer the JWT. Flare uses
    # the same JWT via qci_token.txt.
    for k in ("QCI_JWT", "QCINTEL_TOKEN", "QCINTEL_API_TOKEN"):
        v = os.getenv(k)
        if v and len(v) > 40:  # skip the short non-JWT api token
            return v
    for p in (Path("/opt/scripts/flare/qci_token.txt"),
              Path(__file__).parent.parent / "qci_token.txt"):
        if p.exists():
            return p.read_text().strip()
    return None


def _parse(data):
    out = []
    for item in data.get("data", []):
        a = item.get("attributes", {})
        out.append({
            "id": item.get("id"),
            "headline": (a.get("headline") or "").strip(),
            "sell": a.get("sell") or "",
            "url": a.get("url") or "",
            "published": a.get("published") or "",
            "countries": a.get("countries") or [],
        })
    return out


def fetch_headlines(days_back: int = 5, limit: int = 40) -> list[dict]:
    token = _token()
    if not token:
        print("  ⚠ news: no QCI token in env/file — skipping")
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json"}
    params = {"filter[published_since]": since, "sort": "-published",
              "page[size]": max(limit, 60)}
    for i, prod in enumerate(QCI_PRODUCTS):
        params[f"filter[products][{i}]"] = prod
    try:
        r = requests.get(QCI_API_URL, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            # retry without product filter, keyword-filter client-side
            for k in list(params):
                if k.startswith("filter[products]"):
                    params.pop(k)
            r = requests.get(QCI_API_URL, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            arts = [a for a in _parse(r.json())
                    if _GAS_KW.search(a["headline"] + " " + a["sell"][:200])]
        else:
            arts = _parse(r.json())
            if not arts:  # product slug may differ — fall back to keyword
                params.pop("filter[products][0]", None)
                params.pop("filter[products][1]", None)
                r = requests.get(QCI_API_URL, params=params, headers=headers, timeout=30)
                arts = [a for a in _parse(r.json())
                        if _GAS_KW.search(a["headline"] + " " + a["sell"][:200])]
        print(f"  ✓ news: {len(arts)} gas/LNG headlines")
        return arts[:limit]
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ news fetch failed: {e}")
        return []
