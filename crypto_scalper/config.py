"""Crypto scalper configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
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
class ExchangeConfig:
    name: str = os.getenv("CRYPTO_EXCHANGE", "binanceusdm")
    api_key: str = os.getenv("CRYPTO_API_KEY", "")
    secret: str = os.getenv("CRYPTO_SECRET", "")
    testnet: bool = os.getenv("CRYPTO_TESTNET", "true").lower() == "true"
    # "paper" → simulated fills against real public market data (no API keys).
    # "live"  → real exchange (use only with real money on mainnet — testnet is paper).
    mode: str = os.getenv("CRYPTO_MODE", "paper")
    # USDT-margined perpetual; CCXT unified symbol uses :USDT suffix for linear swaps.
    symbol: str = os.getenv("CRYPTO_SYMBOL", "SOL/USDT:USDT")
    timeframe: str = "1m"
    leverage: int = 1


@dataclass
class StrategyConfig:
    # Session VWAP rolling window (in 1m bars). 1440 = 24h.
    vwap_window: int = 1440
    # Std-dev band multiplier for "extended" condition.
    # Restored to 2.5 (validated tight-entry) after drift to 2.0 caused live losses.
    band_sigma: float = 2.5
    # RSI filter.
    rsi_period: int = 14
    # Restored to 20/80 (validated tight-entry) — walk-forward: +6.85% over 180d, 5/6 windows.
    rsi_long_max: float = 20.0   # long only if RSI <= this AND price below lower band
    rsi_short_min: float = 80.0  # short only if RSI >= this AND price above upper band
    # Regime filter: skip entries when ADX >= adx_max (strong trend kills mean reversion).
    adx_period: int = 14
    # Restored to 30 (validated). Drift to 35 let too many trending signals through.
    adx_max: float = 30.0
    # Exit at VWAP touch, or stop at fixed % from entry.
    stop_pct: float = 0.005      # 0.5%
    # Time stop in bars (force exit if neither stop nor target hit).
    # Restored to 60 bars (validated).
    time_stop_bars: int = 60
    # Cooldown between entries on same side (bars).
    cooldown_bars: int = 5
    # Minimum spread (in bps of mid) to skip entry — wide spread kills scalpers.
    max_spread_bps: float = 5.0
    # Maker entries: how many seconds to leave a post-only limit on the book before cancelling.
    order_fill_timeout_seconds: int = 60
    # Trend filter for long entries: only go long when close > EMA of this many bars.
    # 200 bars on 1m = ~3.3-hour trend window. Set to 0 to disable the filter entirely.
    # Disabled (0) to match validated backtest exactly. Was added in a later session
    # but not present in the walk-forward that produced the +6.85% result.
    trend_ema_period: int = 0


@dataclass
class RiskConfig:
    # Account equity in quote currency (USDT). Live mode reads from broker.
    account_size_usdt: float = 1_000.0
    risk_per_trade_pct: float = 0.005   # 0.5% per trade — scalpers take many trades
    max_concurrent_positions: int = 1
    max_daily_loss_pct: float = 0.02
    # Cap notional at this multiple of equity (1x = no leverage above account).
    max_notional_mult: float = 1.0
    # Round qty to this many decimals (BTC perp = 3 decimals = 0.001 BTC step).
    qty_decimals: int = 3


@dataclass
class SessionConfig:
    poll_interval_seconds: int = 5
    # Crypto runs 24/7, so no market hours, but we do a daily PnL reset at UTC midnight.
    pnl_reset_hour_utc: int = 0


EXCHANGE = ExchangeConfig()
STRATEGY = StrategyConfig()
RISK = RiskConfig()
SESSION = SessionConfig()


def validate() -> None:
    if EXCHANGE.mode == "paper":
        return  # paper mode uses public data only
    if EXCHANGE.mode != "live":
        raise RuntimeError(f"Unknown CRYPTO_MODE={EXCHANGE.mode!r} (expected 'paper' or 'live')")
    if not EXCHANGE.api_key or not EXCHANGE.secret:
        raise RuntimeError(
            "Crypto API keys missing for live mode. Set CRYPTO_API_KEY and CRYPTO_SECRET in .env."
        )
