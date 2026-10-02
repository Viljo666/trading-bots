# Trading Bots

Personal algo-trading stable. Live bots run on the home Mac under launchd against
IB Gateway (paper, port 4002) and Binance (paper); this repo auto-deploys to that
machine every 5 minutes (`commander/deploy_pull.py`).

## Bots

| dir | bot | status |
|-----|-----|--------|
| `poc_futures/` | Prior-day POC continuation, YM+ES 5m — best-validated edge (OOS PF 1.45) | launchd, dry |
| `sweep_futures/` | 4h liquidity-sweep reclaim, NQ+CL (swing-20, EMA-200, 1:3) | launchd, dry |
| `tori_futures/` | 15m trendline-break (shelved after weak validation) | not running |
| `tsmom/` | Carver daily time-series momentum | launchd, daily |
| `momentum_shadow.py` | Long-only U.S. stock relative-momentum swing model | launchd, shadow only |
| `crypto_scalper/` | SOL/USDT 1m VWAP+RSI+ADX scalper (Binance paper) | launchd |
| `orb_futures/` | Opening-range breakout research — thin edge, parked | research only |
| `commander/` | Telegram command interface + auto-deploy puller | launchd |

Root-level `*.py` = the original Alpaca stock bot (ORB + VWAP reversal, regime-gated).
`momentum_shadow.py` is isolated from the Alpaca account: it maintains a virtual
$100k ledger under `data/` and can never submit or close a broker position.

## Remote ops (from phone)

Telegram → the bot chat: `status`, `logs <bot>`, `pause|resume|restart <bot>`,
`dry <bot>`, `golive <bot> now`, `positions`, `flatten <bot> now`, `governor`,
`quality`, `gate`, `stress`, `audit`, `macro`.

Code changes: edit via Claude Code (cloud session on this repo), push to `main`
→ the Mac pulls within 5 min and restarts the affected bots, then Telegrams a
deploy report.

## Rules of the road

- `.env` (API keys, Telegram creds) lives only on the Mac — never in the repo.
- POC and Sweep check shared IBKR futures exposure before every setup, including
  existing TSMOM positions and working parent orders. A blocked setup is logged
  and sent to Telegram; it never submits a bracket.
- Crypto’s corrected paper trial is analysed separately with
  `python crypto_scalper/analyze.py --current-trial`.
- The Sunday weekly Telegram report includes the advisory-only Strategy Governor.
  It grades only collected paper/shadow evidence; it never changes orders, risk,
  position size, bot mode, or schedules automatically.
- POC and Sweep dry setups now receive a later price-bar shadow outcome. Those
  records are before commissions, slippage, and actual fills, so they are used
  for screening rather than as a claim of live performance.
- The shadow ledger also records entry delay, favourable/adverse excursion (MFE/
  MAE), holding time, and exit reason. The promotion gate requires 30 shadow
  outcomes before a human can even consider a live-paper trial.
- A monthly, advisory stress-test message bootstrap-resamples observed outcomes
  to show plausible drawdown risk. It cannot change a strategy or predict returns.
- A silent read-only IBKR execution collector records tagged future paper fills;
  this is how any later paper trial is reconciled with actual execution quality.
- The morning digest includes a calendar advisory for CPI, Employment Situation
  and FOMC events. It tags new setups for research only—there is no event-day
  trading filter until the stored evidence supports one.
- Gateway auto-recovery already runs throughout the day. Recovery notifications
  are rate-limited so travel does not produce repeated Telegram messages.
- Historical data caches (`*/data/*.csv`) are machine-local; regenerate via each
  project's fetcher (`orb_futures/ibkr_data.py` etc.).
- Every strategy change should re-run its backtest + walk-forward before deploy
  (`backtest.py` / `wf.py` in each strategy dir).
