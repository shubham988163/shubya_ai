"""Forex & Crypto Multi-Strategy Backtesting Engine.

Tests the multi-strategy confluence setup (ORB + VWAP + 9/21 EMA + 200 SMA + Volume + Trailing Stop)
on historical candle data from Delta Exchange.
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd

from trading.delta_exchange import DeltaClient, FOREX_WATCHLIST
from trading.forex_engine import calculate_indicators


class ForexBacktester:
    """Historical backtester for Delta Exchange 24/7 markets."""

    def __init__(self, client: DeltaClient | None = None):
        self.client = client or DeltaClient()

    def backtest_symbol(
        self,
        symbol: str,
        resolution: str = "15",
        bars: int = 300,
        risk_per_trade: float = 50.0,
    ) -> dict[str, Any]:
        """Run backtest on a single symbol over historical candles."""
        df = self.client.get_candles(symbol, resolution=resolution, bars=bars)
        if df is None or len(df) < 50:
            return {"symbol": symbol, "error": "Insufficient candle data", "trades": []}

        df = calculate_indicators(df)

        trades: list[dict[str, Any]] = []
        open_trade: dict[str, Any] | None = None

        # Iterate bar by bar after indicator warm-up period (first 30 bars)
        for i in range(30, len(df)):
            curr = df.iloc[i]
            prev = df.iloc[i - 1]
            ts = df.index[i]

            high = float(curr["High"])
            low = float(curr["Low"])
            close = float(curr["Close"])
            ema9 = float(curr["EMA_9"])
            ema21 = float(curr["EMA_21"])
            sma200 = float(curr["SMA_200"])
            vwap = float(curr["VWAP"])
            atr = float(curr["ATR"])
            rvol = float(curr["RVOL"])

            # 1. Manage Open Position if one is active
            if open_trade is not None:
                side = open_trade["side"]
                entry = open_trade["entry_price"]
                sl = open_trade["stop_loss"]
                tg = open_trade["target"]
                size = open_trade["size"]

                r_dist = float(open_trade.get("risk_pts") or abs(entry - sl))
                if r_dist <= 0:
                    r_dist = max(0.5, entry * 0.002)

                if side == "BUY":
                    # Breakeven condition: +1.0R gain locks stop at entry
                    if high >= entry + (1.0 * r_dist) and sl < entry:
                        open_trade["stop_loss"] = entry
                        open_trade["breakeven_locked"] = True
                    # Trailing stop condition: past +1.5R gain, trail 1.0R behind high
                    if high >= entry + (1.5 * r_dist):
                        new_sl = round(high - (1.0 * r_dist), 2)
                        if new_sl > open_trade["stop_loss"]:
                            open_trade["stop_loss"] = new_sl

                    # Check exit
                    if low <= open_trade["stop_loss"]:
                        exit_price = open_trade["stop_loss"]
                        pnl = (exit_price - entry) * size
                        trades.append({
                            **open_trade,
                            "exit_time": str(ts),
                            "exit_price": exit_price,
                            "exit_reason": "Stop Loss / Trailing Stop",
                            "pnl": round(pnl, 2),
                            "win": pnl > 0,
                        })
                        open_trade = None
                    elif high >= tg:
                        exit_price = tg
                        pnl = (tg - entry) * size
                        trades.append({
                            **open_trade,
                            "exit_time": str(ts),
                            "exit_price": exit_price,
                            "exit_reason": "Target Hit (1:2 R:R)",
                            "pnl": round(pnl, 2),
                            "win": True,
                        })
                        open_trade = None

                else:  # SELL (Short)
                    # Breakeven condition: +1.0R gain locks stop at entry
                    if low <= entry - (1.0 * r_dist) and sl > entry:
                        open_trade["stop_loss"] = entry
                        open_trade["breakeven_locked"] = True
                    # Trailing stop condition: past +1.5R gain, trail 1.0R behind low
                    if low <= entry - (1.5 * r_dist):
                        new_sl = round(low + (1.0 * r_dist), 2)
                        if new_sl < open_trade["stop_loss"]:
                            open_trade["stop_loss"] = new_sl

                    if high >= open_trade["stop_loss"]:
                        exit_price = open_trade["stop_loss"]
                        pnl = (entry - exit_price) * size
                        trades.append({
                            **open_trade,
                            "exit_time": str(ts),
                            "exit_price": exit_price,
                            "exit_reason": "Stop Loss / Trailing Stop",
                            "pnl": round(pnl, 2),
                            "win": pnl > 0,
                        })
                        open_trade = None
                    elif low <= tg:
                        exit_price = tg
                        pnl = (entry - tg) * size
                        trades.append({
                            **open_trade,
                            "exit_time": str(ts),
                            "exit_price": exit_price,
                            "exit_reason": "Target Hit (1:2 R:R)",
                            "pnl": round(pnl, 2),
                            "win": True,
                        })
                        open_trade = None

            # 2. Check for New Entry Signal if no position open
            if open_trade is None:
                # 15-bar local consolidation range for ORB
                recent = df.iloc[max(0, i - 15):i]
                range_h = float(recent["High"].max())
                range_l = float(recent["Low"].min())

                trend_up = ema9 > ema21 and close > vwap and close >= sma200 * 0.995
                trend_down = ema9 < ema21 and close < vwap and close <= sma200 * 1.005
                rsi = float(curr.get("RSI", 50.0))
                is_breakout_long = close >= range_h and trend_up
                is_breakout_short = close <= range_l and trend_down

                # Score setup identically to ForexEngine
                score_long = 50
                if trend_up:
                    score_long += 15
                if close > sma200:
                    score_long += 10
                if is_breakout_long:
                    score_long += 20
                if rvol >= 1.2:
                    score_long += 10
                if 48 <= rsi <= 68 and trend_up:
                    score_long += 10

                score_short = 50
                if trend_down:
                    score_short += 15
                if close < sma200:
                    score_short += 10
                if is_breakout_short:
                    score_short += 20
                if rvol >= 1.2:
                    score_short += 10
                if 32 <= rsi <= 52 and trend_down:
                    score_short += 10

                # Buy Trigger (requires A/A+ conviction score >= 75, i.e. 8/10+)
                if is_breakout_long and score_long >= 75:
                    sl = round(max(vwap - 0.5 * atr, close - 1.5 * atr), 2)
                    risk_pts = max(close * 0.003, close - sl)
                    tg = round(close + 2.0 * risk_pts, 2)
                    size = max(0.01, round(risk_per_trade / max(0.1, risk_pts), 3)) if close > 1000 else max(0.1, round(risk_per_trade / max(0.01, risk_pts), 1))
                    open_trade = {
                        "symbol": symbol,
                        "side": "BUY",
                        "entry_time": str(ts),
                        "entry_price": close,
                        "stop_loss": sl,
                        "target": tg,
                        "size": size,
                        "risk_pts": risk_pts,
                        "conviction": min(10, int(score_long / 10)),
                    }

                # Sell Trigger (requires A/A+ conviction score >= 75, i.e. 8/10+)
                elif is_breakout_short and score_short >= 75:
                    sl = round(min(vwap + 0.5 * atr, close + 1.5 * atr), 2)
                    risk_pts = max(close * 0.003, sl - close)
                    tg = round(close - 2.0 * risk_pts, 2)
                    size = max(0.01, round(risk_per_trade / max(0.1, risk_pts), 3)) if close > 1000 else max(0.1, round(risk_per_trade / max(0.01, risk_pts), 1))
                    open_trade = {
                        "symbol": symbol,
                        "side": "SELL",
                        "entry_time": str(ts),
                        "entry_price": close,
                        "stop_loss": sl,
                        "target": tg,
                        "size": size,
                        "risk_pts": risk_pts,
                        "conviction": min(10, int(score_short / 10)),
                    }

        # Calculate performance statistics
        total_trades = len(trades)
        wins = [t for t in trades if t["win"]]
        losses = [t for t in trades if not t["win"]]
        win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0

        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        net_pnl = gross_profit - gross_loss
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 1.0)

        # Max drawdown
        cum_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cum_pnl += t["pnl"]
            if cum_pnl > peak:
                peak = cum_pnl
            dd = peak - cum_pnl
            if dd > max_dd:
                max_dd = dd

        return {
            "symbol": symbol,
            "candles_analyzed": len(df),
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_pnl": round(net_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown": round(max_dd, 2),
            "trades": trades,
        }

    def backtest_all(
        self,
        symbols: list[str] | None = None,
        resolution: str = "15",
        bars: int = 300,
    ) -> dict[str, Any]:
        """Run backtest across all symbols in the portfolio."""
        targets = symbols or ["BTCUSD", "ETHUSD", "SOLUSD", "XAUTUSD"]
        results = {}
        agg_trades = 0
        agg_wins = 0
        agg_gross_profit = 0.0
        agg_gross_loss = 0.0

        for sym in targets:
            res = self.backtest_symbol(sym, resolution=resolution, bars=bars)
            results[sym] = res
            agg_trades += res.get("total_trades", 0)
            agg_wins += res.get("wins", 0)
            agg_gross_profit += res.get("gross_profit", 0.0)
            agg_gross_loss += res.get("gross_loss", 0.0)

        agg_win_rate = (agg_wins / agg_trades * 100.0) if agg_trades > 0 else 0.0
        agg_net_pnl = agg_gross_profit - agg_gross_loss
        agg_pf = (agg_gross_profit / agg_gross_loss) if agg_gross_loss > 0 else 1.0

        return {
            "portfolio_summary": {
                "total_trades": agg_trades,
                "win_rate": round(agg_win_rate, 1),
                "gross_profit": round(agg_gross_profit, 2),
                "gross_loss": round(agg_gross_loss, 2),
                "net_pnl": round(agg_net_pnl, 2),
                "profit_factor": round(agg_pf, 2),
                "symbols_tested": targets,
                "timeframe": f"{resolution}m",
            },
            "symbols": results,
        }


def main():
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Backtest Forex & Crypto Multi-Strategy Engine")
    parser.add_argument("--symbol", type=str, default="ALL", help="Symbol or ALL")
    parser.add_argument("--resolution", type=str, default="15", help="Candle resolution (e.g. 5, 15)")
    parser.add_argument("--bars", type=int, default=300, help="Number of historical bars")
    args = parser.parse_args()

    bt = ForexBacktester()
    if args.symbol.upper() == "ALL":
        res = bt.backtest_all(resolution=args.resolution, bars=args.bars)
        summary = res["portfolio_summary"]
        print("\n" + "=" * 65)
        print("  [+] 24/7 FOREX & CRYPTO MULTI-STRATEGY BACKTEST RESULTS")
        print("  Delta Exchange · ORB + VWAP + 9/21 EMA + 200 SMA + Trailing SL")
        print("=" * 65)
        print(f"  Timeframe:       {summary['timeframe']} candles ({args.bars} bars)")
        print(f"  Total Trades:    {summary['total_trades']}")
        print(f"  Win Rate:        {summary['win_rate']}%")
        print(f"  Profit Factor:   {summary['profit_factor']}")
        print(f"  Gross Profit:    ${summary['gross_profit']:,.2f}")
        print(f"  Gross Loss:      ${summary['gross_loss']:,.2f}")
        print(f"  Net P&L:         ${summary['net_pnl']:,.2f}")
        print("=" * 65)
        for sym, d in res["symbols"].items():
            print(f"  {sym:10s} | Trades: {d.get('total_trades',0):2d} | Win Rate: {d.get('win_rate',0):4.1f}% | Net P&L: ${d.get('net_pnl',0):+7.2f} | PF: {d.get('profit_factor',0):.2f}")
        print("=" * 65 + "\n")
    else:
        res = bt.backtest_symbol(args.symbol, resolution=args.resolution, bars=args.bars)
        print(res)


if __name__ == "__main__":
    main()
