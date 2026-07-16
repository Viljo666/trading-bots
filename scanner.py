"""Candidate scanner: pre-market gap/volume filter + intraday refresh."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from broker import Broker
from config import SCANNER

log = logging.getLogger(__name__)


# Curated large/mid-cap liquid US equities (subset of S&P 500 plus a few high-volume names).
# Kept in-code to avoid an extra dependency; covers the primary names retail day-traders watch.
_SP500_LIQUID = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AVGO", "LLY",
    "JPM", "V", "XOM", "UNH", "MA", "PG", "JNJ", "HD", "COST", "ABBV",
    "MRK", "CVX", "KO", "ADBE", "PEP", "WMT", "CRM", "BAC", "MCD", "LIN",
    "TMO", "CSCO", "ACN", "ABT", "NFLX", "AMD", "ORCL", "DIS", "WFC", "PM",
    "INTC", "TXN", "IBM", "VZ", "NOW", "QCOM", "CMCSA", "DHR", "COP", "NEE",
    "PFE", "UNP", "RTX", "HON", "LOW", "SPGI", "INTU", "GE", "AMAT", "UPS",
    "CAT", "BA", "AXP", "DE", "BKNG", "T", "GS", "BLK", "ELV", "SYK",
    "MS", "AMGN", "LMT", "ADP", "GILD", "MDT", "TJX", "MDLZ", "ISRG", "C",
    "VRTX", "PLD", "REGN", "PANW", "SCHW", "SBUX", "ETN", "CB", "BMY", "ADI",
    "BX", "LRCX", "MU", "CVS", "SO", "MMC", "KLAC", "SNPS", "ZTS", "CDNS",
    "FI", "TGT", "BSX", "DUK", "ITW", "SHW", "ICE", "EOG", "CL", "EQIX",
    "PYPL", "APD", "WM", "NOC", "MCO", "PNC", "USB", "AON", "ROP", "FCX",
    "CME", "MAR", "NSC", "FDX", "GM", "F", "ORLY", "HCA", "EMR", "APH",
    "PGR", "SLB", "PSX", "MCK", "CARR", "NKE", "ECL", "SRE", "AEP", "MPC",
    "WELL", "OXY", "AJG", "COF", "ROST", "DXCM", "KMB", "TT", "CTAS", "AFL",
    "PXD", "AIG", "MSCI", "PAYX", "HLT", "PSA", "TFC", "GIS", "KMI", "TEL",
    "DHI", "CHTR", "ADM", "D", "O", "ALL", "EW", "NXPI", "NUE", "MET",
    "STZ", "BDX", "YUM", "FTNT", "EXC", "WMB", "TRV", "KVUE", "AZO", "OKE",
    "AMP", "A", "HSY", "LEN", "URI", "KR", "IDXX", "VLO", "COIN", "AVB",
    "SMCI", "UBER", "LYFT", "SNAP", "PLTR", "RBLX", "SOFI", "CRWD", "DDOG", "NET",
    "SHOP", "SQ", "MSTR", "RIVN", "LCID", "MARA", "RIOT", "HOOD", "DASH", "ABNB",
]


@dataclass
class Candidate:
    symbol: str
    price: float
    gap_pct: float
    rel_volume: float
    prior_close: float
    avg_volume: float
    range_pct: float

    def score(self) -> float:
        """Higher is better. Favors unusual volume and meaningful gaps."""
        return self.rel_volume * 1.5 + abs(self.gap_pct) * 0.5 + self.range_pct * 0.3


def build_universe() -> list[str]:
    syms = set(_SP500_LIQUID) | set(SCANNER.etf_universe)
    return sorted(syms)


def rank_candidates(daily_bars: dict[str, pd.DataFrame]) -> tuple[list[Candidate], dict[str, int]]:
    """Pure ranker: takes pre-fetched daily bars, returns ranked candidates + funnel counts.

    Reused by both live (`premarket_scan`) and backtest.
    Funnel counts: {has_data, hist_ok, price_ok, vol_ok}.
    """
    etf_set = set(SCANNER.etf_universe)
    counts = {"has_data": len(daily_bars), "hist_ok": 0, "price_ok": 0, "vol_ok": 0}

    candidates: list[Candidate] = []
    for sym, df in daily_bars.items():
        if len(df) < 21:
            continue
        counts["hist_ok"] += 1

        latest = df.iloc[-1]
        prior = df.iloc[-2]
        price = float(latest["close"])
        if price < SCANNER.min_price or price > SCANNER.max_price:
            continue
        counts["price_ok"] += 1

        avg_vol = float(df["volume"].tail(20).mean())
        is_etf = sym in etf_set
        if avg_vol < SCANNER.min_avg_volume and not is_etf:
            continue
        counts["vol_ok"] += 1

        prior_close = float(prior["close"])
        gap_pct = (float(latest["open"]) - prior_close) / prior_close * 100.0
        rel_vol = float(latest["volume"]) / avg_vol if avg_vol else 0.0
        range_pct = (float(latest["high"]) - float(latest["low"])) / price * 100.0

        candidates.append(
            Candidate(
                symbol=sym, price=price, gap_pct=gap_pct, rel_volume=rel_vol,
                prior_close=prior_close, avg_volume=avg_vol, range_pct=range_pct,
            )
        )

    candidates.sort(key=lambda c: c.score(), reverse=True)
    return candidates, counts


def shortlist_with_etfs(candidates: list[Candidate]) -> list[Candidate]:
    """Take top_n by score, then append any missing ETFs."""
    top = candidates[: SCANNER.top_n_premarket]
    etf_set = set(SCANNER.etf_universe)
    top_syms = {c.symbol for c in top}
    for c in candidates:
        if c.symbol in etf_set and c.symbol not in top_syms:
            top.append(c)
            top_syms.add(c.symbol)
    return top


def premarket_scan(broker: Broker) -> list[Candidate]:
    """Live pre-market scan via Alpaca. Wraps `rank_candidates` + `shortlist_with_etfs`."""
    universe = build_universe()
    # 60 calendar days ≈ 42 trading days, comfortably above the 21-bar `hist_ok` gate.
    bars = broker.daily_bars(universe, lookback_days=60)
    candidates, counts = rank_candidates(bars)
    top = shortlist_with_etfs(candidates)

    # Safety net: if the funnel collapsed (e.g. data outage), fall back to ETF candidates
    # so the bot has something to evaluate intraday rather than going dark all day.
    if not top:
        log.warning("Scanner produced 0 candidates — falling back to ETF universe.")
        for sym in SCANNER.etf_universe:
            df = bars.get(sym)
            if df is None or df.empty:
                continue
            latest = df.iloc[-1]
            prior_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else float(latest["close"])
            top.append(Candidate(
                symbol=sym, price=float(latest["close"]), gap_pct=0.0, rel_volume=1.0,
                prior_close=prior_close, avg_volume=float(df["volume"].mean()), range_pct=0.0,
            ))

    log.info(
        "Pre-market funnel: universe=%d data=%d hist_ok=%d price_ok=%d vol_ok=%d -> candidates=%d shortlist=%d",
        len(universe), counts["has_data"], counts["hist_ok"], counts["price_ok"],
        counts["vol_ok"], len(candidates), len(top),
    )
    log.info("Pre-market top: %s", [c.symbol for c in top[:15]])
    return top


def intraday_refresh(broker: Broker, premarket: list[Candidate]) -> list[Candidate]:
    """Re-rank the pre-market shortlist using 15-min intraday bars."""
    if not premarket:
        return []
    symbols = [c.symbol for c in premarket]
    bars = broker.bars(symbols, timeframe="15Min", lookback_days=2)

    refreshed: list[Candidate] = []
    by_sym = {c.symbol: c for c in premarket}
    for sym, df in bars.items():
        if df.empty:
            continue
        base = by_sym[sym]
        today_vol = float(df["volume"].sum())
        today_range_pct = (float(df["high"].max()) - float(df["low"].min())) / base.price * 100.0
        rel_vol = today_vol / base.avg_volume if base.avg_volume else 0.0

        refreshed.append(
            Candidate(
                symbol=sym,
                price=float(df["close"].iloc[-1]),
                gap_pct=base.gap_pct,
                rel_volume=rel_vol,
                prior_close=base.prior_close,
                avg_volume=base.avg_volume,
                range_pct=today_range_pct,
            )
        )

    refreshed.sort(key=lambda c: c.score(), reverse=True)
    return refreshed[: SCANNER.top_n_intraday]
