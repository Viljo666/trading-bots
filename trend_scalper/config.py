"""Trend-follow scalper config — complement to crypto_scalper (mean reversion).

Trades momentum: enters on EMA cross when ADX shows trending market,
exits on opposite cross / ATR stop / time stop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
for d in (LOG_DIR, DATA_DIR):
    d.mkdir(exist_ok=True)


@dataclass
class ExchangeConfig:
    name: str = "binanceusdm"
    symbol: str = os.getenv("TREND_SYMBOL", "SOL/USDT:USDT")
    timeframe: str = "15m"


@dataclass
class StrategyConfig:
    # EMA cross — fast above slow = bullish, fast below slow = bearish.
    ema_fast: int = 20
    ema_slow: int = 50
    # Slope confirmation: slow EMA must be sloping in trade direction.
    slope_lookback: int = 5
    # ADX gate — only trade when market is actually trending.
    adx_period: int = 14
    adx_min: float = 30.0
    # ATR-based stop (wider than mean-reversion since we ride trends).
    atr_period: int = 14
    atr_stop_mult: float = 2.5
    # Reward leg: fixed R multiple on the take-profit, with a trailing component.
    target_r: float = 2.5
    trail_atr_mult: float = 2.5      # once price has moved 1R, trail at this distance
    # Force exit if neither stop, target, nor opposite cross hit.
    time_stop_bars: int = 32         # 8h on 15m
    # Don't re-enter same direction within cooldown bars after an exit.
    cooldown_bars: int = 4


@dataclass
class RiskConfig:
    account_size_usdt: float = 1_000.0
    risk_per_trade_pct: float = 0.005
    max_concurrent_positions: int = 1
    max_daily_loss_pct: float = 0.02
    qty_decimals: int = 3


EXCHANGE = ExchangeConfig()
STRATEGY = StrategyConfig()
RISK = RiskConfig()
