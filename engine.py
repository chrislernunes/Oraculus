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
    Maintains a live Binance WebSocket stream for BTC/ETH/SOL.
    Prices update the moment Binance ticks — no polling lag.
    Falls back to REST if WebSocket fails.
    """
    SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]
    WS_URL  = "wss://stream.binance.com:9443/stream?streams=" + \
              "/".join(f"{s}@bookTicker" for s in SYMBOLS)

    def __init__(self):
        self._prices: Dict[str, CryptoPrice] = {}
        self._hist:   Dict[str, List[Tuple[float,float]]] = {}
        self._pct24:  Dict[str, float] = {}
        self._ws_ok   = False
        self._session: Optional[aiohttp.ClientSession] = None

    def set_session(self, s: aiohttp.ClientSession):
        self._session = s

    def get(self) -> Dict[str, CryptoPrice]:
        return dict(self._prices)

    def _update(self, sym: str, bid: float, ask: float):
        price = (bid + ask) / 2
        now   = time.time()
        hist  = self._hist.setdefault(sym, [])
        hist.append((now, price))
        # Keep 30s of history
        self._hist[sym] = [(t,p) for t,p in hist if t > now - 30]
        old = [p for t,p in self._hist[sym] if t <= now - 5]
        ret5 = (price - old[-1]) / old[-1] if old and old[-1] > 0 else 0.0
        pct24 = self._pct24.get(sym, 0.0)
        self._prices[sym] = CryptoPrice(sym, round(price,2), bid, ask, ret5, pct24)

    async def run_forever(self):
        """Background task — reconnects on any failure."""
        # First fetch 24h change via REST (refreshed every 5 min)
        asyncio.create_task(self._refresh_24h_loop())
        while True:
            try:
                await self._ws_loop()
            except Exception as e:
                log.warning(f"CryptoFeed WS error: {e} — reconnecting in 3s")
                self._ws_ok = False
                await asyncio.sleep(3)

    async def _ws_loop(self):
        log.info("CryptoFeed: connecting to Binance WebSocket...")
        async with self._session.ws_connect(
                self.WS_URL,
                heartbeat=20) as ws:
            self._ws_ok = True
            log.info("CryptoFeed: Binance WebSocket CONNECTED")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    stream = data.get("stream","")
                    d      = data.get("data", {})
                    # bookTicker: b=best bid, a=best ask
                    sym = stream.split("@")[0]
                    if sym in self.SYMBOLS and d.get("b") and d.get("a"):
                        self._update(sym, float(d["b"]), float(d["a"]))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _refresh_24h_loop(self):
        """Refresh 24h % change from REST every 5 minutes."""
        while True:
            try:
                if self._session:
                    tasks = [
                        self._session.get(
                            f"{BINANCE_REST}/api/v3/ticker/24hr",
                            params={"symbol": s.upper()},
                            timeout=aiohttp.ClientTimeout(total=8)
                        ) for s in self.SYMBOLS
                    ]
                    resps = await asyncio.gather(*tasks, return_exceptions=True)
                    for sym, resp in zip(self.SYMBOLS, resps):
                        if isinstance(resp, Exception): continue
                        async with resp as r:
                            if r.status == 200:
                                d = await r.json(content_type=None)
                                self._pct24[sym] = float(d.get("priceChangePercent", 0))
                                # If WS hasn't connected yet, seed price from REST
                                if sym not in self._prices and d.get("lastPrice"):
                                    p = float(d["lastPrice"])
                                    self._update(sym, p*0.9995, p*1.0005)
                                    log.info(f"CryptoFeed REST seed {sym}: ${p:,.2f}")
            except Exception as e:
                log.debug(f"24h REST refresh error: {e}")
            await asyncio.sleep(300)


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
                yes_depth = sum(float(a.get("price",0))*float(a.get("size",0)) for a in asks[:5])

            if nbook:
                asks = sorted(nbook.get("asks",[]), key=lambda x: float(x.get("price",999)))
                bids = sorted(nbook.get("bids",[]), key=lambda x: float(x.get("price",0)), reverse=True)
                if asks: no_ask = float(asks[0].get("price",0))
                if bids: no_bid = float(bids[0].get("price",0))
                no_depth = sum(float(a.get("price",0))*float(a.get("size",0)) for a in asks[:5])

            # Gamma mid fallback
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
    if price <= 0 or price >= 1 or prob <= 0: return 0.0
    b = (1-price)/price
    f = max(0.0, min((b*prob-(1-prob))/b, MAX_KELLY_FRACTION))
    return min(f*balance, MAX_BET_SIZE)


def scan_spread_fade(markets: List[MarketData], balance: float) -> List[dict]:
    """
    SPREAD_FADE: On highly liquid markets, the NO side of a very low-probability
    event is often overpriced by market maker spread. Buy NO when:
      - YES ask is very low (< 0.08) meaning event is unlikely
      - NO ask < 0.97 (market maker spread leaves NO underpriced vs true ~0.96+)
      - Sufficient depth on NO side
    Also catches YES side when it's clearly underpriced vs complement.
    """
    out = []
    for m in markets:
        # ── Case 1: Long NO on very unlikely events ──
        # If YES_ask < 0.07, the true NO probability ≈ 1 - YES_ask = 0.93+
        # But we can buy NO at no_ask. If no_ask < (1 - yes_ask - TAKER_FEE*2), edge exists.
        if 0.01 < m.yes_ask < 0.08:
            implied_no = 1.0 - m.yes_ask
            edge = implied_no - m.no_ask - TAKER_FEE
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
                        "edge": edge,
                        "size": size,
                        "reason": f"YES_ask={m.yes_ask:.3f} → implied_NO={implied_no:.3f} vs ask={m.no_ask:.3f}"
                    })

        # ── Case 2: Long YES on near-certain events ──
        if 0.92 < m.yes_ask < 0.985:
            implied_yes = 1.0 - m.no_ask
            edge = implied_yes - m.yes_ask - TAKER_FEE
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
                        "edge": edge,
                        "size": size,
                        "reason": f"NO_ask={m.no_ask:.3f} → implied_YES={implied_yes:.3f} vs ask={m.yes_ask:.3f}"
                    })
    return out


def scan_cross_market(markets: List[MarketData], balance: float) -> List[dict]:
    """
    CROSS_MARKET: Find groups of mutually exclusive markets (same event, multiple
    candidates) where the sum of all YES prices exceeds 1.0 by enough to cover fees.
    Buy the cheapest NO in each oversubscribed group.

    Example: "Will X win championship?" × 8 teams. If sum(YES_asks) = 1.15,
    the market is oversubscribed. Buy NO on the most overpriced (highest YES/NO ratio
    vs implied probability) candidate.
    """
    out = []

    # Group markets by detected topic
    def topic_key(q: str) -> Optional[str]:
        q = q.lower()
        patterns = [
            (r"win the 2026 nhl stanley cup", "NHL2026"),
            (r"win the 2026 nba finals",      "NBA_FINALS_2026"),
            (r"win the nba (eastern|western) conference finals", "NBA_CONF_2026"),
            (r"win the 202[56][–-]2[67] nba mvp", "NBA_MVP"),
            (r"win the 2026 masters",          "MASTERS_2026"),
            (r"win the 2026 fifa world cup",   "FIFA_WC_2026"),
            (r"win the 202[56][–-]2[67] champions league", "UCL"),
            (r"win the 202[56][–-]2[67] english premier league", "EPL"),
            (r"win the 202[56][–-]2[67] la liga", "LALIGA"),
            (r"win the 202[56][–-]2[67] serie a", "SERIEA"),
            (r"win the 2028 (republican|democratic|us) presidential", "PRES_2028"),
            (r"announced as next james bond", "BOND"),
        ]
        for pat, key in patterns:
            if re.search(pat, q): return key
        return None

    groups: Dict[str, List[MarketData]] = {}
    for m in markets:
        k = topic_key(m.question)
        if k:
            groups.setdefault(k, []).append(m)

    for group_name, grp in groups.items():
        if len(grp) < 3: continue
        yes_sum = sum(m.yes_ask for m in grp)
        # Oversubscribed: sum of YES prices > 1.0 + fees
        overcharge = yes_sum - 1.0
        if overcharge < CROSS_MIN_OVERCHARGE: continue

        # Sort by (yes_ask / volume) desc — most overpriced relative to volume
        grp_sorted = sorted(grp, key=lambda m: m.yes_ask, reverse=True)

        # Buy NO on the top overpriced candidates (capped at 3 per group)
        for m in grp_sorted[:3]:
            # Edge: overcharge is shared across all members
            per_market_edge = overcharge / len(grp) - TAKER_FEE
            if per_market_edge < MIN_EDGE: continue
            if m.no_depth < MIN_DEPTH: continue
            # Implied NO = 1 - (yes_sum - this_yes) / (len-1) ... simplified:
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
                "edge": per_market_edge,
                "size": size,
                "reason": f"{group_name} YES_sum={yes_sum:.3f} overcharge={overcharge:.3f}"
            })

    return out


def scan_dep_graph(markets: List[MarketData], crypto: Dict[str, CryptoPrice],
                   balance: float) -> List[dict]:
    """
    DEP_GRAPH: Crypto price-ladder consistency.
    If BTC is at $X, then "Will BTC hit $X-10k" should price higher than "Will BTC hit $X+10k".
    Find adjacent ladder markets where cheaper strike prices YES < more expensive strike YES.
    Also includes latency arb: if BTC just moved, adjacent ladder markets may not have repriced.
    """
    out = []

    # ── Price ladder consistency ──
    groups: Dict[str, list] = {}
    for m in markets:
        q = m.question.lower()
        asset = next((a for a,kws in [
            ("BTC", ["btc","bitcoin"]),
            ("ETH", ["eth","ethereum"]),
            ("SOL", ["sol","solana"])
        ] if any(k in q for k in kws)), None)
        # Match price targets: $80k, $100,000, $1m, etc.
        mt = re.search(r'\$([\d,]+\.?\d*)([kKmM]?)', m.question)
        if asset and mt:
            v = float(mt.group(1).replace(",",""))
            sfx = mt.group(2).lower()
            if sfx == 'k': v *= 1_000
            elif sfx == 'm': v *= 1_000_000
            groups.setdefault(asset, []).append((m, v))

    for asset, grp in groups.items():
        grp.sort(key=lambda x: x[1])
        cp = crypto.get(asset.lower()+"usdt")
        spot = cp.price if cp else 0.0

        for i in range(len(grp)-1):
            ml, vl = grp[i]   # lower strike
            mh, vh = grp[i+1] # higher strike

            # Constraint: P(hit lower_strike) >= P(hit higher_strike)
            # Violation: YES(lower) < YES(higher) — mispriced
            viol = mh.yes_ask - ml.yes_ask
            if viol < GRAPH_MIN_VIOLATION: continue

            size = kelly_size(ml.yes_ask + viol/2, ml.yes_ask, balance)
            size = min(size, ml.yes_depth * 0.10, MAX_BET_SIZE)
            if size < MIN_BET_SIZE: continue

            reason = f"{asset}@{spot:,.0f} strike${vl:,.0f}<${vh:,.0f} viol={viol:.3f}"
            # Buy the underpriced lower-strike YES
            out.append({"strategy": "DEP_GRAPH",
                "market_id": ml.condition_id, "question": ml.question,
                "token_id": ml.yes_token_id, "outcome": "YES",
                "price": ml.yes_ask, "edge": viol/2, "size": size,
                "reason": reason})

    # ── Latency arb: crypto just moved, prediction markets haven't repriced ──
    KW = {"btcusdt": ["btc","bitcoin","$80","$90","$100","$150","$200","hit $"],
          "ethusdt": ["eth","ethereum","$3","$4","$5","hit $"],
          "solusdt": ["sol","solana","hit $"]}
    TH = {"btcusdt": BTC_MOVE_THRESHOLD, "ethusdt": ETH_MOVE_THRESHOLD, "solusdt": 0.005}

    for sym, cp in crypto.items():
        if abs(cp.return_5s) < TH.get(sym, 0.005): continue
        kws  = KW.get(sym, [])
        up   = cp.return_5s > 0
        for m in markets:
            if not any(k in m.question.lower() for k in kws): continue
            new_p = max(0.05, min(0.95, m.yes_ask + 0.35 * cp.return_5s))
            if up:
                edge = new_p - m.yes_ask - TAKER_FEE
                if edge < MIN_EDGE: continue
                size = min(kelly_size(new_p, m.yes_ask, balance),
                           m.yes_depth*0.10, MAX_BET_SIZE)
                if size < MIN_BET_SIZE: continue
                out.append({"strategy": "DEP_GRAPH",
                    "market_id": m.condition_id, "question": m.question,
                    "token_id": m.yes_token_id, "outcome": "YES",
                    "price": m.yes_ask, "edge": edge, "size": size,
                    "reason": f"{sym} {cp.return_5s:+.3%}/5s → revalue"})
            else:
                edge = (1 - new_p) - m.no_ask - TAKER_FEE
                if edge < MIN_EDGE: continue
                size = min(kelly_size(1-new_p, m.no_ask, balance),
                           m.no_depth*0.10, MAX_BET_SIZE)
                if size < MIN_BET_SIZE: continue
                out.append({"strategy": "DEP_GRAPH",
                    "market_id": m.condition_id, "question": m.question,
                    "token_id": m.no_token_id, "outcome": "NO",
                    "price": m.no_ask, "edge": edge, "size": size,
                    "reason": f"{sym} {cp.return_5s:+.3%}/5s → revalue"})
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

    connector = aiohttp.TCPConnector(limit=100, ssl=False)
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

            # ── Run strategies ────────────────────────────────────────────
            signals = []
            if markets:
                signals += scan_spread_fade(markets, account.balance)
                signals += scan_cross_market(markets, account.balance)
                signals += scan_dep_graph(markets, crypto, account.balance)

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
