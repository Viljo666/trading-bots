"""ICT bot backtest harness with walk-forward.

Fetches HTF + LTF OHLCV from Binance USD-M perp (public, no auth), runs
find_setups(), then simulates exits: hit-stop / hit-target / time-stop, with
maker fees on entry (post-only-style) and taker on exit.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

import ccxt
import pandas as pd

from config import EXCHANGE, RISK, STRATEGY
from indicators import htf_bias_series, align_htf_to_ltf
from strategy import Trade, find_setups

log = logging.getLogger("ict_backtest")

MAKER_FEE_BPS = 2.0
TAKER_FEE_BPS = 5.0
SPREAD_BPS = 1.0


@dataclass
class FilledTrade:
    side: str
    entry_ts: pd.Timestamp
    entry: float
    exit_ts: pd.Timestamp
    exit: float
    qty: float
    pnl_usdt: float
    r_realized: float
    reason: str


def fetch_ohlcv(symbol: str, timeframe: str, since_ms: int, total_bars: int) -> pd.DataFrame:
    cls = getattr(ccxt, EXCHANGE.name)
    ex = cls({"enableRateLimit": True})
    ex.load_markets()
    rows: list[list] = []
    cursor = since_ms
    page = 1000
    while len(rows) < total_bars:
        chunk = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=page)
        if not chunk:
            break
        rows.extend(chunk)
        cursor = chunk[-1][0] + 1
        if len(chunk) < page:
            break
        time.sleep(ex.rateLimit / 1000.0)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")
    return df.iloc[:total_bars]


def simulate(ltf: pd.DataFrame, trades: list[Trade], equity_start: float) -> tuple[list[FilledTrade], float]:
    """For each setup, walk forward bar-by-bar; first of {target, stop, time-stop} wins.
    Sizing: risk per trade is RISK.risk_per_trade_pct of running equity.
    Fees: maker on entry, taker on exit.
    """
    high = ltf["high"].values
    low = ltf["low"].values
    open_ = ltf["open"].values

    filled: list[FilledTrade] = []
    equity = equity_start

    for t in trades:
        if t.entry_idx >= len(ltf) - 1:
            continue
        risk_per_unit = abs(t.entry - t.stop)
        if risk_per_unit <= 0:
            continue
        dollar_risk = equity * RISK.risk_per_trade_pct
        qty = round(dollar_risk / risk_per_unit, RISK.qty_decimals)
        if qty <= 0:
            continue

        entry_cost = t.entry * MAKER_FEE_BPS / 10_000.0
        entry_px = t.entry + entry_cost if t.side == "long" else t.entry - entry_cost

        exit_px = None
        exit_idx = None
        reason = ""
        end = min(len(ltf), t.entry_idx + STRATEGY.time_stop_bars)
        for k in range(t.entry_idx + 1, end):
            if t.side == "long":
                if low[k] <= t.stop:
                    exit_px = t.stop
                    exit_idx = k
                    reason = "stop"
                    break
                if high[k] >= t.target:
                    exit_px = t.target
                    exit_idx = k
                    reason = "target"
                    break
            else:
                if high[k] >= t.stop:
                    exit_px = t.stop
                    exit_idx = k
                    reason = "stop"
                    break
                if low[k] <= t.target:
                    exit_px = t.target
                    exit_idx = k
                    reason = "target"
                    break
        if exit_px is None:
            exit_idx = min(end - 1, len(ltf) - 1)
            exit_px = float(open_[exit_idx]) if exit_idx < len(open_) else float(ltf["close"].iloc[-1])
            reason = "time-stop"

        exit_cost = exit_px * (TAKER_FEE_BPS + SPREAD_BPS) / 10_000.0
        adj_exit = exit_px - exit_cost if t.side == "long" else exit_px + exit_cost
        pnl = (adj_exit - entry_px) * qty if t.side == "long" else (entry_px - adj_exit) * qty
        r_realized = pnl / dollar_risk if dollar_risk > 0 else 0.0
        equity += pnl
        filled.append(FilledTrade(
            side=t.side,
            entry_ts=ltf.index[t.entry_idx],
            entry=entry_px,
            exit_ts=ltf.index[exit_idx],
            exit=adj_exit,
            qty=qty,
            pnl_usdt=pnl,
            r_realized=r_realized,
            reason=reason,
        ))

    return filled, equity


def report(filled: list[FilledTrade], equity_start: float, equity_end: float, label: str = "") -> None:
    n = len(filled)
    if n == 0:
        print(f"{label}: no trades")
        return
    wins = [t for t in filled if t.pnl_usdt > 0]
    losses = [t for t in filled if t.pnl_usdt <= 0]
    pnl = sum(t.pnl_usdt for t in filled)
    win_rate = len(wins) / n
    avg_r = sum(t.r_realized for t in filled) / n
    by_reason: dict[str, int] = {}
    for t in filled:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
    ret = (equity_end / equity_start - 1) * 100 if equity_start > 0 else 0.0
    print(f"{label}: trades={n}  win%={win_rate*100:5.1f}  avgR={avg_r:+.2f}  pnl={pnl:+.2f}  return={ret:+.2f}%  exits={by_reason}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--symbol", default=EXCHANGE.symbol)
    p.add_argument("--walkforward", action="store_true",
                   help="Run 3 non-overlapping 30-day windows.")
    args = p.parse_args()

    if args.walkforward:
        windows = [(30, 0, "recent  [0-30d]"), (60, 30, "mid    [30-60d]"), (90, 60, "oldest [60-90d]")]
        total_days = 95
    else:
        windows = [(args.days, 0, f"last [{args.days}d]")]
        total_days = args.days

    ltf_bpd = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6}
    htf_bpd = {"1m": 1440, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}

    ltf_total = total_days * ltf_bpd[EXCHANGE.ltf]
    htf_total = total_days * htf_bpd[EXCHANGE.htf] + STRATEGY.htf_ema_period + 5
    since_ms = int(time.time() * 1000) - total_days * 86_400_000

    log.info("Fetch LTF %s %d bars ...", EXCHANGE.ltf, ltf_total)
    ltf = fetch_ohlcv(args.symbol, EXCHANGE.ltf, since_ms, ltf_total)
    log.info("Fetch HTF %s %d bars ...", EXCHANGE.htf, htf_total)
    htf_since = int(time.time() * 1000) - (total_days + 30) * 86_400_000  # extra so EMA warms up
    htf = fetch_ohlcv(args.symbol, EXCHANGE.htf, htf_since, htf_total)

    log.info("LTF: %d bars %s → %s", len(ltf), ltf.index[0], ltf.index[-1])
    log.info("HTF: %d bars %s → %s\n", len(htf), htf.index[0], htf.index[-1])

    htf_bias = htf_bias_series(htf, STRATEGY.htf_ema_period)

    print(f"Symbol={args.symbol} HTF={EXCHANGE.htf} LTF={EXCHANGE.ltf} "
          f"params: ema={STRATEGY.htf_ema_period} swing={STRATEGY.swing_left}/{STRATEGY.swing_right} "
          f"min_fvg={STRATEGY.min_fvg_pct*100:.2f}% wick>={STRATEGY.sweep_min_wick_atr}*ATR "
          f"stop_buf={STRATEGY.stop_buffer_atr}*ATR R>={STRATEGY.target_r_min}\n")

    for dstart, dend, label in windows:
        end_ts = ltf.index[-1] - pd.Timedelta(days=dend)
        start_ts = ltf.index[-1] - pd.Timedelta(days=dstart)
        sub = ltf.loc[(ltf.index >= start_ts) & (ltf.index < end_ts)].copy()
        if len(sub) < 100:
            print(f"{label}: insufficient bars")
            continue
        bias_aligned = align_htf_to_ltf(htf_bias, sub.index)
        setups = find_setups(sub, bias_aligned)
        filled, equity_end = simulate(sub, setups, RISK.account_size_usdt)
        report(filled, RISK.account_size_usdt, equity_end, label=label)


if __name__ == "__main__":
    main()
