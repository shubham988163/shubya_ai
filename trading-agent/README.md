# Trading Agent — AI-supervised paper trading system

Implementation of the AI-agent layer from the algo-trading guide:

```
[Market Data] → [Strategy Engine] → [AI Agent Layer] → [Execution Router] → [SQLite Ledger]
                                      (advisory only)     (hard risk kernel)
```

**Design rule enforced throughout: the AI agent advises and supervises;
deterministic code decides and executes.** Every agent output is schema-validated,
clamped to safe ranges, logged to the audit trail, and replaced with safe
defaults on any failure. Hard risk limits live in `ExecutionRouter` and cannot
be overridden by any agent output.

## Components

| File | Role |
|---|---|
| `trading/ledger.py` | SQLite ledger: trades, rejections, full LLM audit log |
| `trading/execution_router.py` | Paper/live switch, hard risk limits, SEBI sub-10-OPS rate limiter |
| `trading/agents/llm.py` | Anthropic API wrapper: structured output via Pydantic, fallbacks, audit logging |
| `trading/agents/premarket.py` | **Role 1** — 8:45 AM: regime + risk multiplier + blocked symbols → `today_config.json` |
| `trading/agents/supervisor.py` | **Role 2** — async per-trade sanity check, verdict logged, never blocks a trade |
| `trading/agents/eod_journal.py` | **Role 3** — 4:00 PM: pattern analysis of the day's ledger → `reports/YYYY-MM-DD.md` |
| `trading/demo.py` | End-to-end smoke test with fake prices |
| `trading/dashboard.py` | Local web dashboard (`python -m trading.dashboard` → http://localhost:8787) — P&L chart, trades + verdicts, rejections, LLM audit log, journal |

## Setup

```bash
cd ~/trading-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# LLM provider — either works; Gemini is auto-selected when its key is set
export GEMINI_API_KEY=...             # Google AI Studio key (free tier: gemini-2.5-flash)
# export ANTHROPIC_API_KEY=sk-ant-... # or Claude; force with LLM_PROVIDER=anthropic|gemini

python -m trading.demo                # verify the pipeline
```

Models are set in `trading/config.py` (`GEMINI_MODEL` / `ANTHROPIC_MODEL`).
Note: `gemini-2.5-pro` requires billing enabled on the Google project — the
free tier only serves the flash models.

## Daily schedule (cron, IST)

```cron
# Pre-market analyst — writes data/today_config.json
45 8 * * 1-5  cd $HOME/trading-agent && .venv/bin/python -m trading.agents.premarket >> logs/premarket.log 2>&1

# EOD journal — writes reports/YYYY-MM-DD.md
0 16 * * 1-5  cd $HOME/trading-agent && .venv/bin/python -m trading.agents.eod_journal >> logs/eod.log 2>&1
```

Run the supervisor as a long-lived process during market hours (systemd/launchd,
or just a terminal while paper trading):

```bash
.venv/bin/python -m trading.agents.supervisor --loop
```

## Wiring in your strategy

Your strategy engine emits signals and hands them to the router:

```python
from trading.execution_router import ExecutionRouter
from trading.ledger import Ledger

router = ExecutionRouter(mode="paper", ledger=Ledger(), get_ltp=my_tick_source)
router.execute({
    "symbol": "RELIANCE", "side": "BUY", "qty": 10, "price": ltp,
    "ts": time.time(), "stop_loss": sl, "target": tgt,
    "strategy_id": "ema_x_v3", "regime": router.day_config["regime"],
})
```

The router reads `data/today_config.json` (written by the pre-market agent) at
startup: `risk_multiplier` shrinks the position cap (never grows it) and
`blocked_symbols` are rejected outright.

On exit, call `ledger.record_exit(trade_id, exit_price, charges, mae, mfe)` —
check exits against **live ticks**, not candle closes, and log MAE/MFE so the
journal agent can use them.

## Path A — TradingView Pine Script automation

`tradingview/ema_9_21.pine` is the same EMA strategy for TradingView; its
order-fill alerts POST into `trading/tv_webhook.py`, which routes through the
same risk kernel and ledger (tagged `strategy_id=tv_ema_9_21`), so TV trades
appear on the dashboard and get supervised/journaled like everything else.

One command starts the receiver + a free Cloudflare tunnel and prints the
webhook URL and secret to paste into TradingView:

```bash
./scripts/tv_bridge.sh
```

(The secret persists in `data/tv_secret`; the trycloudflare URL changes on
every restart — update the TradingView alert's webhook URL when you relaunch.
For a stable URL, use a named Cloudflare tunnel or ngrok with an account.)

TradingView side (paid plan required for webhooks; note TV's own "Paper
Trading" broker is manual-only — Pine strategies can't place orders into it,
which is why this webhook bridge exists):
1. Open a 5-min NSE chart → add `tradingview/ema_9_21.pine` → set the
   "Webhook secret" input to the printed secret.
2. Create ONE alert: condition = the strategy, "Order fills only",
   message `{{strategy.order.alert_message}}`,
   webhook URL = the printed `https://....trycloudflare.com/webhook`.
3. The Strategy Tester panel on the chart is TradingView's simulation record;
   your ledger/dashboard is the authoritative paper record (with charges,
   slippage, and agent verdicts).

Payloads with a wrong/missing secret are rejected (403) and logged to the
rejections table. Alerts fire on bar close, so fidelity ≈ the Python engine.

## Evaluating the supervisor (after 4–6 weeks of paper trading)

The supervisor's verdicts are advisory. Before ever promoting it to a blocking
veto, check whether its vetoes actually predicted losers:

```sql
SELECT agent_verdict, COUNT(*) n, ROUND(AVG(pnl),2) avg_pnl, ROUND(SUM(pnl),2) total_pnl
FROM trades WHERE status='closed' GROUP BY agent_verdict;
```

If `veto` trades are meaningfully worse than `approve` trades, promote it — with
a hard timeout (e.g. 500 ms → default allow) so the LLM can never stall the
trade path.

## Go-live checklist (do NOT flip `mode="live"` before all of these)

1. Backtest validates the edge; **4–6+ weeks of paper trading** across trending
   and choppy conditions validates execution and the slippage model.
2. Compare simulated fills vs. real spreads; tighten `SLIPPAGE_PCT` if paper
   results look too good.
3. Static IP registered with Zerodha (SEBI mandate for API order placement;
   one IP change per calendar week).
4. Kite Connect subscription (₹500/mo) + automated daily OAuth token refresh.
5. Confirm with Zerodha how your sub-10-OPS personal strategy gets Algo-ID
   tagging (the rate limiter in the router keeps you under the threshold).
6. Keep all agents advisory-only for at least the first month live.
7. Start with minimum quantity for 2 weeks regardless of paper results.
8. Verify current STT/charges rates and update `CHARGES_PCT_ROUND_TRIP`.

## Audit trail

Every LLM call (prompt + response + errors) is stored in the `agent_log` table:

```bash
sqlite3 data/ledger.db "SELECT datetime(ts,'unixepoch','localtime'), agent, ok, COALESCE(error,'') FROM agent_log ORDER BY ts DESC LIMIT 20;"
```
