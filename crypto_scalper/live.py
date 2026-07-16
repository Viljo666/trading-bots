"""Live runner for the crypto scalper. Binance USD-M futures testnet by default.

State machine:
    IDLE          → no position, no working order. On signal, place post-only limit at
                    best bid/ask and transition to PENDING.
    PENDING       → maker order working. Poll for fill; cancel if not filled within
                    STRATEGY.order_fill_timeout_seconds, returning to IDLE.
    IN_POSITION   → on exit signal (VWAP target / stop / time-stop), market-close.

Exits use market orders (taker) — speed > fee on the way out.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone

import notify
from config import EXCHANGE, RISK, SESSION, STRATEGY, validate
from indicators import add_indicators
from risk import daily_loss_breached, size_trade
from strategy import latest_signal, should_exit

log = logging.getLogger("crypto_scalper")


def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stdout)


def make_broker():
    if EXCHANGE.mode == "paper":
        from paper_broker import PaperBroker
        return PaperBroker()
    from broker import Broker
    return Broker()


class Runner:
    STATE_IDLE = "IDLE"
    STATE_PENDING = "PENDING"
    STATE_IN_POSITION = "IN_POSITION"

    def __init__(self) -> None:
        self.broker = make_broker()
        self.starting_equity = self.broker.equity_usdt()
        self.last_pnl_reset_day = datetime.now(timezone.utc).day
        self.state = self.STATE_IDLE
        self.pending_order_id: str | None = None
        self.pending_since: float = 0.0
        self.entry_price: float | None = None
        self.entry_qty: float = 0.0
        self.entry_bar_ts = None
        self.intended_side: str | None = None
        # Cooldown: after exiting, don't re-enter same direction for cooldown_bars.
        self.last_exit_side: str | None = None
        self.cooldown_until: float = 0.0
        self.stopped = False

    # ---- Helpers ----
    def _maybe_reset_daily_pnl(self) -> None:
        now = datetime.now(timezone.utc)
        if now.day != self.last_pnl_reset_day and now.hour >= SESSION.pnl_reset_hour_utc:
            self.starting_equity = self.broker.equity_usdt()
            self.last_pnl_reset_day = now.day
            log.info("Daily PnL reset: starting equity = %.2f USDT", self.starting_equity)

    def _spread_bps(self, bid: float, ask: float) -> float:
        if bid <= 0 or ask <= 0:
            return float("inf")
        mid = (bid + ask) / 2.0
        return (ask - bid) / mid * 10_000.0

    def _timeframe_seconds(self) -> int:
        tf = EXCHANGE.timeframe
        unit = tf[-1]
        n = int(tf[:-1])
        if unit == "m": return n * 60
        if unit == "h": return n * 3600
        if unit == "d": return n * 86400
        return 60

    # ---- IDLE: look for signal, place maker entry ----
    def _try_place_maker_entry(self) -> None:
        df = self.broker.ohlcv(EXCHANGE.timeframe, limit=max(500, STRATEGY.vwap_window + 50))
        sig = latest_signal(df)
        if sig.side == "none":
            return

        # Cooldown: don't re-enter same direction within cooldown_bars of last exit.
        now_ts = time.time()
        if sig.side == self.last_exit_side and now_ts < self.cooldown_until:
            remaining = int(self.cooldown_until - now_ts)
            log.info("Skip: cooldown active for %s (%ds remaining)", sig.side, remaining)
            return

        bid, ask = self.broker.best_bid_ask()
        spread_bps = self._spread_bps(bid, ask)
        if spread_bps > STRATEGY.max_spread_bps:
            log.info("Skip: spread %.2f bps > max %.2f", spread_bps, STRATEGY.max_spread_bps)
            return

        # Maker entry price: sit on the inside of the book on our side.
        limit_price = bid if sig.side == "long" else ask
        order_side = "buy" if sig.side == "long" else "sell"

        equity = self.broker.equity_usdt()
        sized = size_trade(sig.side, limit_price, sig.stop, sig.target, equity)
        if sized is None:
            log.info("Skip: sizing returned None (limit=%.4f stop=%.4f)", limit_price, sig.stop)
            return

        order = self.broker.post_only_limit(order_side, sized.qty, limit_price)
        if order is None:
            return  # rejected for crossing — try again next loop

        self.state = self.STATE_PENDING
        self.pending_order_id = str(order.get("id"))
        self.pending_since = time.time()
        self.intended_side = sig.side
        self.entry_price = limit_price
        self.entry_qty = sized.qty
        log.info(
            "PENDING %s qty=%.4f px=%.4f stop=%.4f target=%.4f reason=%s",
            sig.side, sized.qty, limit_price, sig.stop, sig.target, sig.reason,
        )

    # ---- PENDING: poll fill or cancel on timeout ----
    def _poll_pending(self) -> None:
        if self.pending_order_id is None:
            self.state = self.STATE_IDLE
            return

        order = self.broker.get_order(self.pending_order_id)
        if order is None:
            # Couldn't fetch — assume it's gone.
            self._reset_to_idle("fetch failed")
            return

        status = (order.get("status") or "").lower()
        filled = float(order.get("filled") or 0.0)
        if status in ("closed", "filled") or filled > 0:
            self.entry_price = float(order.get("average") or order.get("price") or self.entry_price or 0.0)
            self.entry_qty = filled or self.entry_qty
            self.entry_bar_ts = datetime.now(timezone.utc)
            self.state = self.STATE_IN_POSITION
            log.info("FILLED %s @ %.4f qty=%.4f", self.intended_side, self.entry_price, filled)
            notify.entry_filled(EXCHANGE.symbol, self.intended_side or "", self.entry_qty, self.entry_price)
            self.pending_order_id = None
            return

        if status in ("canceled", "cancelled", "rejected", "expired"):
            self._reset_to_idle(f"order {status}")
            return

        age = time.time() - self.pending_since
        if age >= STRATEGY.order_fill_timeout_seconds:
            log.info("Timeout: cancelling unfilled %s after %.0fs", self.pending_order_id, age)
            self.broker.cancel_order(self.pending_order_id)
            self._reset_to_idle("fill timeout")

    def _reset_to_idle(self, reason: str) -> None:
        log.info("→ IDLE (%s)", reason)
        self.state = self.STATE_IDLE
        self.pending_order_id = None
        self.intended_side = None
        self.entry_price = None
        self.entry_bar_ts = None

    # ---- IN_POSITION: check exit ----
    def _try_exit(self) -> None:
        pos = self.broker.position()
        if pos.side == "flat" or pos.qty == 0:
            self._reset_to_idle("position vanished")
            return

        df = self.broker.ohlcv(EXCHANGE.timeframe, limit=max(500, STRATEGY.vwap_window + 50))
        enriched = add_indicators(
            df, STRATEGY.vwap_window, STRATEGY.band_sigma, STRATEGY.rsi_period, STRATEGY.adx_period
        )
        bar = enriched.iloc[-1]
        current = float(bar["close"])
        vwap = float(bar["vwap"])

        bars_in_trade = 0
        if self.entry_bar_ts is not None:
            bars_in_trade = max(0, len(df.loc[self.entry_bar_ts:]) - 1)

        entry = self.entry_price if self.entry_price is not None else pos.entry_price
        do_exit, why = should_exit(pos.side, entry, current, vwap, bars_in_trade)
        if do_exit:
            log.info("EXIT %s qty=%.4f current=%.4f vwap=%.4f reason=%s", pos.side, pos.qty, current, vwap, why)
            equity_before = self.broker.equity_usdt()
            side_at_exit = pos.side
            qty_at_exit = pos.qty
            self.broker.close_position()
            equity_after = self.broker.equity_usdt()
            pnl = equity_after - equity_before
            notify.position_closed(
                EXCHANGE.symbol, side_at_exit, qty_at_exit, entry, current, pnl, equity_after, why,
            )
            # Arm cooldown so we don't immediately re-enter same direction.
            self.last_exit_side = side_at_exit
            self.cooldown_until = time.time() + STRATEGY.cooldown_bars * self._timeframe_seconds()
            log.info("Cooldown armed: no %s entries for %d seconds",
                     side_at_exit, STRATEGY.cooldown_bars * self._timeframe_seconds())
            self._reset_to_idle(f"closed: {why}")

    # ---- Main loop ----
    def run(self) -> None:
        params_str = (
            f"vwap={STRATEGY.vwap_window} sigma={STRATEGY.band_sigma:.1f} "
            f"rsi={STRATEGY.rsi_long_max:.0f}/{STRATEGY.rsi_short_min:.0f} "
            f"adx<{STRATEGY.adx_max:.0f} stop={STRATEGY.stop_pct*100:.2f}% "
            f"timestop={STRATEGY.time_stop_bars}b"
        )
        log.info(
            "Starting crypto_scalper exchange=%s testnet=%s symbol=%s tf=%s equity=%.2f USDT",
            EXCHANGE.name, EXCHANGE.testnet, EXCHANGE.symbol, EXCHANGE.timeframe, self.starting_equity,
        )
        log.info("Strategy: %s", params_str)
        notify.startup(EXCHANGE.mode, EXCHANGE.symbol, EXCHANGE.timeframe, self.starting_equity, params_str)
        signal.signal(signal.SIGINT, lambda *_: self._shutdown())
        signal.signal(signal.SIGTERM, lambda *_: self._shutdown())

        while not self.stopped:
            try:
                self._maybe_reset_daily_pnl()

                equity = self.broker.equity_usdt()
                if daily_loss_breached(self.starting_equity, equity):
                    log.warning("Daily loss limit hit. Closing & halting.")
                    if self.pending_order_id:
                        self.broker.cancel_order(self.pending_order_id)
                    self.broker.close_position()
                    notify.daily_loss_limit(self.broker.equity_usdt())
                    break

                if self.state == self.STATE_IDLE:
                    self._try_place_maker_entry()
                elif self.state == self.STATE_PENDING:
                    self._poll_pending()
                elif self.state == self.STATE_IN_POSITION:
                    self._try_exit()

            except Exception as e:
                log.exception("loop error: %s", e)
                notify.error(f"loop error: {e}")

            self.broker.sleep(SESSION.poll_interval_seconds)

    def _shutdown(self) -> None:
        log.info("Shutdown requested.")
        self.stopped = True
        try:
            if self.pending_order_id:
                self.broker.cancel_order(self.pending_order_id)
            self.broker.close_position()
            notify.shutdown(self.broker.equity_usdt())
        except Exception as e:
            log.warning("close on shutdown failed: %s", e)


def main() -> None:
    setup_logging()
    validate()
    Runner().run()


if __name__ == "__main__":
    main()
