"""ICT mechanical research bot — config."""
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
    symbol: str = os.getenv("ICT_SYMBOL", "BTC/USDT:USDT")
    htf: str = "1h"     # higher timeframe → bias
    ltf: str = "5m"     # lower timeframe → entries


@dataclass
class StrategyConfig:
    # HTF bias: bullish if close > EMA(htf_ema_period) on HTF.
    htf_ema_period: int = 50

    # Swing detection on LTF: pivot high/low if it's the highest/lowest of the
    # surrounding window (swing_left bars on each side).
    swing_left: int = 10
    swing_right: int = 3
    # Treat a swing as "liquidity" if it hasn't been broken in the last
    # liquidity_lookback bars.
    liquidity_lookback: int = 100

    # FVG: classic 3-candle pattern.
    #   Bullish: low[i] > high[i-2] (gap between candles i-2 and i)
    #   Bearish: high[i] < low[i-2]
    # Min FVG height in % of price (filters tiny gaps).
    min_fvg_pct: float = 0.0008  # 0.08%
    # FVG validity in bars — ignore old FVGs that haven't filled.
    fvg_max_age_bars: int = 50

    # Entry trigger: after a liquidity sweep, wait for an FVG to form aligned
    # with HTF bias, then enter on retrace into the FVG.
    sweep_lookback: int = 20    # how recent the swept liquidity must be
    sweep_min_wick_atr: float = 0.5  # sweep wick >= 0.5 * ATR (real spike, not drift)
    atr_period: int = 14

    # Risk/exit.
    stop_buffer_atr: float = 0.5     # stop = wick extreme ± buffer*ATR
    target_r_min: float = 1.5        # require ≥ 1.5R to opposite-side liquidity
    time_stop_bars: int = 100        # ~8h on 5m


@dataclass
class RiskConfig:
    account_size_usdt: float = 1_000.0
    risk_per_trade_pct: float = 0.005
    qty_decimals: int = 3


EXCHANGE = ExchangeConfig()
STRATEGY = StrategyConfig()
RISK = RiskConfig()
