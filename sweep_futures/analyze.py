"""Prompt-pack #2/#9/#10 — done with real numbers instead of LLM essays.

Reads a trades CSV from backtest.py and computes:
  - Backtest metrics (#2): CAGR, Sharpe, max DD, win rate, profit factor
  - Drawdown analysis (#10): max DD, longest underwater stretch
  - Monte Carlo (#9): bootstrap the trade sequence -> distribution of outcomes,
    probability of a losing year, worst-case drawdown

Position sizing: fixed-fractional, RISK_PCT of equity risked per trade (each
trade's stop = 1R), compounding. Trades ordered by exit date.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RISK_PCT = 0.01          # 1% of equity risked per trade
START_EQUITY = 10_000.0  # matches the IG post's $10k constraint
N_SIMS = 10_000


def equity_curve(r_multiples: np.ndarray, risk_pct: float = RISK_PCT) -> np.ndarray:
    eq = START_EQUITY * np.cumprod(1.0 + risk_pct * r_multiples)
    return np.concatenate([[START_EQUITY], eq])


def max_drawdown(curve: np.ndarray) -> float:
    peak = np.maximum.accumulate(curve)
    return float((curve / peak - 1.0).min())


def main() -> None:
    df = pd.read_csv("final.csv", parse_dates=["entry_ts", "exit_ts"]).sort_values("exit_ts")
    R = df.r_multiple.to_numpy()
    n = len(R)
    years = (df.exit_ts.max() - df.exit_ts.min()).days / 365.25

    # --- #2 core metrics ---
    curve = equity_curve(R)
    cagr = (curve[-1] / curve[0]) ** (1 / years) - 1
    win = (R > 0).mean()
    pf = R[R > 0].sum() / -R[R < 0].sum()
    # monthly Sharpe from the realised equity curve
    eq = pd.Series(curve[1:], index=df.exit_ts.values)
    monthly = eq.resample("ME").last().pct_change().dropna()
    sharpe = monthly.mean() / monthly.std() * np.sqrt(12) if monthly.std() else float("nan")
    mdd = max_drawdown(curve)

    print("=== #2 BACKTEST METRICS (real, sweep-20 + EMA200, 1:3, ES/NQ/YM/CL) ===")
    print(f"  period            {df.exit_ts.min().date()} -> {df.exit_ts.max().date()} ({years:.1f}y)")
    print(f"  trades            {n}")
    print(f"  win rate          {win*100:.1f}%")
    print(f"  profit factor     {pf:.2f}")
    print(f"  total R           {R.sum():+.1f}")
    print(f"  CAGR (1% risk)    {cagr*100:+.1f}%")
    print(f"  Sharpe (monthly)  {sharpe:.2f}")
    print(f"  final equity      ${curve[-1]:,.0f}  (from ${START_EQUITY:,.0f})")

    # --- #10 drawdown ---
    peak = np.maximum.accumulate(curve)
    underwater = curve < peak
    # longest run of consecutive underwater trades
    longest = cur = 0
    for u in underwater:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    print("\n=== #10 DRAWDOWN ANALYSIS ===")
    print(f"  max drawdown      {mdd*100:.1f}%")
    print(f"  longest underwater {longest} trades (~{longest*years/n*12:.1f} months)")

    # --- #9 Monte Carlo: bootstrap trade sequence ---
    rng = np.random.default_rng(42)
    finals, mdds = np.empty(N_SIMS), np.empty(N_SIMS)
    for i in range(N_SIMS):
        sample = rng.choice(R, size=n, replace=True)
        c = equity_curve(sample)
        finals[i] = c[-1]
        mdds[i] = max_drawdown(c)
    ret = finals / START_EQUITY - 1
    print(f"\n=== #9 MONTE CARLO ({N_SIMS:,} bootstrap resamples) ===")
    print(f"  P(net loss over the period)   {(finals < START_EQUITY).mean()*100:.1f}%")
    print(f"  median total return           {np.median(ret)*100:+.1f}%")
    print(f"  5th–95th pctile return        {np.percentile(ret,5)*100:+.0f}%  ...  {np.percentile(ret,95)*100:+.0f}%")
    print(f"  median max drawdown           {np.median(mdds)*100:.1f}%")
    print(f"  95th-pctile (bad) drawdown    {np.percentile(mdds,5)*100:.1f}%")
    print(f"  worst drawdown seen           {mdds.min()*100:.1f}%")
    verdict = "FRAGILE" if np.percentile(mdds, 5) < -0.35 or (finals < START_EQUITY).mean() > 0.25 else "ROBUST-ish"
    print(f"  verdict                       {verdict}")


if __name__ == "__main__":
    main()
