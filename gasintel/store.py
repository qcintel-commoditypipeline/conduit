"""
SQLite persistence layer — the memory the old dashboard never had.

Tiny data (~10y daily across a few hundred entities = a few MB), so SQLite in
WAL mode is plenty and keeps the app self-contained (no server). All writes are
idempotent upserts keyed by (entity, day) so re-runs and backfills are safe.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from typing import Iterable, Sequence

from .config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_daily (
    country     TEXT NOT NULL,
    gas_day     TEXT NOT NULL,
    fill        REAL,            -- %
    gas_twh     REAL,
    wgv_twh     REAL,
    injection   REAL,            -- GWh/d
    withdrawal  REAL,            -- GWh/d
    trend       REAL,
    source      TEXT,
    PRIMARY KEY (country, gas_day)
);
CREATE TABLE IF NOT EXISTS lng_daily (
    country     TEXT NOT NULL,
    gas_day     TEXT NOT NULL,
    send_out    REAL,            -- GWh/d
    inventory   REAL,            -- 1000 m3
    dtrs        REAL,            -- GWh/d capacity
    fullness    REAL,            -- %
    PRIMARY KEY (country, gas_day)
);
CREATE TABLE IF NOT EXISTS flow_daily (
    point       TEXT NOT NULL,
    gas_day     TEXT NOT NULL,
    direction   TEXT NOT NULL,
    operator    TEXT,
    corridor    TEXT,
    region      TEXT,
    is_supply   INTEGER,
    value_gwh   REAL,
    PRIMARY KEY (point, gas_day, direction)
);
CREATE TABLE IF NOT EXISTS price_daily (
    symbol      TEXT NOT NULL,
    trade_day   TEXT NOT NULL,
    price       REAL,
    PRIMARY KEY (symbol, trade_day)
);
CREATE TABLE IF NOT EXISTS weather_daily (
    country      TEXT NOT NULL,
    forecast_day TEXT NOT NULL,
    run_date     TEXT NOT NULL,
    temp_mean    REAL,
    hdd          REAL,
    PRIMARY KEY (country, forecast_day, run_date)
);
-- generic derived-metric store (trajectory projections, spreads, etc.)
CREATE TABLE IF NOT EXISTS metrics_daily (
    metric   TEXT NOT NULL,
    entity   TEXT NOT NULL,
    gas_day  TEXT NOT NULL,
    value    REAL,
    PRIMARY KEY (metric, entity, gas_day)
);
-- ranked signals emitted per run (history retained)
CREATE TABLE IF NOT EXISTS signals (
    run_date  TEXT NOT NULL,
    category  TEXT NOT NULL,
    entity    TEXT,
    severity  TEXT,
    score     REAL,
    headline  TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_run ON signals(run_date);
CREATE INDEX IF NOT EXISTS ix_flow_day ON flow_daily(gas_day);
CREATE INDEX IF NOT EXISTS ix_storage_day ON storage_daily(gas_day);
"""


def connect(path=None) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def session(path=None):
    conn = connect(path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Upserts ─────────────────────────────────────────────────────────────────

def _upsert(conn, table: str, cols: Sequence[str], rows: Iterable[dict]) -> int:
    rows = [r for r in rows if r]
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in cols)
    collist = ",".join(cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in PKEYS[table])
    pk = ",".join(PKEYS[table])
    sql = (f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
           f"ON CONFLICT({pk}) DO UPDATE SET {updates}")
    conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    return len(rows)


PKEYS = {
    "storage_daily": ("country", "gas_day"),
    "lng_daily": ("country", "gas_day"),
    "flow_daily": ("point", "gas_day", "direction"),
    "price_daily": ("symbol", "trade_day"),
    "weather_daily": ("country", "forecast_day", "run_date"),
    "metrics_daily": ("metric", "entity", "gas_day"),
}

STORAGE_COLS = ("country", "gas_day", "fill", "gas_twh", "wgv_twh",
                "injection", "withdrawal", "trend", "source")
