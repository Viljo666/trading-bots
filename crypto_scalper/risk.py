"""Risk: position sizing in BTC contracts, daily loss guard."""
from __future__ import annotations

from dataclasses import dataclass

from config import RISK


@dataclass
class SizedTrade:
    side: str           # "long" / "short"
    qty: float          # in base currency (e.g. BTC)
    entry: float
    stop: float
    target: float
    risk_usdt: float


def size_trade(side: str, entry: float, stop: float, target: float, equity_usdt: float) -> SizedTrade | None:
    """Fixed-fractional sizing: risk = equity * risk_per_trade_pct, sized to stop distance."""
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0 or entry <= 0:
        return None

    dollar_risk = equity_usdt * RISK.risk_per_trade_pct
    qty = dollar_risk / risk_per_unit

    # Cap notional at max_notional_mult * equity.
    max_notional = equity_usdt * RISK.max_notional_mult
    if qty * entry > max_notional:
        qty = max_notional / entry

    qty = round(qty, RISK.qty_decimals)
    if qty <= 0:
        return None

    return SizedTrade(
        side=side,
        qty=qty,
        entry=entry,
        stop=stop,
        target=target,
        risk_usdt=qty * risk_per_unit,
    )


def daily_loss_breached(starting_equity: float, current_equity: float) -> bool:
    if starting_equity <= 0:
        return False
    drawdown = (current_equity - starting_equity) / starting_equity
    return drawdown <= -RISK.max_daily_loss_pct
