"""
Morning brief: turn the ranked signals + trajectory + price + headlines into a
short, plain-English narrative with Claude, then push it to Telegram.

The LLM only *narrates numbers we computed* — the structured analytics are the
source of truth, so no figures are invented. Everything degrades gracefully:
no key -> no brief; the dashboard always renders.
"""
from __future__ import annotations

import os

import requests

MODEL = os.getenv("CONDUIT_BRIEF_MODEL", "claude-sonnet-4-5")
SYSTEM = (
    "You are a European gas market analyst writing a terse morning sitrep for a "
    "trading/pricing desk. Use ONLY the figures provided — never invent numbers. "
    "Write 4–6 short bullet points, lead with what changed and what's abnormal "
    "for the season, then refill outlook, then price, then any notable headline. "
    "No preamble, no headers, plain text bullets starting with '• '."
)


def _facts(analytics: dict, news: list) -> str:
    eu = analytics.get("trajectory", {}).get("EU", {})
    sp = (analytics.get("spreads") or {}).get("ttf") or {}
    sigs = analytics.get("signals", [])
    bal = analytics.get("balance") or {}
    lines = []
    if eu:
        lines.append(f"EU storage {eu.get('current_fill')}% "
                     f"(vs ~{eu.get('normal_now_avg')}% 5yr norm, "
                     f"{eu.get('vs_normal_now_pp'):+}pp); projected "
                     f"{eu.get('projected_fill')}% by Nov 1 (target 90%), "
                     f"pace {eu.get('pace_pp_per_day')}pp/day.")
    if sp:
        lines.append(f"TTF {sp.get('last')} EUR/MWh, {sp.get('chg_1d_pct')}% d/d, "
                     f"30d {sp.get('chg_30d_pct')}%, {sp.get('year_percentile')}th pct of past year.")
    if sigs:
        lines.append("Top signals: " + " | ".join(
            f"{s['headline']} ({s.get('detail','')})" for s in sigs[:6]))
    else:
        lines.append("No storage/price anomalies vs seasonal norms today.")
    behind = [f"{e} {t['projected_fill']:.0f}%" for e, t in
              analytics.get("trajectory", {}).items()
              if not t.get("on_track") and t.get("shortfall_pp", 0) >= 2 and e != "EU"]
    if behind:
        lines.append("Countries projected below 90%: " + ", ".join(behind[:10]))
    if bal.get("available") and bal.get("by_corridor"):
        top = bal["by_corridor"][:5]
        lines.append("Supply by corridor (GWh/d, w/w): " + "; ".join(
            f"{c['corridor']} {c['net_gwh']:.0f} ({c['wow_gwh']:+.0f})" for c in top))
    if news:
        lines.append("Headlines: " + " || ".join(n["headline"] for n in news[:5]))
    return "\n".join(lines)


def generate(analytics: dict, news: list) -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        print("  ⚠ brief: no ANTHROPIC_API_KEY — skipping narrative")
        return None
    facts = _facts(analytics, news)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=MODEL, max_tokens=600, system=SYSTEM,
            messages=[{"role": "user", "content":
                       f"Today's data:\n{facts}\n\nWrite the sitrep bullets."}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        print(f"  ✓ brief: {len(text)} chars ({MODEL})")
        return text
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ brief generation failed: {e}")
        return None


def deliver_telegram(text: str) -> bool:
    if os.getenv("CONDUIT_NO_PUSH"):
        print("  · brief delivery suppressed (CONDUIT_NO_PUSH)")
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat and text):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": "🛰️ Gas Sitrep\n\n" + text,
                  "disable_web_page_preview": True}, timeout=20)
        ok = r.status_code == 200
        print(f"  {'✓' if ok else '⚠'} brief -> Telegram ({r.status_code})")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ Telegram delivery failed: {e}")
        return False


def deliver_powerautomate(text: str, run_date: str = "") -> bool:
    """POST the brief to the Conduit Power Automate flow (Teams routing). The URL
    is a capability token in CONDUIT_POWERAUTOMATE_URL — keep it in the env, never
    commit it. Power Automate's HTTP trigger returns 202 Accepted on success.

    Note: deliberately NOT gated by CONDUIT_NO_PUSH — that flag mutes the legacy
    Telegram path (still set in conduit_run.sh); Power Automate is the live channel."""
    url = os.getenv("CONDUIT_POWERAUTOMATE_URL")
    if not (url and text):
        return False
    try:
        r = requests.post(url, json={
            "app": "conduit",
            "date": run_date,
            "title": "CONDUIT — daily gas market comm",
            "text": text,
        }, timeout=45)  # PA flow cold-starts can take >20s; VPS connect adds ~5s
        ok = r.status_code in (200, 202)
        print(f"  {'✓' if ok else '⚠'} brief -> Power Automate ({r.status_code})")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ Power Automate delivery failed: {e}")
        return False
