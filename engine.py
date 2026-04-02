"""
Oraculus — Trading Engine v4

Crypto prices via Binance WebSocket (real-time).
Falls back to CoinGecko REST if Binance WS is geo-blocked (Render).

Three live strategies: SPREAD_FADE, CROSS_MARKET, DEP_GRAPH.

Fixes in v4 vs v3:
  - BinanceWSFeed: real WebSocket stream (btcusdt/ethusdt/solusdt @ticker)
    with automatic CoinGecko fallback on connection failure / 451 block.
  - _update(): fixed return_10s filter (was: `now-20 <= now-t <= now-8`
    which evaluates to `8 <= t <= 20` in epoch seconds → always empty.
    Now: `8 <= (now - t) <= 20` — correct age-window filter.)
  - Position settlement: positions are auto-closed when market probability
    crosses 0.97 or 0.03, crediting PnL to balance.
  - Stale eviction: now credits mark-to-market value back to balance
    instead of silently destroying capital.
"""

from __future__ import annotations

import asyncio, json, logging, re, time, random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp

from config import *

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("oraculus")

# ── Models ────────────────────────────────────────────────────────────────────

@dataclass
class MarketData:
    condition_id: str; question: str
    yes_token_id: str; no_token_id: str
    yes_ask: float; no_ask: float
    yes_bid: float = 0.0; no_bid: float = 0.0
    yes_depth: float = 0.0; no_depth: float = 0.0
    category: str = ""; volume: float = 0.0

    @property
    def yes_price(self): return self.yes_ask
    @property
    def no_price(self): return self.no_ask
    @property
    def total_cost(self): return self.yes_ask + self.no_ask
    @property
    def spread(self): return self.yes_ask - self.yes_bid
    @property
    def neg_risk_edge(self): return round(1.0 - self.total_cost, 4)
    @property
    def net_edge(self): return 1.0 - self.total_cost - 2 * TAKER_FEE


@dataclass
class CryptoPrice:
    symbol: str
    price: float
    bid: float = 0.0
    ask: float = 0.0
    return_10s: float = 0.0      # 10-second rolling return (was return_5s — now named correctly)
    change_24h: float = 0.0

    # Alias for backwards compat with existing dashboard / DEP_GRAPH code
    @property
    def return_5s(self): return self.return_10s


# ── State I/O ─────────────────────────────────────────────────────────────────

def save_json(path: str, data: dict):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning(f"Save {path}: {e}")


def load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}


# ── Crypto Feed ───────────────────────────────────────────────────────────────
#
#  Primary:  Binance combined WebSocket stream (real-time, no API key needed)
#            wss://stream.binance.com:9443/stream?streams=btcusdt@ticker/...
#            Gives live bid/ask/price/24h-change; updates on every trade.
#
#  Fallback: CoinGecko REST API polled every CRYPTO_POLL_SEC seconds.
#            Used automatically if Binance WS is geo-blocked (Render US servers
#            return HTTP 451 on Binance REST but WS may also be blocked).
#
#  Both paths share the same _update() method and price history buffer.
#  The 10-second rolling return is computed from the shared history.

