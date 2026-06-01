"""
Render the always-on "Sitrep" hero block injected at the top of the dashboard.

Reuses the existing dashboard CSS classes (.pnl/.mc/.st/.cr/colour classes) so
it sits visually inside the page. Self-contained: includes its own small price
chart script. The block is injected after `<div class="main">`, leaving the
monolith's tabs untouched as detail/fallback views.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from html import escape

from .config import COUNTRY_NAME, COUNTRY_FLAG


def integrate_sitrep_tab(base: str) -> str:
    """Replace the legacy 'Intelligence' tab with the new 'Sitrep' tab and wire
    its lazy chart init.

    The old design injected the Sitrep as an always-visible block above the tab
    contents, so clicking any tab swapped content *below* the Sitrep — confusing.
    Instead we make Sitrep a first-class tab (`tab-sitrep`), shown by default,
    and draw its TTF chart only when that tab is visible (the same lazy pattern
    the working Seasonality/Map charts use — a canvas sized inside a hidden
    container renders blank, which is why the chart looked empty).
    """
    # 1. swap the Intelligence tab button for a Sitrep button (keep it first/active)
    base = re.sub(
        r"<button class=\"tab active\" onclick=\"stab\('intel'\)\">.*?</button>",
        "<button class=\"tab active\" onclick=\"stab('sitrep')\">🛰️ Sitrep</button>",
        base, count=1)
    # 2. register sitrep in the tab-name map so the button highlights correctly
    base = base.replace("const tn={overview:'Overview'",
                        "const tn={sitrep:'Sitrep',overview:'Overview'")
    # 3. draw the TTF chart the first time the Sitrep tab is shown
    base = base.replace(
        "if(id==='season'&&!window._si)",
        "if(id==='sitrep'&&!window._sti){window._sti=true;"
        "setTimeout(function(){window.drawSitrepTTF&&window.drawSitrepTTF()},60)};"
        "if(id==='season'&&!window._si)")
    # 4. open on the Sitrep tab by default
    base = base.replace("stab('intel')", "stab('sitrep')")
    return base


def _name(e): return "EU aggregate" if e == "EU" else COUNTRY_NAME.get(e, e)
def _flag(e): return "🇪🇺" if e == "EU" else COUNTRY_FLAG.get(e, "")


def _vs_norm_cell(pp):
    if pp is None:
        return '<span class="dim">–</span>'
    col = "green" if pp > 1 else ("red" if pp < -5 else "orange")
    return f'<span class="{col}">{pp:+.1f}pp</span>'


def _status_cell(t):
    if t.get("on_track"):
        return '<span class="green bold">on track</span>'
    sp = t.get("shortfall_pp", 0)
    col = "red" if sp >= 8 else "orange"
    return f'<span class="{col} bold">-{sp:.0f}pp</span>'


def _signals_html(signals):
    if not signals:
        return ('<div style="padding:14px 16px;color:var(--gn);font-family:\'IBM Plex Mono\','
                'monospace;font-size:12px">✓ No anomalies vs seasonal norms today — '
                'refill tracker and balance below.</div>')
    out = ""
    for s in signals[:8]:
        rgb = "248,113,113" if s["severity"] == "red" else "251,191,36"
        cls = "red" if s["severity"] == "red" else "orange"
        detail = escape(s.get("detail") or "")
        out += (f'<div style="padding:10px 14px;'
                f'background:rgba({rgb},.06);border:1px solid rgba({rgb},.16);border-radius:8px;'
                f'margin-bottom:7px;font-family:\'IBM Plex Mono\',monospace">'
                f'<div class="{cls}" style="font-weight:600;font-size:12px;line-height:1.45">'
                f'{escape(s["headline"])}</div>'
                + (f'<div class="dim" style="font-size:11px;line-height:1.45;margin-top:3px">'
                   f'{detail}</div>' if detail else "")
                + '</div>')
    return out


def _refill_rows(traj):
    order = ["EU"] + sorted([e for e in traj if e != "EU"],
                            key=lambda e: traj[e].get("vs_normal_now_pp") or 0)
    rows = ""
    for e in order:
        t = traj[e]
        emph = "font-weight:700" if e == "EU" else ""
        rows += (f'<tr style="{emph}"><td class="cl">{_flag(e)} {_name(e)}</td>'
                 f'<td class="cr">{t["current_fill"]:.0f}%</td>'
                 f'<td class="cr">{_vs_norm_cell(t.get("vs_normal_now_pp"))}</td>'
                 f'<td class="cr dim">{t["pace_pp_per_day"]:+.2f}</td>'
                 f'<td class="cr">{t["projected_fill"]:.0f}%</td>'
                 f'<td class="cr">{_status_cell(t)}</td></tr>')
    return rows


def _balance_html(bal):
    if not bal or not bal.get("available") or not bal.get("by_corridor"):
        return ('<div class="pnl"><div class="st">Cross-Border Supply</div>'
                '<div class="ss">Corridor balance — building flow history; '
                'populates after backfill</div></div>')
    rows = ""
    for c in bal["by_corridor"][:14]:
        wow = c["wow_gwh"]
        wcol = "green" if wow > 0 else ("red" if wow < 0 else "dim")
        rows += (f'<tr><td class="cl">{escape(c["corridor"])}</td>'
                 f'<td class="cr accent bold">{c["net_gwh"]:,.0f}</td>'
                 f'<td class="cr"><span class="{wcol}">{wow:+,.0f}</span></td></tr>')
    return (f'<div class="pnl"><div class="st">Cross-Border Supply by Corridor</div>'
            f'<div class="ss">ENTSOG physical flows, net GWh/d — {bal["latest_day"]} '
            f'vs {bal["prev_day"]}</div>'
            f'<table><thead><tr><th class="cl">Corridor</th><th class="cr">Net GWh/d</th>'
            f'<th class="cr">w/w</th></tr></thead><tbody>{rows}</tbody></table></div>')


def _news_html(news):
    if not news:
        return ""
    items = ""
    for n in news[:6]:
        items += (f'<div style="padding:8px 0;border-bottom:1px solid rgba(26,35,50,.4)">'
                  f'<div style="font-size:12px;color:var(--tx)">{escape(n.get("headline",""))}</div>'
                  f'<div style="font-size:10px;color:var(--t3);font-family:\'IBM Plex Mono\','
                  f'monospace;margin-top:2px">{escape(n.get("published","")[:10])}'
                  f'{" · " + escape(", ".join(n.get("countries", [])[:3])) if n.get("countries") else ""}'
                  f'</div></div>')
    return (f'<div class="pnl"><div class="st">Gas & LNG Headlines</div>'
            f'<div class="ss">QC Intel — most recent</div>{items}</div>')


def _brief_html(brief_text):
    if not brief_text:
        return ""
    return (f'<div class="pnl" style="border-color:var(--ac)">'
            f'<div class="st">Morning Brief</div>'
            f'<div style="font-size:13px;line-height:1.7;color:var(--t2);white-space:pre-wrap;'
            f'margin-top:6px">{escape(brief_text)}</div></div>')


def build_sitrep_html(analytics: dict, ttf_series: list, hh_last=None,
                      news: list | None = None, brief_text: str | None = None) -> str:
    traj = analytics.get("trajectory", {})
    spreads = analytics.get("spreads", {})
    signals = analytics.get("signals", [])
    eu = traj.get("EU", {})
    ttf = spreads.get("ttf") or {}
    run = analytics.get("run_date", datetime.utcnow().strftime("%Y-%m-%d"))

    behind = sum(1 for e, t in traj.items() if e != "EU"
                 and not t.get("on_track") and t.get("shortfall_pp", 0) >= 2)
    series = (ttf_series or [])[-120:]
    chart_labels = [r["trade_day"] for r in series]
    chart_vals = [r["price"] for r in series]

    eu_fill = eu.get("current_fill")
    eu_vs = eu.get("vs_normal_now_pp")
    eu_proj = eu.get("projected_fill")
    eu_status_col = "green" if eu.get("on_track") else "orange"
    ttf_last = ttf.get("last")
    ttf_chg = ttf.get("chg_1d_pct")
    ttf_col = "green" if (ttf_chg or 0) > 0 else ("red" if (ttf_chg or 0) < 0 else "dim")

    cards = f'''<div class="mg" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--ac),transparent)"></div>
<div class="mc-lbl">EU Storage</div><div class="mc-val accent">{eu_fill:.0f}%</div>
<div class="mc-sub">{_vs_norm_cell(eu_vs)} vs 5yr norm</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--{eu_status_col}),transparent)"></div>
<div class="mc-lbl">Projected Nov 1</div><div class="mc-val {eu_status_col}">{eu_proj:.0f}%</div>
<div class="mc-sub">target 90% · {eu.get("days_to_target","–")}d to go</div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--bl),transparent)"></div>
<div class="mc-lbl">TTF Front-Month</div><div class="mc-val blue">{(f"{ttf_last:.1f}" if ttf_last else "–")}</div>
<div class="mc-sub">€/MWh <span class="{ttf_col}">{(f"{ttf_chg:+.1f}%" if ttf_chg is not None else "")}</span></div></div>
<div class="mc"><div class="mc-accent" style="background:linear-gradient(90deg,var(--or),transparent)"></div>
<div class="mc-lbl">Countries Behind</div><div class="mc-val orange">{behind}</div>
<div class="mc-sub">below 90% projection</div></div>
</div>''' if eu_fill is not None else ""

    price_panel = f'''<div class="pnl"><div class="st">TTF Price — 120 days</div>
<div class="ss">Yahoo Finance front-month (€/MWh){(" · HH " + f"{hh_last:.2f} $/MMBtu") if hh_last else ""}</div>
<div style="position:relative;height:220px;width:100%"><canvas id="sitrepTTF"></canvas></div>
<div id="sitrepTTFmsg" style="color:var(--t3);font-family:'IBM Plex Mono',monospace;font-size:10px;padding:6px 0"></div></div>
<script>
window._ttfLabels={json.dumps(chart_labels)};
window._ttfVals={json.dumps(chart_vals)};
(function(){{
  var tries=0;
  function note(t){{var m=document.getElementById('sitrepTTFmsg');if(m)m.textContent=t;}}
  function attempt(){{
    tries++;
    var el=document.getElementById('sitrepTTF');
    var hasChart=(typeof Chart!=='undefined');
    var w=el?el.clientWidth:-1;
    var n=(window._ttfVals||[]).length;
    note('dbg: chart='+hasChart+' w='+w+' pts='+n+' try='+tries);
    if(!el){{ if(tries<60) return setTimeout(attempt,150); return; }}
    if(!hasChart){{ if(tries<60) return setTimeout(attempt,150); note('FAIL: Chart.js never loaded'); return; }}
    if(w<10){{ if(tries<60) return setTimeout(attempt,150); note('FAIL: canvas width=0 after wait'); return; }}
    if(!n){{ note('FAIL: no price data'); return; }}
    if(el._done) return;
    el._done=1;
    try{{
      new Chart(el,{{type:'line',data:{{labels:window._ttfLabels,
        datasets:[{{label:'TTF',data:window._ttfVals,
        borderColor:'#60a5fa',backgroundColor:'rgba(96,165,250,.08)',fill:true,borderWidth:2,
        pointRadius:0,tension:.25}}]}},options:{{responsive:true,maintainAspectRatio:false,
        animation:false,plugins:{{legend:{{display:false}},tooltip:{{intersect:false,mode:'index'}}}},
        scales:{{x:{{ticks:{{maxTicksLimit:6,autoSkip:true,
        color:'#5e6e84',font:{{size:9}}}},grid:{{display:false}}}},y:{{ticks:{{color:'#5e6e84',
        font:{{size:10}},callback:function(v){{return v+'€'}}}},grid:{{color:'#1a2332'}}}}}}}});
      note('');
    }}catch(e){{el._done=0;note('FAIL: '+(e&&e.message?e.message:e));}}
  }}
  // self-start (sitrep tab is active by default) + re-arm on tab show
  window.drawSitrepTTF=function(){{tries=0;attempt();}};
  if(document.readyState!=='loading') setTimeout(attempt,80);
  else document.addEventListener('DOMContentLoaded',function(){{setTimeout(attempt,80);}});
}})();
</script>'''

    return f'''<div id="tab-sitrep" class="tc active" style="margin-bottom:8px">
<div style="display:flex;align-items:baseline;gap:14px;margin-bottom:14px;flex-wrap:wrap">
<div style="font-size:11px;letter-spacing:4px;color:var(--ac);font-family:'IBM Plex Mono',monospace;font-weight:600">DAILY SITREP</div>
<div style="font-size:11px;color:var(--t3);font-family:'IBM Plex Mono',monospace">{run}</div>
</div>
{_brief_html(brief_text)}
<div class="pnl"><div class="st">Signals</div>
<div class="ss">Ranked deviations from seasonal norm — storage · refill pace · price</div>
<div style="margin-top:10px">{_signals_html(signals)}</div></div>
{cards}
<div style="display:grid;grid-template-columns:1.3fr 1fr;gap:24px;align-items:start" class="sitrep-grid">
<div class="pnl"><div class="st">Refill Tracker — vs 90% by Nov 1</div>
<div class="ss">Current fill, gap to seasonal norm, pace, projected Nov-1 fill</div>
<table><thead><tr><th class="cl">Area</th><th class="cr">Fill</th><th class="cr">vs Norm</th>
<th class="cr">pp/day</th><th class="cr">Proj.</th><th class="cr">Nov 1</th></tr></thead>
<tbody>{_refill_rows(traj)}</tbody></table></div>
<div>{_balance_html(analytics.get("balance"))}{_news_html(news)}</div>
</div>
{price_panel}
</div>
<style>@media(max-width:900px){{.sitrep-grid{{grid-template-columns:1fr!important}}}}</style>
'''
