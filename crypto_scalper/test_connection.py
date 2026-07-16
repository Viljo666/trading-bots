"""Sanity test: connect to Binance USD-M testnet, verify balance/markets/data
and dry-run the latest signal. Does NOT place orders.
"""
from __future__ import annotations

import logging
import sys

from config import EXCHANGE, STRATEGY, validate
from live import make_broker
from strategy import latest_signal

log = logging.getLogger("test_conn")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    validate()

    log.info("Connecting: mode=%s exchange=%s symbol=%s", EXCHANGE.mode, EXCHANGE.name, EXCHANGE.symbol)
    b = make_broker()

    eq = b.equity_usdt()
    log.info("Equity: %.2f USDT", eq)

    bid, ask = b.best_bid_ask()
    log.info("Top of book: bid=%.4f ask=%.4f spread=%.2f bps", bid, ask, (ask - bid) / ((ask + bid) / 2) * 10_000)

    df = b.ohlcv(EXCHANGE.timeframe, limit=max(500, STRATEGY.vwap_window + 50))
    log.info("OHLCV: %d bars from %s to %s last_close=%.4f",
             len(df), df.index[0], df.index[-1], float(df["close"].iloc[-1]))

    sig = latest_signal(df)
    log.info("Latest signal: side=%s reason=%s", sig.side, sig.reason)
    if sig.side != "none":
        log.info("  entry≈%.4f stop=%.4f target=%.4f", sig.entry, sig.stop, sig.target)

    pos = b.position()
    log.info("Position: side=%s qty=%.4f entry=%.4f upnl=%.2f", pos.side, pos.qty, pos.entry_price, pos.unrealized_pnl)

    log.info("OK — connection verified. Ready to run live.py.")


if __name__ == "__main__":
    main()
