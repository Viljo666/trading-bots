"""Indicators: rolling VWAP with std-dev bands, RSI."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_vwap(df: pd.DataFrame, window: int) -> pd.Series:
    """Volume-weighted average price over a rolling window of bars."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"]
    return pv.rolling(window, min_periods=max(20, window // 10)).sum() / df["volume"].rolling(
        window, min_periods=max(20, window // 10)
    ).sum()


def vwap_bands(df: pd.DataFrame, vwap: pd.Series, window: int, sigma: float) -> tuple[pd.Series, pd.Series]:
    """Upper/lower bands at ±sigma standard deviations of close around VWAP."""
    deviation = df["close"] - vwap
    std = deviation.rolling(window, min_periods=max(20, window // 10)).std()
    return vwap + sigma * std, vwap - sigma * std


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX. Values < ~25 → ranging, > 25 → trending."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def ema(close: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def add_indicators(
    df: pd.DataFrame,
    vwap_window: int,
    band_sigma: float,
    rsi_period: int,
    adx_period: int = 14,
    trend_ema_period: int = 200,
) -> pd.DataFrame:
    """Attach vwap, upper/lower bands, rsi, adx, ema_trend columns. Returns a copy."""
    out = df.copy()
    out["vwap"] = rolling_vwap(out, vwap_window)
    out["band_upper"], out["band_lower"] = vwap_bands(out, out["vwap"], vwap_window, band_sigma)
    out["rsi"] = rsi(out["close"], rsi_period)
    out["adx"] = adx(out, adx_period)
    if trend_ema_period > 0:
        out["ema_trend"] = ema(out["close"], trend_ema_period)
    else:
        out["ema_trend"] = np.nan
    return out
