"""Unit tests for the standalone stock screener's daily indicators."""
from __future__ import annotations

import importlib.util
import io
import math
import sys
import unittest
from contextlib import redirect_stdout
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
        cross_age: int = 0,
        cross_type: int | None = 1,
        difference: float = 1.0,
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
            direction=1,
            value=108.0,
            change_date=None,
            bars_since_change=None,
            flip_in_window=False,
            first_resolved_date=index[0].date(),
            bars=len(index),
        )
        cross_date = index[-1 - cross_age].date() if cross_type is not None else None
        crossover = stock_tsmom.CrossoverState(
            direction=1 if difference > 0 else (-1 if difference < 0 else 0),
            jma_value=109.0,
            dwma_value=109.0 - difference,
            difference=difference,
            change_date=cross_date,
            days_since_change=cross_age if cross_type is not None else None,
            crossover_in_window=cross_type is not None,
            first_scanned_date=index[0].date(),
            scanned_bars=len(index) - 10,
            daily_bars=len(index),
            crossover_count=1 if cross_type is not None else 0,
            last_crossover_type=cross_type,
            last_crossover_jma_value=109.0 if cross_type is not None else None,
            last_crossover_dwma_value=109.0 - difference if cross_type is not None else None,
        )
        return daily, stock_tsmom.SupertrendReading(weekly=weekly, daily=daily_st), crossover

    def test_buy_defaults_and_all_required_conditions(self) -> None:
        self.assertEqual(stock_tsmom.DEFAULT_BUY_SCORE, 6.0)
        self.assertEqual(stock_tsmom.DEFAULT_BUY_CROSSOVER_MAX_AGE, 5)
        self.assertEqual(stock_tsmom.DEFAULT_BUY_VOLUME_MULTIPLE, 1.2)
        self.assertEqual(stock_tsmom.DEFAULT_BUY_EMA_LENGTH, 20)
        self.assertEqual(stock_tsmom.DEFAULT_BUY_VOLUME_LOOKBACK, 20)

        daily, supertrend, crossover = self._entry_inputs()
        signal = stock_tsmom.classify_stock_signal(
            self._candidate_stock("BUYME", 7.2), daily, supertrend, crossover
        )
        expected_average = float(daily["volume"].tail(21).iloc[:-1].mean())
        expected_ema = float(
            stock_tsmom.daily_close_series(daily)
            .ewm(span=20, adjust=False, min_periods=20)
            .mean()
            .iloc[-1]
        )
        self.assertEqual(signal.signal, "BUY")
        self.assertEqual(signal.weekly_supertrend, "POS")
        self.assertEqual(signal.crossover_type, "POS")
        self.assertEqual(signal.crossover_age, 0)
        self.assertAlmostEqual(signal.jma_dwma_difference, 1.0)
        self.assertGreater(signal.close, signal.ema20)
        self.assertAlmostEqual(signal.average_volume, expected_average)
        self.assertAlmostEqual(signal.volume_ratio, 1_300.0 / expected_average)
        self.assertAlmostEqual(signal.ema20, expected_ema)
        self.assertTrue(any("Cross POS 0d" in reason for reason in signal.reasons))
        self.assertTrue(any("Volume" in reason and "20D" in reason for reason in signal.reasons))

    def test_exact_volume_threshold_passes_but_zero_spread_does_not(self) -> None:
        daily, supertrend, crossover = self._entry_inputs()
        daily.iloc[-1, daily.columns.get_loc("volume")] = 1_200.0
        exact_volume = stock_tsmom.classify_stock_signal(
            self._candidate_stock("VOLBOUNDARY", 6.0), daily, supertrend, crossover
        )
        self.assertAlmostEqual(exact_volume.volume_ratio, 1.2)
        self.assertEqual(exact_volume.signal, "BUY")

        _, _, zero_spread_cross = self._entry_inputs(difference=0.0)
        zero_spread = stock_tsmom.classify_stock_signal(
            self._candidate_stock("ZEROSPREAD", 6.0), daily, supertrend, zero_spread_cross
        )
        self.assertEqual(zero_spread.signal, "WATCH")
        self.assertTrue(any("JMA-DWMA +0.00" in reason for reason in zero_spread.reasons))

    def test_five_session_cross_age_passes_but_six_session_age_is_watch(self) -> None:
        daily, supertrend, cross_at_five = self._entry_inputs(cross_age=5)
        within_limit = stock_tsmom.classify_stock_signal(
            self._candidate_stock("FIVE", 6.0), daily, supertrend, cross_at_five
        )
        self.assertEqual(within_limit.signal, "BUY")

        _, _, cross_at_six = self._entry_inputs(cross_age=6)
        outside_limit = stock_tsmom.classify_stock_signal(
            self._candidate_stock("SIX", 6.0), daily, supertrend, cross_at_six
        )
        self.assertEqual(outside_limit.signal, "WATCH")
        self.assertTrue(any("6d" in reason for reason in outside_limit.reasons))

    def test_watch_is_strong_score_and_weekly_positive_without_buy(self) -> None:
        daily, supertrend, negative_cross = self._entry_inputs(cross_age=1, cross_type=-1, difference=-0.5)
        watch = stock_tsmom.classify_stock_signal(
            self._candidate_stock("WATCHME", 9.06), daily, supertrend, negative_cross
        )
        self.assertEqual(watch.signal, "WATCH")
        self.assertIn("POS", watch.weekly_supertrend)
        self.assertTrue(any("Cross NEG 1d" in reason for reason in watch.reasons))
        self.assertTrue(any("Score 9.06 >= 6" in reason for reason in watch.reasons))

        low_volume = daily.copy()
        low_volume.iloc[-1, low_volume.columns.get_loc("volume")] = 1_000.0
        low_vol_signal = stock_tsmom.classify_stock_signal(
            self._candidate_stock("LOWVOL", 6.2), low_volume, supertrend,
            self._entry_inputs()[2],
        )
        self.assertEqual(low_vol_signal.signal, "WATCH")
        self.assertLess(low_vol_signal.volume_ratio, stock_tsmom.DEFAULT_BUY_VOLUME_MULTIPLE)

        close_below_ema = daily.copy()
        close_below_ema.iloc[-1, close_below_ema.columns.get_loc("close")] = 100.0
        close_signal = stock_tsmom.classify_stock_signal(
            self._candidate_stock("BELOWEMA", 6.2), close_below_ema, supertrend,
            self._entry_inputs()[2],
        )
        self.assertEqual(close_signal.signal, "WATCH")
        self.assertTrue(any("Close<=EMA20" in reason for reason in close_signal.reasons))

    def test_avoid_means_score_below_threshold_or_weekly_not_positive(self) -> None:
        daily, positive_weekly, crossover = self._entry_inputs()
        low_score = stock_tsmom.classify_stock_signal(
            self._candidate_stock("LOW", 5.21), daily, positive_weekly, crossover
        )
        self.assertEqual(low_score.signal, "AVOID")
        self.assertTrue(any("Score 5.21 < 6" in reason for reason in low_score.reasons))

        daily, negative_weekly, crossover = self._entry_inputs(weekly_direction=-1)
        negative_trend = stock_tsmom.classify_stock_signal(
            self._candidate_stock("NEG", 9.0), daily, negative_weekly, crossover
        )
        self.assertEqual(negative_trend.signal, "AVOID")
        self.assertEqual(negative_trend.weekly_supertrend, "NEG")

    def test_missing_technical_history_does_not_crash_and_cannot_be_buy(self) -> None:
        daily = self._prices(np.linspace(100.0, 101.0, 10), index=pd.bdate_range("2025-01-01", periods=10))
        _, positive_weekly, _ = self._entry_inputs()
        missing_history = stock_tsmom.classify_stock_signal(
            self._candidate_stock("SHORT", 7.0),
            daily,
            positive_weekly,
            None,
            crossover_failure="fewer than 101 daily bars (10 available)",
        )
        self.assertEqual(missing_history.signal, "WATCH")
        self.assertTrue(np.isnan(missing_history.ema20))
        self.assertTrue(np.isnan(missing_history.volume_ratio))
        self.assertTrue(any("unavailable" in reason.lower() for reason in missing_history.reasons))

        missing_weekly = stock_tsmom.classify_stock_signal(
            self._candidate_stock("NOWEEKLY", 7.0),
            daily,
            None,
            None,
            supertrend_failure="fewer than 12 weekly bars",
        )
        self.assertEqual(missing_weekly.signal, "AVOID")
        self.assertTrue(any("fewer than 12 weekly bars" in reason for reason in missing_weekly.reasons))

    def test_selected_members_get_one_signal_and_output_counts_sum_to_selection(self) -> None:
        daily, positive_weekly, positive_cross = self._entry_inputs()
        _, _, negative_cross = self._entry_inputs(cross_type=-1, cross_age=1, difference=-0.5)
        _, negative_weekly, positive_cross_for_avoid = self._entry_inputs(weekly_direction=-1)
        stocks = [
            self._candidate_stock("BUY", 7.0),
            self._candidate_stock("WATCH", 8.0),
            self._candidate_stock("AVOID", 9.0),
        ]
        members = [
            stock_tsmom.PortfolioMember(stock, rank, 1.0 / rank, 0.5, 0.5)
            for rank, stock in enumerate(stocks, start=1)
        ]
        signals = stock_tsmom.classify_selected_portfolio_members(
            members,
            prices={stock.symbol: daily for stock in stocks},
            supertrend_states={
                "BUY": positive_weekly,
                "WATCH": positive_weekly,
                "AVOID": negative_weekly,
            },
            crossover_states={
                "BUY": positive_cross,
                "WATCH": negative_cross,
                "AVOID": positive_cross_for_avoid,
            },
        )
        self.assertEqual([item.symbol for item in signals], ["BUY", "WATCH", "AVOID"])
        self.assertEqual([item.signal for item in signals], ["BUY", "WATCH", "AVOID"])

        output = io.StringIO()
        with redirect_stdout(output):
            stock_tsmom.print_signal_classifications(
                signals,
                selected_count=len(members),
                as_of_month=pd.Period("2025-06", freq="M"),
                factor_month=pd.Period("2025-06", freq="M"),
            )
        rendered = output.getvalue()
        self.assertIn("BUY / WATCH / AVOID SIGNALS", rendered)
        self.assertIn("BUY: 1", rendered)
        self.assertIn("WATCH: 1", rendered)
        self.assertIn("AVOID: 1", rendered)
        self.assertIn("1 + 1 + 1 = 3 (3 selected portfolio members) [PASS]", rendered)
        self.assertIn("Factor signal: latest available monthly factor data", rendered)
        self.assertIn("Technical entry signal: latest completed daily session", rendered)
        self.assertIn("JMA-DWMA", rendered)
        self.assertIn("Volume", rendered)


if __name__ == "__main__":
    unittest.main()
