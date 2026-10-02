"""Paged, cached OHLCV fetching for the live bot.

Binance returns at most 1000 klines per request, so fetch_ohlcv(limit=N) with
N > 1000 silently comes back short. The live bot asked for 1490 bars and got
1000, so its 1440-bar VWAP and bands were really computed over ~16.6h instead
of the 24h the backtest validated. OhlcvCache backfills by paging once, then
each poll only fetches bars from the newest cached one onward.
"""
from __future__ import annotations

import pandas as pd

PAGE = 1000
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


class OhlcvCache:
    def __init__(self, ex, symbol: str, timeframe: str) -> None:
        self.ex = ex
        self.symbol = symbol
        self.timeframe = timeframe
        self.tf_ms = ex.parse_timeframe(timeframe) * 1000
        self._df: pd.DataFrame | None = None
        self._depth = 0

    def get(self, limit: int) -> pd.DataFrame:
        """Latest `limit` bars, oldest first. The last bar may still be forming."""
        now_ms = self.ex.milliseconds()
        if (
            self._df is None
            or self._df.empty
            or limit > self._depth
            or now_ms - self._newest_ms() > limit * self.tf_ms  # slept longer than the window
        ):
            merged = self._frame(self._fetch_since(now_ms - (limit + 1) * self.tf_ms, now_ms))
            self._depth = limit if not merged.empty else 0
        else:
            # Refetch from the newest cached bar: it was probably still forming last poll.
            fresh = self._frame(self._fetch_since(self._newest_ms(), now_ms))
            merged = pd.concat([self._df, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self._df = merged.iloc[-limit:]
        return self._df.copy()

    def _newest_ms(self) -> int:
        return int(self._df.index[-1].value // 1_000_000)

    def _fetch_since(self, since_ms: int, now_ms: int) -> list[list]:
        rows: list[list] = []
        cursor = since_ms
        max_pages = (now_ms - since_ms) // (PAGE * self.tf_ms) + 3
        for _ in range(max_pages):
            chunk = self.ex.fetch_ohlcv(self.symbol, timeframe=self.timeframe, since=cursor, limit=PAGE)
            if not chunk:
                break
            rows.extend(chunk)
            last_ms = chunk[-1][0]
            if last_ms + self.tf_ms > now_ms:
                break  # reached the forming bar
            cursor = last_ms + self.tf_ms
        return rows

    @staticmethod
    def _frame(rows: list[list]) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=COLUMNS)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("ts")
