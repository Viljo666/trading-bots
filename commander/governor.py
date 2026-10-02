"""Advisory-only weekly evidence review for the trading-bot stable.

This module never changes a bot, order, position, risk setting, or schedule.
It is intentionally conservative: a strategy remains ``COLLECTING`` until it
has a useful amount of comparable paper/shadow evidence.  The output is small
enough for one Telegram message and is included in the scheduled weekly report.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT = Path(__file__).resolve().parent.parent
MIN_FUTURES_OUTCOMES = 30
MIN_CRYPTO_TRADES = 20
MIN_TSMOM_OBSERVATIONS = 20


def _timestamp(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (TypeError, ValueError):
        return None


def _events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    except (OSError, json.JSONDecodeError):
        pass
    return rows


def _profit_factor(values: Iterable[float]) -> float | None:
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if losses == 0:
        return None if gains == 0 else float("inf")
    return gains / losses


def _max_drawdown(values: Iterable[float], start: float = 0.0) -> float:
    equity = peak = start
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def _status(n: int, minimum: int, average: float | None, pf: float | None) -> str:
    if n < minimum:
        return "COLLECTING"
    if average is not None and (average <= 0 or (pf is not None and pf < 1.0)):
        return "REVIEW"
    return "HEALTHY"


def _shadow_section(strategy: str, directory: str) -> str:
    log = PROJECT / directory / "live_log.jsonl"
    rows = _events(log)
    completed = [r for r in rows if r.get("kind") == "shadow_closed" and
                 r.get("strategy") == strategy]
    expired = [r for r in rows if r.get("kind") == "shadow_expired" and
               r.get("strategy") == strategy]
    started = [r for r in rows if r.get("kind") == "shadow_armed" and
               r.get("strategy") == strategy]
    active = 0
    try:
        state = json.loads((PROJECT / directory / "state.json").read_text())
        active = sum(1 for v in state.get("shadow_active", {}).values()
                     if isinstance(v, dict) and v.get("strategy") == strategy)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    r_values = [float(r["r_multiple_before_costs"]) for r in completed
                if r.get("r_multiple_before_costs") is not None]
    n = len(r_values)
    average = (sum(r_values) / n) if n else None
    pf = _profit_factor(r_values)
    status = _status(n, MIN_FUTURES_OUTCOMES, average, pf)
    label = "POC dry shadow" if strategy == "poc" else "Sweep dry shadow"
    if not n:
        starter = "tracking starts with the next dry setup" if not started else "awaiting the first closed outcome"
        return (f"*{label}* — {status} (0/{MIN_FUTURES_OUTCOMES} outcomes)\n"
                f"  {len(started)} armed · {len(expired)} unfilled · {active} active; _{starter}_")
    pf_text = "∞" if pf == float("inf") else f"{pf:.2f}" if pf is not None else "—"
    dd = _max_drawdown(r_values)
    return (f"*{label}* — {status} ({n}/{MIN_FUTURES_OUTCOMES} outcomes)\n"
            f"  avg {average:+.2f}R · PF {pf_text} · total {sum(r_values):+.1f}R · max DD {dd:.1f}R\n"
            f"  {len(started)} armed · {len(expired)} unfilled · {active} active\n"
            f"  _Price-bar shadow only: before commissions, slippage, and fills._")


def _crypto_pairs() -> tuple[list[tuple[datetime, float]], datetime | None]:
    trial_start = None
    try:
        trial_start = _timestamp(json.loads(
            (PROJECT / "crypto_scalper" / "paper_state.json").read_text()
        ).get("trial_started_at"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    rows: list[dict[str, Any]] = []
    for path in sorted((PROJECT / "crypto_scalper" / "journal").glob("paper_*.jsonl")):
        rows.extend(_events(path))
    rows.sort(key=lambda r: str(r.get("ts", "")))
    active: dict[str, Any] | None = None
    trades: list[tuple[datetime, float]] = []
    for row in rows:
        stamp = _timestamp(row.get("ts"))
        if stamp is None or (trial_start is not None and stamp < trial_start):
            continue
        if row.get("event") == "open":
            active = row
        elif row.get("event") == "close" and active is not None:
            # Cash in PaperBroker reflects both fees.  Use the same net result
            # here, rather than its raw price P&L field alone.
            net = (float(row.get("pnl", 0.0)) - float(active.get("fee", 0.0))
                   - float(row.get("fee", 0.0)))
            trades.append((stamp, net))
            active = None
    return trades, trial_start


def _crypto_section() -> str:
    trades, started = _crypto_pairs()
    values = [value for _, value in trades]
    n = len(values)
    average = sum(values) / n if n else None
    pf = _profit_factor(values)
    status = _status(n, MIN_CRYPTO_TRADES, average, pf)
    since = started.strftime("%d %b") if started else "unknown"
    if not n:
        return (f"*Crypto paper* — {status} (0/{MIN_CRYPTO_TRADES} net trades)\n"
                f"  current trial since {since}; _waiting for completed trades_")
    wins = sum(v > 0 for v in values) / n * 100
    pf_text = "∞" if pf == float("inf") else f"{pf:.2f}" if pf is not None else "—"
    return (f"*Crypto paper* — {status} ({n}/{MIN_CRYPTO_TRADES} net trades)\n"
            f"  win {wins:.0f}% · avg ${average:+.2f} · PF {pf_text} · total ${sum(values):+.2f}\n"
            f"  max DD ${_max_drawdown(values):.2f} · trial since {since}")


def _tsmom_section() -> str:
    by_day: dict[str, tuple[datetime, float]] = {}
    for row in _events(PROJECT / "tsmom" / "daily_log.jsonl"):
        if row.get("kind") != "connected" or "capital_usd" not in row:
            continue
        stamp = _timestamp(row.get("ts"))
        if stamp is not None:
            by_day[stamp.date().isoformat()] = (stamp, float(row["capital_usd"]))
    observations = [by_day[k] for k in sorted(by_day)]
    n = len(observations)
    if n < 2:
        return (f"*TSMOM paper* — COLLECTING ({n}/{MIN_TSMOM_OBSERVATIONS} daily NLV readings)\n"
                "  _waiting for a comparable daily account series_")
    capital = [value for _, value in observations]
    returns = [(capital[i] / capital[i - 1] - 1) for i in range(1, len(capital))
               if capital[i - 1] > 0]
    average = sum(returns) / len(returns) if returns else None
    status = _status(n - 1, MIN_TSMOM_OBSERVATIONS - 1, average, None)
    peak = capital[0]
    max_dd = 0.0
    for value in capital:
        peak = max(peak, value)
        max_dd = min(max_dd, value / peak - 1)
    change = capital[-1] / capital[0] - 1
    return (f"*TSMOM paper* — {status} ({n}/{MIN_TSMOM_OBSERVATIONS} daily NLV readings)\n"
            f"  ${capital[0]:,.0f} → ${capital[-1]:,.0f} ({change:+.2%}) · max DD {max_dd:.2%}\n"
            "  _Account-level result; other paper positions would contaminate it._")


def build() -> str:
    """Return one Telegram-ready weekly Governor report. No side effects."""
    import trade_quality
    import execution_audit
    return "\n".join([
        "🧭 *Strategy Governor* — advisory only",
        "_No automatic order, size, pause, or mode changes._",
        _tsmom_section(),
        _shadow_section("poc", "poc_futures"),
        _shadow_section("sweep", "sweep_futures"),
        _crypto_section(),
        trade_quality.build(),
        trade_quality.promotion_gate(),
        execution_audit.build(),
        ("_Decision rule: collect comparable samples first. A REVIEW flag means "
         "inspect the strategy; it never changes trading by itself._"),
    ])


if __name__ == "__main__":
    print(build())
