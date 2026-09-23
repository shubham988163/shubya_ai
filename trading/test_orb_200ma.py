"""Unit tests for the ORB + Multi-TF 200MA Trend Filter Strategy."""
from __future__ import annotations

import unittest
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

from trading.strategies.orb_200ma import (
    calculate_atr,
    get_opening_range,
    add_orb_indicators,
    detect_orb_signal,
    orb_stop,
    orb_target,
    fetch_mtf_200ma,
)
from trading.strategy import STRATEGIES, Engine
from trading.execution_router import ExecutionRouter
from trading.ledger import Ledger

IST = ZoneInfo("Asia/Kolkata")


def make_sample_df(n_bars: int = 40, base_price: float = 1000.0) -> pd.DataFrame:
    """Create a sample 15m OHLCV dataframe with IST timestamps."""
    start_dt = datetime.combine(datetime.now(IST).date(), dtime(9, 15), tzinfo=IST)
    times = [start_dt + timedelta(minutes=15 * i) for i in range(n_bars)]

    np.random.seed(42)
    prices = [base_price]
    for _ in range(n_bars - 1):
        prices.append(prices[-1] + np.random.uniform(-2, 2))

    df = pd.DataFrame(
        {
            "Open": prices,
            "High": [p + 2.0 for p in prices],
            "Low": [p - 2.0 for p in prices],
            "Close": [p + 0.5 for p in prices],
            "Volume": [100_000 for _ in prices],
        },
        index=pd.DatetimeIndex(times),
    )
    return df


class TestOrb200MaStrategy(unittest.TestCase):
    def test_calculate_atr(self):
        df = make_sample_df(30, 1000.0)
        atr = calculate_atr(df, length=14)
        self.assertEqual(len(atr), len(df))
        self.assertTrue((atr > 0).all())

    def test_opening_range_calculation(self):
        df = make_sample_df(10, 1000.0)
        orb = get_opening_range(df, or_minutes=15)
        self.assertIsNotNone(orb["or_high"])
        self.assertIsNotNone(orb["or_low"])
        self.assertTrue(orb["or_high"] >= orb["or_low"])
        self.assertTrue(orb["range_pct"] >= 0)
        # Should be locked since we have 10 bars (beyond 09:30)
        self.assertTrue(orb["locked"])

    def test_opening_range_size_filtering(self):
        # 1. Normal valid range (0.5% range)
        start_dt = datetime.combine(datetime.now(IST).date(), dtime(9, 15), tzinfo=IST)
        times = [start_dt, start_dt + timedelta(minutes=15)]
        df_normal = pd.DataFrame(
            {
                "Open": [1000.0, 1002.0],
                "High": [1005.0, 1004.0],  # range = 5 pts on 1000 = 0.5%
                "Low": [1000.0, 1001.0],
                "Close": [1003.0, 1002.0],
                "Volume": [100000, 120000],
            },
            index=pd.DatetimeIndex(times),
        )
        orb_normal = get_opening_range(df_normal, or_minutes=15)
        self.assertTrue(orb_normal["valid"])

        # 2. Too narrow range (0.1% range -> skip)
        df_tight = pd.DataFrame(
            {
                "Open": [1000.0, 1000.5],
                "High": [1001.0, 1000.8],  # range = 1 pt on 1000 = 0.1%
                "Low": [1000.0, 1000.2],
                "Close": [1000.5, 1000.5],
                "Volume": [100000, 100000],
            },
            index=pd.DatetimeIndex(times),
        )
        orb_tight = get_opening_range(df_tight, or_minutes=15)
        self.assertFalse(orb_tight["valid"])

        # 3. Too wide range (2.5% range -> skip)
        df_wide = pd.DataFrame(
            {
                "Open": [1000.0, 1015.0],
                "High": [1025.0, 1020.0],  # range = 25 pts on 1000 = 2.5%
                "Low": [1000.0, 1005.0],
                "Close": [1010.0, 1015.0],
                "Volume": [100000, 100000],
            },
            index=pd.DatetimeIndex(times),
        )
        orb_wide = get_opening_range(df_wide, or_minutes=15)
        self.assertFalse(orb_wide["valid"])

    def test_orb_stop_and_target_math(self):
        df = make_sample_df(30, 2000.0)
        df = add_orb_indicators(df)

        entry_long = 2000.0
        sl_long = orb_stop(df, "BUY", sl_atr_mult=1.0)
        self.assertTrue(sl_long < entry_long)

        tp_long = orb_target(df, "BUY", entry_long, sl_long, tp_r_mult=1.8)
        self.assertTrue(tp_long > entry_long)
        self.assertAlmostEqual(tp_long - entry_long, (entry_long - sl_long) * 1.8, places=1)

        entry_short = 2000.0
        sl_short = orb_stop(df, "SELL", sl_atr_mult=1.0)
        self.assertTrue(sl_short > entry_short)

        tp_short = orb_target(df, "SELL", entry_short, sl_short, tp_r_mult=1.8)
        self.assertTrue(tp_short < entry_short)
        self.assertAlmostEqual(entry_short - tp_short, (sl_short - entry_short) * 1.8, places=1)

    def test_strategy_registration(self):
        self.assertIn("orb_200ma", STRATEGIES)
        strat = STRATEGIES["orb_200ma"]
        self.assertEqual(strat["id"], "orb_200ma")
        self.assertTrue(callable(strat["prepare"]))
        self.assertTrue(callable(strat["detect"]))
        self.assertTrue(callable(strat["stop"]))
        self.assertTrue(callable(strat["target"]))


if __name__ == "__main__":
    unittest.main()
