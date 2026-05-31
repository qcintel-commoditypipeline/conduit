# Conduit — European Gas Market Intelligence

A daily pipeline that turns raw European gas data into a persisted time series
and a ranked, seasonally-aware **sitrep** — storage, refill outlook, cross-border
supply, LNG, prices and news in one self-contained dashboard, plus an optional
Claude-written morning brief pushed to Telegram.

Live: **http://165.232.110.29/conduit/**

---

## What it does

Each morning (after GIE publishes, ~08:00 UTC) the pipeline:

1. **Fetches** storage (GIE AGSI), LNG (GIE ALSI), pipeline flows (ENTSOG),
   UK storage/flows (National Gas), prices (Yahoo: TTF, Henry Hub) and weather
   (Open-Meteo HDD).
2. **Persists** everything to a SQLite store — the memory the original snapshot
   script never had. This is what makes trends, anomalies and projections possible.
3. **Analyses**: seasonal-normal deviations, refill trajectory vs the 90%/Nov-1
   mandate, cross-border supply by corridor, and price context. Emits ranked
   **signals**.
4. **Briefs**: Claude narrates the signals into a short plain-English sitrep and
   (optionally) pushes it to Telegram. The LLM only narrates computed numbers —
   it never invents figures.
5. **Renders** an always-on Sitrep hero block on top of the existing dashboard
   tabs (storage / seasonality / map / LNG / flows).

### Why it's different from the old script
The original `gas_dashboard.py` was a single 2,000-line snapshot generator: no
memory, and "intelligence" gated on winter withdrawal so it fell silent every
summer. Conduit keeps that script's proven fetchers and rich HTML, but adds the
persistence + analytics layer that was missing, so the insights work year-round.

---

## Architecture

```
gasintel/
  config.py            paths, constants, country + corridor maps
  store.py             SQLite schema + typed upserts/queries (WAL)
  sources.py           prices (Yahoo) + flows (ENTSOG, corridor-classified)
  persist.py           map reused fetch_all() output -> store rows
  news.py              QC Intel gas/LNG headlines
  brief.py             Claude narrative -> Telegram
  render.py            Sitrep hero block (reuses dashboard CSS)
  analytics/
    seasonal.py        day-of-year percentile / z-score / band (winsorised)
    deviations.py      rank what's abnormal today -> signals
    trajectory.py      refill projection vs 90%/Nov-1 + seasonal path
    spreads.py         price level / momentum / year-percentile
    balance.py         cross-border supply by corridor (net, w/w)
pipeline.py            orchestrator (the daily entrypoint)
backfill.py            one-time history load (seasonality, prices, flows)
gas_dashboard.py       original v6 — reused for fetchers + base HTML
tests/                 analytics unit tests (no network)
data/conduit.db        SQLite store (gitignored)
```

Each optional stage (prices, flows, news, brief, sitrep) is wrapped — any can
fail without stopping the dashboard from being written.

---

## Running

```bash
# Daily run (writes gas_dashboard.html)
python pipeline.py

# One-time history backfill
python backfill.py --all                 # seasonality + 2y prices + 2y flows
python backfill.py --seasonality --prices  # fast, no heavy fetching
python backfill.py --flows --years 2

# Tests
python -m unittest discover -s tests -q
```

Environment vars (from `/opt/scripts/.env`): `AGSI_KEY`, `ANTHROPIC_API_KEY`,
`QCINTEL_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
`CONDUIT_NO_PUSH=1` suppresses Telegram delivery (for test runs).

## Deployment (lon1 box)

Source of truth is GitHub (`qcintel-commoditypipeline/conduit`). The box can't
reach GitHub, so deploys go via a bare repo + `post-receive` checkout:

```bash
git push box main      # ssh://root@165.232.110.29/opt/git/conduit.git
                       # -> checks out to /opt/scripts/conduit-dev
```

Once promoted, `conduit_run.sh` runs `pipeline.py` daily at 08:00 UTC and nginx
serves the generated HTML at `/conduit/`.

---

## Data sources

| Source | Data | Notes |
|--------|------|-------|
| GIE AGSI | Underground storage | API key; daily ~08:00 UTC |
| GIE ALSI | LNG terminals | inventory/send-out |
| ENTSOG TP | Pipeline physical flows | public; corridor-classified |
| National Gas (UK) | GB storage/flows | catalogue-driven, no auth |
| Yahoo Finance | TTF, Henry Hub | front-month daily |
| Open-Meteo | Temperature / HDD | 7-day forecast |
| QC Intel | Gas & LNG headlines | Bearer token |

## Roadmap
- Forward curve (Barchart) for summer-winter spread + TTF-JKM.
- Demand-vs-forecast and linepack from the National Gas catalogue.
- Freshness/quality panel; cron-failure alerting.
