"""Live config for the POC-continuation bot.

Validated (walk-forward, see project memory): trade WITH the sweep through the
prior day's RTH volume-profile POC. Open below POC + rally to it -> LONG;
open above + sell-off to it -> SHORT. Stop 0.25x dailyATR beyond POC, 1:3 RR,
NO breakeven move, no entries after 15:00 ET, flat by close. YM + ES (NQ fails).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "live_log.jsonl"
STATE_FILE = ROOT / "state.json"
ET = "America/New_York"


@dataclass(frozen=True)
class StrategyParams:
    atr_mult: float = 0.25       # stop distance = this x 14-day RTH ATR
    rr: float = 3.0              # target = rr x risk
    atr_period: int = 14
    entry_cutoff_et: str = "15:00"   # cancel unfilled entry after this
    flatten_et: str = "15:55"        # force-flat any open position
    arm_after_et: str = "09:35"      # compute levels/arm once the open bar exists


@dataclass(frozen=True)
class SymbolSpec:
    ibkr_symbol: str
    exchange: str
    dollars_per_point: float
    tick_size: float
    bin_pts: float               # volume-profile bin size (matches backtest)


# Validated basket: YM + ES (NQ tested negative — do not add without
# re-validation), traded via MICRO contracts (MYM $0.50/pt, MES $5/pt) for
# honest 1%-risk sizing. POC/ATR are computed from the micro's own bars; micro
# volume tracks full-size closely enough that the POC lands on the same level.
SYMBOLS: list[SymbolSpec] = [
    SymbolSpec("MYM", "CBOT", dollars_per_point=0.5, tick_size=1.0, bin_pts=10.0),
    SymbolSpec("MES", "CME", dollars_per_point=5.0, tick_size=0.25, bin_pts=1.0),
]


@dataclass(frozen=True)
class RiskConfig:
    account_equity_usd: float = float(os.getenv("POC_EQUITY", "10000"))
    risk_per_trade_pct: float = 0.01
    max_contracts: int = 10      # micros are 1/10th size — allow more room
    min_contracts: int = 1


@dataclass(frozen=True)
class SessionConfig:
    paper_host: str = "127.0.0.1"
    paper_port: int = 4002
    history_duration: str = "30 D"   # 5m bars: covers ATR(14) + prior-day POC
    poll_interval_sec: int = 60
    watchdog_stall_min: int = 30


PARAMS = StrategyParams()
RISK = RiskConfig()
SESSION = SessionConfig()
