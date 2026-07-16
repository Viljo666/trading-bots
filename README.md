# Trading Bots

Personal algo-trading stable. Live bots run on the home Mac under launchd against
IB Gateway (paper, port 4002) and Binance (paper); this repo auto-deploys to that
machine every 5 minutes (`commander/deploy_pull.py`).

## Bots

| dir | bot | status |
|-----|-----|--------|
| `poc_futures/` | Prior-day POC continuation, YM+ES 5m — best-validated edge (OOS PF 1.45) | launchd, dry |
| `sweep_futures/` | 4h liquidity-sweep reclaim, NQ+CL (swing-20, EMA-200, 1:3) | launchd, dry |
| `tori_futures/` | 15m trendline-break (shelved strategy, infra reused) | launchd |
| `tsmom/` | Carver daily time-series momentum | launchd, daily |
| `crypto_scalper/` | SOL/USDT 1m VWAP+RSI+ADX scalper (Binance paper) | launchd |
| `orb_futures/` | Opening-range breakout research — thin edge, parked | research only |
| `commander/` | Telegram command interface + auto-deploy puller | launchd |

Root-level `*.py` = the original Alpaca stock bot (ORB + VWAP reversal, regime-gated).

## Remote ops (from phone)

Telegram → the bot chat: `status`, `logs <bot>`, `pause|resume|restart <bot>`,
`dry <bot>`, `golive <bot> now`, `positions`, `flatten <bot> now`.

Code changes: edit via Claude Code (cloud session on this repo), push to `main`
→ the Mac pulls within 5 min and restarts the affected bots, then Telegrams a
deploy report.

## Rules of the road

- `.env` (API keys, Telegram creds) lives only on the Mac — never in the repo.
- Historical data caches (`*/data/*.csv`) are machine-local; regenerate via each
  project's fetcher (`orb_futures/ibkr_data.py` etc.).
- Every strategy change should re-run its backtest + walk-forward before deploy
  (`backtest.py` / `wf.py` in each strategy dir).
