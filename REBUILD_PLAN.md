# Conduit — Full Rebuild Plan

_European gas market intelligence. Rebuild from a single 2,000-line snapshot
script into a clean, persistent, replicable pipeline._

Status: **proposed** · Target env: the lon1 box (`/opt/scripts/conduit/`, Python 3.12,
shared venv, nginx static serve at `/conduit/`, daily cron 08:00 UTC).

---

## 1. Why rebuild

The current `gas_dashboard.py` works and deploys fine (the live `/conduit/` page
refreshes daily). But it has two structural ceilings:

1. **No memory.** Only storage *fill %* is persisted (`gas_cache.json`). Flows, LNG,
   prices, demand, derived metrics — all thrown away each run. So nothing except
   storage can show a trend, an anomaly, or a correlation.
2. **Winter-only "intelligence."** Alerts are gated on net storage *withdrawal*, so in
   injection season (now) the Intelligence tab is structurally empty. "Days of supply"
   is a winter metric shown year-round.

Plus smaller issues: TTF is fetched then discarded to a single tile; ENTSOG flows take
`v[0]` as "latest" (wrong — not date-sorted) and mix demand points into supply; the
5-yr band is crisis-skewed; facility-level map fills are cosmetic.

The goal isn't a rewrite for its own sake — it's to add a **persistence + analytics
layer** so the insights become genuinely useful, structured as a **replicable framework**
the rest of the QCI suite can reuse.

---

## 2. Target architecture

A small, testable pipeline replacing the monolith. Each stage is a package, not a
1-file blob:

```
conduit/
  conduit/
    config.py        # env, constants, country/corridor maps (one source of truth)
    store.py         # SQLite schema + typed access layer (WAL mode)
    ingest/
      agsi.py        # storage  (GIE)
      alsi.py        # LNG      (GIE)
      entsog.py      # flows    (ENTSOG TP)
      nationalgas.py # UK storage/flows/demand/linepack (catalogue-driven)
      prices.py      # TTF/JKM/HH via Databento (+ EIA for Henry Hub)
      weather.py     # Open-Meteo HDD
    analytics/
      seasonal.py    # day-of-year baseline: percentile / z-score / band
      deviations.py  # rank "what's abnormal today" across all series -> signals
      trajectory.py  # refill pace vs 90% Nov-1 target, per country + EU
      balance.py     # supply stack: pipeline-by-corridor + LNG + storage draw
      spreads.py     # front-month, summer-winter, TTF-JKM, TTF-HH
    brief.py         # rank signals -> Anthropic narrative -> Telegram/Teams push
    render/
      templates/     # Jinja2 (tabs split out, CSS/JS in static files)
      build.py       # read DB -> render HTML
    main.py          # orchestrate: ingest -> analytics -> brief -> render
  data/conduit.db    # SQLite (gitignored)
  requirements.txt   # pinned
  tests/             # analytics unit tests (fixtures, no network)
```

**Key design choices** (confirm before building):

- **Store: SQLite** (file `data/conduit.db`, WAL). Tiny data (~10y daily = a few MB),
  self-contained, matches the suite's "no persistent server" model. Postgres exists on
  the box (precog) if we ever want central/multi-commodity — revisit then.
- **Keep static-HTML delivery.** Daily batch regenerates the page; nginx serves it.
  Robust, cheap, suite-consistent. Interactivity stays client-side (Chart.js/Leaflet).
- **Prices: switch Yahoo → Databento** (`DATABENTO_API_KEY` already in `.env`) for a
  real forward curve (enables summer-winter spread) + EIA for Henry Hub. Keep Yahoo as
  fallback. _Needs check: Databento entitlements for TTF/JKM._
- **Secrets from `.env` only** (`os.getenv`) — incl. `ANTHROPIC_API_KEY`. Nothing
  hardcoded.

---

## 3. Phased delivery

Value is front-loaded: after Phases 1–2 the insights are already useful, even before
the UI is reworked.

