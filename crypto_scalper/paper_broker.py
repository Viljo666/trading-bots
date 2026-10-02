"""Paper-trading broker: real Binance mainnet data, simulated fills.

Implements the same interface as broker.Broker so live.py can use either
based on EXCHANGE.mode. Public data only — no API keys required.

Fill model:
    market order      → fills immediately at best ask (buy) / best bid (sell), taker fee
    post-only limit   → rejected if it would cross the spread; otherwise sits as a
                        working order. Fills the next time the opposite side of the
                        book touches our price (best ask <= limit for buys, etc.),
                        at the limit price, with a maker fee.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt
import pandas as pd

from config import EXCHANGE, JOURNAL_DIR, PAPER_STATE_FILE, RISK
from market_data import OhlcvCache

log = logging.getLogger(__name__)

TAKER_FEE_BPS = 5.0
MAKER_FEE_BPS = 2.0


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry_price: float
    unrealized_pnl: float


class PaperBroker:
    def __init__(self) -> None:
        # Public-only mainnet client; no auth needed.
        cls = getattr(ccxt, "binanceusdm")
        self.ex = cls({"enableRateLimit": True})
        self.symbol = EXCHANGE.symbol
        self.ex.load_markets()
        self._ohlcv_caches: dict[str, OhlcvCache] = {}

        self._cash = RISK.account_size_usdt
        self._side = "flat"           # "long" / "short" / "flat"
        self._qty = 0.0
        self._entry_price = 0.0
        self._next_id = 1
        self._open_orders: dict[str, dict] = {}
        self._last_bid = 0.0
        self._last_ask = 0.0
        self._trial_started_at: str | None = None
        self._journal_path = JOURNAL_DIR / f"paper_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        self._restore_state()
        log.info("PaperBroker initialised: equity=%.2f, journal=%s", self._cash, self._journal_path)

    def _restore_state(self) -> None:
        """Resume the simulated account after a bot or machine restart."""
        try:
            if PAPER_STATE_FILE.exists():
                with PAPER_STATE_FILE.open() as f:
                    state = json.load(f)
                self._cash = float(state["cash"])
                self._side = state.get("side", "flat")
                self._qty = float(state.get("qty", 0.0))
                self._entry_price = float(state.get("entry_price", 0.0))
                self._next_id = int(state.get("next_id", 1))
                self._open_orders = state.get("open_orders", {})
                self._trial_started_at = state.get("trial_started_at") or datetime.now(timezone.utc).isoformat()
                if "trial_started_at" not in state:
                    self._save_state()
                log.info("Restored paper account state (%s, cash=%.2f)", self._side, self._cash)
                return
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            log.warning("Could not restore paper account state: %s", e)

        # First run after this upgrade: continue from the most recent paper
        # journal balance instead of silently resetting the account to $1,000.
        latest: dict | None = None
        for path in JOURNAL_DIR.glob("paper_*.jsonl"):
            try:
                for line in path.read_text().splitlines():
                    event = json.loads(line)
                    if event.get("ts") and (latest is None or event["ts"] > latest.get("ts", "")):
                        latest = event
            except (OSError, json.JSONDecodeError):
                continue
        if latest and "cash" in latest:
            self._cash = float(latest["cash"])
            # The final journal event is authoritative for position state.
            if latest.get("event") == "open":
                self._side = str(latest.get("side", "flat"))
                self._qty = float(latest.get("qty", 0.0))
                self._entry_price = float(latest.get("price", 0.0))
            log.info("Bootstrapped paper account from journal (cash=%.2f)", self._cash)
        self._trial_started_at = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def _save_state(self) -> None:
        state = {
            "version": 1,
            "cash": self._cash,
            "side": self._side,
            "qty": self._qty,
            "entry_price": self._entry_price,
            "next_id": self._next_id,
            "open_orders": self._open_orders,
            "trial_started_at": self._trial_started_at,
        }
        tmp = PAPER_STATE_FILE.with_suffix(".tmp")
        try:
            with tmp.open("w") as f:
                json.dump(state, f)
            tmp.replace(PAPER_STATE_FILE)
        except OSError as e:
            log.warning("Could not save paper account state: %s", e)

    # ---- internals ----
    def _gen_id(self) -> str:
        oid = str(self._next_id)
        self._next_id += 1
        return oid

    def _journal(self, event: dict) -> None:
        event["ts"] = datetime.now(timezone.utc).isoformat()
        with self._journal_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    def _refresh_book(self) -> tuple[float, float]:
        ob = self.ex.fetch_order_book(self.symbol, limit=5)
        bid = float(ob["bids"][0][0]) if ob["bids"] else 0.0
        ask = float(ob["asks"][0][0]) if ob["asks"] else 0.0
        self._last_bid, self._last_ask = bid, ask
        return bid, ask

    def _apply_fill(self, side: str, qty: float, price: float, fee_bps: float, reduce_only: bool) -> None:
        notional = qty * price
        fee = notional * fee_bps / 10_000.0

        if reduce_only or self._side != "flat":
            # Closing or flipping. Compute realized PnL on the closed portion.
            close_qty = min(qty, self._qty)
            if self._side == "long":
                pnl = (price - self._entry_price) * close_qty
            elif self._side == "short":
                pnl = (self._entry_price - price) * close_qty
            else:
                pnl = 0.0
            self._cash += pnl - fee
            self._qty -= close_qty
            if self._qty <= 1e-9:
                self._side = "flat"
                self._qty = 0.0
                self._entry_price = 0.0
            self._journal({"event": "close", "side": side, "qty": close_qty, "price": price,
                           "fee": fee, "pnl": pnl, "cash": self._cash})
            log.info("[paper] CLOSE %s qty=%.4f @ %.4f pnl=%+.2f fee=%.2f cash=%.2f",
                     side, close_qty, price, pnl, fee, self._cash)
            # If the order was bigger than the position (flip case), open the remainder.
            remainder = qty - close_qty
            if remainder > 1e-9 and not reduce_only:
                self._open_position("buy" if side == "buy" else "sell", remainder, price, fee_bps=fee_bps)
        else:
            self._open_position(side, qty, price, fee_bps=fee_bps)
        self._save_state()

    def _open_position(self, side: str, qty: float, price: float, fee_bps: float) -> None:
        fee = qty * price * fee_bps / 10_000.0
        self._cash -= fee
        self._side = "long" if side == "buy" else "short"
        self._qty = qty
        self._entry_price = price
        self._journal({"event": "open", "side": self._side, "qty": qty, "price": price,
                       "fee": fee, "cash": self._cash})
        log.info("[paper] OPEN %s qty=%.4f @ %.4f fee=%.2f cash=%.2f",
                 self._side, qty, price, fee, self._cash)

    def _maybe_fill_open_orders(self) -> None:
        if not self._open_orders:
            return
        bid, ask = self._refresh_book()
        for oid, o in list(self._open_orders.items()):
            if o["status"] != "open":
                continue
            side, price, qty = o["side"], o["price"], o["qty"]
            filled = False
            if side == "buy" and ask <= price:
                filled = True
            elif side == "sell" and bid >= price:
                filled = True
            if filled:
                o["status"] = "closed"
                o["filled"] = qty
                o["average"] = price
                self._apply_fill(side, qty, price, MAKER_FEE_BPS, reduce_only=False)

    # ---- public interface (mirrors Broker) ----
    def equity_usdt(self) -> float:
        if self._side == "flat":
            return self._cash
        bid, ask = self._refresh_book()
        mark = (bid + ask) / 2.0 if bid and ask else self._entry_price
        if self._side == "long":
            upnl = (mark - self._entry_price) * self._qty
        else:
            upnl = (self._entry_price - mark) * self._qty
        return self._cash + upnl

    def ohlcv(self, timeframe: str = "1m", limit: int = 500) -> pd.DataFrame:
        if timeframe not in self._ohlcv_caches:
            self._ohlcv_caches[timeframe] = OhlcvCache(self.ex, self.symbol, timeframe)
        return self._ohlcv_caches[timeframe].get(limit)

    def best_bid_ask(self) -> tuple[float, float]:
        return self._refresh_book()

    def position(self) -> Position:
        bid, ask = self._last_bid, self._last_ask
        if not bid or not ask:
            bid, ask = self._refresh_book()
        mark = (bid + ask) / 2.0 if bid and ask else self._entry_price
        upnl = 0.0
        if self._side == "long":
            upnl = (mark - self._entry_price) * self._qty
        elif self._side == "short":
            upnl = (self._entry_price - mark) * self._qty
        return Position(self.symbol, self._side, self._qty, self._entry_price, upnl)

    def market_order(self, side: str, qty: float, reduce_only: bool = False) -> dict:
        bid, ask = self._refresh_book()
        price = ask if side == "buy" else bid
        self._apply_fill(side, qty, price, TAKER_FEE_BPS, reduce_only)
        oid = self._gen_id()
        self._save_state()
        return {"id": oid, "status": "closed", "filled": qty, "average": price, "price": price}

    def post_only_limit(self, side: str, qty: float, price: float) -> dict | None:
        bid, ask = self._refresh_book()
        if side == "buy" and price >= ask:
            log.info("[paper] post-only buy @ %.4f rejected (>= ask %.4f)", price, ask)
            return None
        if side == "sell" and price <= bid:
            log.info("[paper] post-only sell @ %.4f rejected (<= bid %.4f)", price, bid)
            return None
        oid = self._gen_id()
        order = {"id": oid, "status": "open", "side": side, "qty": qty, "price": price,
                 "filled": 0.0, "average": None}
        self._open_orders[oid] = order
        self._save_state()
        log.info("[paper] LMT %s qty=%.4f @ %.4f post-only id=%s", side, qty, price, oid)
        return order

    def get_order(self, order_id: str) -> dict | None:
        self._maybe_fill_open_orders()
        return self._open_orders.get(order_id)

    def cancel_order(self, order_id: str) -> None:
        o = self._open_orders.get(order_id)
        if o and o["status"] == "open":
            o["status"] = "canceled"
            self._save_state()
            log.info("[paper] cancelled order %s", order_id)

    def cancel_all(self) -> None:
        for o in self._open_orders.values():
            if o["status"] == "open":
                o["status"] = "canceled"
        self._save_state()

    def close_position(self) -> None:
        if self._side == "flat" or self._qty == 0:
            return
        side = "sell" if self._side == "long" else "buy"
        self.market_order(side, self._qty, reduce_only=True)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
