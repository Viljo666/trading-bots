"""Shared exposure guard for all IBKR futures bots in this workspace.

TSMOM, POC and Sweep use the same IBKR account.  This module is deliberately
small and broker-neutral: before a POC/Sweep bracket is sent, it reads the
account's existing futures positions and active parent orders, then blocks a
new trade that would breach the shared notional or directional-exposure rails.

These are safety rails, not alpha inputs.  The strategies keep their own
position sizing rules; this guard only prevents accidental stacking.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


MULTIPLIER_BY_SYMBOL = {
    "ES": 50.0, "MES": 5.0, "NQ": 20.0, "MNQ": 2.0,
    "YM": 5.0, "MYM": 0.5, "RTY": 50.0, "M2K": 5.0,
    "CL": 1000.0, "MCL": 100.0, "GC": 100.0, "MGC": 10.0,
    "ZN": 1000.0, "ZB": 1000.0, "EUR": 125000.0,
}

GROUP_BY_SYMBOL = {
    "ES": "equity-index", "MES": "equity-index", "NQ": "equity-index",
    "MNQ": "equity-index", "YM": "equity-index", "MYM": "equity-index",
    "RTY": "equity-index", "M2K": "equity-index",
    "CL": "energy", "MCL": "energy",
    "GC": "metals", "MGC": "metals",
    "ZN": "rates", "ZB": "rates", "EUR": "fx",
}

# Fractions of account net liquidation.  They are intentionally above normal
# model sizing, but low enough to stop a bad multiplier, duplicate process, or
# several highly-correlated index brackets from quietly accumulating.
MAX_TOTAL_GROSS_NOTIONAL = 3.0
MAX_DIRECTIONAL_NOTIONAL_BY_GROUP = {
    "equity-index": 0.90,
    "energy": 0.40,
    "metals": 0.25,
    "rates": 1.25,
    "fx": 0.85,
    "other": 0.25,
}
MAX_SINGLE_TRADE_STOP_RISK = 0.01


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    capital_usd: float = 0.0
    proposed_notional: float = 0.0
    proposed_stop_risk: float = 0.0
    total_gross_notional: float = 0.0
    group: str = "other"
    group_directional_notional: float = 0.0

    def event_fields(self) -> dict[str, Any]:
        return asdict(self)


def _symbol(contract) -> str:
    return str(getattr(contract, "symbol", "")).upper()


def _group(contract) -> str:
    return GROUP_BY_SYMBOL.get(_symbol(contract), "other")


def _multiplier(contract) -> float:
    symbol = _symbol(contract)
    configured = MULTIPLIER_BY_SYMBOL.get(symbol)
    if configured is not None:
        return configured
    try:
        value = float(getattr(contract, "multiplier", 0) or 0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return 0.0


def account_net_liquidation_usd(ib) -> float:
    """Read NetLiquidation in USD, including EUR-denominated IBKR accounts."""
    values = list(ib.accountValues())
    direct = next((v for v in values if v.tag == "NetLiquidation" and v.currency == "USD"), None)
    if direct is not None:
        value = float(direct.value)
        if value > 0:
            return value

    base = next((v for v in values if v.tag == "NetLiquidation" and v.currency not in ("BASE", "USD")), None)
    usd_rate = next((v for v in values if v.tag == "ExchangeRate" and v.currency == "USD"), None)
    if base is not None and usd_rate is not None:
        rate = float(usd_rate.value)
        if rate > 0:
            return float(base.value) / rate
    raise RuntimeError("IBKR did not provide usable USD net liquidation")


def _is_active_parent(trade) -> bool:
    order = getattr(trade, "order", None)
    status = str(getattr(getattr(trade, "orderStatus", None), "status", "")).lower()
    if order is None or getattr(order, "parentId", 0):
        return False
    return status not in {"filled", "cancelled", "apicancelled", "inactive"}


def _order_price(order) -> float:
    for field in ("lmtPrice", "auxPrice"):
        try:
            value = float(getattr(order, field, 0) or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 0.0


def check_new_futures_trade(ib, contract, side: str, qty: int, entry: float, stop: float) -> RiskDecision:
    """Return an allow/block decision for one proposed futures bracket.

    Fails closed if reliable account or contract information is unavailable.
    A trade that reduces current directional exposure remains eligible even when
    that group is already near its cap.
    """
    try:
        capital = account_net_liquidation_usd(ib)
        multiplier = _multiplier(contract)
        entry = float(entry)
        stop = float(stop)
        qty = int(qty)
        if capital <= 0 or multiplier <= 0 or entry <= 0 or qty <= 0:
            raise ValueError("invalid capital, contract multiplier, entry, or quantity")

        group = _group(contract)
        sign = 1.0 if side.lower() in {"long", "buy"} else -1.0
        proposed = sign * qty * entry * multiplier
        proposed_abs = abs(proposed)
        stop_risk = abs(entry - stop) * qty * multiplier

        total_gross = 0.0
        group_net = 0.0
        for position in ib.positions():
            p_contract = position.contract
            if str(getattr(p_contract, "secType", "")) != "FUT":
                continue
            # IBKR's Position.avgCost for futures is already the *per-contract
            # dollar cost* (for example ES price × $50 multiplier). Do not
            # multiply it again; doing so would inflate exposure by 50–125,000×.
            cost_per_contract = float(getattr(position, "avgCost", 0) or 0)
            quantity = float(getattr(position, "position", 0) or 0)
            if cost_per_contract <= 0 or quantity == 0:
                continue
            signed_notional = quantity * cost_per_contract
            total_gross += abs(signed_notional)
            if _group(p_contract) == group:
                group_net += signed_notional

        # A resting parent order has future exposure even before it is filled.
        for trade in ib.openTrades():
            if not _is_active_parent(trade) or str(getattr(trade.contract, "secType", "")) != "FUT":
                continue
            p_multiplier = _multiplier(trade.contract)
            price = _order_price(trade.order)
            quantity = float(getattr(trade.order, "totalQuantity", 0) or 0)
            if p_multiplier <= 0 or price <= 0 or quantity <= 0:
                continue
            action = str(getattr(trade.order, "action", "")).upper()
            signed_notional = (1.0 if action == "BUY" else -1.0) * quantity * price * p_multiplier
            total_gross += abs(signed_notional)
            if _group(trade.contract) == group:
                group_net += signed_notional

        total_after = total_gross + proposed_abs
        group_after = group_net + proposed
        if stop_risk > capital * MAX_SINGLE_TRADE_STOP_RISK:
            return RiskDecision(False, "proposed stop risk exceeds 1% shared-account limit", capital,
                                proposed_abs, stop_risk, total_after, group, group_after)
        if total_after > capital * MAX_TOTAL_GROSS_NOTIONAL:
            return RiskDecision(False, "shared futures gross notional limit exceeded", capital,
                                proposed_abs, stop_risk, total_after, group, group_after)

        cap = capital * MAX_DIRECTIONAL_NOTIONAL_BY_GROUP[group]
        reducing = abs(group_after) < abs(group_net)
        if not reducing and abs(group_after) > cap:
            return RiskDecision(False, f"{group} directional exposure limit exceeded", capital,
                                proposed_abs, stop_risk, total_after, group, group_after)
        return RiskDecision(True, "ok", capital, proposed_abs, stop_risk, total_after, group, group_after)
    except Exception as e:
        return RiskDecision(False, f"shared risk check unavailable: {e}")
