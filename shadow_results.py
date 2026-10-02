"""Small, shared ledger helpers for dry-run strategy outcome tracking.

The futures POC and Sweep runners use these helpers to record what a *dry*
setup would have done after it was detected.  These are price-bar simulations,
not broker fills: they deliberately make no orders, do not affect positions,
and do not claim to include execution quality, fees, or slippage.
"""
from __future__ import annotations

from typing import Any


def active_shadows(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the persisted active-shadow map, repairing old state safely."""
    shadows = state.setdefault("shadow_active", {})
    if not isinstance(shadows, dict):
        shadows = {}
        state["shadow_active"] = shadows
    return shadows


def observe_bar(record: dict[str, Any], high: float, low: float) -> None:
    """Update post-entry favourable/adverse excursion from one completed bar."""
    entry = float(record["entry"])
    if record["side"] == "long":
        record["best_favorable"] = max(float(record.get("best_favorable", entry)), float(high))
        record["worst_adverse"] = min(float(record.get("worst_adverse", entry)), float(low))
    else:
        # Store actual price extremes; close_event converts them into R values.
        record["best_favorable"] = min(float(record.get("best_favorable", entry)), float(low))
        record["worst_adverse"] = max(float(record.get("worst_adverse", entry)), float(high))
    record["bars_held"] = int(record.get("bars_held", 0)) + 1


def close_event(record: dict[str, Any], exit_price: float, reason: str,
                closed_at: str) -> dict[str, Any]:
    """Make one standardized, cost-free shadow outcome event."""
    entry = float(record["entry"])
    stop = float(record["stop"])
    risk = abs(entry - stop)
    side = record["side"]
    pnl_points = (exit_price - entry) if side == "long" else (entry - exit_price)
    dollars_per_point = float(record.get("dollars_per_point", 0.0))
    favorable_price = float(record.get("best_favorable", entry))
    adverse_price = float(record.get("worst_adverse", entry))
    mfe_points = (favorable_price - entry) if side == "long" else (entry - favorable_price)
    mae_points = (entry - adverse_price) if side == "long" else (adverse_price - entry)
    return {
        "kind": "shadow_closed",
        "mode": "shadow",
        "strategy": record["strategy"],
        "symbol": record["symbol"],
        "side": side,
        "entry": round(entry, 8),
        "exit": round(float(exit_price), 8),
        "stop": round(stop, 8),
        "target": round(float(record["target"]), 8),
        "risk_pts": round(risk, 8),
        "pnl_pts": round(pnl_points, 8),
        "pnl_usd_before_costs": round(pnl_points * dollars_per_point, 2),
        "r_multiple_before_costs": round(pnl_points / risk, 4) if risk > 0 else None,
        "mfe_r_before_costs": round(mfe_points / risk, 4) if risk > 0 else None,
        "mae_r_before_costs": round(mae_points / risk, 4) if risk > 0 else None,
        "entry_lag_bars": int(record.get("entry_lag_bars", 0)),
        "holding_bars": int(record.get("bars_held", 0)),
        "reason": reason,
        "opened_at": record.get("opened_at"),
        "closed_at": closed_at,
    }


def expired_event(record: dict[str, Any], reason: str, closed_at: str) -> dict[str, Any]:
    """Record a setup that never became a shadow trade."""
    return {
        "kind": "shadow_expired",
        "mode": "shadow",
        "strategy": record["strategy"],
        "symbol": record["symbol"],
        "side": record["side"],
        "entry": round(float(record["entry"]), 8),
        "stop": round(float(record["stop"]), 8),
        "target": round(float(record["target"]), 8),
        "reason": reason,
        "armed_at": record.get("armed_at"),
        "closed_at": closed_at,
    }
