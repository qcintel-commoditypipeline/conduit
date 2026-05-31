"""
Analytics unit tests — no network. Build a synthetic SQLite store in a temp
file and assert the seasonal/trajectory/deviation math behaves.

Run on the box venv:
    cd /opt/scripts/conduit-dev && python3 -m pytest tests/ -q
or with plain unittest:
    python3 -m unittest discover -s tests -q
"""
import os
import tempfile
import unittest
from datetime import date, timedelta

from gasintel import store
from gasintel.analytics import seasonal, trajectory


def _seed(conn):
    """5 years of history + a current year sitting unusually low."""
    rows = []
    # Historical years 2021–2025: fill rises from ~40% in spring to ~95% by Nov.
    for yr in range(2021, 2026):
        for doy in range(1, 366):
            frac = (doy - 100) / 205.0
            fill = 40 + max(0.0, min(1.0, frac)) * 55
            d = date(yr, 1, 1) + timedelta(days=doy - 1)
            rows.append({"country": "DE", "gas_day": d.isoformat(), "fill": round(fill, 1)})
    # Current year 2026 up to ~day 150 but ~20pp BELOW the historical path.
    for doy in range(1, 151):
        frac = (doy - 100) / 205.0
        fill = 40 + max(0.0, min(1.0, frac)) * 55 - 20
        d = date(2026, 1, 1) + timedelta(days=doy - 1)
        rows.append({"country": "DE", "gas_day": d.isoformat(), "fill": round(max(0, fill), 1)})
    store.upsert_storage_fill(conn, rows)


class SeasonalTests(unittest.TestCase):
    def test_percentile_and_label(self):
        dist = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        self.assertLess(seasonal.percentile_of(15, dist), 20)
        self.assertGreater(seasonal.percentile_of(95, dist), 80)
        self.assertEqual(seasonal.label_for_percentile(3), "exceptionally low")
        self.assertEqual(seasonal.label_for_percentile(50), "around normal")

    def test_winsorize_clips_outliers(self):
        xs = [0] + list(range(40, 60)) + [1000]
        w = seasonal.winsorize(xs, 0.10)
        self.assertLess(max(w), 1000)
        self.assertGreater(min(w), 0)

    def test_zscore_zero_variance(self):
        self.assertEqual(seasonal.zscore(5, [5, 5, 5, 5, 5]), 0.0)


class TrajectoryTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = store.connect(self.path)
        store.init_db(self.conn)
        _seed(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_seasonal_projection_is_bounded(self):
        t = trajectory._entity_trajectory(self.conn, "DE", "2026-05-31")
        self.assertIsNotNone(t)
        # projection must never exceed 100% (the old linear bug gave 112%)
        self.assertLessEqual(t["projected_fill"], 100.0)
        self.assertEqual(t["proj_method"], "seasonal")

    def test_below_normal_is_flagged_behind(self):
        t = trajectory._entity_trajectory(self.conn, "DE", "2026-05-31")
        # current year seeded ~20pp below history -> below seasonal norm now
        self.assertLess(t["vs_normal_now_pp"], 0)
        # starting low with only a normal gain -> short of the 90% target
        self.assertFalse(t["on_track"])
        self.assertGreater(t["shortfall_pp"], 0)

    def test_projection_reflects_low_start(self):
        t = trajectory._entity_trajectory(self.conn, "DE", "2026-05-31")
        # projected should sit clearly below the normal-at-target level
        self.assertLess(t["projected_fill"], t["normal_at_target_avg"])


if __name__ == "__main__":
    unittest.main()