class BinanceWSFeed:
    """
    Streams BTC/ETH/SOL prices from Binance combined ticker WebSocket.
    Auto-falls back to CoinGecko REST on geo-block or any connection failure.
    """

    WS_URL = (
        "wss://stream.binance.com:9443/stream"
        "?streams=btcusdt@ticker/ethusdt@ticker/solusdt@ticker"
    )

    # Binance symbol (uppercase) → internal symbol key
    SYM_MAP = {"BTCUSDT": "btcusdt", "ETHUSDT": "ethusdt", "SOLUSDT": "solusdt"}

    # Typical bid-ask half-spread as fraction of mid (used to synthesise when absent)
    SPREAD_EST = {"btcusdt": 0.0002, "ethusdt": 0.0003, "solusdt": 0.0008}

    # CoinGecko fallback params
    CG_URL   = f"{COINGECKO_API}/simple/price"
    CG_COINS = {"bitcoin": "btcusdt", "ethereum": "ethusdt", "solana": "solusdt"}

    def __init__(self):
        self._prices: Dict[str, CryptoPrice] = {}
        self._hist:   Dict[str, List[Tuple[float, float]]] = {}
        self.ws_ok   = False          # True = Binance WS live; False = CoinGecko fallback
        self._cg_fallback = False     # latched True if WS fails
        self._session: Optional[aiohttp.ClientSession] = None

    def set_session(self, s: aiohttp.ClientSession):
        self._session = s

    def get(self) -> Dict[str, CryptoPrice]:
        return dict(self._prices)

    # ── Shared price-update logic ──────────────────────────────────────────────

    def _update(self, sym: str, price: float,
                bid: float, ask: float, change_24h: float):
        """
        Update price history and compute 10-second rolling return.

        FIX: original condition was `now - 20 <= now - t <= now - 8`
        which simplifies to `8 <= t <= 20` (absolute epoch time — always empty).
        Correct condition: `8 <= (now - t) <= 20`  (age of the history entry).
        """
        now = time.time()
        hist = self._hist.setdefault(sym, [])
        hist.append((now, price))
        # Keep 60-second rolling window
        self._hist[sym] = [(t, p) for t, p in hist if t > now - 60]

        # Entries between 8 and 20 seconds old → use as the baseline
        old = [p for t, p in self._hist[sym] if 8 <= (now - t) <= 20]
        if not old:
            # Fallback: any entry older than 5s
            old = [p for t, p in self._hist[sym] if (now - t) >= 5]
        ret = (price - old[-1]) / old[-1] if old and old[-1] > 0 else 0.0

        # Synthesise bid/ask if not provided (CoinGecko path)
        if bid <= 0 or ask <= 0:
            half = price * self.SPREAD_EST.get(sym, 0.0005) / 2
            bid, ask = price - half, price + half

        self._prices[sym] = CryptoPrice(
            sym, round(price, 2), bid, ask, ret, change_24h
        )

    # ── Run-forever (tries WS, latches to CoinGecko on failure) ───────────────

    async def run_forever(self):
        log.info("CryptoFeed: starting (will try Binance WS first)")
        while True:
            if not self._cg_fallback:
                try:
                    await self._run_ws()
                except Exception as e:
                    log.warning(
                        f"BinanceWS failed ({e}) — "
                        "switching to CoinGecko fallback permanently"
                    )
                    self._cg_fallback = True
                    self.ws_ok = False
                    await asyncio.sleep(2)
            else:
                try:
                    await self._poll_coingecko()
                except Exception as e:
                    log.warning(f"CoinGecko poll error: {e}")
                await asyncio.sleep(CRYPTO_POLL_SEC)

    # ── Binance WebSocket path ─────────────────────────────────────────────────

    async def _run_ws(self):
        log.info(f"BinanceWS: connecting to {self.WS_URL[:60]}...")
        if not self._session:
            raise RuntimeError("No aiohttp session")

        async with self._session.ws_connect(
            self.WS_URL,
            heartbeat=20,
            timeout=aiohttp.ClientWSTimeout(ws_close=10.0),
            headers={"User-Agent": "oraculus-engine/4.0"},
        ) as ws:
            self.ws_ok = True
            self._cg_fallback = False
            log.info("BinanceWS: connected ✓  (real-time streaming)")

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        outer = json.loads(msg.data)
                        ticker = outer.get("data", {})
                        raw_sym = ticker.get("s", "")
                        sym = self.SYM_MAP.get(raw_sym)
                        if not sym:
                            continue
                        price = float(ticker.get("c") or 0)
                        bid   = float(ticker.get("b") or 0)
                        ask   = float(ticker.get("a") or 0)
                        pct   = float(ticker.get("P") or 0)
                        if price > 0:
                            self._update(sym, price, bid, ask, pct)
                    except Exception as e:
                        log.debug(f"BinanceWS parse: {e}")

                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    raise Exception(f"WS closed/errored: {msg.type}")

        raise Exception("WS loop exited unexpectedly")

    # ── CoinGecko fallback path ────────────────────────────────────────────────

    async def _poll_coingecko(self):
        if not self._session:
            return
        async with self._session.get(
            self.CG_URL,
            params={
                "ids": ",".join(self.CG_COINS.keys()),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"Accept": "application/json", "User-Agent": "oraculus-engine/4.0"},
        ) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                got = 0
                for coin_id, sym in self.CG_COINS.items():
                    d = data.get(coin_id, {})
                    price = d.get("usd")
                    if price:
                        change = float(d.get("usd_24h_change", 0) or 0)
                        self._update(sym, float(price), 0.0, 0.0, change)
                        got += 1
                if got and not hasattr(self, "_cg_logged"):
                    btc = self._prices.get("btcusdt")
                    log.info(
                        f"CoinGecko fallback OK — "
                        f"BTC=${btc.price:,.0f} | polling every {CRYPTO_POLL_SEC}s"
                    )
                    self._cg_logged = True
            elif r.status == 429:
                log.warning("CoinGecko 429 — backing off 30s")
                await asyncio.sleep(30)
            else:
                body = await r.text()
                log.warning(f"CoinGecko → {r.status}: {body[:120]}")


# ── Rate Limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, rate_per_sec: float):
        self._rate   = rate_per_sec
        self._tokens = rate_per_sec
        self._last   = time.monotonic()
        self._lock   = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            wait = (1 - self._tokens) / self._rate
            self._tokens = 0
            await asyncio.sleep(wait)


# ── Data Fetcher ──────────────────────────────────────────────────────────────

