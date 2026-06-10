"""
Brief facts-block unit tests — no network, no API key needed (we only test the
deterministic Python side: _facts / _fmt). Proves:
  * a missing metric (None) renders as 'n/a' and never raises (the old code
    TypeError'd on vs_normal_now_pp=None and killed the whole brief);
  * the enriched facts give the LLM real material: refill vs the 90% mandate,
    top-3 corridor w/w moves, biggest country deviation, TTF w/w.
"""
import unittest

from gasintel import brief


def _analytics():
    return {
        "run_date": "2026-06-10",
        "trajectory": {
            "EU": {"current_fill": 53.4, "normal_now_avg": 62.1,
                   "vs_normal_now_pp": -8.7, "projected_fill": 84.2,
                   "on_track": False, "shortfall_pp": 5.8,
                   "pace_pp_per_day": 0.31, "days_to_target": 144},
            "DE": {"current_fill": 48.0, "normal_now_avg": 64.0,
                   "vs_normal_now_pp": -16.0, "projected_fill": 81.0,
                   "on_track": False, "shortfall_pp": 9.0},
            "FR": {"current_fill": 60.0, "normal_now_avg": 58.0,
                   "vs_normal_now_pp": 2.0, "projected_fill": 92.0,
                   "on_track": True, "shortfall_pp": 0.0},
        },
        "spreads": {"ttf": {"last": 34.56, "chg_1d_pct": 1.2, "chg_1w_pct": -4.3,
                            "chg_30d_pct": 6.0, "year_percentile": 41.0}},
        "signals": [{"headline": "DE storage 48% — well below normal",
                     "detail": "8th percentile for this date"}],
        "balance": {
            "available": True, "latest_day": "2026-06-08",
            "prev_day": "2026-06-01", "partial_day_excluded": "2026-06-09",
            "prev_gap_days": 0,
            "by_corridor": [
                {"corridor": "Norwegian (North Sea)", "net_gwh": 3200.0, "wow_gwh": 50.0},
                {"corridor": "LNG (FR)", "net_gwh": 900.0, "wow_gwh": -210.0},
                {"corridor": "Azeri (TAP)", "net_gwh": 310.0, "wow_gwh": 5.0},
                {"corridor": "Eastern (Yamal)", "net_gwh": 120.0, "wow_gwh": 90.0},
            ],
        },
    }


class FmtTests(unittest.TestCase):
    def test_none_is_na(self):
        self.assertEqual(brief._fmt(None, "+.1f", "pp"), "n/a")

    def test_bad_spec_value_is_na(self):
        self.assertEqual(brief._fmt("not-a-number", "+.1f"), "n/a")

    def test_normal_formatting(self):
        self.assertEqual(brief._fmt(-8.66, "+.1f", "pp"), "-8.7pp")


class FactsTests(unittest.TestCase):
    def test_none_metric_does_not_raise(self):
        """The exact prod crash: vs_normal_now_pp=None inside a '+' f-string."""
        a = _analytics()
        a["trajectory"]["EU"]["vs_normal_now_pp"] = None
        a["trajectory"]["EU"]["normal_now_avg"] = None
        text = brief._facts(a, [])
        self.assertIn("n/a", text)
        self.assertIn("EU storage 53.4%", text)

    def test_everything_missing_still_returns_text(self):
        text = brief._facts({}, [])
        self.assertIsInstance(text, str)
        self.assertTrue(text)  # at least the 'no anomalies' line

    def test_mandate_context_present(self):
        text = brief._facts(_analytics(), [])
        self.assertIn("90% Nov-1 mandate", text)
        self.assertIn("projected 84.2%", text)
        self.assertIn("5.8pp short", text)
        self.assertIn("144 days to target", text)

    def test_biggest_country_deviation_is_de(self):
        text = brief._facts(_analytics(), [])
        self.assertIn("Biggest country deviation", text)
        self.assertIn("DE 48.0% vs ~64.0% norm (-16.0pp)", text)

    def test_top3_corridor_movers_ranked_by_abs_wow(self):
        text = brief._facts(_analytics(), [])
        line = next(l for l in text.split("\n")
                    if l.startswith("Top corridor w/w moves"))
        self.assertIn("LNG (FR) -210", line)
        self.assertIn("Eastern (Yamal) +90", line)
        self.assertIn("Norwegian (North Sea) +50", line)
        self.assertNotIn("Azeri", line)  # 4th-largest move stays out
        # ranked by magnitude
        self.assertLess(line.index("LNG (FR)"), line.index("Eastern (Yamal)"))

    def test_ttf_includes_wow(self):
        text = brief._facts(_analytics(), [])
        self.assertIn("-4.3% w/w", text)

    def test_balance_comparison_dates_shown(self):
        text = brief._facts(_analytics(), [])
        self.assertIn("(2026-06-08 vs 2026-06-01)", text)

    def test_headlines_appended(self):
        text = brief._facts(_analytics(), [{"headline": "Norway maintenance extended"}])
        self.assertIn("Norway maintenance extended", text)


if __name__ == "__main__":
    unittest.main()
