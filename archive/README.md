# Archive

Retired bots and shelved research, moved here 2026-10-11. Nothing in this folder
runs under launchd. Code is kept as-is for reference; imports assume the old
locations, so move a folder back to the repo root before running it.

| item | why it was archived |
|------|---------------------|
| `alpaca_orb/` | Original Alpaca stock bot (ORB + VWAP reversal, `main.py` and the root modules only it used, plus its `STATUS.md`). No fills since 2026-06-01 and it didn't beat SPY. |
| `momentum_shadow/` | Long-only stock momentum shadow model. Failed its promotion bar, lagging SPY. Needs the `alpaca_orb/` modules (`config`, `scanner`, `strategy_lab`, ...) on the path to run. |
| `research/ict_bot/` | ICT-style crypto strategy backtest — shelved. |
| `research/trend_scalper/` | Crypto trend-scalper backtest — shelved. |
| `research/es_copilot/` | ES trading copilot experiment — shelved. |
| `research/tradingview/` | TradingView Pine scripts from POC/NQ research — shelved. |

Paused, not archived (code still in place, launchd plists moved to
`~/Library/LaunchAgents/paused/`): `sweep_futures/`, `crypto_scalper/`,
`stock_screener/`.

Shelved but left at the repo root because live code depends on them:
`tori_futures/` (TSMOM imports its `broker.py`) and `orb_futures/` (POC's
backtests read its data cache). `shadow_results.py` also stays at the root — it
is the POC/Sweep dry-shadow ledger, not part of Momentum Shadow.
