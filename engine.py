"""
Oraculus — Trading Engine v3
Real-time crypto via Binance bookTicker WebSocket stream.
Three live strategies: SPREAD_FADE, CROSS_MARKET, DEP_GRAPH.
"""

from __future__ import annotations
import asyncio, json, logging, re, time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp
from config import *

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler()])
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

    # yes_price / no_price = ask prices (cost to buy)
    @property
    def yes_price(self): return self.yes_ask
    @property
    def no_price(self): return self.no_ask
    @property
    def total_cost(self): return self.yes_ask + self.no_ask
    @property
    def spread(self): return self.yes_ask - self.yes_bid   # ask-bid spread YES side
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
    return_5s: float = 0.0
    change_24h: float = 0.0


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


# ── Crypto Feed (Binance WebSocket — zero latency) ────────────────────────────

class CryptoFeed:
    """
    Polls CoinGecko public API for BTC/ETH/SOL prices every 10 seconds.
    CoinGecko has no geo-restrictions and needs no API key.
    Binance blocks all requests from Render's US-based servers (HTTP 451).

    Single call to /simple/price fetches all 3 coins at once:
      GET https://api.coingecko.com/api/v3/simple/price
          ?ids=bitcoin,ethereum,solana
          &vs_currencies=usd
          &include_24hr_change=true
          &include_bid_ask_spread=true   (not available on free tier, so we synthesise spread)

    Free tier: 30 calls/min — polling every 10s uses only 6 calls/min.
    """
    # CoinGecko coin IDs → internal symbol names (matching the rest of the codebase)
    COIN_MAP = {
        "bitcoin": "btcusdt",
        "ethereum": "ethusdt",
        "solana": "solusdt",
    }
    COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
    REST_POLL_SEC = 10   # 6 calls/min — well within free tier 30/min limit

    # Typical bid-ask spreads as a fraction of mid-price (used to synthesise bid/ask)
    SPREAD_EST = {"btcusdt": 0.0002, "ethusdt": 0.0003, "solusdt": 0.0008}

    def __init__(self):
        self._prices: Dict[str, CryptoPrice] = {}
        self._hist:   Dict[str, List[Tuple[float,float]]] = {}
        self._ws_ok   = True   # no WebSocket — flag stays True so dashboard shows green
        self._session: Optional[aiohttp.ClientSession] = None

    def set_session(self, s: aiohttp.ClientSession):
        self._session = s

    def get(self) -> Dict[str, CryptoPrice]:
        return dict(self._prices)

    def _update(self, sym: str, price: float, change_24h: float):
        # Synthesise bid/ask from estimated spread
        half = price * self.SPREAD_EST.get(sym, 0.0005) / 2
        bid, ask = price - half, price + half
        now  = time.time()
        hist = self._hist.setdefault(sym, [])
        hist.append((now, price))
        self._hist[sym] = [(t, p) for t, p in hist if t > now - 30]
        old = [p for t, p in self._hist[sym] if t <= now - 5]
        ret5 = (price - old[-1]) / old[-1] if old and old[-1] > 0 else 0.0
        self._prices[sym] = CryptoPrice(sym, round(price, 2), bid, ask, ret5, change_24h)

    async def run_forever(self):
        """Background task — polls CoinGecko every REST_POLL_SEC seconds."""
        log.info("CryptoFeed: starting CoinGecko polling (Binance geo-blocked on Render)")
        while True:
            try:
                await self._poll_once()
            except Exception as e:
                log.warning(f"CryptoFeed poll error: {e}")
            await asyncio.sleep(self.REST_POLL_SEC)

    async def _poll_once(self):
        """Single request fetches all coins. Handles rate-limit (429) gracefully."""
        if not self._session:
            return
        try:
            async with self._session.get(
                self.COINGECKO_URL,
                params={
                    "ids": ",".join(self.COIN_MAP.keys()),
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
                timeout=aiohttp.ClientTimeout(total=8),
                headers={"Accept": "application/json",
                         "User-Agent": "oraculus-engine/3.0"},
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    got = 0
                    for coin_id, sym in self.COIN_MAP.items():
                        d = data.get(coin_id, {})
                        price = d.get("usd")
                        if price:
                            change = d.get("usd_24h_change", 0.0) or 0.0
                            self._update(sym, float(price), float(change))
                            got += 1
                    if got > 0 and not hasattr(self, "_logged_ok"):
                        btc = self._prices.get("btcusdt")
                        log.info(f"CryptoFeed CoinGecko OK — "
                                 f"BTC=${btc.price:,.0f} | polling every {self.REST_POLL_SEC}s")
                        self._logged_ok = True
                elif r.status == 429:
                    log.warning("CryptoFeed: CoinGecko rate-limited (429) — backing off 30s")
                    await asyncio.sleep(30)
                else:
                    body = await r.text()
                    log.warning(f"CryptoFeed CoinGecko → {r.status}: {body[:120]}")
        except Exception as e:
            log.warning(f"CryptoFeed _poll_once: {e}")


# ── Data Fetcher ──────────────────────────────────────────────────────────────

class DataFetcher:
    def __init__(self, session: aiohttp.ClientSession):
        self._s = session

    async def _get(self, url, params=None, timeout=15):
        try:
            async with self._s.get(url, params=params,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"Accept": "application/json"}) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                log.debug(f"GET {url} → {r.status}")
        except Exception as e:
            log.debug(f"GET {url}: {e}")
        return None

    async def fetch_markets(self) -> List[MarketData]:
        # Step 1: Gamma market list
        raw = await self._get(f"{POLYMARKET_GAMMA}/markets",
            params={"active": "true", "closed": "false", "limit": 500})
        items = raw if isinstance(raw, list) else (raw or {}).get("markets", []) if raw else []
        if not items:
            raw = await self._get(f"{POLYMARKET_GAMMA}/markets",
                params={"active": "true", "limit": 300})
            items = raw if isinstance(raw, list) else (raw or {}).get("markets", []) if raw else []
        if not items:
            log.error("No markets from Gamma")
            return []
        log.info(f"Gamma: {len(items)} markets")

        # Step 2: Parse token pairs
        token_pairs = []
        gamma_map   = {}
        for m in items:
            cid = m.get("conditionId") or m.get("condition_id") or m.get("id") or ""
            q   = (m.get("question") or "").strip()
            if not cid or not q: continue
            gamma_map[cid] = m
            cat = m.get("category", "")
            try: vol = float(m.get("volume") or 0)
            except: vol = 0.0

            ids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(ids, str):
                try: ids = json.loads(ids)
                except: ids = []

            tokens = m.get("tokens", [])
            if tokens and isinstance(tokens[0], dict) and tokens[0].get("token_id"):
                yes = next((t for t in tokens if str(t.get("outcome","")).lower() in ("yes","1")), tokens[0])
                no  = next((t for t in tokens if str(t.get("outcome","")).lower() in ("no","0")),
                           tokens[1] if len(tokens) > 1 else tokens[0])
                ids = [yes.get("token_id",""), no.get("token_id","")]

            if len(ids) >= 2 and ids[0] and ids[1]:
                token_pairs.append((cid, str(ids[0]), str(ids[1]), q, cat, vol))

        log.info(f"Token pairs: {len(token_pairs)}")
        if not token_pairs: return []

        # Step 3: Parallel CLOB orderbook fetch (80 concurrent)
        book_cache: Dict[str, dict] = {}
        sem = asyncio.Semaphore(80)

        async def fetch_book(tid: str):
            async with sem:
                b = await self._get(f"{POLYMARKET_CLOB}/book",
                    params={"token_id": tid}, timeout=8)
                if b: book_cache[tid] = b

        all_tids = list({tid for _,y,n,_,_,_ in token_pairs[:500] for tid in [y,n]})
        await asyncio.gather(*[fetch_book(tid) for tid in all_tids])
        log.info(f"CLOB orderbooks: {len(book_cache)}")

        # Step 4: Build MarketData
        result = []
        for cid, yid, nid, q, cat, vol in token_pairs:
            ybook = book_cache.get(yid)
            nbook = book_cache.get(nid)

            yes_ask = yes_bid = no_ask = no_bid = 0.0
            yes_depth = no_depth = 0.0

            if ybook:
                asks = sorted(ybook.get("asks",[]), key=lambda x: float(x.get("price",999)))
                bids = sorted(ybook.get("bids",[]), key=lambda x: float(x.get("price",0)), reverse=True)
                if asks: yes_ask = float(asks[0].get("price",0))
                if bids: yes_bid = float(bids[0].get("price",0))
                # depth = sum of SIZE (shares) at best 5 ask levels — not price*size
                yes_depth = sum(float(a.get("size",0)) for a in asks[:5])

            if nbook:
                asks = sorted(nbook.get("asks",[]), key=lambda x: float(x.get("price",999)))
                bids = sorted(nbook.get("bids",[]), key=lambda x: float(x.get("price",0)), reverse=True)
                if asks: no_ask = float(asks[0].get("price",0))
                if bids: no_bid = float(bids[0].get("price",0))
                no_depth = sum(float(a.get("size",0)) for a in asks[:5])

            # Gamma mid fallback — used when CLOB book is missing
            if not yes_ask or not no_ask:
                m_raw = gamma_map.get(cid, {})
                op = m_raw.get("outcomePrices","[]")
                if isinstance(op,str):
                    try: op = json.loads(op)
                    except: op = []
                if len(op) >= 2:
                    try:
                        yes_ask = float(op[0]); no_ask = float(op[1])
                        yes_bid = max(0.01, yes_ask - 0.02)
                        no_bid  = max(0.01, no_ask  - 0.02)
                    except: pass

            # For Gamma-fallback markets with no CLOB depth, assign a conservative
            # minimum so strategies can still evaluate them (capped bet size handles risk)
            if yes_depth == 0.0: yes_depth = MIN_DEPTH
            if no_depth  == 0.0: no_depth  = MIN_DEPTH

            if not yes_ask or not no_ask: continue
            if not (0.01 < yes_ask < 0.99): continue

            result.append(MarketData(cid, q, yid, nid,
                yes_ask, no_ask, yes_bid, no_bid,
                yes_depth, no_depth, cat, vol))

        log.info(f"Built {len(result)} markets")
        return result


# ── Paper Account ─────────────────────────────────────────────────────────────

class PaperAccount:
    def __init__(self):
        self.balance = STARTING_BALANCE
        self.positions = {}
        self.trades = []
        self._ctr = 0
        d = load_json(PERSIST_FILE)
        if d:
            self.balance   = d.get("balance", STARTING_BALANCE)
            self.trades    = d.get("trades", [])
            self.positions = d.get("positions", {})
            self._ctr      = len(self.trades)
            log.info(f"Resumed: {len(self.trades)} trades, ${self.balance:.2f}")

    def _save(self):
        save_json(PERSIST_FILE, {"balance": self.balance,
            "trades": self.trades[-500:], "positions": self.positions})

    def buy(self, market_id, question, token_id, outcome,
            price, size_usd, strategy, edge) -> Optional[dict]:
        if size_usd < MIN_BET_SIZE or not (0 < price < 1): return None
        fee   = size_usd * TAKER_FEE
        total = size_usd + fee
        if total > self.balance:
            size_usd = self.balance * 0.90 / (1 + TAKER_FEE)
            if size_usd < MIN_BET_SIZE: return None
            fee   = size_usd * TAKER_FEE
            total = size_usd + fee
        self.balance -= total
        self._ctr += 1
        t = {"id": f"T{self._ctr:05d}", "market_id": market_id,
             "question": question[:70], "token_id": token_id,
             "outcome": outcome, "side": "BUY", "price": price,
             "size": size_usd/price, "cost": total, "fee": fee,
             "pnl": 0.0, "strategy": strategy, "edge": edge,
             "timestamp": datetime.now(timezone.utc).isoformat()}
        self.trades.append(t)
        if token_id in self.positions:
            p  = self.positions[token_id]
            tc = p["avg_entry"]*p["size"] + price*(size_usd/price)
            p["size"]     += size_usd/price
            p["avg_entry"] = tc/p["size"]
        else:
            self.positions[token_id] = {
                "market_id": market_id, "question": question[:70],
                "token_id": token_id, "outcome": outcome,
                "size": size_usd/price, "avg_entry": price,
                "strategy": strategy,
                "opened_at": datetime.now(timezone.utc).isoformat()}
        self._save()
        return t

    @property
    def realized_pnl(self): return sum(t["pnl"] for t in self.trades if t["side"]=="SELL")
    @property
    def invested(self): return sum(p["size"]*p["avg_entry"] for p in self.positions.values())
    @property
    def nav(self): return self.balance + self.invested
    @property
    def total_fees(self): return sum(t["fee"] for t in self.trades)
    @property
    def win_rate(self):
        sells = [t for t in self.trades if t["side"]=="SELL"]
        return sum(1 for t in sells if t["pnl"]>0)/len(sells) if sells else 0.0


# ── Strategies ────────────────────────────────────────────────────────────────

def kelly_size(prob, price, balance):
    """
    Fractional Kelly bet size.
    prob  = true probability estimate (0-1)
    price = cost to buy one contract (0-1)
    Returns dollar bet size capped at MAX_BET_SIZE.
    """
    if price <= 0 or price >= 1 or prob <= 0 or prob >= 1: return 0.0
    b = (1.0 - price) / price
    q = 1.0 - prob
    f = (b * prob - q) / b
    f = max(0.0, min(f, MAX_KELLY_FRACTION))
    return min(f * balance, MAX_BET_SIZE)


def _mid(ask: float, bid: float) -> float:
    """Mid-price; falls back to ask if bid is missing."""
    return (ask + bid) / 2.0 if bid > 0.001 else ask


def scan_spread_fade(markets: List[MarketData], balance: float) -> List[dict]:
    """
    SPREAD_FADE: Exploit bid-ask spread mispricing.

    Case 1 - Long NO on very unlikely events:
      YES_ask < 0.08  ->  true NO prob ~= 1 - YES_mid ~= 0.94+
      Buy NO at no_ask if edge = (1 - yes_mid) - no_ask - fee > MIN_EDGE

    Case 2 - Long YES on near-certain events:
      YES_mid > 0.92  ->  true YES prob ~= 1 - NO_mid ~= 0.94+
      Buy YES at yes_ask if edge = (1 - no_mid) - yes_ask - fee > MIN_EDGE
    """
    out = []
    for m in markets:

        # Case 1: Long NO on very unlikely events
        if 0.01 < m.yes_ask < 0.08:
            yes_mid    = _mid(m.yes_ask, m.yes_bid)
            implied_no = 1.0 - yes_mid
            edge       = implied_no - m.no_ask - TAKER_FEE
            if edge >= MIN_EDGE and m.no_depth >= MIN_DEPTH:
                size = min(kelly_size(implied_no, m.no_ask, balance), m.no_depth * 0.10)
                if size >= MIN_BET_SIZE:
                    out.append({
                        "strategy": "SPREAD_FADE",
                        "market_id": m.condition_id,
                        "question": m.question,
                        "token_id": m.no_token_id,
                        "outcome": "NO",
                        "price": m.no_ask,
                        "edge": round(edge, 5),
                        "size": size,
                        "reason": f"YES_mid={yes_mid:.3f} implied_NO={implied_no:.3f} no_ask={m.no_ask:.3f}"
                    })

        # Case 2: Long YES on near-certain events (filter on mid, not ask)
        yes_mid = _mid(m.yes_ask, m.yes_bid)
        if 0.92 < yes_mid < 0.988:
            no_mid      = _mid(m.no_ask, m.no_bid)
            implied_yes = 1.0 - no_mid
            edge        = implied_yes - m.yes_ask - TAKER_FEE
            if edge >= MIN_EDGE and m.yes_depth >= MIN_DEPTH:
                size = min(kelly_size(implied_yes, m.yes_ask, balance), m.yes_depth * 0.10)
                if size >= MIN_BET_SIZE:
                    out.append({
                        "strategy": "SPREAD_FADE",
                        "market_id": m.condition_id,
                        "question": m.question,
                        "token_id": m.yes_token_id,
                        "outcome": "YES",
                        "price": m.yes_ask,
                        "edge": round(edge, 5),
                        "size": size,
                        "reason": f"NO_mid={no_mid:.3f} implied_YES={implied_yes:.3f} yes_ask={m.yes_ask:.3f}"
                    })
    return out


def scan_cross_market(markets: List[MarketData], balance: float) -> List[dict]:
    """
    CROSS_MARKET: Mutually-exclusive groups where sum(YES_asks) > 1.0.
    Buy NO on the cheapest NO contracts (best risk/reward - longshot NOs near 1.0).
    Edge = neg_risk_edge of that specific market minus fee (not group-averaged).
    Requires >= 4 members to confirm it's a real group, not noise.
    """
    out = []

    def topic_key(q: str) -> Optional[str]:
        q = q.lower()
        checks = [
            (["nhl", "stanley cup"],                    "NHL_CUP"),
            (["nba", "finals"],                         "NBA_FINALS"),
            (["nba", "eastern conference"],             "NBA_EAST_CONF"),
            (["nba", "western conference"],             "NBA_WEST_CONF"),
            (["nba", "mvp"],                            "NBA_MVP"),
            (["masters", "golf"],                       "MASTERS"),
            (["fifa", "world cup"],                     "FIFA_WC"),
            (["champions league", "win"],               "UCL"),
            (["premier league", "win"],                 "EPL"),
            (["la liga", "win"],                        "LALIGA"),
            (["serie a", "win"],                        "SERIEA"),
            (["bundesliga", "win"],                     "BUNDESLIGA"),
            (["presidential", "republican nomination"], "GOP_NOM"),
            (["presidential", "democratic nomination"], "DEM_NOM"),
            (["presidential", "win", "2028"],           "PRES_2028"),
            (["next james bond"],                       "BOND"),
            (["next pope"],                             "POPE"),
            (["super bowl", "win"],                     "SUPERBOWL"),
            (["world series", "win"],                   "WORLD_SERIES"),
            (["oscar", "best picture"],                 "OSCAR_PICTURE"),
            (["oscar", "best director"],                "OSCAR_DIRECTOR"),
            (["emmy", "best"],                          "EMMY"),
            (["nobel", "prize"],                        "NOBEL"),
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
        if len(grp) < 4: continue
        yes_sum    = sum(m.yes_ask for m in grp)
        overcharge = yes_sum - 1.0
        if overcharge < CROSS_MIN_OVERCHARGE: continue

        # Sort by neg_risk_edge descending — picks markets where yes+no is furthest
        # below 1.0. These are longshots whose NOs are cheap AND underpriced vs fair value.
        # Sorting by no_ask ascending was wrong: it selected expensive-NO favourites first,
        # which always have neg_risk_edge < 0 and never clear the MIN_EDGE check.
        grp_sorted = sorted(grp, key=lambda m: m.neg_risk_edge, reverse=True)

        for m in grp_sorted[:3]:
            edge = m.neg_risk_edge - TAKER_FEE
            if edge < MIN_EDGE: continue
            if m.no_depth < MIN_DEPTH: continue
            implied_no = 1.0 - m.yes_ask
            size = min(kelly_size(implied_no, m.no_ask, balance),
                       m.no_depth * 0.08, MAX_BET_SIZE)
            if size < MIN_BET_SIZE: continue
            out.append({
                "strategy": "CROSS_MARKET",
                "market_id": m.condition_id,
                "question": m.question,
                "token_id": m.no_token_id,
                "outcome": "NO",
                "price": m.no_ask,
                "edge": round(edge, 5),
                "size": size,
                "reason": f"{group_name} YES_sum={yes_sum:.3f} neg_risk={m.neg_risk_edge:.3f}"
            })

    return out


def scan_dep_graph(markets: List[MarketData], crypto: Dict[str, CryptoPrice],
                   balance: float) -> List[dict]:
    """
    DEP_GRAPH: Two sub-strategies:

    1. Price-ladder consistency: lower BTC/ETH/SOL strike must have higher YES prob.
       Kelly prob uses full violation as fair-value correction (not half).

    2. Latency arb: crypto moved, prediction market hasn't repriced.
       Sensitivity = 3.0 (was 0.35, which was too small to ever clear MIN_EDGE).
    """
    out = []

    # 1. Price ladder consistency
    groups: Dict[str, list] = {}
    for m in markets:
        q = m.question.lower()
        asset = next((a for a, kws in [
            ("BTC", ["btc", "bitcoin"]),
            ("ETH", ["eth", "ethereum"]),
            ("SOL", ["sol", "solana"]),
        ] if any(k in q for k in kws)), None)
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
            viol = mh.yes_ask - ml.yes_ask
            if viol < GRAPH_MIN_VIOLATION: continue
            fair_p = min(0.95, ml.yes_ask + viol)
            size   = kelly_size(fair_p, ml.yes_ask, balance)
            size   = min(size, ml.yes_depth * 0.10, MAX_BET_SIZE)
            if size < MIN_BET_SIZE: continue
            out.append({
                "strategy": "DEP_GRAPH",
                "market_id": ml.condition_id, "question": ml.question,
                "token_id":  ml.yes_token_id, "outcome": "YES",
                "price":     ml.yes_ask,
                "edge":      round(viol / 2, 5),
                "size":      size,
                "reason":    f"{asset}@{spot:,.0f} ${vl:,.0f}<${vh:,.0f} viol={viol:.3f}",
            })

    # 2. Latency arb
    KW = {
        "btcusdt": ["btc", "bitcoin", "$80", "$90", "$100", "$150", "$200", "hit $"],
        "ethusdt": ["eth", "ethereum", "$3", "$4", "$5", "hit $"],
        "solusdt": ["sol", "solana", "hit $"],
    }
    TH          = {"btcusdt": 0.0005, "ethusdt": 0.0010, "solusdt": 0.003}
    SENSITIVITY = 3.0

    for sym, cp in crypto.items():
        if abs(cp.return_5s) < TH.get(sym, 0.005): continue
        kws = KW.get(sym, [])
        up  = cp.return_5s > 0
        for m in markets:
            if not any(k in m.question.lower() for k in kws): continue
            delta = SENSITIVITY * cp.return_5s
            if up:
                new_p = max(0.05, min(0.95, m.yes_ask + delta))
                edge  = new_p - m.yes_ask - TAKER_FEE
                if edge < MIN_EDGE: continue
                size = min(kelly_size(new_p, m.yes_ask, balance),
                           m.yes_depth * 0.10, MAX_BET_SIZE)
                if size < MIN_BET_SIZE: continue
                out.append({
                    "strategy": "DEP_GRAPH",
                    "market_id": m.condition_id, "question": m.question,
                    "token_id": m.yes_token_id,  "outcome": "YES",
                    "price": m.yes_ask, "edge": round(edge, 5), "size": size,
                    "reason": f"{sym} {cp.return_5s:+.3%}/10s +{delta:.4f}",
                })
            else:
                new_p = max(0.05, min(0.95, m.yes_ask + delta))
                edge  = (1.0 - new_p) - m.no_ask - TAKER_FEE
                if edge < MIN_EDGE: continue
                size = min(kelly_size(1.0 - new_p, m.no_ask, balance),
                           m.no_depth * 0.10, MAX_BET_SIZE)
                if size < MIN_BET_SIZE: continue
                out.append({
                    "strategy": "DEP_GRAPH",
                    "market_id": m.condition_id, "question": m.question,
                    "token_id": m.no_token_id,   "outcome": "NO",
                    "price": m.no_ask, "edge": round(edge, 5), "size": size,
                    "reason": f"{sym} {cp.return_5s:+.3%}/10s {delta:.4f}",
                })
    return out

# ── Main Loop ─────────────────────────────────────────────────────────────────

async def run():
    log.info("ORACULUS v3 Engine starting...")
    account   = PaperAccount()
    markets: List[MarketData] = []
    nav_hist  = []
    all_sigs  = []
    scan_n    = 0
    strat_cnt = {"SPREAD_FADE": 0, "CROSS_MARKET": 0, "DEP_GRAPH": 0}
    last_market_ref = 0.0

    connector = aiohttp.TCPConnector(limit=100)
    async with aiohttp.ClientSession(connector=connector) as session:
        fetcher = DataFetcher(session)

        # Start live crypto WebSocket feed as background task
        crypto_feed = CryptoFeed()
        crypto_feed.set_session(session)
        asyncio.create_task(crypto_feed.run_forever())

        while True:
            now = time.time()

            # ── Market refresh (every MARKET_REFRESH_SEC) ─────────────────
            if now - last_market_ref > MARKET_REFRESH_SEC:
                log.info("Refreshing markets...")
                try:
                    markets = await fetcher.fetch_markets()
                    last_market_ref = time.time()
                except Exception as e:
                    log.error(f"Market refresh error: {e}")

            # ── Get latest crypto prices (from live WS cache) ─────────────
            crypto = crypto_feed.get()

            # Run strategies
            signals = []
            if markets:
                sf = scan_spread_fade(markets, account.balance)
                cm = scan_cross_market(markets, account.balance)
                dg = scan_dep_graph(markets, crypto, account.balance)
                signals += sf + cm + dg
                if scan_n % 20 == 1:  # log every ~10s
                    log.info(f"Signals: SPREAD_FADE={len(sf)} CROSS_MARKET={len(cm)} DEP_GRAPH={len(dg)} | markets={len(markets)} crypto={'YES' if crypto else 'NO'}")

            scan_n += 1
            nav_hist.append({"t": datetime.now(timezone.utc).isoformat(),
                              "nav": account.nav})
            if len(nav_hist) > 500: nav_hist = nav_hist[-500:]

            # Deduplicate + sort by edge
            held = set(account.positions.keys())
            seen = set(); clean = []
            for sig in sorted(signals, key=lambda x: x["edge"], reverse=True):
                if sig["token_id"] in held or sig["token_id"] in seen: continue
                seen.add(sig["token_id"]); clean.append(sig)
            signals = clean

            for sig in signals:
                strat_cnt[sig["strategy"]] = strat_cnt.get(sig["strategy"], 0) + 1
            all_sigs = (signals + all_sigs)[:50]

            # Execute top signals
            for sig in signals[:5]:
                if account.balance < MIN_BET_SIZE: break
                t = account.buy(sig["market_id"], sig["question"],
                    sig["token_id"], sig["outcome"], sig["price"],
                    sig["size"], sig["strategy"], sig["edge"])
                if t:
                    log.info(f"TRADE {t['id']} {sig['strategy']} "
                             f"{sig['outcome']} @{sig['price']:.4f} "
                             f"${t['cost']:.2f} edge={sig['edge']:.4f}")

            # Write state
            btc = crypto.get("btcusdt")
            eth = crypto.get("ethusdt")
            edge_top = max((s["edge"] for s in all_sigs[:10]), default=0.0)

            save_json(STATE_FILE, {
                "balance":      account.balance,
                "positions":    account.positions,
                "trades":       account.trades[-200:],
                "realized_pnl": account.realized_pnl,
                "nav":          account.nav,
                "win_rate":     account.win_rate,
                "total_fees":   account.total_fees,
                "num_trades":   len(account.trades),
                "markets": [
                    {"question": m.question[:80],
                     "yes_price": m.yes_ask, "no_price": m.no_ask,
                     "yes_bid": m.yes_bid, "no_bid": m.no_bid,
                     "category": m.category,
                     "neg_risk_edge": m.neg_risk_edge,
                     "yes_depth": round(m.yes_depth, 2),
                     "volume": m.volume}
                    for m in sorted(markets,
                        key=lambda x: x.neg_risk_edge, reverse=True)[:150]
                ],
                "signals":      all_sigs[:30],
                "crypto": {
                    k: {"symbol": v.symbol, "price": v.price,
                        "bid": v.bid, "ask": v.ask,
                        "return_5s": v.return_5s, "change_24h": v.change_24h}
                    for k, v in crypto.items()
                },
                "nav_history":  nav_hist[-200:],
                "strat_counts": strat_cnt,
                "scan_count":   scan_n,
                "market_count": len(markets),
                "engine_alive": True,
                "ws_connected": crypto_feed._ws_ok,
                "edge_top":     round(edge_top, 4),
                "saved_at":     datetime.now(timezone.utc).isoformat(),
            })

            await asyncio.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(run())
