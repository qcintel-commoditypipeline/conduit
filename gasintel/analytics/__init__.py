"""
Analytics engine — turns persisted time series into seasonally-aware,
ranked signals. Pure functions over the store; everything is testable without
the network.

  seasonal   — day-of-year baseline math (percentile / z / band)
  deviations — "what's abnormal today" across storage, injection, price
  trajectory — refill pace vs the 90% / Nov-1 mandate and the 5-yr path
  spreads    — price level, momentum, and transatlantic ratio
  balance    — cross-border supply by corridor (needs flow history)

`build()` runs them all and returns a single analytics dict + ranked signals.
"""
from . import seasonal, deviations, trajectory, spreads, balance


def build(conn, run_date: str) -> dict:
    out = {
        "run_date": run_date,
        "trajectory": trajectory.compute(conn, run_date),
        "spreads": spreads.compute(conn),
        "balance": balance.compute(conn),
    }
    out["signals"] = deviations.build_signals(conn, run_date, out)
    return out
