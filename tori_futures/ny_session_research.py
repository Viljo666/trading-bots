"""Research-only test of the archived Tori trendline rules during New York RTH.

This is deliberately separate from the archived live runner.  It uses completed
15-minute ES bars, builds the two-touch trendline, waits for a close-through and
pullback, trails with the safety line, and force-closes at the end of each US
cash session.  It neither connects to a broker nor places orders.

The test applies one tick of adverse slippage on *each* fill and a configurable
round-turn commission.  It reports a chronological development period and an
untouched later period, without optimising parameters on either one.

Usage (after creating orb_futures/data/ES_15m.csv):
    ../.venv/bin/python ny_session_research.py
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from strategy import ES_DOLLARS_PER_POINT, Strategy, StrategyConfig, Trade


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "orb_futures" / "data" / "ES_15m.csv"
ET = "America/New_York"
TICK_SIZE = 0.25
ROUND_TURN_COMMISSION_USD = 4.62
DEVELOPMENT_FRACTION = 0.60


@dataclass(frozen=True)
class NetTrade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: str
    reason: str
    gross_points: float
    net_points: float


def load_bars(path: Path = DEFAULT_DATA) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Fetch the historical 15-minute ES cache first."
        )
    df = pd.read_csv(path, index_col="timestamp", parse_dates=["timestamp"])
    # The history spans both EST and EDT, so parsing the offset-bearing CSV
    # directly produces a mixed-timezone index.  Normalise through UTC first.
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
    df = df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
    return df[~df.index.duplicated(keep="last")]


def ny_cash_sessions(bars: pd.DataFrame) -> Iterable[pd.DataFrame]:
    """Yield complete RTH sessions; signals, entries, and exits stay in NY hours."""
    local = bars.tz_convert(ET)
    rth = local[(local.index.weekday < 5)].between_time("09:30", "15:45")
    for _, session in rth.groupby(rth.index.date):
        # 09:30 through 15:45 is 26 fifteen-minute bars on a normal RTH day.
        # Skip shortened/partial sessions rather than pretending their exits are normal.
        if len(session) >= 24:
            yield session


def net_trade(trade: Trade) -> NetTrade:
    """Apply conservative adverse execution: one tick at entry and exit."""
    assert trade.exit_price is not None and trade.exit_time is not None
    if trade.direction == "long":
        entry = trade.entry_price + TICK_SIZE
        exit_ = trade.exit_price - TICK_SIZE
        gross = exit_ - entry
    else:
        entry = trade.entry_price - TICK_SIZE
        exit_ = trade.exit_price + TICK_SIZE
        gross = entry - exit_
    net = gross - ROUND_TURN_COMMISSION_USD / ES_DOLLARS_PER_POINT
    return NetTrade(
        entry_time=trade.entry_time,
        exit_time=trade.exit_time,
        direction=trade.direction,
        reason=trade.exit_reason or "unknown",
        gross_points=gross,
        net_points=net,
    )


def run_rth_only(bars: pd.DataFrame) -> list[NetTrade]:
    """Run each NY session independently so no setup or position crosses overnight."""
    config = StrategyConfig(
        rth_only=False,  # bars are already restricted to RTH
        dollars_per_point=ES_DOLLARS_PER_POINT,
    )
    trades: list[NetTrade] = []
    for session in ny_cash_sessions(bars):
        trades.extend(net_trade(t) for t in Strategy(session, config).run())
    return sorted(trades, key=lambda t: t.entry_time)


def summarize(name: str, trades: list[NetTrade]) -> dict[str, object]:
    if not trades:
        return {"period": name, "trades": 0}
    results = pd.Series([t.net_points for t in trades], dtype=float)
    equity = results.cumsum()
    drawdown = equity - equity.cummax()
    wins = results[results > 0]
    losses = results[results <= 0]
    gross_loss = abs(float(losses.sum()))
    return {
        "period": name,
        "trades": len(results),
        "win_rate": round(float((results > 0).mean() * 100), 1),
        "net_points": round(float(results.sum()), 2),
        "net_usd": round(float(results.sum() * ES_DOLLARS_PER_POINT), 2),
        "avg_points": round(float(results.mean()), 3),
        "profit_factor": round(float(wins.sum()) / gross_loss, 2) if gross_loss else float("inf"),
        "max_drawdown_points": round(float(drawdown.min()), 2),
        "max_drawdown_usd": round(float(drawdown.min() * ES_DOLLARS_PER_POINT), 2),
    }


def split_by_entry_date(trades: list[NetTrade]) -> tuple[list[NetTrade], list[NetTrade]]:
    dates = sorted({t.entry_time.date() for t in trades})
    if len(dates) < 2:
        return trades, []
    cut = dates[max(1, int(len(dates) * DEVELOPMENT_FRACTION)) - 1]
    return (
        [t for t in trades if t.entry_time.date() <= cut],
        [t for t in trades if t.entry_time.date() > cut],
    )


def verdict(development: dict[str, object], holdout: dict[str, object]) -> str:
    required = (
        int(holdout.get("trades", 0)) >= 30
        and float(development.get("profit_factor", 0)) >= 1.20
        and float(holdout.get("profit_factor", 0)) >= 1.15
        and float(holdout.get("avg_points", 0)) > 0
    )
    if required:
        return "PROMISING FOR A LIMITED DRY-RUN ONLY — forward testing is still required."
    return "DO NOT PROMOTE — the New York-only variation did not clear the research gate."


def main() -> None:
    bars = load_bars()
    print("=== Tori trendline: New York-session research only ===")
    print(f"Data: {bars.index[0]} to {bars.index[-1]} ({len(bars):,} 15-minute bars)")
    print("Rules: RTH-only trendlines, no overnight carry, 1 tick adverse slippage per fill, $4.62 round turn.")
    print("Parameters are fixed; no optimisation is performed in this study.\n")

    trades = run_rth_only(bars)
    development_trades, holdout_trades = split_by_entry_date(trades)
    development = summarize("development (first 60%)", development_trades)
    holdout = summarize("holdout (last 40%)", holdout_trades)
    report = pd.DataFrame([summarize("all data", trades), development, holdout])
    print(report.to_string(index=False))
    print(f"\nVerdict: {verdict(development, holdout)}")


if __name__ == "__main__":
    main()
