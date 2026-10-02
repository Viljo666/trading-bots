"""Live config for the 4h liquidity-sweep bot.

Validated edge (see project memory / backtest): swept 20-bar swing + EMA-200
trend filter + 1:3 RR on the NQ+CL basket. Survived walk-forward OOS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# shared .env with the parent project (Telegram creds etc.)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "live_log.jsonl"
STATE_FILE = ROOT / "state.json"


@dataclass(frozen=True)
class StrategyParams:
    swing_lookback: int = 20     # sweep the 20-bar swing extreme
    trend_ema: int = 200         # only longs above / shorts below
    rr: float = 3.0              # 1:3 reward:risk
    confirm_window: int = 3      # reclaim must occur within N bars of the sweep


@dataclass(frozen=True)
class SymbolSpec:
    ibkr_symbol: str
    exchange: str
    dollars_per_point: float
    tick_size: float


# The validated basket (NQ best index proxy; CL the only diversifier, ~0 corr),
# traded via MICRO contracts (MNQ $2/pt, MCL $100/pt) so 1%-of-equity risk
# sizing is honest on a small account. Signals are identical: micros track the
# full-size price series tick-for-tick.
SYMBOLS: list[SymbolSpec] = [
    SymbolSpec("MNQ", "CME", dollars_per_point=2.0, tick_size=0.25),
    SymbolSpec("MCL", "NYMEX", dollars_per_point=100.0, tick_size=0.01),
]


@dataclass(frozen=True)
class RiskConfig:
    account_equity_usd: float = float(os.getenv("SWEEP_EQUITY", "10000"))
    risk_per_trade_pct: float = 0.01    # 1% of equity per trade
    max_contracts: int = 5              # hard cap (sanity guard)
    min_contracts: int = 1


@dataclass(frozen=True)
class SessionConfig:
    paper_host: str = "127.0.0.1"
    paper_port: int = 4002              # IB Gateway paper; TWS paper = 7497
    bar_size: str = "4 hours"
    history_duration: str = "90 D"      # ~250+ 4h bars: covers EMA-200 + swing-20
    poll_interval_sec: int = 300        # 4h bars — checking every 5 min is plenty
    watchdog_stall_min: int = 30


PARAMS = StrategyParams()
RISK = RiskConfig()
SESSION = SessionConfig()
