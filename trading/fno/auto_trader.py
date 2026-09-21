"""Automated paper-trading daemon for the F&O Scanner.

Directly bridges the live Scanner API (/api/scan) into the ExecutionRouter.
Ensures 100% synchronization between what is shown in the Scanner UI
and what is executed in the Paper Trading Ledger.
"""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

from trading.config import RISK_PER_TRADE, TODAY_CONFIG_PATH, TRADING_MODE
from trading.execution_router import ExecutionRouter

IST = ZoneInfo("Asia/Kolkata")
SCAN_API_URL = "http://127.0.0.1:8787/api/scan"


def get_ltp_stub(sym: str) -> float:
    return 1000.0


def run_auto_trader():
    print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] F&O Auto-Trader Started in {TRADING_MODE.upper()} mode.")
    print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Communicating directly with Scanner API: {SCAN_API_URL}")
    router = ExecutionRouter(mode=TRADING_MODE, get_ltp=get_ltp_stub)

    last_heartbeat = 0
    executed_symbols = set()

    while True:
        try:
            now = datetime.now(IST)
            hhmm = now.strftime("%H:%M")
            is_weekday = now.weekday() < 5

            # Market scanning hours: 09:15 to 15:25 IST
            if is_weekday and "09:15" <= hhmm <= "15:25":
                try:
                    req = urllib.request.Request(
                        SCAN_API_URL,
                        headers={"User-Agent": "AutoTraderDaemon/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                except Exception as net_err:
                    if time.time() - last_heartbeat > 30:
                        print(f"[{now.strftime('%H:%M:%S')}] Waiting for Scanner web service... ({net_err})")
                        last_heartbeat = time.time()
                    time.sleep(5)
                    continue

                scan = data.get("scan") or {}
                candidates = scan.get("candidates") or []
                window = scan.get("window", "")

                # Calculate position risk size using dynamic challenge risk
                from trading.config import get_dynamic_risk_per_trade
                base_risk = get_dynamic_risk_per_trade()
                risk_amount = base_risk * 0.5
                if "risk_multiplier" in router.day_config:
                    risk_amount = base_risk * router.day_config["risk_multiplier"]

                # Check all candidates from the scanner
                buyable_candidates = []
                for cand in candidates:
                    if not isinstance(cand, dict):
                        continue
                    sym = cand.get("symbol")
                    verdict = cand.get("verdict", "")
                    tradeable = cand.get("tradeable", False)
                    trade = cand.get("trade") or {}

                    # If tradeable / BUY and not already hit targets (is_actionable)
                    if tradeable or verdict == "BUY":
                        if trade.get("is_actionable", True):
                            buyable_candidates.append(cand)

                # Periodic heartbeat log every 20 seconds so user sees communication
                if time.time() - last_heartbeat > 20:
                    t2_done = sum(1 for c in candidates if isinstance(c, dict) and (c.get("trade") or {}).get("target2_hit"))
                    print(
                        f"[{now.strftime('%H:%M:%S')}] Scanner Sync [{window}]: "
                        f"{len(candidates)} candidates tracked | "
                        f"{len(buyable_candidates)} actionable BUY setups | "
                        f"{t2_done} already hit Target 2"
                    )
                    last_heartbeat = time.time()

                # Execute actionable setups
                open_symbols = {t["symbol"] for t in router.ledger.open_trades()}
                for cand in buyable_candidates:
                    sym = cand.get("symbol")
                    if not sym or sym in open_symbols:
                        continue

                    trade = cand.get("trade") or {}
                    entry_price = float(trade.get("entry", cand.get("price", 0)))
                    stop_loss = float(trade.get("stop", entry_price * 0.99))
                    risk = float(trade.get("risk", abs(entry_price - stop_loss)))

                    qty = 1
                    if risk > 0:
                        qty = max(1, int(risk_amount // risk))

                    signal = {
                        "strategy_id": "fno_auto_scanner",
                        "symbol": sym,
                        "side": "BUY",
                        "price": entry_price,
                        "qty": qty,
                        "target1": trade.get("target1"),
                        "target2": trade.get("target2"),
                        "stop": stop_loss,
                        "ts": time.time(),
                        "regime": router.day_config.get("regime", "trending"),
                    }

                    trade_id = router.execute(signal)
                    if trade_id:
                        open_symbols.add(sym)
                        print(f"[{now.strftime('%H:%M:%S')}] [EXECUTED] SCANNER TRADE: BUY {qty} {sym} @ {entry_price:.2f} -> Trade #{trade_id}")

                # Check Index Option setup from scanner
                idx = scan.get("index_options") or {}
                opt = idx.get("option") or {}
                if isinstance(opt, dict) and opt.get("tradeable", False):
                    q = opt.get("quote") or {}
                    last_price = float(q.get("last_price", 0))
                    prem_stop = float(opt.get("premium_at_stop", 0) or 0)
                    risk_pts = last_price - prem_stop if prem_stop > 0 else last_price

                    opt_qty = max(1, int(risk_amount // risk_pts)) if risk_pts > 0 else 1
                    strike = q.get("strike", 0)
                    opttype = str(q.get("option_type", "CE"))[:2].upper()
                    idx_symbol = f"NIFTY {strike:g} {opttype}"

                    if idx_symbol not in open_symbols:
                        idx_signal = {
                            "strategy_id": "fno_auto_scanner",
                            "symbol": idx_symbol,
                            "side": "BUY",
                            "price": last_price,
                            "qty": opt_qty,
                            "target1": opt.get("premium_at_t1"),
                            "target2": opt.get("premium_at_t2"),
                            "stop": prem_stop,
                            "timestamp": now.isoformat(),
                        }

                        idx_trade_id = router.execute(idx_signal)
                        if idx_trade_id:
                            open_symbols.add(idx_symbol)
                            print(f"[{now.strftime('%H:%M:%S')}] [EXECUTED] INDEX OPTION: BUY {opt_qty} {idx_signal['symbol']} @ {last_price:.2f} -> Trade #{idx_trade_id}")

            # Sleep 5 seconds between scans
            time.sleep(5)

        except KeyboardInterrupt:
            print("Auto-Trader Stopped.")
            break
        except Exception as e:
            import traceback
            print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Auto-Trader Loop Error: {e}")
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    run_auto_trader()
