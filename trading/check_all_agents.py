import json
from datetime import datetime
from trading.delta_exchange import DeltaClient
from trading.ledger import Ledger
from trading.forex_trader import ForexAutoTrader
from trading.forex_reflection import get_recent_lessons
from trading.agents.llm import credentials_available, provider

def main():
    print("=" * 60)
    print("1. LLM & AGENT CORE HEALTH")
    print("=" * 60)
    has_creds = credentials_available()
    llm_prov = provider()
    print(f"LLM Provider    : {llm_prov.upper()}")
    print(f"API Credentials : {'DETECTED & ACTIVE' if has_creds else 'MISSING'}")

    print("\n" + "=" * 60)
    print("2. FOREX & CRYPTO 24/7 TRADING STATUS")
    print("=" * 60)
    client = DeltaClient()
    trader = ForexAutoTrader(client=client)
    eq = trader.get_account_equity()
    print(f"Starting Capital : ${eq['starting_capital']:.2f}")
    print(f"Current Equity   : ${eq['current_equity']:.2f}")
    print(f"Total Realized   : ${eq['total_realized_pnl']:+.2f}")
    print(f"Unrealized P&L   : ${eq['unrealized_pnl']:+.2f}")
    print(f"Dynamic Risk/Trd : ${eq['dynamic_risk_usd']:.2f} ({eq['risk_pct_per_trade']}%)")
    print(f"Max Daily Loss   : ${eq['max_daily_loss_usd']:.2f}")
    print(f"Milestone Target : ${eq['target_milestone']:.2f} (Progress: {eq['progress_pct']}%)")

    all_pos = client.get_all_positions()
    open_pos = [p for p in all_pos if p.get("status") == "OPEN"]
    closed_pos = [p for p in all_pos if p.get("status") == "CLOSED"]
    
    print(f"\nTotal Forex Trades Executed: {len(all_pos)}")
    print(f"Currently OPEN Positions   : {len(open_pos)}")
    print(f"CLOSED Trades              : {len(closed_pos)}")

    if open_pos:
        print("\n--- ACTIVE OPEN POSITIONS ---")
        for p in open_pos:
            print(f"  #{p['id']} {p['symbol']} {p['side']} {p['size']} @ ${p['entry_price']:.4f} | Current: ${p.get('current_price', 0):.4f} | SL: ${p['stop_loss']:.4f} | Tgt: ${p['target']:.4f} | PnL: ${p.get('pnl', 0):+.2f}")
    else:
        print("  (No positions currently open - capital safely resting)")

    wins = [p for p in closed_pos if float(p.get("pnl", 0)) > 0]
    losses = [p for p in closed_pos if float(p.get("pnl", 0)) < 0]
    evens = [p for p in closed_pos if float(p.get("pnl", 0)) == 0]
    win_rate = (len(wins) / len(closed_pos) * 100) if closed_pos else 0
    total_pnl = sum(float(p.get("pnl", 0)) for p in closed_pos)
    print(f"\nWin Rate: {win_rate:.1f}% ({len(wins)} Wins, {len(losses)} Losses, {len(evens)} Breakeven)")
    print(f"Cumulative P&L: ${total_pnl:+.2f}")

    print("\nRecent 5 Closed Forex Trades:")
    for p in closed_pos[-5:]:
        print(f"  #{p['id']} {p['symbol']} {p['side']} | Entry: ${p['entry_price']} -> Exit: ${p.get('exit_price')} | Reason: {p.get('exit_reason')} | PnL: ${float(p.get('pnl',0)):+.2f} ({p.get('pnl_pct',0):+.2f}%) | TrailNote: {p.get('trailing_note', 'None')}")

    print("\n" + "=" * 60)
    print("3. AUTONOMOUS CONTINUOUS LEARNING / AUTO-REFLECTOR")
    print("=" * 60)
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    lessons = get_recent_lessons(limit=5)
    print(f"Active Lessons in Memory ({len(lessons)} total):")
    for i, l in enumerate(lessons, 1):
        clean_l = l.encode("ascii", "ignore").decode("ascii")
        print(f"  {i}. {clean_l}")

    print("\n" + "=" * 60)
    print("4. AGENT AUDIT LOGS (SQLITE LEDGER)")
    print("=" * 60)
    ledger = Ledger()
    with ledger._conn() as conn:
        row = conn.execute("SELECT COUNT(*), SUM(ok) FROM agent_log").fetchone()
        total_calls = row[0] or 0
        ok_calls = row[1] or 0
        recent_calls = conn.execute(
            "SELECT agent, model, ok, error, datetime(ts, 'unixepoch', 'localtime') FROM agent_log ORDER BY id DESC LIMIT 5"
        ).fetchall()

    print(f"Total AI Agent LLM Invocations: {total_calls}")
    print(f"Successful Structured Calls    : {ok_calls} ({(ok_calls/total_calls*100) if total_calls else 0:.1f}%)")
    print("\nRecent AI Agent Decisions in Ledger:")
    for c in recent_calls:
        print(f"  [{c[4]}] Agent: {c[0]} | Model: {c[1]} | Status: {'OK' if c[2] else 'FAILED'} | Err: {c[3] or 'None'}")

    print("\n" + "=" * 60)
    print("5. INDIAN EQUITIES / F&O LEDGER & AGENTS")
    print("=" * 60)
    with ledger._conn() as conn:
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 10").fetchall()
        nse_trades = [dict(r) for r in rows]
    print(f"Total Recent NSE Trades in Ledger: {len(nse_trades)}")
    for t in nse_trades[:5]:
        print(f"  #{t.get('id')} {t.get('symbol')} {t.get('side')} {t.get('qty')} @ {t.get('entry_price')} | Status: {t.get('status')} | PnL: {t.get('pnl')} | Strategy: {t.get('strategy_id')}")

if __name__ == "__main__":
    main()
