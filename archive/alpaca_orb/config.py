"""Central configuration for the trading bot."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
JOURNAL_DIR = ROOT / "journal"
DATA_DIR = ROOT / "data"
for d in (LOG_DIR, JOURNAL_DIR, DATA_DIR):
    d.mkdir(exist_ok=True)


@dataclass
class AlpacaConfig:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    secret_key: str = os.getenv("ALPACA_SECRET_KEY", "")
    base_url: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    data_url: str = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
    paper: bool = True


@dataclass
class RiskConfig:
    account_size: float = 100_000.0
    risk_per_trade_pct: float = 0.01
    max_concurrent_positions: int = 3
    max_daily_loss_pct: float = 0.03
    max_position_pct_of_equity: float = 0.25


@dataclass
class StrategyConfig:
    atr_period: int = 14
    atr_stop_mult: float = 1.0
    target_r_multiple: float = 2.0
    value_area_pct: float = 0.70
    vp_bins: int = 40
    reversal_volume_mult: float = 1.5
    level_proximity_atr: float = 0.5
    swing_convert_time: str = "15:45"
    # Swing-carry rules (only effective if allow_swing_conversion is True).
    # Tightened from 1.0 -> 2.0 after May 13-14 losses on APH/MU swing shorts.
    swing_min_r_multiple: float = 2.0
    # Master switch: when False, every position is flattened at 15:55 regardless of P&L.
    # Disabled by default after last week's swing-carry losses.
    allow_swing_conversion: bool = False
    # Setup toggles. Reversal disabled live as of Jun 2: backtest showed edge,
    # but 6 weeks of live trading showed it consistently bleeds in real conditions.
    # Bot is now ORB-only — fires only in trend_up/trend_down regimes.
    enable_orb: bool = True
    enable_reversal: bool = False
    # Break-even stop: once price reaches +N×R favorable, move stop to entry.
    # Backtested winner = 1.0 (vs 0.5 = too noisy, None = baseline). 0 disables.
    be_trigger_r: float = 1.0
    # Trend filter for reversal setups: block reversal shorts if SPY is up
    # >= threshold_pct over the last lookback_days, and vice versa for longs.
    # Idea: don't fade a strong macro trend with mean-reversion shorts.
    trend_filter_enabled: bool = False  # tested May 16, was a wash, not adopted
    trend_filter_lookback_days: int = 3
    trend_filter_threshold_pct: float = 1.0
    # Re-entry cooldown: after any exit from a symbol, block re-entry for this
    # many minutes. Prevents churning the same name repeatedly on a noisy open.
    reentry_cooldown_minutes: int = 45


@dataclass
class ORBConfig:
    # Opening Range Breakout: first 15-min bar's high/low is the range.
    range_bars: int = 2                 # number of 15-min bars forming the OR (2 = 09:30-10:00)
    volume_mult: float = 1.5            # breakout bar must be >= 1.5x recent avg volume
    stop_buffer_atr: float = 0.25       # stop placed beyond opposite side of range + buffer
    target_r_multiple: float = 2.0
    latest_entry_time: str = "10:30"    # no ORB entries after this (ET)


@dataclass
class RegimeConfig:
    # SPY distance from its own VWAP decides trend vs range.
    spy_symbol: str = "SPY"
    trend_threshold_pct: float = 0.30   # |SPY - VWAP| / SPY * 100 >= 0.30 -> trend
    range_threshold_pct: float = 0.15   # < 0.15 -> range; between = neutral (no trades)
    min_bars: int = 4                   # need at least this many bars before calling regime
    # Gap-day override: on big open gaps, VWAP and price both sit near the gap
    # level early in the session — deviation reads near zero, regime gets called
    # "range" and the bot shorts into a rally. Override to trend for the first
    # N bars when |open vs prior_close| exceeds the threshold.
    gap_override_threshold_pct: float = 0.4
    gap_override_min_bars: int = 4      # ~60 min on 15-min bars


@dataclass
class ScannerConfig:
    min_price: float = 10.0
    max_price: float = 2000.0
    min_avg_volume: int = 1_000_000
    top_n_premarket: int = 40
    top_n_intraday: int = 10
    relative_volume_min: float = 1.5
    gap_pct_min: float = 0.5
    etf_universe: list[str] = field(
        default_factory=lambda: [
            "SPY", "QQQ", "DIA", "IWM", "XLF", "XLK", "XLE", "XLV",
            "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "ARKK",
        ]
    )


@dataclass
class SessionConfig:
    timezone: str = "America/New_York"
    market_open: str = "09:30"
    market_close: str = "16:00"
    premarket_scan_time: str = "09:00"
    flatten_time: str = "15:55"
    scan_interval_seconds: int = 60
    bar_timeframe: str = "15Min"
    benchmark_symbol: str = "SPY"


ALPACA = AlpacaConfig()
RISK = RiskConfig()
STRATEGY = StrategyConfig()
ORB = ORBConfig()
REGIME = RegimeConfig()
SCANNER = ScannerConfig()
SESSION = SessionConfig()


def validate() -> None:
    if not ALPACA.api_key or not ALPACA.secret_key:
        raise RuntimeError("Alpaca API keys missing. Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
