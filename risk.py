"""Risk management: position sizing, stops, daily loss guard."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config import RISK

log = logging.getLogger(__name__)


@dataclass
class SizedTrade:
    symbol: str
    side: str              # "buy" or "sell"
    qty: int
    entry: float
    stop: float
    target: float
    risk_dollars: float
    reward_dollars: float

    @property
    def r_multiple(self) -> float:
        return self.reward_dollars / self.risk_dollars if self.risk_dollars else 0.0


def size_trade(
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    target: float,
    equity: float,
) -> SizedTrade | None:
    """Compute position size using fixed-fractional risk."""
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        log.warning("%s: invalid stop (entry=%.2f stop=%.2f)", symbol, entry, stop)
        return None

    dollar_risk = equity * RISK.risk_per_trade_pct
    qty = int(dollar_risk // risk_per_share)
    if qty <= 0:
        log.info("%s: position size rounds to 0 (risk/share=%.2f budget=$%.2f)", symbol, risk_per_share, dollar_risk)
        return None

    # Cap position size at max_position_pct_of_equity
    max_notional = equity * RISK.max_position_pct_of_equity
    if qty * entry > max_notional:
        qty = int(max_notional // entry)
        if qty <= 0:
            return None

    reward_per_share = abs(target - entry)
    return SizedTrade(
        symbol=symbol,
        side=side,
        qty=qty,
        entry=entry,
        stop=stop,
        target=target,
        risk_dollars=qty * risk_per_share,
        reward_dollars=qty * reward_per_share,
    )


def can_open_new_position(current_positions: int, starting_equity: float, current_equity: float) -> tuple[bool, str]:
    if current_positions >= RISK.max_concurrent_positions:
        return False, f"max {RISK.max_concurrent_positions} concurrent positions reached"

    day_pnl_pct = (current_equity - starting_equity) / starting_equity
    if day_pnl_pct <= -RISK.max_daily_loss_pct:
        return False, f"daily loss limit hit ({day_pnl_pct:.2%})"

    return True, "ok"
