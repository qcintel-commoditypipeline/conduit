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
    "You are a senior European natural-gas analyst writing the morning sitrep "
    "for a trading/pricing desk. You are given a block of pre-computed facts; "
    "those are the ONLY numbers you may use — never invent, derive or "
    "extrapolate a figure, and treat 'n/a' as unavailable rather than guessing. "
    "Write 4–6 short plain-text bullets starting with '• ' — no preamble, no "
    "headers. Desk style: do not simply restate the data — for each point say "
    "what changed, why it matters for the supply/demand balance or price, and "
    "what to watch next. Lead with the most market-relevant change or anomaly "
    "vs the seasonal norm, then the refill outlook vs the 90% Nov-1 mandate, "
    "then corridor/flow shifts, then price action, then any headline that "
    "connects to the data. If nothing is abnormal, say so in one bullet rather "
    "than padding."
)


def _fmt(v, spec: str = "", suffix: str = "") -> str:
    """Format a metric defensively: a missing/odd value becomes 'n/a' instead
    of killing the whole brief (None used to TypeError inside f-strings)."""
    if v is None:
        return "n/a"
    try:
        return (f"{v:{spec}}" if spec else f"{v}") + suffix
    except (TypeError, ValueError):
        return "n/a"


def _facts(analytics: dict, news: list) -> str:
    traj = analytics.get("trajectory") or {}
    eu = traj.get("EU") or {}
    sp = (analytics.get("spreads") or {}).get("ttf") or {}
    sigs = analytics.get("signals") or []
    bal = analytics.get("balance") or {}
    lines = []

    if eu:
        mandate = ("on track" if eu.get("on_track") else
                   f"{_fmt(eu.get('shortfall_pp'), '.1f', 'pp')} short")
        lines.append(
            f"EU storage {_fmt(eu.get('current_fill'), '.1f', '%')} "
            f"(vs ~{_fmt(eu.get('normal_now_avg'), '.1f', '%')} 5yr norm, "
            f"{_fmt(eu.get('vs_normal_now_pp'), '+.1f', 'pp')}); "
            f"refill vs 90% Nov-1 mandate: projected "
            f"{_fmt(eu.get('projected_fill'), '.1f', '%')} ({mandate}), "
            f"pace {_fmt(eu.get('pace_pp_per_day'), '.2f', 'pp/day')}, "
            f"{_fmt(eu.get('days_to_target'))} days to target.")

    # biggest single-country deviation vs the seasonal norm (from seasonal.py
    # baselines surfaced through trajectory) — the LLM's anomaly anchor
    dev = [(e, t) for e, t in traj.items()
           if e != "EU" and t.get("vs_normal_now_pp") is not None]
    if dev:
        e, t = max(dev, key=lambda kv: abs(kv[1]["vs_normal_now_pp"]))
        lines.append(
            f"Biggest country deviation vs seasonal norm: {e} "
            f"{_fmt(t.get('current_fill'), '.1f', '%')} vs "
            f"~{_fmt(t.get('normal_now_avg'), '.1f', '%')} norm "
            f"({_fmt(t.get('vs_normal_now_pp'), '+.1f', 'pp')}).")

    if sp:
        lines.append(
            f"TTF front-month {_fmt(sp.get('last'), '.2f')} EUR/MWh: "
            f"{_fmt(sp.get('chg_1d_pct'), '+.1f', '%')} d/d, "
            f"{_fmt(sp.get('chg_1w_pct'), '+.1f', '%')} w/w, "
            f"{_fmt(sp.get('chg_30d_pct'), '+.1f', '%')} 30d, "
            f"{_fmt(sp.get('year_percentile'), '.0f')}th pct of past year.")

    if sigs:
        lines.append("Top signals: " + " | ".join(
            f"{s.get('headline', '')} ({s.get('detail') or ''})" for s in sigs[:6]))
    else:
        lines.append("No storage/price anomalies vs seasonal norms today.")

    behind = [f"{e} {_fmt(t.get('projected_fill'), '.0f', '%')}"
              for e, t in traj.items()
              if e != "EU" and not t.get("on_track")
              and (t.get("shortfall_pp") or 0) >= 2]
    if behind:
        lines.append("Countries projected below 90%: " + ", ".join(behind[:10]))

    if bal.get("available") and bal.get("by_corridor"):
        when = f" ({bal.get('latest_day')} vs {bal.get('prev_day')})" \
            if bal.get("latest_day") else ""
        top = bal["by_corridor"][:5]
        lines.append(f"Supply by corridor (GWh/d, w/w){when}: " + "; ".join(
            f"{c.get('corridor')} {_fmt(c.get('net_gwh'), '.0f')} "
            f"({_fmt(c.get('wow_gwh'), '+.0f')})" for c in top))
        movers = sorted((c for c in bal["by_corridor"]
                         if c.get("wow_gwh") is not None),
                        key=lambda c: abs(c["wow_gwh"]), reverse=True)[:3]
        if movers:
            lines.append("Top corridor w/w moves (GWh/d): " + "; ".join(
                f"{c.get('corridor')} {_fmt(c.get('wow_gwh'), '+.0f')} "
                f"to {_fmt(c.get('net_gwh'), '.0f')}" for c in movers))

    if news:
        lines.append("Headlines: " + " || ".join(
            n.get("headline", "") for n in news[:5]))
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


def _teams_card(title: str, date: str, text: str) -> dict:
    """Wrap the brief in the Teams 'message' envelope the Power Automate
    'webhook request received -> post card' flow expects: it reads the adaptive
    card from attachments[0].content, so the payload itself must be a valid
    AdaptiveCard (top-level type == 'AdaptiveCard'). Each text line becomes its
    own TextBlock so bullets/paragraphs render. No emoji — clean text only."""
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder",
             "size": "Medium", "wrap": True}]
    if date:
        body.append({"type": "TextBlock", "text": date, "isSubtle": True,
                     "spacing": "None", "wrap": True})
    for line in (text or "").split("\n"):
        if line.strip():
            body.append({"type": "TextBlock", "text": line.rstrip(),
                         "wrap": True, "spacing": "Small"})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.4",
                "body": body,
            },
        }],
    }


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
        r = requests.post(
            url,
            json=_teams_card("CONDUIT — daily gas market comm", run_date, text),
            timeout=45)  # PA flow cold-starts can take >20s; VPS connect adds ~5s
        ok = r.status_code in (200, 202)
        print(f"  {'✓' if ok else '⚠'} brief -> Power Automate ({r.status_code})")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ Power Automate delivery failed: {e}")
        return False
