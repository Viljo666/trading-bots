"""Session orchestrator: runs the bot through the trading day."""
from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import time
from datetime import datetime, time as dtime

import pandas as pd
import pytz

import config
import journal
import notify
import regime as regime_mod
import strategy
from broker import Broker
from risk import can_open_new_position, size_trade
from scanner import intraday_refresh, premarket_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")

TZ = pytz.timezone(config.SESSION.timezone)
_shutdown = False


def _handle_sigint(signum, frame):
    global _shutdown
    log.info("Shutdown signal received, will exit after current loop.")
    _shutdown = True


signal.signal(signal.SIGINT, _handle_sigint)
signal.signal(signal.SIGTERM, _handle_sigint)


_LOCKFILE = config.ROOT / "bot.lock"


def _acquire_singleton_lock() -> bool:
    """Refuse to start if another live instance is already running.

    Prevents the launchd-start + manual-start collision that produced duplicate
    orders. Writes our PID; if a stale lock points at a dead PID, we take over.
    Returns True if the lock was acquired, False if another instance holds it.
    """
    if _LOCKFILE.exists():
        try:
            old_pid = int(_LOCKFILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and old_pid != os.getpid():
            try:
                os.kill(old_pid, 0)  # signal 0 = liveness probe, doesn't kill
            except OSError:
                log.warning("Stale lock from dead PID %s — taking over.", old_pid)
            else:
                log.error("Another bot instance is running (PID %s). Exiting.", old_pid)
                return False
    _LOCKFILE.write_text(str(os.getpid()))
    atexit.register(_release_singleton_lock)
    return True


def _release_singleton_lock() -> None:
    try:
        if _LOCKFILE.exists() and _LOCKFILE.read_text().strip() == str(os.getpid()):
            _LOCKFILE.unlink()
    except OSError:
        pass


def _now_et() -> datetime:
    return datetime.now(TZ)


def _parse_time(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def run() -> None:
    config.validate()
    if not _acquire_singleton_lock():
        sys.exit(0)
    broker = Broker()

    account = broker.get_account()
    starting_equity = float(account.equity)
    log.info("Session start. Account equity=$%.2f (paper=%s)", starting_equity, config.ALPACA.paper)
    notify.startup(starting_equity, config.ALPACA.paper)

    premarket: list = []
    shortlist: list = []
    last_scan = 0.0
    premarket_done = False
    flattened = False
    last_regime_label: str | None = None
    notified_fill_ids: set[str] = set()
    known_positions: dict[str, float] = {}  # symbol -> last unrealized P&L $
    be_state: dict[str, dict] = {}           # symbol -> {original_stop, stop_order_id, armed}
    symbol_cooldown: dict[str, datetime] = {}  # symbol -> earliest re-entry time
    loss_limit_notified = False
    spy_prior_close = _fetch_spy_prior_close(broker)
    log.info("SPY prior close (for gap override): %s",
             f"${spy_prior_close:.2f}" if spy_prior_close else "unavailable")

    scan_t = _parse_time(config.SESSION.premarket_scan_time)
    open_t = _parse_time(config.SESSION.market_open)
    close_t = _parse_time(config.SESSION.market_close)
    flatten_t = _parse_time(config.SESSION.flatten_time)
    convert_t = _parse_time(config.STRATEGY.swing_convert_time)

    while not _shutdown:
        now = _now_et()
        t = now.time()

        # Pre-market scan (or mid-session fallback if bot was started after open)
        if not premarket_done and (scan_t <= t < open_t or open_t <= t < flatten_t):
            try:
                mid_session = t >= open_t
                if mid_session:
                    log.info("Bot started mid-session — running pre-market scan now.")
                premarket = premarket_scan(broker)
                premarket_done = True
            except Exception as e:
                log.exception("premarket_scan failed: %s", e)

        # After close — write journal and stop
        if t >= close_t:
            if not flattened:
                broker.cancel_all_orders()
            try:
                journal.write_daily_report(broker, starting_equity)
            except Exception as e:
                log.exception("journal failed: %s", e)
            _notify_session_end(broker, starting_equity, notified_fill_ids)
            log.info("Session end.")
            break

        # Flatten day trades near the close (unless swing-converted above)
        if not flattened and t >= flatten_t:
            _end_of_day_flatten(broker, convert_t_passed=t >= convert_t)
            flattened = True

        # Intraday: scan + evaluate signals
        if open_t <= t < flatten_t and premarket_done:
            if time.time() - last_scan >= config.SESSION.scan_interval_seconds:
                last_scan = time.time()
                try:
                    current_regime = _detect_regime(broker, prior_close=spy_prior_close)
                    log.info("Regime: %s (SPY %.2f vs VWAP %.2f, dev %+.2f%%)",
                             current_regime.label, current_regime.spy_price,
                             current_regime.spy_vwap, current_regime.deviation_pct)
                    if current_regime.label != last_regime_label and current_regime.label != "neutral":
                        notify.regime_change(current_regime.label, current_regime.deviation_pct)
                    last_regime_label = current_regime.label

                    shortlist = intraday_refresh(broker, premarket)
                    # Hybrid universe: ORB uses the high-vol top-N, reversal uses
                    # the full pre-market shortlist (stable names with clean levels).
                    _evaluate_and_trade(broker, shortlist, premarket, starting_equity, current_regime, symbol_cooldown)

                    _poll_fills(broker, notified_fill_ids)
                    _poll_position_closes(broker, known_positions, symbol_cooldown)
                    _arm_break_evens(broker, be_state)

                    if not loss_limit_notified:
                        ok, reason = can_open_new_position(
                            len(broker.positions()), starting_equity, broker.equity()
                        )
                        if not ok and "daily loss" in reason:
                            notify.daily_loss_limit()
                            loss_limit_notified = True
                except Exception as e:
                    log.exception("intraday loop failed: %s", e)

        time.sleep(5)


def _detect_regime(broker: Broker, prior_close: float | None = None) -> regime_mod.Regime:
    """Fetch SPY current-session bars and classify regime."""
    bars = broker.bars([config.REGIME.spy_symbol], timeframe=config.SESSION.bar_timeframe, lookback_days=2)
    spy_df = bars.get(config.REGIME.spy_symbol)
    if spy_df is None or spy_df.empty:
        return regime_mod.Regime(label="neutral", spy_price=0.0, spy_vwap=0.0, deviation_pct=0.0)
    spy_df = spy_df.copy()
    spy_df.index = pd.to_datetime(spy_df.index) if not isinstance(spy_df.index, pd.DatetimeIndex) else spy_df.index
    today = spy_df.index[-1].date()
    current = spy_df[spy_df.index.date == today]
    return regime_mod.detect(current, prior_close=prior_close)


def _fetch_spy_prior_close(broker: Broker) -> float | None:
    """Fetch SPY's most recent COMPLETED daily close (for gap-override calc).

    Filter to bars whose date is strictly before today (ET), then take the last.
    Cached for the whole session; only called once at startup.
    """
    try:
        daily = broker.daily_bars([config.REGIME.spy_symbol], lookback_days=10)
    except Exception as e:
        log.warning("SPY prior-close fetch failed: %s", e)
        return None
    df = daily.get(config.REGIME.spy_symbol)
    if df is None or df.empty:
        return None
    today = _now_et().date()
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        mask = idx.tz_convert("UTC").date < today
    else:
        mask = idx.date < today
    prior = df[mask]
    if prior.empty:
        return None
    return float(prior["close"].iloc[-1])


def _evaluate_and_trade(
    broker: Broker, orb_candidates: list, reversal_candidates: list,
    starting_equity: float, current_regime: regime_mod.Regime,
    symbol_cooldown: dict[str, datetime] | None = None,
) -> None:
    """Pick the right candidate universe for the regime, then evaluate signals.

    - trend_up/trend_down -> ORB on intraday-refreshed top-N (high-vol movers).
    - range              -> reversal on the full pre-market shortlist (stable names).
    - neutral            -> no entries.
    """
    if current_regime.label == "neutral":
        log.info("Regime neutral — no entries this cycle.")
        return

    if current_regime.label == "range":
        if not config.STRATEGY.enable_reversal:
            return  # reversal disabled — skip range regime entirely
        shortlist = reversal_candidates
    else:  # trend_up / trend_down
        if not config.STRATEGY.enable_orb:
            return
        shortlist = orb_candidates

    if not shortlist:
        return

    positions = broker.positions()
    open_symbols = {p.symbol for p in positions}
    equity = broker.equity()

    ok, reason = can_open_new_position(len(positions), starting_equity, equity)
    if not ok:
        log.info("Skip new entries: %s", reason)
        return

    # Filter out symbols still in cooldown after a recent exit
    now_et = datetime.now(pytz.timezone(config.SESSION.timezone))
    if symbol_cooldown:
        cooled = {s for s, until in symbol_cooldown.items() if now_et < until}
        if cooled:
            log.info("Cooldown active for: %s", sorted(cooled))
    else:
        cooled = set()

    symbols = [c.symbol for c in shortlist if c.symbol not in open_symbols and c.symbol not in cooled]
    if not symbols:
        return

    bars = broker.bars(symbols, timeframe=config.SESSION.bar_timeframe, lookback_days=5)
    for sym in symbols:
        df = bars.get(sym)
        if df is None or df.empty:
            continue
        sig = strategy.evaluate(sym, df, current_regime)
        if sig is None:
            continue

        trade = size_trade(
            symbol=sig.symbol,
            side=sig.side,
            entry=sig.entry,
            stop=sig.stop,
            target=sig.target,
            equity=equity,
        )
        if trade is None:
            continue

        log.info(
            "SIGNAL [%s] %s %s qty=%d entry=%.2f stop=%.2f tgt=%.2f R:R=%.2f — %s",
            sig.setup.upper(), trade.side.upper(), trade.symbol, trade.qty, trade.entry,
            trade.stop, trade.target, trade.r_multiple, sig.reason,
        )
        try:
            order = broker.submit_bracket(
                symbol=trade.symbol,
                qty=trade.qty,
                side=trade.side,
                entry_price=None,  # market entry
                stop_price=trade.stop,
                take_profit_price=trade.target,
                atr=sig.atr,
            )
            if order is None:
                log.warning("Skipped %s %s — stale signal or oversized clamp", trade.side, trade.symbol)
                continue
            notify.order_submitted(
                setup=sig.setup,
                side=trade.side,
                symbol=trade.symbol,
                qty=trade.qty,
                entry=trade.entry,
                stop=trade.stop,
                target=trade.target,
                r_mult=trade.r_multiple,
                reason=sig.reason,
            )
        except Exception as e:
            log.exception("order submit failed %s: %s", trade.symbol, e)
            notify.error(f"Order submit failed {trade.symbol}: {e}")

        # Refresh position count; respect max_concurrent_positions
        positions = broker.positions()
        ok, reason = can_open_new_position(len(positions), starting_equity, broker.equity())
        if not ok:
            log.info("Stop evaluating: %s", reason)
            return


def _end_of_day_flatten(broker: Broker, convert_t_passed: bool) -> None:
    """Flatten everything except positions we decide to convert to swing.

    Order matters: bracket children (stop + target) "hold" the shares, so Alpaca
    rejects close_position() while they're alive. We must cancel orders FIRST,
    then close. Keepers are an exception — we leave their brackets in place.
    """
    positions = broker.positions()
    if not positions:
        log.info("EOD: already flat.")
        return

    # Decide who stays as a swing carry-over. Use today's session VWAP via the latest
    # 15-min bar VWAP estimate, not the bid/ask mid (mid vs mid always returns False).
    keepers: list[str] = []
    if convert_t_passed and config.STRATEGY.allow_swing_conversion:
        try:
            recent = broker.bars([p.symbol for p in positions], timeframe="15Min", lookback_days=1)
        except Exception:
            recent = {}
        for p in positions:
            side = "long" if float(p.qty) > 0 else "short"
            try:
                bid, ask = broker.latest_quote(p.symbol)
                mid = (bid + ask) / 2.0
            except Exception:
                mid = p.avg_entry_price
            df = recent.get(p.symbol)
            session_vwap = None
            if df is not None and not df.empty:
                today = pd.to_datetime(df.index).date.max()
                today_df = df[pd.to_datetime(df.index).date == today]
                if not today_df.empty and float(today_df["volume"].sum()) > 0:
                    typical = (today_df["high"] + today_df["low"] + today_df["close"]) / 3.0
                    session_vwap = float((typical * today_df["volume"]).sum() / today_df["volume"].sum())
            if strategy.should_convert_to_swing(float(p.unrealized_plpc), mid, session_vwap, side):
                keepers.append(p.symbol)

    # Fast path: no keepers (the default since allow_swing_conversion=False).
    # Use Alpaca's atomic close_all_positions(cancel_orders=True) — server-side
    # cancels brackets and closes positions in one shot, no async race.
    if not keepers:
        broker.close_all_positions()  # already passes cancel_orders=True
        return

    # Slow path: keepers exist. Cancel orders first (async), wait briefly for
    # Alpaca to release the held shares, then close non-keepers individually.
    broker.cancel_all_orders()
    time.sleep(3)
    for p in positions:
        if p.symbol in keepers:
            log.info("Converting %s to swing (unrealized %+.2f%%)", p.symbol, p.unrealized_plpc * 100)
            continue
        broker.close_position(p.symbol)


def _poll_fills(broker: Broker, notified_ids: set[str]) -> None:
    """Notify on newly-filled orders since last poll."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    now = _now_et()
    start = datetime(now.year, now.month, now.day, tzinfo=TZ).astimezone(pytz.UTC)
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=start, limit=500)
        orders = broker.trading.get_orders(filter=req)
    except Exception as e:
        log.warning("poll_fills: %s", e)
        return

    for o in orders:
        if o.id in notified_ids:
            continue
        if o.filled_at is None or o.filled_qty is None:
            continue
        qty = float(o.filled_qty)
        if qty <= 0:
            continue
        price = float(o.filled_avg_price) if o.filled_avg_price else 0.0
        side = o.side.value if hasattr(o.side, "value") else str(o.side)
        otype = o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type)
        notify.order_filled(o.symbol, side, qty, price, otype)
        notified_ids.add(o.id)


def _arm_break_evens(broker: Broker, be_state: dict[str, dict]) -> None:
    """Move each open position's stop to break-even after price reaches +N×R.

    Tracked per-symbol in `be_state`: looks up the bracket's stop child once
    (caching its id and original stop_price), then on each cycle checks the mid
    quote against the trigger price. Once armed, replaces the stop order in
    place so the bracket linkage is preserved.
    """
    trigger_r = config.STRATEGY.be_trigger_r
    if not trigger_r or trigger_r <= 0:
        return

    positions = broker.positions()
    open_syms = {p.symbol for p in positions}

    # Drop state for closed positions
    for sym in list(be_state.keys()):
        if sym not in open_syms:
            be_state.pop(sym, None)

    for p in positions:
        sym = p.symbol
        side = "long" if p.qty > 0 else "short"
        entry = p.avg_entry_price

        st = be_state.get(sym)
        if st is None:
            try:
                stop_order = broker.get_open_stop_order(sym)
            except Exception as e:
                log.warning("BE lookup %s failed: %s", sym, e)
                continue
            if stop_order is None:
                continue  # parent not filled yet, or no bracket
            st = {
                "original_stop": float(stop_order.stop_price),
                "stop_order_id": stop_order.id,
                "armed": False,
            }
            be_state[sym] = st

        if st["armed"]:
            continue

        try:
            bid, ask = broker.latest_quote(sym)
        except Exception as e:
            log.warning("BE quote %s failed: %s", sym, e)
            continue
        mid = (bid + ask) / 2.0

        if side == "long":
            risk = entry - st["original_stop"]
            if risk <= 0:
                continue
            trigger = entry + risk * trigger_r
            armed = mid >= trigger
        else:
            risk = st["original_stop"] - entry
            if risk <= 0:
                continue
            trigger = entry - risk * trigger_r
            armed = mid <= trigger

        if not armed:
            continue

        try:
            broker.replace_stop_price(st["stop_order_id"], entry)
            r_now = (mid - entry) / risk if side == "long" else (entry - mid) / risk
            log.info("BE armed %s: stop -> entry $%.2f (MFE %.2fR)", sym, entry, r_now)
            notify.send(f"[BE] `{sym}` stop -> break-even @ ${entry:.2f}")
            st["armed"] = True
        except Exception as e:
            log.exception("BE replace failed for %s: %s", sym, e)


def _poll_position_closes(
    broker: Broker, known: dict[str, float],
    symbol_cooldown: dict[str, datetime] | None = None,
) -> None:
    """Detect positions that disappeared since last poll and notify realized P&L.

    When a position closes, start the re-entry cooldown so the bot doesn't
    immediately churn back into the same symbol.
    """
    from datetime import timedelta
    current = {p.symbol: p for p in broker.positions()}
    # Update last-known unrealized on still-open positions
    for sym, p in current.items():
        known[sym] = float(p.unrealized_pl)

    closed = [s for s in list(known.keys()) if s not in current]
    for sym in closed:
        approx_pnl = known.pop(sym)
        notify.position_closed(sym, approx_pnl, 0.0)
        # Start cooldown — block re-entry for reentry_cooldown_minutes
        if symbol_cooldown is not None:
            cooldown_mins = config.STRATEGY.reentry_cooldown_minutes
            until = datetime.now(pytz.timezone(config.SESSION.timezone)) + timedelta(minutes=cooldown_mins)
            symbol_cooldown[sym] = until
            log.info("Cooldown started for %s — no re-entry until %s (%d min)",
                     sym, until.strftime("%H:%M"), cooldown_mins)


def _notify_session_end(broker: Broker, starting_equity: float, notified_ids: set[str]) -> None:
    try:
        end_equity = float(broker.get_account().equity)
    except Exception:
        end_equity = starting_equity
    # Benchmark via SPY daily move
    bench = None
    try:
        daily = broker.daily_bars([config.SESSION.benchmark_symbol], lookback_days=5)
        df = daily.get(config.SESSION.benchmark_symbol)
        if df is not None and len(df) >= 2:
            prior = float(df["close"].iloc[-2])
            latest = float(df["close"].iloc[-1])
            bench = (latest - prior) / prior * 100.0
    except Exception:
        pass
    notify.session_summary(starting_equity, end_equity, len(notified_ids), bench)

    # Friday: send a weekly rollup so idle weeks don't go unnoticed.
    if _now_et().weekday() == 4:
        try:
            _notify_weekly(end_equity)
        except Exception as e:
            log.warning("weekly summary failed: %s", e)


def _notify_weekly(end_equity: float) -> None:
    """Parse this week's journal files (Mon–Fri) and send a digest."""
    import re

    today = _now_et().date()
    monday = today.fromordinal(today.toordinal() - today.weekday())
    week_pnl = 0.0
    days_traded = 0
    total_fills = 0
    for i in range(5):
        d = monday.fromordinal(monday.toordinal() + i)
        jf = config.JOURNAL_DIR / f"{d:%Y-%m-%d}.md"
        if not jf.exists():
            continue
        text = jf.read_text()
        m = re.search(r"Day P&L:\s+\$([-\d,]+\.\d+)", text)
        if m:
            week_pnl += float(m.group(1).replace(",", ""))
        f = re.search(r"(\d+) fill\(s\)", text)
        fills = int(f.group(1)) if f else 0
        total_fills += fills
        if fills > 0:
            days_traded += 1
    notify.weekly_summary(week_pnl, days_traded, total_fills, end_equity)


if __name__ == "__main__":
    run()
