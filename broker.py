"""Alpaca broker wrapper: account, data, orders."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd
import pytz
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import AssetClass, AssetStatus, OrderClass

from config import ALPACA

log = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float
    side: str


class Broker:
    def __init__(self) -> None:
        self.trading = TradingClient(ALPACA.api_key, ALPACA.secret_key, paper=ALPACA.paper)
        self.data = StockHistoricalDataClient(ALPACA.api_key, ALPACA.secret_key)

    # ---- Account ----
    def get_account(self):
        return self.trading.get_account()

    def equity(self) -> float:
        return float(self.get_account().equity)

    def buying_power(self) -> float:
        return float(self.get_account().buying_power)

    def is_market_open(self) -> bool:
        return self.trading.get_clock().is_open

    def next_open_close(self) -> tuple[datetime, datetime]:
        clock = self.trading.get_clock()
        return clock.next_open, clock.next_close

    # ---- Universe ----
    def tradable_us_equities(self) -> list[str]:
        req = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        assets = self.trading.get_all_assets(req)
        return [
            a.symbol
            for a in assets
            if a.tradable and a.fractionable and a.shortable and "/" not in a.symbol
        ]

    # ---- Positions / orders ----
    def positions(self) -> list[Position]:
        raw = self.trading.get_all_positions()
        out: list[Position] = []
        for p in raw:
            out.append(
                Position(
                    symbol=p.symbol,
                    qty=float(p.qty),
                    avg_entry_price=float(p.avg_entry_price),
                    market_value=float(p.market_value),
                    unrealized_pl=float(p.unrealized_pl),
                    unrealized_plpc=float(p.unrealized_plpc),
                    side=p.side.value if hasattr(p.side, "value") else str(p.side),
                )
            )
        return out

    def has_position(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions())

    def close_position(self, symbol: str) -> None:
        try:
            self.trading.close_position(symbol)
            log.info("Closed position %s", symbol)
        except Exception as e:
            log.warning("close_position(%s) failed: %s", symbol, e)

    def close_all_positions(self) -> None:
        try:
            self.trading.close_all_positions(cancel_orders=True)
            log.info("Flattened all positions")
        except Exception as e:
            log.warning("close_all_positions failed: %s", e)

    def cancel_all_orders(self) -> None:
        try:
            self.trading.cancel_orders()
        except Exception as e:
            log.warning("cancel_orders failed: %s", e)

    def submit_bracket(
        self,
        symbol: str,
        qty: int,
        side: str,
        entry_price: float | None,
        stop_price: float,
        take_profit_price: float,
        atr: float | None = None,
    ):
        """Submit a bracket order. entry_price=None → market entry.

        atr: the signal's ATR value, used to detect stale signals. If the
        current market price has moved more than 1 ATR from the signal's
        entry_price, the trade is skipped (returns None) rather than submitting
        with a wildly-clamped stop. Pass None to skip the staleness check.
        """
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        # --- Quote pre-check ---
        # 1. Stale-signal guard: if price has moved > 1 ATR from signal entry,
        #    the setup is no longer valid — skip rather than widen the stop.
        # 2. Stop-clamp guard: fix Alpaca error 42210000 (stop within $0.01 of
        #    market), but only allow tiny adjustments (≤ 0.5 ATR or $0.05 max).
        #    If a larger clamp is needed, the market has moved too far — skip.
        try:
            bid, ask = self.latest_quote(symbol)
            mid = (bid + ask) / 2.0

            # Stale signal check
            if atr is not None and entry_price is not None and atr > 0:
                drift = abs(mid - entry_price)
                if drift > atr:
                    log.warning(
                        "submit_bracket %s %s: STALE SIGNAL — market %.2f moved %.2f "
                        "from entry %.2f (> 1 ATR=%.2f). Skipping.",
                        symbol, side, mid, drift, entry_price, atr,
                    )
                    return None

            # Stop clamp (small adjustments only)
            clamp_limit = (atr * 0.5) if (atr and atr > 0) else 0.05
            if side.lower() == "buy":
                max_stop = round(ask - 0.01, 2)
                if stop_price > max_stop:
                    adjustment = stop_price - max_stop
                    if adjustment > clamp_limit:
                        log.warning(
                            "submit_bracket %s buy: stop %.2f needs clamp of %.2f "
                            "(limit %.2f, ask=%.2f). Skipping stale trade.",
                            symbol, stop_price, adjustment, clamp_limit, ask,
                        )
                        return None
                    log.warning("submit_bracket %s buy: stop %.2f clamped -> %.2f (ask=%.2f)",
                                symbol, stop_price, max_stop, ask)
                    stop_price = max_stop
            else:
                min_stop = round(ask + 0.01, 2)
                if stop_price < min_stop:
                    adjustment = min_stop - stop_price
                    if adjustment > clamp_limit:
                        log.warning(
                            "submit_bracket %s sell: stop %.2f needs clamp of %.2f "
                            "(limit %.2f, ask=%.2f). Skipping stale trade.",
                            symbol, stop_price, adjustment, clamp_limit, ask,
                        )
                        return None
                    log.warning("submit_bracket %s sell: stop %.2f clamped -> %.2f (ask=%.2f)",
                                symbol, stop_price, min_stop, ask)
                    stop_price = min_stop
        except Exception as e:
            log.warning("submit_bracket %s: quote pre-check failed (%s), proceeding raw", symbol, e)

        stop_loss = StopLossRequest(stop_price=round(stop_price, 2))
        take_profit = TakeProfitRequest(limit_price=round(take_profit_price, 2))

        if entry_price is None:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        else:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                limit_price=round(entry_price, 2),
                order_class=OrderClass.BRACKET,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

        order = self.trading.submit_order(req)
        log.info(
            "Submitted %s %s x%d stop=%.2f tp=%.2f (entry=%s)",
            side, symbol, qty, stop_price, take_profit_price,
            f"{entry_price:.2f}" if entry_price else "MKT",
        )
        return order

    # ---- Market data ----
    def bars(
        self,
        symbols: Iterable[str],
        timeframe: str = "15Min",
        lookback_days: int = 10,
    ) -> dict[str, pd.DataFrame]:
        tf = _parse_timeframe(timeframe)
        end = datetime.now(pytz.UTC) - timedelta(minutes=16)  # free-tier 15-min delay
        start = end - timedelta(days=lookback_days)
        syms = list(symbols)
        if not syms:
            return {}

        req = StockBarsRequest(symbol_or_symbols=syms, timeframe=tf, start=start, end=end)
        bars = self.data.get_stock_bars(req)
        df = bars.df
        if df is None or df.empty:
            return {}

        out: dict[str, pd.DataFrame] = {}
        for sym in syms:
            if sym in df.index.get_level_values(0):
                sub = df.xs(sym, level=0).copy()
                sub.index = pd.to_datetime(sub.index)
                out[sym] = sub
        return out

    def daily_bars(self, symbols: Iterable[str], lookback_days: int = 30) -> dict[str, pd.DataFrame]:
        return self.bars(symbols, timeframe="1Day", lookback_days=lookback_days)

    def get_open_stop_order(self, symbol: str):
        """Return the open stop-loss child order for `symbol`, or None.

        After a bracket parent fills, the stop and take-profit children become
        active (status OPEN). We use this to look up the stop's order id so we
        can replace its stop_price (e.g. moving to break-even).
        """
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        for o in self.trading.get_orders(filter=req):
            otype = o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type)
            if otype in ("stop", "stop_limit"):
                return o
        return None

    def replace_stop_price(self, order_id: str, new_stop: float):
        """Replace the stop_price on an existing stop order. Preserves bracket linkage."""
        req = ReplaceOrderRequest(stop_price=round(new_stop, 2))
        return self.trading.replace_order_by_id(order_id, req)

    def latest_quote(self, symbol: str) -> tuple[float, float]:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        q = self.data.get_stock_latest_quote(req)[symbol]
        return float(q.bid_price), float(q.ask_price)


def _parse_timeframe(tf: str) -> TimeFrame:
    tf = tf.strip()
    mapping = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "30Min": TimeFrame(30, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1Day": TimeFrame(1, TimeFrameUnit.Day),
    }
    if tf not in mapping:
        raise ValueError(f"Unsupported timeframe: {tf}")
    return mapping[tf]