class DataFetcher:
    CLOB_RATE_PER_SEC = 5.0
    CLOB_CONCURRENCY  = 10
    MAX_CLOB_MARKETS  = 200
    BOOK_CACHE_TTL    = 90

    def __init__(self, session: aiohttp.ClientSession):
        self._s        = session
        self._clob_rl  = RateLimiter(self.CLOB_RATE_PER_SEC)
        self._sem      = asyncio.Semaphore(self.CLOB_CONCURRENCY)
        self._book_cache: Dict[str, dict] = {}

    async def _get(self, url: str, params=None, timeout: int = 15,
                   retries: int = 3, label: str = "") -> Optional[dict]:
        for attempt in range(retries + 1):
            try:
                async with self._s.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    headers={"Accept": "application/json"}
                ) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    if r.status == 429:
                        wait = (2 ** attempt) * 5 + random.uniform(0, 2)
                        log.warning(f"Rate limit (429) on {label or url[-40:]} "
                                    f"— backoff {wait:.1f}s (attempt {attempt+1})")
                        await asyncio.sleep(wait)
                        continue
                    if r.status >= 500:
                        wait = (2 ** attempt) * 2 + random.uniform(0, 1)
                        log.warning(f"Server error {r.status} on {label or url[-40:]} "
                                    f"— backoff {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue
                    log.debug(f"GET {url[-60:]} → {r.status}")
                    return None
            except asyncio.TimeoutError:
                wait = (2 ** attempt) + random.uniform(0, 1)
                log.debug(f"Timeout on {label or url[-40:]} — backoff {wait:.1f}s")
                await asyncio.sleep(wait)
            except Exception as e:
                log.debug(f"GET {url[-60:]}: {e}")
                return None
        log.debug(f"Gave up on {label or url[-40:]} after {retries+1} attempts")
        return None

    async def _fetch_book(self, tid: str) -> Optional[dict]:
        cached = self._book_cache.get(tid)
        if cached and (time.time() - cached["ts"]) < self.BOOK_CACHE_TTL:
            return cached["data"]
        await asyncio.sleep(random.uniform(0, 0.2))
        async with self._sem:
            await self._clob_rl.acquire()
            data = await self._get(
                f"{POLYMARKET_CLOB}/book",
                params={"token_id": tid},
                timeout=8, retries=2,
                label=f"CLOB/book/{tid[:12]}"
            )
            if data is not None:
                self._book_cache[tid] = {"data": data, "ts": time.time()}
            return data

    async def fetch_markets(self) -> List[MarketData]:
        # Step 1: Gamma market list
        raw = await self._get(
            f"{POLYMARKET_GAMMA}/markets",
            params={"active": "true", "closed": "false", "limit": 500},
            retries=2, label="Gamma/markets"
        )
        items = raw if isinstance(raw, list) else (raw or {}).get("markets", []) if raw else []
        if not items:
            raw = await self._get(
                f"{POLYMARKET_GAMMA}/markets",
                params={"active": "true", "limit": 300},
                retries=2, label="Gamma/markets-fallback"
            )
            items = raw if isinstance(raw, list) else (raw or {}).get("markets", []) if raw else []
        if not items:
            log.error("No markets from Gamma")
            return []
        log.info(f"Gamma: {len(items)} markets")

        # Step 2: Parse token pairs, sorted by volume
        token_pairs = []
        gamma_map   = {}
        for m in items:
            cid = m.get("conditionId") or m.get("condition_id") or m.get("id") or ""
            q   = (m.get("question") or "").strip()
            if not cid or not q:
                continue
            gamma_map[cid] = m
            cat = m.get("category", "")
            try:   vol = float(m.get("volume") or 0)
            except: vol = 0.0

            ids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(ids, str):
                try:    ids = json.loads(ids)
                except: ids = []

            tokens = m.get("tokens", [])
            if tokens and isinstance(tokens[0], dict) and tokens[0].get("token_id"):
                yes = next((t for t in tokens if str(t.get("outcome","")).lower() in ("yes","1")), tokens[0])
                no  = next((t for t in tokens if str(t.get("outcome","")).lower() in ("no","0")),
                           tokens[1] if len(tokens) > 1 else tokens[0])
                ids = [yes.get("token_id",""), no.get("token_id","")]

            if len(ids) >= 2 and ids[0] and ids[1]:
                token_pairs.append((cid, str(ids[0]), str(ids[1]), q, cat, vol))

        token_pairs.sort(key=lambda x: x[5], reverse=True)
        token_pairs = token_pairs[:self.MAX_CLOB_MARKETS]
        log.info(f"Token pairs: {len(token_pairs)} (top {self.MAX_CLOB_MARKETS} by volume)")
        if not token_pairs:
            return []

        # Step 3: Rate-limited CLOB orderbook fetch
        seen_tids: set = set()
        ordered_tids: List[str] = []
        for _, yid, nid, *_ in token_pairs:
            for tid in (yid, nid):
                if tid not in seen_tids:
                    seen_tids.add(tid)
                    ordered_tids.append(tid)

        book_cache: Dict[str, Optional[dict]] = {}
        tasks   = [self._fetch_book(tid) for tid in ordered_tids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for tid, res in zip(ordered_tids, results):
            if isinstance(res, Exception):
                log.debug(f"Book fetch exception {tid[:12]}: {res}")
                book_cache[tid] = None
            else:
                book_cache[tid] = res

        filled = sum(1 for v in book_cache.values() if v)
        cached = sum(
            1 for tid in ordered_tids
            if tid in self._book_cache
            and (time.time() - self._book_cache[tid]["ts"]) < self.BOOK_CACHE_TTL
            and book_cache.get(tid) is not None
        )
        log.info(f"CLOB orderbooks: {filled}/{len(ordered_tids)} "
                 f"({cached} from cache, {filled-cached} fresh)")

        # Step 4: Build MarketData
        result = []
        for cid, yid, nid, q, cat, vol in token_pairs:
            ybook = book_cache.get(yid)
            nbook = book_cache.get(nid)
            yes_ask = yes_bid = no_ask = no_bid = 0.0
            yes_depth = no_depth = 0.0

            if ybook:
                asks = sorted(ybook.get("asks", []), key=lambda x: float(x.get("price", 999)))
                bids = sorted(ybook.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
                if asks: yes_ask = float(asks[0].get("price", 0))
                if bids: yes_bid = float(bids[0].get("price", 0))
                yes_depth = sum(float(a.get("size", 0)) for a in asks[:5])

            if nbook:
                asks = sorted(nbook.get("asks", []), key=lambda x: float(x.get("price", 999)))
                bids = sorted(nbook.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
                if asks: no_ask = float(asks[0].get("price", 0))
                if bids: no_bid = float(bids[0].get("price", 0))
                no_depth = sum(float(a.get("size", 0)) for a in asks[:5])

            # Gamma mid-price fallback when CLOB book is missing
            if not yes_ask or not no_ask:
                m_raw = gamma_map.get(cid, {})
                op = m_raw.get("outcomePrices", "[]")
                if isinstance(op, str):
                    try:    op = json.loads(op)
                    except: op = []
                if len(op) >= 2:
                    try:
                        yes_ask = float(op[0]); no_ask = float(op[1])
                        yes_bid = max(0.01, yes_ask - 0.02)
                        no_bid  = max(0.01, no_ask  - 0.02)
                    except: pass
                if yes_depth == 0.0: yes_depth = MIN_DEPTH
                if no_depth  == 0.0: no_depth  = MIN_DEPTH

            if not yes_ask or not no_ask:                  continue
            if not (0.01 < yes_ask < 0.99):                continue

            result.append(MarketData(
                cid, q, yid, nid,
                yes_ask, no_ask, yes_bid, no_bid,
                yes_depth, no_depth, cat, vol
            ))

        log.info(f"Built {len(result)} markets")
        return result


# ── Paper Account ─────────────────────────────────────────────────────────────

class PaperAccount:
    def __init__(self):
        self.balance   = STARTING_BALANCE
        self.positions = {}
        self.trades    = []
        self._ctr      = 0
        d = load_json(PERSIST_FILE)
        if d:
            self.balance   = d.get("balance",   STARTING_BALANCE)
            self.trades    = d.get("trades",     [])
            self.positions = d.get("positions",  {})
            self._ctr      = len(self.trades)
            log.info(f"Resumed: {len(self.trades)} trades, ${self.balance:.2f}")

    def _save(self):
        save_json(PERSIST_FILE, {
            "balance":   self.balance,
            "trades":    self.trades[-500:],
            "positions": self.positions,
        })

    def buy(self, market_id, question, token_id, outcome,
            price, size_usd, strategy, edge) -> Optional[dict]:
        if size_usd < MIN_BET_SIZE or not (0 < price < 1):
            return None
        fee   = size_usd * TAKER_FEE
        total = size_usd + fee
        if total > self.balance:
            size_usd = self.balance * 0.90 / (1 + TAKER_FEE)
            if size_usd < MIN_BET_SIZE:
                return None
            fee   = size_usd * TAKER_FEE
            total = size_usd + fee
        self.balance -= total
        self._ctr += 1
        t = {
            "id":        f"T{self._ctr:05d}",
            "market_id": market_id,
            "question":  question[:70],
            "token_id":  token_id,
            "outcome":   outcome,
            "side":      "BUY",
            "price":     price,
            "size":      size_usd / price,
            "cost":      total,
            "fee":       fee,
            "pnl":       0.0,
            "strategy":  strategy,
            "edge":      edge,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.trades.append(t)
        if token_id in self.positions:
            p = self.positions[token_id]
            tc = p["avg_entry"] * p["size"] + price * (size_usd / price)
            p["size"]      += size_usd / price
            p["avg_entry"]  = tc / p["size"]
        else:
            self.positions[token_id] = {
                "market_id": market_id,
                "question":  question[:70],
                "token_id":  token_id,
                "outcome":   outcome,
                "size":      size_usd / price,
                "avg_entry": price,
                "strategy":  strategy,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
        self._save()
        return t

    def settle(self, token_id: str, settle_price: float) -> Optional[dict]:
        """
        Close a position at settle_price (0.0 = total loss, 1.0 = full win).
        Credits proceeds back to balance and records a SELL trade.
        """
        p = self.positions.pop(token_id, None)
        if p is None:
            return None
        proceeds = settle_price * p["size"]
        pnl      = proceeds - p["avg_entry"] * p["size"]
        self.balance += proceeds
        self._ctr += 1
        t = {
            "id":        f"T{self._ctr:05d}",
            "market_id": p["market_id"],
            "question":  p["question"],
            "token_id":  token_id,
            "outcome":   p["outcome"],
            "side":      "SELL",
            "price":     settle_price,
            "size":      p["size"],
            "cost":      0.0,
            "fee":       0.0,
            "pnl":       round(pnl, 4),
            "strategy":  p["strategy"],
            "edge":      0.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.trades.append(t)
        self._save()
        log.info(
            f"SETTLE {t['id']} {p['outcome']} @{settle_price:.2f} "
            f"pnl={'+'if pnl>=0 else ''}{pnl:.2f}"
        )
        return t

    @property
    def realized_pnl(self):
        return sum(t["pnl"] for t in self.trades if t["side"] == "SELL")

    @property
    def invested(self):
        return sum(p["size"] * p["avg_entry"] for p in self.positions.values())

    @property
    def nav(self):
        return self.balance + self.invested

    @property
    def total_fees(self):
        return sum(t["fee"] for t in self.trades)

    @property
    def win_rate(self):
        sells = [t for t in self.trades if t["side"] == "SELL"]
        return sum(1 for t in sells if t["pnl"] > 0) / len(sells) if sells else 0.0


# ── Strategies ────────────────────────────────────────────────────────────────

def kelly_size(prob, price, balance):
    if price <= 0 or price >= 1 or prob <= 0 or prob >= 1:
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - prob
    f = (b * prob - q) / b
    f = max(0.0, min(f, MAX_KELLY_FRACTION))
    return min(f * balance, MAX_BET_SIZE)


def _mid(ask: float, bid: float) -> float:
    return (ask + bid) / 2.0 if bid > 0.001 else ask


def scan_spread_fade(markets: List[MarketData], balance: float) -> List[dict]:
    """
    SPREAD_FADE: Exploit bid-ask spread mispricing on near-certain/near-zero markets.
    Case 1 — Long NO on very unlikely events  (YES ask < 0.12)
    Case 2 — Long YES on near-certain events  (YES mid > 0.90)
    Each market can only fire one signal (Case 1 takes priority).
    """
    SF_MIN_EDGE = 0.002
    out = []
    for m in markets:
        fired = False

        # Case 1: Long NO on very unlikely events
        if 0.01 < m.yes_ask < 0.12:
            yes_mid    = _mid(m.yes_ask, m.yes_bid)
            implied_no = 1.0 - yes_mid
            edge       = implied_no - m.no_ask - TAKER_FEE
            if edge >= SF_MIN_EDGE and m.no_depth >= MIN_DEPTH:
                size = min(kelly_size(implied_no, m.no_ask, balance), m.no_depth * 0.10)
                if size >= MIN_BET_SIZE:
                    out.append({
                        "strategy": "SPREAD_FADE",
                        "market_id": m.condition_id,
                        "question":  m.question,
                        "token_id":  m.no_token_id,
                        "outcome":   "NO",
                        "price":     m.no_ask,
                        "edge":      round(edge, 5),
                        "size":      size,
                        "reason":    f"YES_mid={yes_mid:.3f} implied_NO={implied_no:.3f} no_ask={m.no_ask:.3f}",
                    })
                    fired = True

        # Case 2: Long YES on near-certain events (skip if Case 1 already fired on this market)
        if not fired:
            yes_mid = _mid(m.yes_ask, m.yes_bid)
            if 0.90 < yes_mid < 0.990:
                no_mid      = _mid(m.no_ask, m.no_bid)
                implied_yes = 1.0 - no_mid
                edge        = implied_yes - m.yes_ask - TAKER_FEE
                if edge >= SF_MIN_EDGE and m.yes_depth >= MIN_DEPTH:
                    size = min(kelly_size(implied_yes, m.yes_ask, balance), m.yes_depth * 0.10)
                    if size >= MIN_BET_SIZE:
                        out.append({
                            "strategy": "SPREAD_FADE",
                            "market_id": m.condition_id,
                            "question":  m.question,
                            "token_id":  m.yes_token_id,
                            "outcome":   "YES",
                            "price":     m.yes_ask,
                            "edge":      round(edge, 5),
                            "size":      size,
                            "reason":    f"NO_mid={no_mid:.3f} implied_YES={implied_yes:.3f} yes_ask={m.yes_ask:.3f}",
                        })
    return out


def scan_cross_market(markets: List[MarketData], balance: float) -> List[dict]:
    """
    CROSS_MARKET: Mutually-exclusive groups where sum(YES_asks) > 1.0.
    Buy NO on the markets with the highest neg_risk_edge.
    Min group size 3 — real Polymarket often has 3-team groups.
    """
    CM_MIN_EDGE = 0.002
    out = []

    def topic_key(q: str) -> Optional[str]:
        q = q.lower()
        checks = [
            (["nba", "finals"],                       "NBA_FINALS"),
            (["nba", "eastern conference"],            "NBA_EAST_CONF"),
            (["nba", "western conference"],            "NBA_WEST_CONF"),
            (["nba", "championship"],                  "NBA_CHAMP"),
            (["nba", "mvp"],                           "NBA_MVP"),
            (["nhl", "stanley cup"],                   "NHL_CUP"),
            (["nhl", "mvp"],                           "NHL_MVP"),
            (["champions league", "win"],              "UCL"),
            (["champions league", "title"],            "UCL"),
            (["premier league", "win"],                "EPL"),
            (["premier league", "title"],              "EPL"),
            (["la liga", "win"],                       "LALIGA"),
            (["serie a", "win"],                       "SERIEA"),
            (["bundesliga", "win"],                    "BUNDESLIGA"),
            (["world cup", "win"],                     "WORLD_CUP"),
            (["world cup", "champion"],                "WORLD_CUP"),
            (["super bowl", "win"],                    "SUPERBOWL"),
            (["super bowl", "champion"],               "SUPERBOWL"),
            (["world series", "win"],                  "WORLD_SERIES"),
            (["nfl", "mvp"],                           "NFL_MVP"),
            (["masters", "golf"],                      "MASTERS"),
            (["us open", "golf"],                      "US_OPEN_GOLF"),
            (["oscar", "best picture"],                "OSCAR_PICTURE"),
            (["oscar", "best director"],               "OSCAR_DIRECTOR"),
            (["oscar", "best actor"],                  "OSCAR_ACTOR"),
            (["oscar", "best actress"],                "OSCAR_ACTRESS"),
            (["emmy", "best drama"],                   "EMMY_DRAMA"),
            (["emmy", "best comedy"],                  "EMMY_COMEDY"),
            (["grammy", "album of the year"],          "GRAMMY_AOTY"),
            (["nobel", "prize"],                       "NOBEL"),
            (["presidential", "republican nomination"],"GOP_NOM"),
            (["presidential", "democratic nomination"],"DEM_NOM"),
            (["republican", "nominee"],                "GOP_NOM"),
            (["democratic", "nominee"],                "DEM_NOM"),
            (["presidential", "win", "2028"],          "PRES_2028"),
            (["president", "2028"],                    "PRES_2028"),
            (["next james bond"],                      "BOND"),
            (["james bond"],                           "BOND"),
            (["next pope"],                            "POPE"),
            (["pope"],                                 "POPE"),
        ]
        for kws, key in checks:
            if all(k in q for k in kws):
                return key
        return None

    groups: Dict[str, List[MarketData]] = {}
    for m in markets:
        k = topic_key(m.question)
        if k:
            groups.setdefault(k, []).append(m)

    for group_name, grp in groups.items():
        if len(grp) < 3:
            continue
        yes_sum    = sum(m.yes_ask for m in grp)
        overcharge = yes_sum - 1.0
        if overcharge < CROSS_MIN_OVERCHARGE:
            continue
        grp_sorted = sorted(grp, key=lambda m: m.neg_risk_edge, reverse=True)
        for m in grp_sorted[:3]:
            edge = m.neg_risk_edge - TAKER_FEE
            if edge < CM_MIN_EDGE:   continue
            if m.no_depth < MIN_DEPTH: continue
            implied_no = 1.0 - m.yes_ask
            size = min(
                kelly_size(implied_no, m.no_ask, balance),
                m.no_depth * 0.08, MAX_BET_SIZE
            )
            if size < MIN_BET_SIZE: continue
            out.append({
                "strategy": "CROSS_MARKET",
                "market_id": m.condition_id,
                "question":  m.question,
                "token_id":  m.no_token_id,
                "outcome":   "NO",
                "price":     m.no_ask,
                "edge":      round(edge, 5),
                "size":      size,
                "reason":    f"{group_name} YES_sum={yes_sum:.3f} neg_risk={m.neg_risk_edge:.3f}",
            })
    return out


def scan_dep_graph(markets: List[MarketData], crypto: Dict[str, CryptoPrice],
                   balance: float) -> List[dict]:
    """
    DEP_GRAPH:
    1. Price-ladder consistency — lower-strike must have higher YES prob.
    2. Latency arb — crypto moved; prediction market hasn't repriced yet.
       (Requires fixed return_10s — was broken in v3 due to filter bug.)
    """
    out = []

    # ── Sub-strategy 1: Price ladder ──────────────────────────────────────────
    groups: Dict[str, list] = {}
    for m in markets:
        q     = m.question.lower()
        asset = next((a for a, kws in [
            ("BTC", ["btc", "bitcoin"]),
            ("ETH", ["eth", "ethereum"]),
            ("SOL", ["sol", "solana"]),
        ] if any(k in q for k in kws)), None)
        # Match prices like $80,000  $80k  $80K  $3,500  $150m
        mt = re.search(r'\$([\d,]+\.?\d*)([kKmM]?)', m.question)
        if asset and mt:
            v   = float(mt.group(1).replace(",", ""))
            sfx = mt.group(2).lower()
            if sfx == 'k': v *= 1_000
            elif sfx == 'm': v *= 1_000_000
            groups.setdefault(asset, []).append((m, v))

    for asset, grp in groups.items():
        grp.sort(key=lambda x: x[1])
        cp   = crypto.get(asset.lower() + "usdt")
        spot = cp.price if cp else 0.0
        for i in range(len(grp) - 1):
            ml, vl = grp[i]
            mh, vh = grp[i + 1]
            viol   = mh.yes_ask - ml.yes_ask
            if viol < GRAPH_MIN_VIOLATION: continue
            fair_p = min(0.95, ml.yes_ask + viol)
            size   = kelly_size(fair_p, ml.yes_ask, balance)
            size   = min(size, ml.yes_depth * 0.10, MAX_BET_SIZE)
            if size < MIN_BET_SIZE: continue
            out.append({
                "strategy": "DEP_GRAPH",
                "market_id": ml.condition_id,
                "question":  ml.question,
                "token_id":  ml.yes_token_id,
                "outcome":   "YES",
                "price":     ml.yes_ask,
                "edge":      round(viol / 2, 5),
                "size":      size,
                "reason":    f"{asset}@{spot:,.0f} ${vl:,.0f}<${vh:,.0f} viol={viol:.3f}",
            })

    # ── Sub-strategy 2: Latency arb ───────────────────────────────────────────
    #
    #  Now that return_10s is correctly calculated, this strategy can actually fire.
    #
    #  Keyword matching: use broad patterns that match real Polymarket question formats.
    #  E.g. "Will BTC hit $90,000 by..." / "Bitcoin above $100k by..." etc.
    #  Avoid overly specific dollar strings that won't match question text.

    KW = {
        "btcusdt": ["btc", "bitcoin"],
        "ethusdt": ["eth", "ethereum"],
        "solusdt": ["sol", "solana"],
    }

    # Minimum 10-second return to consider latency arb worthwhile
    # At 3.0x sensitivity: need return >= (MIN_EDGE + fee) / 3.0 = (0.004+0.002)/3 = 0.002
    # We keep threshold lower to enter the loop; edge filter below catches weak signals.
    TH = {"btcusdt": 0.0005, "ethusdt": 0.0010, "solusdt": 0.003}

    SENSITIVITY = 3.0

    for sym, cp in crypto.items():
        ret = cp.return_10s
        if abs(ret) < TH.get(sym, 0.005): continue
        kws = KW.get(sym, [])
        up  = ret > 0

        for m in markets:
            q = m.question.lower()
            if not any(k in q for k in kws): continue

            # Skip markets that don't reference a price level (no $ in question)
            # These are "will BTC go up/down" style — less reliable for latency arb
            if "$" not in m.question: continue

            delta = SENSITIVITY * ret

            if up:
                new_p = max(0.05, min(0.95, m.yes_ask + delta))
                edge  = new_p - m.yes_ask - TAKER_FEE
                if edge < MIN_EDGE: continue
                size = min(
                    kelly_size(new_p, m.yes_ask, balance),
                    m.yes_depth * 0.10, MAX_BET_SIZE
                )
                if size < MIN_BET_SIZE: continue
                out.append({
                    "strategy": "DEP_GRAPH",
                    "market_id": m.condition_id,
                    "question":  m.question,
                    "token_id":  m.yes_token_id,
                    "outcome":   "YES",
                    "price":     m.yes_ask,
                    "edge":      round(edge, 5),
                    "size":      size,
                    "reason":    f"{sym} {ret:+.3%}/10s δ={delta:+.4f}",
                })
            else:
                new_p = max(0.05, min(0.95, m.yes_ask + delta))   # delta < 0 here
                edge  = (1.0 - new_p) - m.no_ask - TAKER_FEE
                if edge < MIN_EDGE: continue
                size = min(
                    kelly_size(1.0 - new_p, m.no_ask, balance),
                    m.no_depth * 0.10, MAX_BET_SIZE
                )
                if size < MIN_BET_SIZE: continue
                out.append({
                    "strategy": "DEP_GRAPH",
                    "market_id": m.condition_id,
                    "question":  m.question,
                    "token_id":  m.no_token_id,
                    "outcome":   "NO",
                    "price":     m.no_ask,
                    "edge":      round(edge, 5),
                    "size":      size,
                    "reason":    f"{sym} {ret:+.3%}/10s δ={delta:+.4f}",
                })
    return out


# ── Main Loop ─────────────────────────────────────────────────────────────────

# Settlement thresholds — market is considered resolved when price crosses these
SETTLE_WIN_THRESHOLD  = 0.97   # YES/NO ask above this → that side won
SETTLE_LOSE_THRESHOLD = 0.03   # YES/NO ask below this → that side lost


async def run():
    log.info("ORACULUS v4 Engine starting...")
    account  = PaperAccount()
    markets: List[MarketData] = []
    nav_hist = []
    all_sigs = []
    scan_n   = 0
    strat_cnt = {"SPREAD_FADE": 0, "CROSS_MARKET": 0, "DEP_GRAPH": 0}

    last_market_ref = 0.0
    refresh_task    = None

    connector = aiohttp.TCPConnector(limit=100)
    async with aiohttp.ClientSession(connector=connector) as session:
        fetcher     = DataFetcher(session)
        crypto_feed = BinanceWSFeed()
        crypto_feed.set_session(session)
        asyncio.create_task(crypto_feed.run_forever())

        async def do_refresh():
            nonlocal markets, last_market_ref
            log.info("Refreshing markets (background)...")
            try:
                fresh = await fetcher.fetch_markets()
                if fresh:
                    markets = fresh
                    last_market_ref = time.time()
            except Exception as e:
                log.error(f"Market refresh error: {e}")

        while True:
            now = time.time()

            # Background market refresh
            if now - last_market_ref > MARKET_REFRESH_SEC:
                if refresh_task is None or refresh_task.done():
                    refresh_task = asyncio.create_task(do_refresh())
                last_market_ref = now

            crypto = crypto_feed.get()

            # ── Strategy scans ────────────────────────────────────────────────
            signals = []
            if markets:
                sf = scan_spread_fade(markets, account.balance)
                cm = scan_cross_market(markets, account.balance)
                dg = scan_dep_graph(markets, crypto, account.balance)
                signals += sf + cm + dg

                if scan_n % 20 == 1:
                    btc_ret = (crypto.get("btcusdt") or CryptoPrice("btcusdt", 0)).return_10s
                    feed_src = "BinanceWS" if crypto_feed.ws_ok else "CoinGecko"
                    log.info(
                        f"Signals: SF={len(sf)} CM={len(cm)} DG={len(dg)} "
                        f"| mkts={len(markets)} held={len(account.positions)} "
                        f"bal=${account.balance:.0f} btc_ret={btc_ret:+.4f} src={feed_src}"
                    )

            scan_n += 1
            nav_hist.append({"t": datetime.now(timezone.utc).isoformat(), "nav": account.nav})
            if len(nav_hist) > 500: nav_hist = nav_hist[-500:]

            # ── Position settlement ───────────────────────────────────────────
            #
            # For each open position, check if the current CLOB price indicates
            # the market has resolved. Settle those positions and credit balance.
            #
            mkt_map = {m.condition_id: m for m in markets}

            for tid, p in list(account.positions.items()):
                m = mkt_map.get(p.get("market_id", ""))
                if not m:
                    continue
                outcome = p["outcome"]
                if outcome == "YES":
                    if m.yes_ask > SETTLE_WIN_THRESHOLD:
                        account.settle(tid, 1.0)   # YES won
                    elif m.yes_ask < SETTLE_LOSE_THRESHOLD:
                        account.settle(tid, 0.0)   # YES lost
                elif outcome == "NO":
                    if m.no_ask > SETTLE_WIN_THRESHOLD:
                        account.settle(tid, 1.0)   # NO won
                    elif m.no_ask < SETTLE_LOSE_THRESHOLD:
                        account.settle(tid, 0.0)   # NO lost

            # ── Stale position eviction ───────────────────────────────────────
            #
            # FIX v3→v4: stale eviction now credits mark-to-market value back
            # to balance instead of silently destroying capital.
            # Positions >48h old have almost certainly resolved on-chain;
            # we settle at current mid-price as best estimate.
            #
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            stale  = [
                k for k, p in account.positions.items()
                if datetime.fromisoformat(
                    p.get("opened_at", "2000-01-01T00:00:00+00:00")
                    .replace("Z", "+00:00")
                ) < cutoff
            ]
            for k in stale:
                p = account.positions.get(k)
                if not p: continue
                # Best-guess settle price from current market mid
                m = mkt_map.get(p.get("market_id", ""))
                if m:
                    if p["outcome"] == "YES":
                        cur = _mid(m.yes_ask, m.yes_bid)
                    else:
                        cur = _mid(m.no_ask, m.no_bid)
                else:
                    cur = 0.0   # Market gone from scan — assume total loss
                account.settle(k, cur)
                log.info(f"Evicted stale position {k[:20]} at {cur:.3f}")

            # ── Deduplication + execution ─────────────────────────────────────
            held  = set(account.positions.keys())
            seen  = set(); clean = []
            for sig in sorted(signals, key=lambda x: x["edge"], reverse=True):
                if sig["token_id"] in held or sig["token_id"] in seen: continue
                seen.add(sig["token_id"]); clean.append(sig)
            signals = clean

            for sig in signals:
                strat_cnt[sig["strategy"]] = strat_cnt.get(sig["strategy"], 0) + 1
            all_sigs = (signals + all_sigs)[:50]

            for sig in signals[:5]:
                if account.balance < MIN_BET_SIZE: break
                t = account.buy(
                    sig["market_id"], sig["question"],
                    sig["token_id"], sig["outcome"], sig["price"],
                    sig["size"], sig["strategy"], sig["edge"]
                )
                if t:
                    log.info(
                        f"TRADE {t['id']} {sig['strategy']} "
                        f"{sig['outcome']} @{sig['price']:.4f} "
                        f"${t['cost']:.2f} edge={sig['edge']:.4f}"
                    )

            # ── State write ───────────────────────────────────────────────────
            btc = crypto.get("btcusdt")
            eth = crypto.get("ethusdt")
            edge_top = max((s["edge"] for s in all_sigs[:10]), default=0.0)

            total_unrealized = 0.0
            pos_out = {}
            for tid, p in account.positions.items():
                m = mkt_map.get(p.get("market_id", ""))
                if m:
                    cur    = m.yes_ask if p["outcome"] == "YES" else m.no_ask
                    unreal = (cur - p["avg_entry"]) * p["size"]
                else:
                    unreal = 0.0
                total_unrealized += unreal
                pos_out[tid] = {
                    **p,
                    "unrealized_pnl": round(unreal, 4),
                    "pnl_pct": round(
                        (unreal / max(p["avg_entry"] * p["size"], 0.01)) * 100, 2
                    ),
                }

            nav_mtm = account.balance + account.invested + total_unrealized

            feed_label = "BINANCE_WS" if crypto_feed.ws_ok else "COINGECKO"

            save_json(STATE_FILE, {
                "balance":        account.balance,
                "positions":      pos_out,
                "trades":         account.trades[-100:],
                "realized_pnl":   account.realized_pnl,
                "unrealized_pnl": round(total_unrealized, 4),
                "nav":            account.nav,
                "nav_mtm":        round(nav_mtm, 2),
                "win_rate":       account.win_rate,
                "total_fees":     account.total_fees,
                "num_trades":     len(account.trades),
                "markets": [
                    {
                        "question":      m.question[:80],
                        "yes_price":     m.yes_ask,
                        "no_price":      m.no_ask,
                        "yes_bid":       m.yes_bid,
                        "no_bid":        m.no_bid,
                        "category":      m.category,
                        "neg_risk_edge": m.neg_risk_edge,
                        "yes_depth":     round(m.yes_depth, 2),
                        "volume":        m.volume,
                    }
                    for m in sorted(markets, key=lambda x: x.neg_risk_edge, reverse=True)[:150]
                ],
                "signals":  all_sigs[:30],
                "crypto": {
                    k: {
                        "symbol":    v.symbol,
                        "price":     v.price,
                        "bid":       v.bid,
                        "ask":       v.ask,
                        "return_5s": v.return_10s,   # keep key name for dashboard compat
                        "change_24h": v.change_24h,
                    }
                    for k, v in crypto.items()
                },
                "nav_history":  nav_hist[-200:],
                "strat_counts": strat_cnt,
                "scan_count":   scan_n,
                "market_count": len(markets),
                "engine_alive": True,
                "ws_connected": crypto_feed.ws_ok,
                "crypto_source": feed_label,
                "edge_top":     round(edge_top, 4),
                "saved_at":     datetime.now(timezone.utc).isoformat(),
            })

            await asyncio.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(run())
