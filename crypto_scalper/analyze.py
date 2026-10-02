"""Trade journal analyzer.

Reads journal/paper_*.jsonl, pairs open/close events into trades, prints:
  - Aggregate stats (count, win rate, expectancy, profit factor, max DD)
  - Equity curve summary (start/end/peak)
  - Day-of-week and hour-of-day buckets
  - Backtest-vs-live gap (vs the validated SOL walk-forward expectation)

Usage:
  python analyze.py            # all-time
  python analyze.py --days 7   # last 7 days only
  python analyze.py --current-trial  # only the post-fix paper trial
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import JOURNAL_DIR, PAPER_STATE_FILE, RISK


# Validated SOL walk-forward baseline (90d, 3 windows after ADX<30 filter).
# These are what the backtest predicted; we compare live to this.
BASELINE = {
    "win_rate": 0.55,            # ~55% blended across windows
    "expectancy_per_trade_pct": 0.0019,  # +0.19% / trade on equity (4.15% / 22 trades)
    "trades_per_30d": 7,         # 22 trades in 90d ≈ 7/month
    "monthly_return_pct": 1.4,
}


@dataclass
class Trade:
    open_ts: datetime
    close_ts: datetime
    side: str            # "long" / "short"
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    entry_fee: float
    exit_fee: float

    @property
    def duration_min(self) -> float:
        return (self.close_ts - self.open_ts).total_seconds() / 60.0

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def net_pnl(self) -> float:
        """Price P&L after both recorded exchange-simulation fees."""
        return self.pnl - self.entry_fee - self.exit_fee


def load_events(journal_dir: Path) -> list[dict]:
    events: list[dict] = []
    for f in sorted(journal_dir.glob("paper_*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for e in events:
        e["ts"] = datetime.fromisoformat(e["ts"])
    events.sort(key=lambda e: e["ts"])
    return events


def current_trial_start() -> datetime:
    try:
        value = json.loads(PAPER_STATE_FILE.read_text()).get("trial_started_at")
        if not value:
            raise ValueError("missing trial_started_at")
        return datetime.fromisoformat(value)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Current paper-trial start is unavailable: {e}") from e


def pair_trades(events: Iterable[dict]) -> tuple[list[Trade], dict | None]:
    """Pair each open with the next close. Returns (trades, open_position_or_None)."""
    trades: list[Trade] = []
    active: dict | None = None
    for e in events:
        if e.get("event") == "open":
            active = e
        elif e.get("event") == "close" and active is not None:
            trades.append(Trade(
                open_ts=active["ts"],
                close_ts=e["ts"],
                side=active["side"],
                qty=float(active["qty"]),
                entry_price=float(active["price"]),
                exit_price=float(e["price"]),
                pnl=float(e["pnl"]),
                entry_fee=float(active.get("fee", 0)),
                exit_fee=float(e.get("fee", 0)),
            ))
            active = None
    return trades, active


def stats_block(trades: list[Trade], starting_equity: float) -> str:
    if not trades:
        return "No completed trades yet."

    n = len(trades)
    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if not t.is_win]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)
    pnl = sum(t.net_pnl for t in trades)
    win_rate = len(wins) / n
    expectancy = pnl / n
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # Equity walk for peak/drawdown
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for t in trades:
        equity += t.net_pnl
        peak = max(peak, equity)
        dd = (equity - peak) / peak
        max_dd = min(max_dd, dd)
    end_equity = equity
    ret_pct = (end_equity / starting_equity - 1) * 100

    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    avg_dur = sum(t.duration_min for t in trades) / n
    longs = sum(1 for t in trades if t.side == "long")
    shorts = n - longs

    span_days = (trades[-1].close_ts - trades[0].open_ts).total_seconds() / 86400.0
    trades_per_30d = (n / span_days * 30) if span_days > 0 else 0

    lines = [
        f"Trades:          {n}  (longs {longs}, shorts {shorts})",
        f"Window:          {trades[0].open_ts:%Y-%m-%d %H:%M} → {trades[-1].close_ts:%Y-%m-%d %H:%M}  ({span_days:.1f}d)",
        f"Win rate:        {win_rate:.1%}",
        f"Expectancy:      ${expectancy:+.2f} / trade  ({expectancy/starting_equity*100:+.3f}% of equity)",
        f"Profit factor:   {profit_factor:.2f}" if profit_factor != float("inf") else "Profit factor:   ∞ (no losses yet)",
        f"Avg win/loss:    +${avg_win:.2f} / -${avg_loss:.2f}  (ratio {(avg_win/avg_loss if avg_loss>0 else 0):.2f})",
        f"Avg duration:    {avg_dur:.1f} min",
        f"Trade rate:      {trades_per_30d:.1f} / 30d",
        f"Equity:          ${starting_equity:,.2f} → ${end_equity:,.2f}  ({ret_pct:+.2f}%)",
        f"Max drawdown:    {max_dd*100:.2f}%",
    ]
    return "\n".join(lines)


def baseline_compare(trades: list[Trade], starting_equity: float) -> str:
    if len(trades) < 5:
        return f"(need ≥5 trades for backtest comparison; have {len(trades)})"
    n = len(trades)
    wins = sum(1 for t in trades if t.is_win)
    win_rate = wins / n
    pnl = sum(t.net_pnl for t in trades)
    exp_pct = (pnl / n) / starting_equity

    span_days = (trades[-1].close_ts - trades[0].open_ts).total_seconds() / 86400.0
    rate_per_30d = (n / span_days * 30) if span_days > 0 else 0

    def gap(actual: float, expected: float) -> str:
        if expected == 0:
            return ""
        diff = (actual - expected) / abs(expected) * 100
        sign = "+" if diff >= 0 else ""
        return f"({sign}{diff:.0f}% vs backtest)"

    lines = [
        f"Win rate:        live {win_rate:.1%}    backtest {BASELINE['win_rate']:.0%}    {gap(win_rate, BASELINE['win_rate'])}",
        f"Expectancy:      live {exp_pct*100:+.3f}%    backtest {BASELINE['expectancy_per_trade_pct']*100:+.3f}%    {gap(exp_pct, BASELINE['expectancy_per_trade_pct'])}",
        f"Trade rate:      live {rate_per_30d:.1f}/30d    backtest {BASELINE['trades_per_30d']}/30d    {gap(rate_per_30d, BASELINE['trades_per_30d'])}",
    ]
    flags = []
    if rate_per_30d < BASELINE["trades_per_30d"] * 0.5:
        flags.append("⚠ trade rate is <50% of backtest — fills probably not happening (limit orders timing out?)")
    if win_rate < BASELINE["win_rate"] * 0.7 and n >= 10:
        flags.append("⚠ win rate is <70% of backtest — strategy may be miscalibrated to current regime")
    if exp_pct < 0 and n >= 10:
        flags.append("⚠ live expectancy is negative — pause and review before going to real money")
    if flags:
        lines.append("")
        lines.extend(flags)
    return "\n".join(lines)


def time_buckets(trades: list[Trade]) -> str:
    if not trades:
        return ""
    by_dow: dict[str, list[float]] = defaultdict(list)
    by_hour: dict[int, list[float]] = defaultdict(list)
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for t in trades:
        by_dow[dow_names[t.open_ts.weekday()]].append(t.net_pnl)
        by_hour[t.open_ts.hour].append(t.net_pnl)

    out = ["By day of week:", f"  {'day':<5}{'n':>4}{'win%':>8}{'pnl':>10}"]
    for dow in dow_names:
        pnls = by_dow.get(dow, [])
        if not pnls:
            continue
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        out.append(f"  {dow:<5}{len(pnls):>4}{wr*100:>7.1f}%{sum(pnls):>10.2f}")

    out.append("")
    out.append("By hour (UTC):")
    out.append(f"  {'hr':<4}{'n':>4}{'win%':>8}{'pnl':>10}")
    for hour in sorted(by_hour):
        pnls = by_hour[hour]
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        out.append(f"  {hour:02d}  {len(pnls):>4}{wr*100:>7.1f}%{sum(pnls):>10.2f}")
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=None, help="Limit to last N days (default: all)")
    p.add_argument("--since", help="Limit to events at/after ISO UTC timestamp")
    p.add_argument("--current-trial", action="store_true", help="Use the post-fix paper-trial start")
    p.add_argument("--starting-equity", type=float, default=RISK.account_size_usdt)
    p.add_argument("--no-time", action="store_true", help="Skip day-of-week / hour buckets")
    args = p.parse_args()

    events = load_events(JOURNAL_DIR)
    if args.since and args.current_trial:
        p.error("use either --since or --current-trial, not both")
    if args.since:
        cutoff = datetime.fromisoformat(args.since)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        events = [e for e in events if e["ts"] >= cutoff]
    elif args.current_trial:
        cutoff = current_trial_start()
        events = [e for e in events if e["ts"] >= cutoff]
    if args.days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - args.days * 86400
        events = [e for e in events if e["ts"].timestamp() >= cutoff]

    trades, open_pos = pair_trades(events)

    print("=" * 64)
    print("CRYPTO SCALPER — JOURNAL ANALYSIS")
    print("=" * 64)
    print()
    print(stats_block(trades, args.starting_equity))
    print()
    print("─ vs backtest baseline ─")
    print(baseline_compare(trades, args.starting_equity))
    if open_pos:
        print()
        print(f"⏵ Open position: {open_pos['side']} {open_pos['qty']} @ ${float(open_pos['price']):,.4f} "
              f"(opened {open_pos['ts']:%Y-%m-%d %H:%M} UTC)")
    if not args.no_time and trades:
        print()
        print("─ time buckets ─")
        print(time_buckets(trades))
    print()


if __name__ == "__main__":
    main()
