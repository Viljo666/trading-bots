"""Monthly, advisory stress tests using only the evidence already collected.

Bootstrap resampling answers a practical question: if the same mix of outcomes
arrived in a worse order, what drawdown could the account face?  It is a risk
diagnostic, not a forecast, and it never alters a trading system.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import trade_quality


PROJECT = Path(__file__).resolve().parent.parent
N_SIMS = 5_000
MIN_OBSERVATIONS = 30
RISK_PER_TRADE = 0.01


def _drawdown(returns: list[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def bootstrap(returns: list[float], simulations: int = N_SIMS,
              seed: int = 20260815) -> dict[str, float]:
    """Deterministic bootstrap statistics so a report is reproducible."""
    if not returns:
        raise ValueError("at least one return is required")
    rng = random.Random(seed)
    finals: list[float] = []
    drawdowns: list[float] = []
    for _ in range(simulations):
        sample = [rng.choice(returns) for _ in returns]
        equity = 1.0
        for value in sample:
            equity *= 1.0 + value
        finals.append(equity - 1.0)
        drawdowns.append(_drawdown(sample))
    finals.sort()
    drawdowns.sort()
    i5 = max(0, round((simulations - 1) * 0.05))
    i50 = round((simulations - 1) * 0.50)
    return {
        "loss_probability": sum(v < 0 for v in finals) / simulations,
        "median_return": finals[i50],
        "p05_return": finals[i5],
        "p05_drawdown": drawdowns[i5],  # 5th percentile is a bad drawdown
    }


def _tsmom_returns() -> list[float]:
    values: dict[str, float] = {}
    try:
        for line in (PROJECT / "tsmom" / "daily_log.jsonl").read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") != "connected" or "capital_usd" not in row:
                continue
            date = str(row.get("ts", ""))[:10]
            values[date] = float(row["capital_usd"])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []
    capital = [values[day] for day in sorted(values)]
    return [capital[i] / capital[i - 1] - 1 for i in range(1, len(capital))
            if capital[i - 1] > 0]


def _crypto_returns() -> list[float]:
    # The current corrected trial uses a $1,000 paper account.  This is merely
    # a normalisation for a stress diagnostic; the Governor remains the source
    # of exact net-dollar reporting.
    try:
        import governor
        trades, _ = governor._crypto_pairs()
        return [net / 1_000.0 for _, net in trades]
    except Exception:
        return []


def _section(label: str, returns: list[float]) -> str:
    n = len(returns)
    if n < MIN_OBSERVATIONS:
        return f"*{label}* — collecting ({n}/{MIN_OBSERVATIONS} comparable observations)"
    result = bootstrap(returns)
    return (f"*{label}* — {n} observations\n"
            f"  bootstrap: P(loss) {result['loss_probability']:.0%} · "
            f"median {result['median_return']:+.1%} · 5% outcome {result['p05_return']:+.1%}\n"
            f"  5% worst-case drawdown {result['p05_drawdown']:.1%}")


def build() -> str:
    poc = [value * RISK_PER_TRADE for value in trade_quality.metrics("poc", "poc_futures")["r_values"]]
    sweep = [value * RISK_PER_TRADE for value in trade_quality.metrics("sweep", "sweep_futures")["r_values"]]
    return "\n".join([
        "🧪 *Monthly stress test* — advisory only",
        _section("POC dry shadow (1% risk model)", poc),
        _section("Sweep dry shadow (1% risk model)", sweep),
        _section("TSMOM paper daily NLV", _tsmom_returns()),
        _section("Crypto paper net", _crypto_returns()),
        "_Bootstrap resamples observed outcomes; it cannot predict future returns._",
    ])


if __name__ == "__main__":
    print(build())