LNG_COLS = ("country", "gas_day", "send_out", "inventory", "dtrs", "fullness")
FLOW_COLS = ("point", "gas_day", "direction", "operator", "corridor",
             "region", "is_supply", "value_gwh")
PRICE_COLS = ("symbol", "trade_day", "price")
WEATHER_COLS = ("country", "forecast_day", "run_date", "temp_mean", "hdd")
METRIC_COLS = ("metric", "entity", "gas_day", "value")


def upsert_storage(conn, rows): return _upsert(conn, "storage_daily", STORAGE_COLS, rows)
def upsert_lng(conn, rows): return _upsert(conn, "lng_daily", LNG_COLS, rows)
def upsert_flows(conn, rows): return _upsert(conn, "flow_daily", FLOW_COLS, rows)
def upsert_prices(conn, rows): return _upsert(conn, "price_daily", PRICE_COLS, rows)
def upsert_weather(conn, rows): return _upsert(conn, "weather_daily", WEATHER_COLS, rows)
def upsert_metrics(conn, rows): return _upsert(conn, "metrics_daily", METRIC_COLS, rows)


def write_signals(conn, run_date: str, signals: list[dict]) -> int:
    conn.execute("DELETE FROM signals WHERE run_date=?", (run_date,))
    conn.executemany(
        "INSERT INTO signals (run_date, category, entity, severity, score, headline, detail)"
        " VALUES (?,?,?,?,?,?,?)",
        [(run_date, s.get("category"), s.get("entity"), s.get("severity"),
          s.get("score"), s.get("headline"), s.get("detail")) for s in signals],
    )
    return len(signals)


# ── Queries ──────────────────────────────────────────────────────────────────

def storage_series(conn, country: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT gas_day, fill, gas_twh, wgv_twh, injection, withdrawal "
        "FROM storage_daily WHERE country=? ORDER BY gas_day", (country,)
    ).fetchall()


def latest_storage(conn, country: str):
    return conn.execute(
        "SELECT * FROM storage_daily WHERE country=? ORDER BY gas_day DESC LIMIT 1",
        (country,)
    ).fetchone()


def price_series(conn, symbol: str, limit: int = 400) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT trade_day, price FROM price_daily WHERE symbol=? "
        "ORDER BY trade_day DESC LIMIT ?", (symbol, limit)
    ).fetchall()
    return list(reversed(rows))


def flow_series(conn, point: str, direction: str | None = None):
    if direction:
        return conn.execute(
            "SELECT gas_day, value_gwh FROM flow_daily WHERE point=? AND direction=? "
            "ORDER BY gas_day", (point, direction)).fetchall()
    return conn.execute(
        "SELECT gas_day, direction, value_gwh FROM flow_daily WHERE point=? "
        "ORDER BY gas_day", (point,)).fetchall()


def doy_distribution(conn, table: str, value_col: str, entity_col: str,
                     entity: str, target_doy: int, window: int,
                     exclude_year: int | None = None) -> list[float]:
    """Pool historical values within +/-window days of a day-of-year.

    The core of the seasonal-normal engine: returns every historical value that
    fell near `target_doy` (across all years), optionally excluding the current
    year, so analytics can compute percentile / z-score / min-max bands.
    """
    rows = conn.execute(
        f"SELECT gas_day, {value_col} AS v FROM {table} "
        f"WHERE {entity_col}=? AND {value_col} IS NOT NULL", (entity,)
    ).fetchall()
    out = []
    for r in rows:
        try:
            d = datetime.strptime(r["gas_day"], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if exclude_year and d.year == exclude_year:
            continue
        doy = d.timetuple().tm_yday
        # circular distance across year boundary
        dist = min(abs(doy - target_doy), 365 - abs(doy - target_doy))
        if dist <= window:
            out.append(r["v"])
    return out


def table_count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def day_range(conn, table: str, day_col: str = "gas_day"):
    r = conn.execute(f"SELECT MIN({day_col}) lo, MAX({day_col}) hi FROM {table}").fetchone()
    return (r["lo"], r["hi"]) if r else (None, None)
