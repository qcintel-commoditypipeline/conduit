"""
gasintel — European gas market intelligence framework.

A small, replicable pipeline that turns raw market data into persisted
time series and ranked, seasonally-aware signals:

    ingest (reused fetchers) -> store (SQLite) -> analytics -> render / brief

Each stage is independent and testable. This package holds the *new* logic
(persistence, analytics, news, brief, sitrep render); data acquisition is
currently reused from the proven `gas_dashboard.py` fetchers and will be
folded into `gasintel.ingest` in a later pass.
"""

__version__ = "0.1.0"