### Phase 0 — Foundations & safety (~0.5–1 day)
- `git init`, push to `qcintel-commoditypipeline/conduit`, `.gitignore`
  (`*.db`, `gas_cache.json`, `gas_dashboard.html`, `__pycache__/`, `.env`).
  **Get the current code under version control before touching it.**
- Fix the cp1252 console crash (`sys.stdout.reconfigure(encoding="utf-8")`); delete the
  dead Windows `refresh.bat`. Single source of truth = git → deploy to box.
- `requirements.txt` pinned; verify against shared venv.

### Phase 1 — Persistence layer (~1–2 days) · **the unlock**
- `store.py`: schema — `storage_daily`, `lng_daily`, `flow_daily`, `price_daily`,
  `demand_daily`, `weather_daily`, `metrics_daily` (derived), `signals`. Idempotent
  upserts keyed by (entity, gas_day).
- Refactor existing fetchers to normalize → write rows (minimal behaviour change).
- `--backfill`: storage ~2016→now (AGSI from/to), flows ~2y (ENTSOG date ranges),
  prices history (Databento). One-time history load.
- Parity check: render from DB reproduces today's page.

### Phase 2 — Analytics engine (~2–3 days) · **the value**
- `seasonal.py`: day-of-year baseline (percentile + z), winsorized / configurable
  lookback so 2022 doesn't dominate the "normal" band.
- `deviations.py`: score every series (storage, injection rate, LNG send-out, each
  corridor, price) as deviation-from-normal; emit ranked `signals`. **Works year-round**,
  replacing the winter-only thresholds.
- `trajectory.py`: project injection pace → fill on Nov 1 vs 90% mandate vs 5-yr path.
  _"DE on track for 88% by Nov 1 — 6 days behind the 5-yr norm."_
- `entsog` fix + `balance.py`: correct latest (date-sort), net by direction, real
  corridor mapping, drop demand points → supply-by-corridor series + utilisation.
- `spreads.py`: front-month, summer-winter (injection economics), TTF-JKM (LNG arb),
  TTF-HH (US cargo economics, via EIA).

### Phase 3 — New surface / UX (~2–3 days)
- Replace winter-only "Intelligence" with year-round **"Sitrep"**: top deviations,
  supply stack, refill tracker, price + spreads chart.
- Jinja2 templating; split CSS/JS out of the Python string.
- Flows tab → supply-by-corridor + utilisation (not a flat 40-row table).
- Honesty: per-facility fill only where real (GB); label estimates as such.

### Phase 4 — Brief & delivery (~1 day) · **the intel angle**
- `brief.py`: rank `signals` → Anthropic (`ANTHROPIC_API_KEY`) writes a 5-bullet daily
  narrative → push to Telegram (`TELEGRAM_BOT_TOKEN`/`_CHAT_ID`) and/or Teams
  (`TEAMS_WEBHOOK_URL`). The "10-second sitrep" lands on your phone each morning.
  Keep the structured signals as the source of truth; the LLM only narrates them
  (grounded, no free-floating numbers).

### Phase 5 — Hardening (~1 day, ongoing)
- Data-freshness/quality panel (staleness, revisions).
- Analytics unit tests (fixtures, no network).
- Cron-failure alert (the suite already has Telegram/Teams).

**Rough total: ~8–12 focused days**, but useful after Phase 2.

---

## 4. Open questions for Tom
1. Databento entitlements — does the key cover TTF / JKM futures? (Determines spreads.)
2. LLM brief: daily auto-push, or on-demand only? (Tiny token cost either way.)
3. SQLite (recommended) vs reuse the box's Postgres now (for future multi-commodity)?
4. Backfill depth for flows — 2y is cheap; more is heavier on a 2 GB box.

---

## 5. Immediate next step
Phase 0: stand up the git repo (current code first, untouched), fix the encoding crash,
pin deps. Small, safe, and it gives us a clean baseline to build on.
