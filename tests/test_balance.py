"""
Balance (corridor w/w) unit tests — synthetic flow fixtures, no network.

Proves the two fixes to the w/w methodology:
  1. a partial trailing gas-day (fewer rows than the recent norm) is excluded
     and the latest COMPLETE day is used instead;
  2. the comparison day is exactly 7 calendar days before the chosen latest
     day (nearest day with data when the exact day is missing, flagged when
     the gap exceeds 2 days).
"""
import os
import tempfile
import unittest
from datetime import date, timedelta

from gasintel import store
from gasintel.analytics import balance

POINTS = [
    # (point, corridor, region)
    ("Dornum", "Norwegian (North Sea)", "Norway"),
    ("Emden", "Norwegian (North Sea)", "Norway"),
    ("Mazara del Vallo", "North African (Transmed)", "North Africa"),
    ("Gate Terminal", "LNG (NL)", "LNG"),
    ("Zeebrugge", "LNG / IUK", "LNG"),
    ("Mallnow", "Eastern (Yamal)", "Russia"),
]


def _rows_for_day(day: str, value: float, points=POINTS):
    return [{"point": p, "gas_day": day, "direction": "entry", "operator": "op",
             "corridor": c, "region": r, "is_supply": 1, "value_gwh": value}
            for p, c, r in points]


class BalanceTests(unittest.TestCase):
    LATEST = date(2026, 6, 2)   # newest COMPLETE day in the fixtures

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = store.connect(self.path)
        store.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def _seed(self, days_back=14, value=100.0, skip=()):
        """Full 6-point days from LATEST going back `days_back` days."""
        for k in range(days_back + 1):
            d = self.LATEST - timedelta(days=k)
            if d in skip:
                continue
            store.upsert_flows(self.conn, _rows_for_day(d.isoformat(), value))
        self.conn.commit()

    def _add_partial_newest(self, value=100.0):
        """A trailing day after LATEST with only 2 of 6 points reporting."""
        d = (self.LATEST + timedelta(days=1)).isoformat()
        store.upsert_flows(self.conn, _rows_for_day(d, value, POINTS[:2]))
        self.conn.commit()
        return d

    # ── partial-day exclusion ────────────────────────────────────────────────

    def test_partial_trailing_day_is_excluded(self):
        self._seed()
        partial = self._add_partial_newest()
        out = balance.compute(self.conn)
        self.assertTrue(out["available"])
        self.assertEqual(out["latest_day"], self.LATEST.isoformat())
        self.assertEqual(out["partial_day_excluded"], partial)

    def test_complete_trailing_day_is_kept(self):
        self._seed()
        out = balance.compute(self.conn)
        self.assertEqual(out["latest_day"], self.LATEST.isoformat())
        self.assertIsNone(out["partial_day_excluded"])

    def test_partial_day_values_never_pollute_wow(self):
        """The old code's 'latest' could be the partial day, making every
        corridor look like it collapsed w/w. With the partial day excluded the
        w/w deltas come out flat for a flat series."""
        self._seed(value=100.0)
        self._add_partial_newest(value=5.0)  # tiny partial values
        out = balance.compute(self.conn)
        for c in out["by_corridor"]:
            self.assertEqual(c["wow_gwh"], 0.0, c)

    # ── exact 7-day comparison ──────────────────────────────────────────────

    def test_prev_day_is_exactly_seven_days_before(self):
        self._seed()
        out = balance.compute(self.conn)
        self.assertEqual(out["prev_day"],
                         (self.LATEST - timedelta(days=7)).isoformat())
        self.assertEqual(out["prev_gap_days"], 0)

    def test_wow_uses_the_seven_day_old_value(self):
        """Seed 60 GWh per point 7 days ago and 100 today -> +80 w/w per
        2-point corridor, regardless of what intermediate days did."""
        sevenago = self.LATEST - timedelta(days=7)
        self._seed(value=100.0, skip=(sevenago,))
        store.upsert_flows(self.conn, _rows_for_day(sevenago.isoformat(), 60.0))
        self.conn.commit()
        out = balance.compute(self.conn)
        norwegian = next(c for c in out["by_corridor"]
                         if c["corridor"] == "Norwegian (North Sea)")
        self.assertEqual(norwegian["net_gwh"], 200.0)   # 2 points x 100
        self.assertEqual(norwegian["wow_gwh"], 80.0)    # vs 2 x 60
        # the old code compared vs the 8th-most-recent distinct day; make sure
        # the chosen day really is the calendar-7d one
        self.assertEqual(out["prev_day"], sevenago.isoformat())

    def test_nearest_day_used_and_gap_flagged_when_target_missing(self):
        """Remove a window around the 7-day target so the nearest data day is
        3 days off -> still compared, but the gap is surfaced."""
        skip = {self.LATEST - timedelta(days=k) for k in (4, 5, 6, 7, 8, 9)}
        self._seed(days_back=14, skip=skip)
        out = balance.compute(self.conn)
        self.assertEqual(out["prev_day"],
                         (self.LATEST - timedelta(days=10)).isoformat())
        self.assertEqual(out["prev_gap_days"], 3)

    # ── degraded states ─────────────────────────────────────────────────────

    def test_empty_store_unavailable(self):
        out = balance.compute(self.conn)
        self.assertFalse(out["available"])

    def test_single_day_store_degrades_to_zero_wow(self):
        store.upsert_flows(self.conn, _rows_for_day(self.LATEST.isoformat(), 100.0))
        self.conn.commit()
        out = balance.compute(self.conn)
        self.assertTrue(out["available"])
        self.assertEqual(out["latest_day"], out["prev_day"])
        for c in out["by_corridor"]:
            self.assertEqual(c["wow_gwh"], 0.0)


if __name__ == "__main__":
    unittest.main()
