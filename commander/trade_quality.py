"""Detailed, advisory analysis of the dry futures shadow ledger.

These are diagnostics rather than a trading signal.  They answer whether a
setup reaches entry, how much room it had before exit, and how long it needed.
No calculation in this module can alter a bot or an order.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parent.parent
MIN_OUTCOMES = 30


def _rows(strategy: str, directory: str) -> list[dict[str, Any]]:
    path = PROJECT / directory / "live_log.jsonl"
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get("strategy") == strategy and event.get("mode") == "shadow":
                rows.append(event)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return rows


def metrics(strategy: str, directory: str) -> dict[str, Any]:
    rows = _rows(strategy, directory)
    outcomes = [r for r in rows if r.get("kind") == "shadow_closed"]
    armed = [r for r in rows if r.get("kind") == "shadow_armed"]
    filled = [r for r in rows if r.get("kind") == "shadow_filled"]
    expired = [r for r in rows if r.get("kind") == "shadow_expired"]

    def values(name: str) -> list[float]:
        return [float(r[name]) for r in outcomes if r.get(name) is not None]

    r_values = values("r_multiple_before_costs")
    mfe = values("mfe_r_before_costs")
    mae = values("mae_r_before_costs")
    holds = values("holding_bars")
    lags = values("entry_lag_bars")
    reasons: dict[str, int] = {}
    for row in outcomes:
        reason = str(row.get("reason", "unknown"))
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "strategy": strategy,
        "armed": len(armed),
        "filled": len(filled),
        "expired": len(expired),
        "outcomes": len(outcomes),
        "r_values": r_values,
        "avg_r": sum(r_values) / len(r_values) if r_values else None,
        "avg_mfe_r": sum(mfe) / len(mfe) if mfe else None,
        "avg_mae_r": sum(mae) / len(mae) if mae else None,
        "avg_holding_bars": sum(holds) / len(holds) if holds else None,
        "avg_entry_lag_bars": sum(lags) / len(lags) if lags else None,
        "reasons": reasons,
    }


def _line(data: dict[str, Any]) -> str:
    label = "POC" if data["strategy"] == "poc" else "Sweep"
    if not data["outcomes"]:
        return (f"*{label}* — collecting: {data['armed']} setups · {data['filled']} filled · "
                f"{data['expired']} unfilled")
    exits = ", ".join(f"{reason}×{count}" for reason, count in sorted(data["reasons"].items()))
    def fmt(value: float | None) -> str:
        return f"{value:+.2f}R" if value is not None else "—"
    def bars(value: float | None) -> str:
        return f"{value:.1f}" if value is not None else "—"
    return (
        f"*{label}* — {data['outcomes']}/{MIN_OUTCOMES} outcomes\n"
        f"  avg {fmt(data['avg_r'])} · MFE {fmt(data['avg_mfe_r'])} · "
        f"MAE {fmt(data['avg_mae_r'])}\n"
        f"  wait {bars(data['avg_entry_lag_bars'])} bars · hold {bars(data['avg_holding_bars'])} bars · {exits}"
    )


def promotion_gate() -> str:
    """A human decision gate; it never makes a mode switch automatically."""
    rows = [metrics("poc", "poc_futures"), metrics("sweep", "sweep_futures")]
    lines = ["🔒 *Promotion gate* — no automatic switching"]
    for data in rows:
        label = "POC" if data["strategy"] == "poc" else "Sweep"
        if data["outcomes"] < MIN_OUTCOMES:
            lines.append(f"{label}: HOLD dry — {data['outcomes']}/{MIN_OUTCOMES} shadow outcomes")
        elif data["avg_r"] is None or data["avg_r"] <= 0:
            lines.append(f"{label}: HOLD dry — sample is not positive after {data['outcomes']} outcomes")
        else:
            lines.append(f"{label}: candidate for a *human-reviewed* live-paper trial only")
    lines.append("_Any real-money decision still requires ≥20 verified live-paper exits, costs, and a manual review._")
    return "\n".join(lines)


def build() -> str:
    return "\n".join([
        "📐 *Trade quality* — dry shadow evidence",
        _line(metrics("poc", "poc_futures")),
        _line(metrics("sweep", "sweep_futures")),
        "_MFE/MAE are price-bar estimates before commissions, slippage, and fills._",
    ])


if __name__ == "__main__":
    print(build())
    print()
    print(promotion_gate())
