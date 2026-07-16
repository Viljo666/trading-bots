# Bot Status — PARKED 2026-06-03

Decision: keep the bot running as-is on paper, revise later. No real money.

## What's running

- **One** `main.py` process, auto-started weekdays 08:00 local by launchd
  (`~/Library/LaunchAgents/com.villerajala.trading-bot.plist`).
- Paper account, ~$105.9k equity (started $100k on 2026-04-22).
- Telegram alerts active.

## Live config (config.py)

| Setting | Value | Note |
|---|---|---|
| enable_orb | True | ORB is the only active setup |
| enable_reversal | False | killed 2026-06-02 — bled in live |
| allow_swing_conversion | False | always flat at 15:55 |
| be_trigger_r | 1.0 | stop → break-even at +1R |
| gap_override_threshold_pct | 0.4 | early-session gap → force trend regime |
| trend_filter_enabled | False | tested, was a wash, not adopted |
| risk_per_trade | 1% · max 3 positions · 3% daily-loss halt | |

## Bugs fixed along the way (all live)

- EOD flatten race → use atomic `close_all_positions(cancel_orders=True)`
- Stop-too-close rejection (42210000) → clamp stop to quote in `submit_bracket`
- Gap-day "range" misread → gap override in `regime.detect`
- Scanner 0-candidate days → lookback 60 + ETF fallback
- Narrow-universe starving reversal → hybrid universe (ORB=top10, reversal=full)
- Duplicate-process collisions → PID lock (`bot.lock`) in main.py (2026-06-10)
- Idle weeks unnoticed → Friday weekly Telegram digest (2026-06-10)

## What we learned (the honest scorecard)

Tested across multiple windows. **Nothing reliably beats SPY.**

| Approach | Verdict |
|---|---|
| ORB-only (current live) | lags SPY (~-4% alpha) |
| ORB + reversal | bled in live, removed |
| ORB + swing carry | never triggers (= ORB-only) |
| Core 70/30 SPY+bot | structurally can't beat SPY |
| Momentum swing (daily) | ties SPY, ~20% lower drawdown |
| **Dual Momentum (GEM)** | **underperformed SPY even with 2022 bear included** |

Dual Momentum 5-yr (2021-05 → 2026-06, incl. 2022 bear):
  return 53.96% vs SPY 75.41% · MaxDD -20.3% vs -24.8% · Sharpe 0.70 vs 0.78.
Its defensive bond-switch DID cut drawdown, but it lagged on return AND Sharpe —
whipsawed by 2022's short bear + sharp recovery (worst case for trend-following).

**The core lesson:** over the last 5 years you could not beat SPY with anything we
tested — not the bot, not momentum swing, not even the century-backed Dual Momentum.
The available data window (no long grinding bear) is buy-and-hold's best case.

## Open paths for "later"

1. **Accept SPY buy-hold** if the goal is max return / min effort. Honest default.
2. Dual Momentum still has a real role: drawdown protection in a LONG bear
   (1970s/2008-style), which our data can't test. If you fear a multi-year
   grind, it's a defensible allocation — just not an alpha source.
3. Momentum swing — ties SPY with less drawdown; same caveat.
   Code for both lives in `strategy_lab.py` (`--dual-momentum`, momentum table).

## Tooling

- `backtest.py` — intraday walk-forward. Flags: `--be-trigger`, `--trend-filter`,
  `--orb-swing`, `--etfs-only`, `--months`.
- `strategy_lab.py` — fast daily-bar lab (momentum swing, core-satellite, SPY).
- Both are backtest-only; neither touches the live bot.
