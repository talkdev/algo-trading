"""Unit tests for the standalone stock screener's daily indicators."""
from __future__ import annotations

import importlib.util
import io
import math
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "stock-tsmom.py"
SPEC = importlib.util.spec_from_file_location("stock_tsmom_indicators_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"Could not load {MODULE_PATH}")
stock_tsmom = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stock_tsmom
SPEC.loader.exec_module(stock_tsmom)


class DailyIndicatorTests(unittest.TestCase):
    @staticmethod
    def _prices(closes: np.ndarray, index: pd.DatetimeIndex | None = None) -> pd.DataFrame:
        if index is None:
            index = pd.bdate_range("2022-01-03", periods=len(closes))
        close = np.asarray(closes, dtype=float)
        return pd.DataFrame(
            {
                "open": close * 0.999,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": np.full(len(close), 1_000_000.0),
            },
            index=index,
        )

    def test_default_jma_constants_match_spec(self) -> None:
        params = stock_tsmom.jma_parameters()
        self.assertAlmostEqual(params.beta, 0.5744680851, places=9)
        self.assertAlmostEqual(params.len1, 2.792481, places=5)
        self.assertAlmostEqual(params.pow1, 0.792481, places=5)
        self.assertEqual(params.div, 0.1)
        self.assertAlmostEqual(params.phase_ratio, 1.1, places=12)
        self.assertAlmostEqual(params.cap, 3.655, delta=0.001)
        self.assertAlmostEqual(params.avolty_factor, 2 / 31, places=12)

    def test_jma_matches_the_given_recurrence(self) -> None:
        """Check that the adaptive recurrence updates in the prescribed order."""
        closes = 100.0 + np.cumsum(np.tile([0.6, -0.2, 0.1, -0.7, 0.3], 30))
        index = pd.bdate_range("2024-01-01", periods=len(closes))
        series = pd.Series(closes, index=index)
        actual = stock_tsmom.calculate_jma(series).to_numpy()

        p = stock_tsmom.jma_parameters()
        beta, pow1 = p.beta, p.pow1
        volty_hist: list[float] = []
        vsum = avolty = ma1 = det0 = e2 = 0.0
        bsmax = bsmin = jma = float(closes[0])
        expected: list[float] = []
        for i, price in enumerate(closes):
            del1, del2 = price - bsmax, price - bsmin
            volty = max(abs(del1), abs(del2))
            volty_hist.append(volty)
            volty_lag10 = volty_hist[i - 10] if i >= 10 else 0.0
            vsum += p.div * (volty - volty_lag10)
            avolty += p.avolty_factor * (vsum - avolty)
            d_volty = min(max(volty / avolty if avolty > 0 else 0.0, 1.0), p.cap)
            pow2 = d_volty**pow1
            kv = beta**math.sqrt(pow2)
            upper = price if del1 > 0 else price - kv * del1
            lower = price if del2 < 0 else price - kv * del2
            alpha = beta**pow2
            ma1 = (1 - alpha) * price + alpha * ma1
            det0 = (price - ma1) * (1 - beta) + beta * det0
            ma2 = ma1 + p.phase_ratio * det0
            det1 = (ma2 - jma) * (1 - alpha) ** 2 + alpha**2 * e2
            jma += det1
            e2 = det1
            bsmax, bsmin = upper, lower
            expected.append(jma)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-12)

    def test_supplied_jma_recurrence_does_not_use_power_parameter(self) -> None:
        close = pd.Series(100.0 + np.arange(120) * 0.1, index=pd.bdate_range("2024-01-01", periods=120))
        default_power = stock_tsmom.calculate_jma(close, power=0.35)
        other_power = stock_tsmom.calculate_jma(close, power=0.8)
        np.testing.assert_array_equal(default_power.to_numpy(), other_power.to_numpy())

    def test_dwma_uses_oldest_to_newest_weights_and_210_total(self) -> None:
        close = pd.Series(np.arange(1.0, 21.0), index=pd.bdate_range("2024-01-01", periods=20))
        result = stock_tsmom.calculate_dwma(close)
        self.assertTrue(result.iloc[:-1].isna().all())
        self.assertAlmostEqual(result.iloc[-1], sum(i * i for i in range(1, 21)) / 210)

    def test_crossover_frame_skips_first_100_and_applies_strict_rules(self) -> None:
        rng = np.random.default_rng(7)
        drift = np.concatenate((np.full(300, -0.0012), np.full(300, 0.0015), np.full(300, -0.0006)))
        closes = 1000.0 * np.exp(np.cumsum(drift + rng.normal(0.0, 0.012, len(drift))))
        series = pd.Series(closes, index=pd.bdate_range("2023-01-02", periods=len(closes)))
        frame = stock_tsmom.jma_dwma_crossover_frame(series)
        self.assertEqual(len(frame), len(series) - 100)
        self.assertEqual(frame.index[0], series.index[100])
        previous = frame["jma_prev"] - frame["dwma_prev"]
        current = frame["jma"] - frame["dwma"]
        expected = np.select(
            [(previous <= 0) & (current > 0), (previous >= 0) & (current < 0)],
            [1, -1],
            default=0,
        )
        np.testing.assert_array_equal(frame["crossover"], expected)
        self.assertTrue(np.any(frame["crossover"] == 1))
        self.assertTrue(np.any(frame["crossover"] == -1))

    def test_crossover_equality_uses_strict_current_side_rules(self) -> None:
        index = pd.bdate_range("2024-01-01", periods=6)
        close = pd.Series(np.ones(6), index=index)
        jma = pd.Series([0.0, 10.0, 10.0, 11.0, 9.0, 8.0], index=index)
        dwma = pd.Series([0.0, 10.0, 10.0, 10.0, 10.0, 10.0], index=index)
        with patch.object(stock_tsmom, "calculate_jma", return_value=jma), patch.object(
            stock_tsmom, "calculate_dwma", return_value=dwma
        ):
            frame = stock_tsmom.jma_dwma_crossover_frame(close, warmup_bars=1)
        # Bar 2 is equal/equal, bar 3 leaves equality upward (Positive), and
        # bar 4 crosses downward (Negative). The first scanned row lacks a
        # previous bar and therefore remains None.
        self.assertEqual(frame["crossover"].tolist(), [0, 0, 1, -1, 0])

    def test_crossover_state_contains_last_event_values_and_date(self) -> None:
        rng = np.random.default_rng(17)
        close = 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, 500)))
        daily = self._prices(close)
        state, reason = stock_tsmom.crossover_state(daily)
        self.assertIsNone(reason)
        self.assertIsNotNone(state)
        assert state is not None
        close_series = stock_tsmom.daily_close_series(daily)
        frame = stock_tsmom.jma_dwma_crossover_frame(close_series)
        events = frame[frame["crossover"] != 0]
        self.assertFalse(events.empty)
        day = events.index[-1]
        event = events.iloc[-1]
        self.assertEqual(state.change_date, day.date())
        self.assertEqual(state.last_crossover_type, int(event["crossover"]))
        self.assertAlmostEqual(state.last_crossover_jma_value or 0, float(event["jma"]))
        self.assertAlmostEqual(state.last_crossover_dwma_value or 0, float(event["dwma"]))
        self.assertEqual(state.daily_bars, len(close_series))
        self.assertEqual(state.last_crossover_label, "Positive" if event["crossover"] > 0 else "Negative")

    def test_daily_and_weekly_supertrend_are_both_reported(self) -> None:
        t = np.arange(400, dtype=float)
        close = 100 + 0.08 * t + 6 * np.sin(t / 9.0)
        daily = self._prices(close)
        daily_state, daily_reason = stock_tsmom.daily_supertrend_state(daily)
        weekly_state, weekly_reason = stock_tsmom.weekly_supertrend_state(daily)
        self.assertIsNone(daily_reason)
        self.assertIsNone(weekly_reason)
        self.assertIsNotNone(daily_state)
        self.assertIsNotNone(weekly_state)
        assert daily_state is not None and weekly_state is not None
        self.assertEqual(daily_state.timeframe, "daily")
        self.assertEqual(weekly_state.timeframe, "weekly")
        self.assertEqual(daily_state.bars, 400)
        self.assertLess(weekly_state.bars, daily_state.bars)
        daily_result = stock_tsmom.calculate_supertrend(stock_tsmom.clean_daily_bars(daily))
        trends = daily_result["trend"].to_numpy(dtype=int)
        flips = [i for i in range(1, len(trends)) if trends[i - 1] != 0 and trends[i] != trends[i - 1]]
        self.assertTrue(flips)
        self.assertEqual(daily_state.change_date, daily_result.index[flips[-1]].date())
        self.assertTrue(daily_state.since_label().endswith("d)"))
        # The legacy helper remains weekly, and the new helper handles daily.
        legacy_state, legacy_reason = stock_tsmom.supertrend_state(daily)
        self.assertIsNone(legacy_reason)
        self.assertEqual(legacy_state, weekly_state)

    def test_sort_uses_latest_crossover_date_not_supertrend(self) -> None:
        def member(symbol: str, rank: int) -> stock_tsmom.PortfolioMember:
            stock = stock_tsmom.RankedStock(
                symbol=symbol,
                score=10.0 - rank,
                residual_sum=0.1,
                residual_volatility=0.05,
                beta_market=1.0,
                beta_smb=0.0,
                beta_hml=0.0,
                r_squared=0.5,
                latest_price=100.0,
                median_daily_turnover_inr=1_000_000.0,
            )
            return stock_tsmom.PortfolioMember(stock, rank, float(rank), 0.5, 0.5)

        members = [member("OLD", 1), member("NEW", 2), member("NOX", 3)]

        def state(day: pd.Timestamp | None) -> stock_tsmom.CrossoverState:
            change = day.date() if day is not None else None
            return stock_tsmom.CrossoverState(
                direction=1,
                jma_value=101.0,
                dwma_value=100.0,
                difference=1.0,
                change_date=change,
                days_since_change=1 if change else None,
                crossover_in_window=change is not None,
                first_scanned_date=pd.Timestamp("2025-01-01").date(),
                scanned_bars=100,
                daily_bars=200,
                crossover_count=1 if change else 0,
                last_crossover_type=1 if change else None,
                last_crossover_jma_value=101.0 if change else None,
                last_crossover_dwma_value=100.0 if change else None,
            )

        signals = {
            "OLD": state(pd.Timestamp("2025-02-01")),
            "NEW": state(pd.Timestamp("2025-04-01")),
            "NOX": state(None),
        }
        desc = stock_tsmom.sort_members_by_crossover(members, signals, "desc")
        asc = stock_tsmom.sort_members_by_crossover(members, signals, "asc")
        self.assertEqual([m.stock.symbol for m in desc], ["NEW", "OLD", "NOX"])
        self.assertEqual([m.stock.symbol for m in asc], ["NOX", "OLD", "NEW"])

    def test_daily_close_series_keeps_valid_close_when_ohlc_is_incomplete(self) -> None:
        daily = self._prices(np.array([10.0, 11.0, 12.0]))
        daily.loc[daily.index[1], "high"] = np.nan
        self.assertEqual(len(stock_tsmom.clean_daily_bars(daily)), 2)
        closes = stock_tsmom.daily_close_series(daily)
        self.assertEqual(closes.tolist(), [10.0, 11.0, 12.0])

    def test_daily_close_series_prefers_adjusted_close_when_present(self) -> None:
        daily = self._prices(np.array([100.0, 102.0, 104.0]))
        daily["Adj Close"] = [50.0, 51.0, 52.0]
        self.assertEqual(stock_tsmom.daily_close_series(daily).tolist(), [50.0, 51.0, 52.0])

    @staticmethod
    def _candidate_stock(symbol: str, score: float) -> stock_tsmom.RankedStock:
        return stock_tsmom.RankedStock(
            symbol=symbol,
            score=score,
            residual_sum=0.1,
            residual_volatility=0.05,
            beta_market=1.0,
            beta_smb=0.0,
            beta_hml=0.0,
            r_squared=0.5,
            latest_price=110.0,
            median_daily_turnover_inr=1_000_000.0,
        )

    def _entry_inputs(
        self,
        weekly_direction: int = 1,
        daily_direction: int = 1,
        cross_age: int = 0,
    ) -> tuple[pd.DataFrame, stock_tsmom.SupertrendReading, stock_tsmom.CrossoverState]:
        index = pd.bdate_range("2025-01-01", periods=80)
        close = np.linspace(100.0, 110.0, len(index))
        daily = self._prices(close, index=index)
        daily["volume"] = 1_000.0
        daily.iloc[-1, daily.columns.get_loc("volume")] = 1_300.0
        weekly = stock_tsmom.SupertrendState(
            timeframe="weekly",
            direction=weekly_direction,
            value=108.0,
            change_date=None,
            bars_since_change=None,
            flip_in_window=False,
            first_resolved_date=index[0].date(),
            bars=16,
        )
        daily_st = stock_tsmom.SupertrendState(
            timeframe="daily",
            direction=daily_direction,
            value=108.0,
            change_date=None,
            bars_since_change=None,
            flip_in_window=False,
            first_resolved_date=index[0].date(),
            bars=len(index),
        )
        cross_date = index[-1 - cross_age].date()
        crossover = stock_tsmom.CrossoverState(
            direction=1,
            jma_value=109.0,
            dwma_value=108.0,
            difference=1.0,
            change_date=cross_date,
            days_since_change=cross_age,
            crossover_in_window=True,
            first_scanned_date=index[0].date(),
            scanned_bars=len(index) - 10,
            daily_bars=len(index),
            crossover_count=1,
            last_crossover_type=1,
            last_crossover_jma_value=109.0,
            last_crossover_dwma_value=108.0,
        )
        return daily, stock_tsmom.SupertrendReading(weekly=weekly, daily=daily_st), crossover

    def test_entry_screen_buy_requires_all_five_technical_rules(self) -> None:
        daily, supertrend, crossover = self._entry_inputs()
        candidate = stock_tsmom.classify_entry_candidate(
            self._candidate_stock("BUYME", 7.2), daily, supertrend, crossover
        )
        self.assertEqual(candidate.classification, "BUY")
        self.assertTrue(candidate.fresh_positive_cross)
        self.assertEqual(candidate.weekly_supertrend, "POS")
        self.assertEqual(candidate.crossover_type, "Positive")
        self.assertGreater(candidate.close, candidate.ema20)
        self.assertGreaterEqual(candidate.volume_multiple, 1.2)
        self.assertLessEqual(candidate.close, 1.08 * candidate.ema20)

    def test_entry_screen_watch_and_avoid_wait_rules(self) -> None:
        daily, supertrend, old_cross = self._entry_inputs(cross_age=1)
        watch = stock_tsmom.classify_entry_candidate(
            self._candidate_stock("WATCHME", 6.5), daily, supertrend, old_cross
        )
        self.assertEqual(watch.classification, "WATCH")
        self.assertIn("No Positive JMA/DWMA cross on the latest session", watch.reasons)

        negative_weekly = stock_tsmom.SupertrendState(
            timeframe="weekly",
            direction=-1,
            value=112.0,
            change_date=pd.Timestamp("2025-07-01").date(),
            bars_since_change=1,
            flip_in_window=True,
            first_resolved_date=pd.Timestamp("2024-01-01").date(),
            bars=80,
        )
        avoid = stock_tsmom.classify_entry_candidate(
            self._candidate_stock("AVOIDME", 8.0),
            daily,
            stock_tsmom.SupertrendReading(weekly=negative_weekly, daily=supertrend.daily),
            old_cross,
        )
        self.assertEqual(avoid.classification, "AVOID/WAIT")
        self.assertIn("Weekly Supertrend is NEG", avoid.reasons)

        positive_weekly_negative_daily = replace(
            supertrend,
            daily=replace(supertrend.daily, direction=-1),
        )
        deteriorating = stock_tsmom.classify_entry_candidate(
            self._candidate_stock("DAILYNEG", 6.8), daily, positive_weekly_negative_daily, old_cross
        )
        self.assertEqual(deteriorating.classification, "AVOID/WAIT")
        self.assertIn("Daily Supertrend is NEG", deteriorating.reasons)

        overextended_daily = daily.copy()
        overextended_daily.iloc[-1, overextended_daily.columns.get_loc("close")] = 120.0
        overextended = stock_tsmom.classify_entry_candidate(
            self._candidate_stock("EXTENDED", 6.2), overextended_daily, supertrend, old_cross
        )
        self.assertEqual(overextended.classification, "WATCH")
        self.assertTrue(any("more than 8% above EMA20" in reason for reason in overextended.reasons))

    def test_entry_screen_uses_all_score_qualified_stocks_and_prints_after_review(self) -> None:
        daily, supertrend, crossover = self._entry_inputs()
        stocks = [self._candidate_stock("QUALIFIED", 7.0), self._candidate_stock("LOW", 5.9)]
        candidates, below = stock_tsmom.screen_entry_candidates(
            stocks,
            prices={"QUALIFIED": daily},
            supertrend_states={"QUALIFIED": supertrend},
            crossover_states={"QUALIFIED": crossover},
        )
        self.assertEqual(below, 1)
        self.assertEqual([candidate.symbol for candidate in candidates], ["QUALIFIED"])
        self.assertEqual(candidates[0].classification, "BUY")

        output = io.StringIO()
        with redirect_stdout(output):
            stock_tsmom.print_entry_candidate_screen(candidates, screened_count=2, below_score_threshold=1)
        rendered = output.getvalue()
        self.assertIn("ENTRY CANDIDATE REVIEW", rendered)
        self.assertIn("BUY rules", rendered)
        self.assertIn("QUALIFIED", rendered)
        self.assertIn("BUY 1 / WATCH 0 / AVOID/WAIT 0", rendered)


if __name__ == "__main__":
    unittest.main()
