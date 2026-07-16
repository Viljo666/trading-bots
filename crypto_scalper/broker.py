"""CCXT-based broker wrapper. Defaults to Bybit testnet (USDT perps)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import ccxt
import pandas as pd

from config import EXCHANGE

log = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    side: str          # "long", "short", or "flat"
    qty: float
    entry_price: float
    unrealized_pnl: float


class Broker:
    def __init__(self) -> None:
        cls = getattr(ccxt, EXCHANGE.name)
        opts: dict = {
            "apiKey": EXCHANGE.api_key,
            "secret": EXCHANGE.secret,
            "enableRateLimit": True,
        }
        # binanceusdm is USD-M futures only; setting defaultType breaks it.
        # bybit needs defaultType=swap to route to linear perps.
        if EXCHANGE.name == "bybit":
            opts["options"] = {"defaultType": "swap"}
        self.ex = cls(opts)
        if EXCHANGE.testnet and hasattr(self.ex, "set_sandbox_mode"):
            self.ex.set_sandbox_mode(True)
        self.symbol = EXCHANGE.symbol
        self.ex.load_markets()
        self._set_leverage(EXCHANGE.leverage)

    def _set_leverage(self, lev: int) -> None:
        try:
            self.ex.set_leverage(lev, self.symbol)
        except Exception as e:
            log.warning("set_leverage(%dx, %s) failed: %s", lev, self.symbol, e)

    # ---- Account ----
    def equity_usdt(self) -> float:
        bal = self.ex.fetch_balance()
        # Bybit unified account: USDT total equity sits in 'USDT' total or 'info'.
        usdt = bal.get("USDT", {})
        total = usdt.get("total")
        if total is not None:
            return float(total)
        # Fallback: walk 'info' for unified-account equity.
        info = bal.get("info", {})
        for key in ("totalEquity", "equity", "walletBalance"):
            if key in info:
                try:
                    return float(info[key])
                except (TypeError, ValueError):
                    pass
        return 0.0

    # ---- Market data ----
    def ohlcv(self, timeframe: str = "1m", limit: int = 500) -> pd.DataFrame:
        raw = self.ex.fetch_ohlcv(self.symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts")
        return df

    def best_bid_ask(self) -> tuple[float, float]:
        ob = self.ex.fetch_order_book(self.symbol, limit=5)
        bid = ob["bids"][0][0] if ob["bids"] else 0.0
        ask = ob["asks"][0][0] if ob["asks"] else 0.0
        return float(bid), float(ask)

    # ---- Positions ----
    def position(self) -> Position:
        try:
            positions = self.ex.fetch_positions([self.symbol])
        except Exception as e:
            log.warning("fetch_positions failed: %s", e)
            return Position(self.symbol, "flat", 0.0, 0.0, 0.0)

        for p in positions:
            qty = float(p.get("contracts") or 0.0)
            if qty == 0.0:
                continue
            side_raw = (p.get("side") or "").lower()
            side = "long" if side_raw == "long" else ("short" if side_raw == "short" else "flat")
            return Position(
                symbol=self.symbol,
                side=side,
                qty=qty,
                entry_price=float(p.get("entryPrice") or 0.0),
                unrealized_pnl=float(p.get("unrealizedPnl") or 0.0),
            )
        return Position(self.symbol, "flat", 0.0, 0.0, 0.0)

    # ---- Orders ----
    def market_order(self, side: str, qty: float, reduce_only: bool = False) -> dict:
        params = {"reduceOnly": True} if reduce_only else {}
        order = self.ex.create_order(self.symbol, "market", side, qty, None, params)
        log.info("MKT %s %s qty=%.4f reduce_only=%s", side, self.symbol, qty, reduce_only)
        return order

    def post_only_limit(self, side: str, qty: float, price: float) -> dict | None:
        """Place a post-only limit order (maker). Returns None if rejected for crossing."""
        params = {"postOnly": True}
        try:
            order = self.ex.create_order(self.symbol, "limit", side, qty, price, params)
            log.info("LMT %s %s qty=%.4f px=%.4f post-only", side, self.symbol, qty, price)
            return order
        except ccxt.OrderImmediatelyFillable:
            log.info("post-only %s @ %.4f rejected (would take liquidity)", side, price)
            return None
        except ccxt.InvalidOrder as e:
            log.warning("post-only limit rejected: %s", e)
            return None

    def get_order(self, order_id: str) -> dict | None:
        try:
            return self.ex.fetch_order(order_id, self.symbol)
        except Exception as e:
            log.warning("fetch_order(%s) failed: %s", order_id, e)
            return None

    def cancel_order(self, order_id: str) -> None:
        try:
            self.ex.cancel_order(order_id, self.symbol)
            log.info("Cancelled order %s", order_id)
        except Exception as e:
            log.warning("cancel_order(%s) failed: %s", order_id, e)

    def close_position(self) -> None:
        pos = self.position()
        if pos.side == "flat" or pos.qty == 0:
            return
        close_side = "sell" if pos.side == "long" else "buy"
        self.market_order(close_side, pos.qty, reduce_only=True)

    def cancel_all(self) -> None:
        try:
            self.ex.cancel_all_orders(self.symbol)
        except Exception as e:
            log.warning("cancel_all_orders failed: %s", e)

    # ---- Throttle helper ----
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
