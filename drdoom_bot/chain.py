"""On-chain and market data for the DRDOOM community bot.

Two read-only sources, no API keys needed:
  - DexScreener  -> price / market cap / volume for `/price`
  - Solana RPC   -> recent transactions touching the mint, parsed into buys

Buy detection: a transaction counts as a buy when its fee payer ends up with MORE of
the token and LESS SOL. That holds on the Pump.fun bonding curve and on the AMM pool
after graduation, so the watcher needs no venue-specific decoding. SOL spent is the
fee payer's lamport decrease, so it includes network fees (a few thousand lamports).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("drdoom.chain")

DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/{ca}"
MAX_TX_PER_POLL = 15   # public RPC is rate-limited; anything beyond this is skipped
SIG_PAGE = 25
FETCH_PAUSE = 0.4       # seconds between getTransaction calls


@dataclass(frozen=True)
class Buy:
    signature: str
    buyer: str
    sol: float
    tokens: float


def rpc(url: str, method: str, params: list):
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      timeout=15)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def _token_amount(balances: list, mint: str, owner: str) -> float:
    return sum(float(b["uiTokenAmount"].get("uiAmountString") or 0)
               for b in balances or [] if b.get("mint") == mint and b.get("owner") == owner)


def parse_buy(tx: dict, mint: str, signature: str = "") -> Buy | None:
    """Return a Buy if the fee payer gained `mint` and spent SOL in this transaction."""
    meta = (tx or {}).get("meta") or {}
    if not meta or meta.get("err"):
        return None
    keys = tx["transaction"]["message"]["accountKeys"]
    payer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
    tokens = (_token_amount(meta.get("postTokenBalances"), mint, payer)
              - _token_amount(meta.get("preTokenBalances"), mint, payer))
    sol = (meta["preBalances"][0] - meta["postBalances"][0]) / 1e9
    if tokens <= 0 or sol <= 0:
        return None
    return Buy(signature, payer, sol, tokens)


class BuyWatcher:
    """Polls the mint's signature list and yields buys newer than the last one seen.

    `load`/`save` persist the newest processed signature so a restart neither replays
    old buys nor loses its place. The first run only records a starting point.
    """

    def __init__(self, rpc_url: str, mint: str, load, save, min_sol: float):
        self.rpc_url, self.mint, self.load, self.save, self.min_sol = rpc_url, mint, load, save, min_sol

    def poll(self) -> list[Buy]:
        last = self.load()
        opts = {"limit": SIG_PAGE, "commitment": "confirmed"}
        if last:
            opts["until"] = last
        sigs = rpc(self.rpc_url, "getSignaturesForAddress", [self.mint, opts]) or []
        if not sigs:
            return []
        self.save(sigs[0]["signature"])
        if not last:
            return []
        buys = []
        fresh = [s for s in reversed(sigs) if not s.get("err")][-MAX_TX_PER_POLL:]
        for s in fresh:
            try:
                tx = self._transaction(s["signature"])
            except Exception as e:  # one bad fetch must not drop the rest of the batch
                log.warning("getTransaction %s failed: %s", s["signature"][:12], e)
                continue
            buy = parse_buy(tx, self.mint, s["signature"])
            if buy and buy.sol >= self.min_sol:
                buys.append(buy)
        return buys

    def _transaction(self, signature: str):
        params = [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                              "maxSupportedTransactionVersion": 1}]
        for attempt in range(3):
            time.sleep(FETCH_PAUSE * (1 + 4 * attempt))  # public RPC answers bursts with 429
            try:
                return rpc(self.rpc_url, "getTransaction", params)
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 429 or attempt == 2:
                    raise


_info_cache: dict = {"ts": 0.0, "ca": None, "data": None}


def token_info(ca: str, ttl: int = 30) -> dict | None:
    """Most liquid DexScreener pair for the token, or None if it isn't indexed yet."""
    now = time.time()
    if _info_cache["ca"] == ca and now - _info_cache["ts"] < ttl:
        return _info_cache["data"]
    try:
        pairs = requests.get(DEXSCREENER.format(ca=ca), timeout=10).json().get("pairs") or []
    except Exception as e:
        log.warning("dexscreener failed: %s", e)
        return _info_cache["data"] if _info_cache["ca"] == ca else None
    pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, default=None)
    _info_cache.update(ts=now, ca=ca, data=pair)
    return pair
