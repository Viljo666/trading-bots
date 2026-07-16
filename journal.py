"""End-of-day report: trades, P&L, benchmark comparison, written to journal/YYYY-MM-DD.md."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from broker import Broker
from config import JOURNAL_DIR, SESSION

log = logging.getLogger(__name__)


def _today_et() -> datetime:
    return datetime.now(pytz.timezone(SESSION.timezone))


def write_daily_report(broker: Broker, starting_equity: float) -> Path:
    """Write a markdown report of today's activity and benchmark vs SPY."""
    today = _today_et()
    report_path = JOURNAL_DIR / f"{today:%Y-%m-%d}.md"

    try:
        account = broker.get_account()
        ending_equity = float(account.equity)
    except Exception as e:
        log.error("Could not fetch account: %s", e)
        ending_equity = starting_equity

    day_pl = ending_equity - starting_equity
    day_pl_pct = (day_pl / starting_equity) * 100.0 if starting_equity else 0.0

    # Benchmark: SPY daily return
    bench_pct = _benchmark_return(broker)
    alpha = day_pl_pct - bench_pct if bench_pct is not None else None

    trades = _fetch_todays_trades(broker)
    positions = broker.positions()

    lines = [
        f"# Trading Journal — {today:%Y-%m-%d}",
        "",
        "## Account",
        f"- Starting equity: ${starting_equity:,.2f}",
        f"- Ending equity:   ${ending_equity:,.2f}",
        f"- Day P&L:         ${day_pl:,.2f} ({day_pl_pct:+.2f}%)",
    ]
    if bench_pct is not None:
        lines.append(f"- SPY return:      {bench_pct:+.2f}%")
        lines.append(f"- Alpha vs SPY:    {alpha:+.2f}%")
    lines.append("")

    lines.append("## Trades")
    if not trades:
        lines.append("_No fills today._")
    else:
        lines.append("| Time (ET) | Symbol | Side | Qty | Price | Notional |")
        lines.append("|-----------|--------|------|-----|-------|----------|")
        for t in trades:
            lines.append(
                f"| {t['time']} | {t['symbol']} | {t['side']} | {t['qty']} | ${t['price']:.2f} | ${t['notional']:,.2f} |"
            )
    lines.append("")

    lines.append("## Open Positions at Close")
    if not positions:
        lines.append("_Flat._")
    else:
        lines.append("| Symbol | Side | Qty | Avg Entry | Market Value | Unrealized P&L | % |")
        lines.append("|--------|------|-----|-----------|--------------|----------------|---|")
        for p in positions:
            lines.append(
                f"| {p.symbol} | {p.side} | {p.qty:g} | ${p.avg_entry_price:.2f} | "
                f"${p.market_value:,.2f} | ${p.unrealized_pl:,.2f} | {p.unrealized_plpc*100:+.2f}% |"
            )
    lines.append("")

    lines.append("## Notes")
    lines.append(_day_notes(day_pl_pct, bench_pct, len(trades)))
    lines.append("")

    report_path.write_text("\n".join(lines))
    log.info("Wrote journal: %s", report_path)
    return report_path


def _fetch_todays_trades(broker: Broker) -> list[dict]:
    """Fetch today's filled orders."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    today = _today_et()
    start = datetime(today.year, today.month, today.day, tzinfo=pytz.timezone(SESSION.timezone))
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=start.astimezone(pytz.UTC), limit=500)
        orders = broker.trading.get_orders(filter=req)
    except Exception as e:
        log.warning("Could not fetch orders: %s", e)
        return []

    out: list[dict] = []
    tz = pytz.timezone(SESSION.timezone)
    for o in orders:
        if o.filled_at is None or o.filled_qty is None:
            continue
        qty = float(o.filled_qty)
        price = float(o.filled_avg_price) if o.filled_avg_price else 0.0
        out.append(
            {
                "time": o.filled_at.astimezone(tz).strftime("%H:%M:%S"),
                "symbol": o.symbol,
                "side": o.side.value if hasattr(o.side, "value") else str(o.side),
                "qty": qty,
                "price": price,
                "notional": qty * price,
            }
        )
    out.sort(key=lambda t: t["time"])
    return out


def _benchmark_return(broker: Broker) -> float | None:
    try:
        bars = broker.daily_bars([SESSION.benchmark_symbol], lookback_days=5)
        df = bars.get(SESSION.benchmark_symbol)
        if df is None or len(df) < 2:
            return None
        prior = float(df["close"].iloc[-2])
        latest = float(df["close"].iloc[-1])
        return (latest - prior) / prior * 100.0
    except Exception as e:
        log.warning("benchmark_return failed: %s", e)
        return None


def _day_notes(day_pl_pct: float, bench_pct: float | None, n_trades: int) -> str:
    parts: list[str] = []
    if n_trades == 0:
        parts.append("No setups triggered — scanner found no confluence. Market may have been rangeless or choppy.")
    elif day_pl_pct > 0:
        parts.append(f"Positive day ({day_pl_pct:+.2f}%). {n_trades} fill(s).")
    else:
        parts.append(f"Negative day ({day_pl_pct:+.2f}%). {n_trades} fill(s). Review stops and level selection.")
    if bench_pct is not None:
        diff = day_pl_pct - bench_pct
        if diff > 0:
            parts.append(f"Outperformed SPY by {diff:+.2f}%.")
        else:
            parts.append(f"Underperformed SPY by {diff:+.2f}%.")
    return " ".join(parts)
