"""Manual end-to-end pipeline test.

Submits 1 share of SPY as a bracket order so you can see:
- Telegram 'order submitted' alert
- Telegram 'order filled' alert
- Position appearing in Alpaca
- Telegram 'position closed' alert when stop/target hits

Usage:
    python test_trade.py            # submit the test trade
    python test_trade.py --close    # close any open SPY position immediately
"""
from __future__ import annotations

import sys

import notify
from broker import Broker


def _tight_bracket(price: float) -> tuple[float, float]:
    """Return (stop, target) tight enough to fill quickly in a normal session."""
    stop = round(price * 0.995, 2)    # 0.5% stop
    target = round(price * 1.0025, 2)  # 0.25% target
    return stop, target


def main() -> int:
    broker = Broker()
    clock = broker.trading.get_clock()

    if "--close" in sys.argv:
        print("Closing all SPY positions...")
        try:
            broker.close_position("SPY")
            print("Close order submitted.")
        except Exception as e:
            print(f"No position to close, or error: {e}")
        return 0

    if not clock.is_open:
        print(f"Market is CLOSED. Next open: {clock.next_open}")
        print("Order would queue and fill at next open. Continue? [y/N] ", end="")
        if input().strip().lower() != "y":
            return 0

    try:
        bid, ask = broker.latest_quote("SPY")
        price = (bid + ask) / 2.0
    except Exception:
        price = 500.0  # fallback if quote fails
    stop, target = _tight_bracket(price)

    print(f"SPY @ ~${price:.2f}")
    print(f"Bracket: entry=MKT, stop=${stop}, target=${target}")
    print("Submitting 1 share...")

    order = broker.submit_bracket(
        symbol="SPY", qty=1, side="buy",
        entry_price=None, stop_price=stop, take_profit_price=target,
    )
    print(f"Submitted order {order.id}")

    notify.order_submitted(
        setup="test", side="buy", symbol="SPY", qty=1,
        entry=price, stop=stop, target=target,
        r_mult=(target - price) / (price - stop) if price > stop else 0.0,
        reason="manual pipeline test",
    )
    print("Telegram alert sent. Check your phone.")
    print("The bracket will close at stop or target. Run `python test_trade.py --close` to exit early.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
