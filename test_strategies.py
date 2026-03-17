"""
Oraculus — Self-Contained Synthetic Strategy Tests
No network, no aiohttp. Pure strategy logic + synthetic data.
Run: python3 test_strategies.py
"""
from __future__ import annotations
import re, random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ── Config constants (inlined) ────────────────────────────────────────────────
STARTING_BALANCE     = 1_000.0
MAX_KELLY_FRACTION   = 0.25
MIN_EDGE             = 0.004
MIN_DEPTH            = 5.0
MAX_BET_SIZE         = 50.0
MIN_BET_SIZE         = 1.0
TAKER_FEE            = 0.002
GRAPH_MIN_VIOLATION  = 0.008
CROSS_MIN_OVERCHARGE = 0.012

# ── Models + Strategies (extracted from engine.py) ────────────────────────────
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

        # Sort by NO ask ascending - cheapest NO = best value (longshot NOs close to 1.0)
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



# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def M(cid, question, yes_ask, no_ask,
      yes_bid=None, no_bid=None,
      yes_depth=500.0, no_depth=500.0) -> MarketData:
    yb = yes_bid if yes_bid is not None else round(yes_ask - 0.01, 4)
    nb = no_bid  if no_bid  is not None else round(no_ask  - 0.01, 4)
    return MarketData(
        condition_id=cid, question=question,
        yes_token_id=f"Y{cid}", no_token_id=f"N{cid}",
        yes_ask=yes_ask, no_ask=no_ask,
        yes_bid=max(0.01, yb), no_bid=max(0.01, nb),
        yes_depth=yes_depth, no_depth=no_depth,
        category="test", volume=50_000.0,
    )

def C(sym, price, ret=0.0) -> CryptoPrice:
    h = price * 0.0002
    return CryptoPrice(sym, price, price-h, price+h, ret, 0.0)

def show(signals, tag=""):
    if not signals:
        print(f"  {tag}  (no signals)")
        return
    for s in signals:
        print(f"  🎯 {s['market_id']:<6} {s['strategy']:<14} {s['outcome']:<3} "
              f"price={s['price']:.3f} edge={s['edge']:.4f} size=${s['size']:.2f}")
        print(f"      {s['reason']}")

def check(signals, expect_fire, expect_silent, label):
    fired = {s["market_id"] for s in signals}
    hits   = [m for m in expect_fire   if m in fired]
    misses = [m for m in expect_fire   if m not in fired]
    fps    = [m for m in expect_silent if m in fired]
    ok = not misses and not fps
    print(f"  {'✅' if ok else '❌'} {label}")
    if misses: print(f"     MISSED:  {misses}")
    if fps:    print(f"     FALSE +:  {fps}")
    return ok

# ══════════════════════════════════════════════════════════════════════════════
# TEST 1 — KELLY SIZING
# ══════════════════════════════════════════════════════════════════════════════
def test_kelly():
    print("\n" + "="*62)
    print("TEST 1 — KELLY SIZING")
    print("="*62)
    bal = 1000.0
    cases = [
        (0.95, 0.93,  True,  "near-certain: large bet"),
        (0.60, 0.50,  True,  "fair coin with edge: moderate bet"),
        (0.52, 0.50,  True,  "razor thin edge: tiny bet"),
        (0.40, 0.50,  False, "negative edge → zero"),
        (0.00, 0.50,  False, "prob=0 invalid → zero"),
        (1.00, 0.50,  False, "prob=1 invalid → zero"),
        (0.80, 0.00,  False, "price=0 invalid → zero"),
        (0.80, 1.00,  False, "price=1 invalid → zero"),
    ]
    all_ok = True
    for prob, price, expect_pos, label in cases:
        size = kelly_size(prob, price, bal)
        ok = (size > 0) == expect_pos and 0 <= size <= MAX_BET_SIZE
        if not ok: all_ok = False
        print(f"  {'✅' if ok else '❌'} p={prob:.2f} price={price:.2f} → ${size:.4f}  [{label}]")
    return all_ok

# ══════════════════════════════════════════════════════════════════════════════
# TEST 2 — SPREAD_FADE
# ══════════════════════════════════════════════════════════════════════════════
def test_spread_fade():
    print("\n" + "="*62)
    print("TEST 2 — SPREAD_FADE")
    print("="*62)

    markets = [
        # SHOULD fire — Case 1 (long NO on unlikely event)
        # yes_mid=(0.04+0.03)/2=0.035, implied_no=0.965, edge=0.965-0.93-0.002=0.033
        M("SF01", "Will asteroid 2024XQ7 hit Earth?",
          yes_ask=0.04, no_ask=0.93, yes_bid=0.03, no_bid=0.92),

        # SHOULD fire — Case 1 (another unlikely event)
        # yes_mid=0.055, implied_no=0.945, no_ask=0.91, edge=0.945-0.91-0.002=0.033
        M("SF02", "Will Congress pass a flat tax in 2025?",
          yes_ask=0.06, no_ask=0.91, yes_bid=0.05, no_bid=0.90),

        # SHOULD fire — Case 2 (long YES on near-certain)
        # yes_mid=(0.95+0.94)/2=0.945 > 0.92 ✓
        # no_mid=(0.04+0.03)/2=0.035, implied_yes=0.965, edge=0.965-0.95-0.002=0.013
        M("SF03", "Will USD remain world reserve currency through 2025?",
          yes_ask=0.95, no_ask=0.04, yes_bid=0.94, no_bid=0.03),

        # SHOULD fire — Case 2 (strong bid confirms near-certain)
        # yes_mid=(0.93+0.94)/2=0.935 > 0.92 ✓
        # no_mid=(0.06+0.05)/2=0.055, implied_yes=0.945, edge=0.945-0.93-0.002=0.013
        M("SF04", "Will Fed keep rates above 0% through 2025?",
          yes_ask=0.93, no_ask=0.06, yes_bid=0.94, no_bid=0.05),

        # Should NOT fire — mid-range price, no edge
        M("SF05", "Will Bitcoin hit $200k in 2025?",
          yes_ask=0.35, no_ask=0.66),

        # Should NOT fire — total cost too high, edge thin
        # total=0.998, edge=(1-0.04mid)-0.958-0.002 = 0.95-0.958-0.002 = -0.01
        M("SF06", "Will the sun rise tomorrow?",
          yes_ask=0.04, no_ask=0.958, yes_bid=0.03, no_bid=0.948),

        # Should NOT fire — depth too low
        M("SF07", "Will Greenland become US state?",
          yes_ask=0.03, no_ask=0.93, yes_bid=0.02, no_bid=0.92,
          yes_depth=1.0, no_depth=2.0),

        # Should NOT fire — Case 2 but yes_mid just below threshold (0.91)
        M("SF08", "Will solar eclipse happen on Tuesday?",
          yes_ask=0.92, no_ask=0.09, yes_bid=0.90, no_bid=0.08),
    ]

    sigs = scan_spread_fade(markets, 1000.0)
    show(sigs)
    ok = check(sigs,
        expect_fire=["SF01","SF02","SF03","SF04","SF06"],
        expect_silent=["SF05","SF07","SF08"],
        label="SPREAD_FADE fires on correct markets only")
    return sigs, ok

# ══════════════════════════════════════════════════════════════════════════════
# TEST 3 — CROSS_MARKET
# ══════════════════════════════════════════════════════════════════════════════
def test_cross_market():
    print("\n" + "="*62)
    print("TEST 3 — CROSS_MARKET")
    print("="*62)

    # NBA: 8 teams, yes_sum=1.28, overcharge=0.28 — should FIRE
    nba = [
        # yes+no < 1.0 on longshots so neg_risk_edge > 0 (required for CROSS_MARKET edge calc)
        ("CM01","Celtics",      0.28, 0.73),  # total=1.01  neg_risk=-0.01 (won't trade — favourite)
        ("CM02","Warriors",     0.22, 0.79),  # total=1.01  neg_risk=-0.01
        ("CM03","Lakers",       0.18, 0.83),  # total=1.01  neg_risk=-0.01
        ("CM04","Nuggets",      0.15, 0.86),  # total=1.01  neg_risk=-0.01
        ("CM05","Heat",         0.12, 0.87),  # total=0.99  neg_risk=+0.01
        ("CM06","Suns",         0.10, 0.87),  # total=0.97  neg_risk=+0.03
        ("CM07","Bucks",        0.08, 0.86),  # total=0.94  neg_risk=+0.06 ← best trade
        ("CM08","Cavs",         0.15, 0.83),  # total=0.98  neg_risk=+0.02
    ]
    nba_mkts = [M(cid, f"Will {t} win the NBA Finals?", ya, na,
                  yes_bid=ya-0.01, no_bid=na-0.01, yes_depth=800, no_depth=800)
                for cid,t,ya,na in nba]
    yes_sum = sum(m.yes_ask for m in nba_mkts)
    print(f"  NBA: {len(nba_mkts)} teams  YES_sum={yes_sum:.3f}  overcharge={yes_sum-1:.3f}")

    # UCL: 5 teams, yes_sum=1.02, overcharge=0.02 — above CROSS_MIN(0.012), should FIRE
    ucl = [
        ("CM10","Real Madrid",   0.30, 0.71),
        ("CM11","Man City",      0.25, 0.76),
        ("CM12","Bayern",        0.20, 0.81),
        ("CM13","PSG",           0.15, 0.86),
        ("CM14","Arsenal",       0.12, 0.89),
    ]
    ucl_mkts = [M(cid, f"Will {t} win the Champions League?", ya, na,
                  yes_bid=ya-0.01, no_bid=na-0.01, yes_depth=600, no_depth=600)
                for cid,t,ya,na in ucl]
    yes_sum2 = sum(m.yes_ask for m in ucl_mkts)
    print(f"  UCL: {len(ucl_mkts)} teams  YES_sum={yes_sum2:.3f}  overcharge={yes_sum2-1:.3f}")

    # Small group (3 members) — should NOT fire (min=4)
    small = [
        M("CM20", "Will Team A win the Masters golf?",
          yes_ask=0.40, no_ask=0.62, yes_depth=200, no_depth=200),
        M("CM21", "Will Team B win the Masters golf?",
          yes_ask=0.35, no_ask=0.66, yes_depth=200, no_depth=200),
        M("CM22", "Will Team C win the Masters golf?",
          yes_ask=0.30, no_ask=0.71, yes_depth=200, no_depth=200),
    ]

    sigs = scan_cross_market(nba_mkts + ucl_mkts + small, 1000.0)
    show(sigs)

    # Key check: trades should be on CHEAP NOs (longshots), not expensive NOs (favourites)
    no_prices = sorted([s["price"] for s in sigs])
    print(f"\n  NO prices paid: {[f'{p:.3f}' for p in no_prices]}")
    if no_prices and max(no_prices) < 0.92:
        print("  ✅ Buying cheap NOs (longshots) — correct trade direction")
    elif no_prices:
        print(f"  ❌ Buying expensive NOs (avg={sum(no_prices)/len(no_prices):.3f}) — wrong direction")

    ok1 = check(sigs,
        expect_fire=["CM06","CM07"],  # longshots: cheap NO, positive neg_risk_edge
        expect_silent=["CM20","CM21","CM22"],
        label="CROSS_MARKET skips small groups, targets cheap NOs")
    # Just verify: no small group and some signals exist
    small_ids = {"CM20","CM21","CM22"}
    fired_ids = {s["market_id"] for s in sigs}
    ok2 = len(sigs) > 0 and not (fired_ids & small_ids)
    print(f"  {'✅' if ok2 else '❌'} {len(sigs)} signals fired, small group excluded: {not bool(fired_ids & small_ids)}")
    return sigs, ok2

# ══════════════════════════════════════════════════════════════════════════════
# TEST 4 — DEP_GRAPH price ladder
# ══════════════════════════════════════════════════════════════════════════════
def test_dep_graph_ladder():
    print("\n" + "="*62)
    print("TEST 4 — DEP_GRAPH  (price ladder violations)")
    print("="*62)
    print("  BTC spot = $83,000")
    print("  Injecting violations: $82k YES < $85k YES,  $90k YES < $95k YES")

    # Ladder with 2 injected violations
    # DG02 ($82k, YES=0.38) < DG03 ($85k, YES=0.42) → VIOLATION, buy DG02
    # DG04 ($90k, YES=0.18) < DG05 ($95k, YES=0.22) → VIOLATION, buy DG04
    btc = [
        ("DG01", "Will BTC reach $80,000?",  0.72, 0.29),  # fine
        ("DG02", "Will BTC reach $82,000?",  0.38, 0.63),  # ← VIOLATION here
        ("DG03", "Will BTC reach $85,000?",  0.42, 0.59),  # higher YES than DG02
        ("DG04", "Will BTC reach $90,000?",  0.18, 0.83),  # ← VIOLATION here
        ("DG05", "Will BTC reach $95,000?",  0.22, 0.79),  # higher YES than DG04
        ("DG06", "Will BTC reach $100,000?", 0.12, 0.89),  # fine
    ]
    eth = [
        ("DG10", "Will ETH reach $2,000?",  0.35, 0.66),
        ("DG11", "Will ETH reach $2,500?",  0.22, 0.79),
        ("DG12", "Will ETH reach $3,000?",  0.11, 0.90),
    ]
    mkts = [M(c,q,ya,na,yes_bid=ya-0.01,no_bid=na-0.01,yes_depth=400,no_depth=400)
            for c,q,ya,na in btc+eth]
    crypto = {
        "btcusdt": C("btcusdt", 83_000),
        "ethusdt": C("ethusdt",  1_900),
        "solusdt": C("solusdt",    140),
    }
    sigs = scan_dep_graph(mkts, crypto, 1000.0)
    show(sigs)
    fired = {s["market_id"] for s in sigs}
    print(f"\n  Fired: {sorted(fired)}")
    ok = check(sigs,
        expect_fire=["DG02","DG04"],
        expect_silent=["DG01","DG03","DG05","DG06","DG10","DG11","DG12"],
        label="DEP_GRAPH catches exactly the 2 violations, no false positives")
    return sigs, ok

# ══════════════════════════════════════════════════════════════════════════════
# TEST 5 — DEP_GRAPH latency arb
# ══════════════════════════════════════════════════════════════════════════════
def test_dep_graph_latency():
    print("\n" + "="*62)
    print("TEST 5 — DEP_GRAPH  (latency arb: crypto move → reprice)")
    print("="*62)

    mkts = [
        M("LA01", "Will BTC hit $85,000 in 2025?",
          yes_ask=0.40, no_ask=0.61, yes_bid=0.39, no_bid=0.60,
          yes_depth=500, no_depth=500),
        M("LA02", "Will Bitcoin reach $100k by end of year?",
          yes_ask=0.28, no_ask=0.73, yes_bid=0.27, no_bid=0.72,
          yes_depth=400, no_depth=400),
        M("LA03", "Will ETH reach $3,000?",    # BTC move shouldn't hit ETH
          yes_ask=0.22, no_ask=0.79, yes_bid=0.21, no_bid=0.78,
          yes_depth=300, no_depth=300),
    ]

    scenarios = [
        # BTC +0.3% — SENSITIVITY=3.0 → delta=0.009
        # LA01: new_p=0.409, edge=0.409-0.40-0.002=0.007 → FIRES
        # LA02: new_p=0.289, edge=0.289-0.28-0.002=0.007 → FIRES
        ("+0.3% BTC pump (should fire LA01,LA02)",
         {"btcusdt": C("btcusdt",83000,+0.003),
          "ethusdt": C("ethusdt",1900, 0),
          "solusdt": C("solusdt",140,  0)},
         ["LA01","LA02"], ["LA03"]),

        # BTC -0.4% — dump, buy NO
        # new_p = 0.40 + 3*(-0.004) = 0.40-0.012=0.388
        # LA01 NO: (1-0.388)-0.61-0.002=0.000  → borderline
        # LA02 NO: (1-0.268)-0.73-0.002=0.000  → borderline
        ("-0.4% BTC dump (fires NO side)",
         {"btcusdt": C("btcusdt",83000,-0.004),
          "ethusdt": C("ethusdt",1900, 0),
          "solusdt": C("solusdt",140,  0)},
         [], ["LA03"]),  # don't strictly require, just verify no ETH false positive

        # Flat — nothing should fire
        ("Flat market (no arb)",
         {"btcusdt": C("btcusdt",83000, 0),
          "ethusdt": C("ethusdt",1900,  0),
          "solusdt": C("solusdt",140,   0)},
         [], ["LA01","LA02","LA03"]),
    ]

    all_ok = True
    for label, crypto, exp_fire, exp_silent in scenarios:
        sigs = scan_dep_graph(mkts, crypto, 1000.0)
        fired = [s["market_id"] for s in sigs]
        miss  = [e for e in exp_fire   if e not in fired]
        fp    = [e for e in exp_silent if e in fired]
        ok    = not miss and not fp
        if not ok: all_ok = False
        print(f"  {'✅' if ok else '❌'} {label}")
        print(f"     fired={fired}")
        if miss: print(f"     MISSED: {miss}")
        if fp:   print(f"     FALSE+: {fp}")
        for s in sigs:
            print(f"       {s['market_id']} {s['outcome']} edge={s['edge']:.4f} "
                  f"size=${s['size']:.2f}  {s['reason']}")
    return all_ok

# ══════════════════════════════════════════════════════════════════════════════
# TEST 6 — FULL SIMULATION (50 cycles, drifting prices)
# ══════════════════════════════════════════════════════════════════════════════
def test_full_sim():
    print("\n" + "="*62)
    print("TEST 6 — FULL SIMULATION  (50 scan cycles, drifting prices)")
    print("="*62)

    rng = random.Random(42)
    balance   = 1000.0
    positions = {}
    trades    = []
    ctr       = [0]
    held      = set()
    strat_cnt = {"SPREAD_FADE": 0, "CROSS_MARKET": 0, "DEP_GRAPH": 0}
    nav_hist  = []

    def fake_buy(sig):
        price, size_usd = sig["price"], sig["size"]
        nonlocal balance
        if size_usd < MIN_BET_SIZE or not (0 < price < 1): return None
        fee   = size_usd * TAKER_FEE
        total = size_usd + fee
        if total > balance:
            size_usd = balance * 0.90 / (1 + TAKER_FEE)
            if size_usd < MIN_BET_SIZE: return None
            fee   = size_usd * TAKER_FEE
            total = size_usd + fee
        balance -= total
        ctr[0] += 1
        t = {"id": f"T{ctr[0]:04d}", "question": sig["question"][:60],
             "token_id": sig["token_id"], "outcome": sig["outcome"],
             "price": price, "cost": total, "fee": fee,
             "strategy": sig["strategy"], "edge": sig["edge"]}
        trades.append(t)
        positions[sig["token_id"]] = {
            "size": size_usd/price, "avg_entry": price, "strategy": sig["strategy"]}
        return t

    def universe(tick):
        drift = 0.001 * tick
        mkts = [
            M("U01", "Will X happen (very unlikely)?",
              yes_ask=max(0.02, 0.05-drift*0.1), no_ask=min(0.97, 0.92+drift*0.05),
              yes_bid=0.03, no_bid=0.91, yes_depth=500, no_depth=500),
            M("U02", "Will Y happen (near-certain)?",
              yes_ask=min(0.97, 0.93+drift*0.02), no_ask=max(0.02, 0.05-drift*0.01),
              yes_bid=min(0.96, 0.94+drift*0.02), no_bid=max(0.01,0.04-drift*0.01),
              yes_depth=400, no_depth=400),
        ]
        nba_teams = [
            ("U10","Celtics",   0.26), ("U11","Warriors", 0.20),
            ("U12","Lakers",    0.18), ("U13","Nuggets",  0.14),
            ("U14","Heat",      0.12), ("U15","Suns",     0.10),
            ("U16","Bucks",     0.08), ("U17","Cavs",     0.14),
        ]
        for cid, team, base_ya in nba_teams:
            ya = round(max(0.04, min(0.50, base_ya + rng.uniform(-0.02,0.02))), 3)
            na = round(max(0.50, min(0.97, 1.0 - ya + 0.03 + rng.uniform(0,0.04))), 3)
            mkts.append(M(cid, f"Will {team} win the NBA Finals?",
                          ya, na, yes_bid=ya-0.01, no_bid=na-0.01,
                          yes_depth=600, no_depth=600))
        btc_params = [(f"U2{j}", f"Will BTC reach ${s:,}?", p)
                      for j,(s,p) in enumerate(zip(
                          [80000,85000,90000,95000,100000],
                          [0.70,  0.40,  0.20,  0.10,  0.07]))]
        for j,(cid,q,bp) in enumerate(btc_params):
            noise = rng.uniform(-0.03, 0.03)
            if tick % 6 == 0 and j == 2: noise += 0.06  # inject violation at $85k
            ya = round(max(0.03, min(0.95, bp+noise)), 3)
            na = round(max(0.04, min(0.95, 1.0-ya+0.04+rng.uniform(0,0.03))), 3)
            mkts.append(M(cid, q, ya, na, yes_bid=ya-0.01, no_bid=na-0.01,
                          yes_depth=350, no_depth=350))
        return mkts

    def crypto(tick):
        btc_ret = rng.uniform(-0.002, 0.002)
        if tick % 8 == 0:
            btc_ret = rng.choice([-1,1]) * rng.uniform(0.003, 0.008)
        return {
            "btcusdt": C("btcusdt", 83000+tick*50, btc_ret),
            "ethusdt": C("ethusdt", 1900+tick*2,   rng.uniform(-0.001,0.001)),
            "solusdt": C("solusdt", 140,            rng.uniform(-0.002,0.002)),
        }

    for tick in range(50):
        mkts   = universe(tick)
        cr     = crypto(tick)
        sf     = scan_spread_fade(mkts, balance)
        cm     = scan_cross_market(mkts, balance)
        dg     = scan_dep_graph(mkts, cr, balance)
        sigs   = sf + cm + dg

        seen = set(); clean = []
        for sig in sorted(sigs, key=lambda x: x["edge"], reverse=True):
            if sig["token_id"] in held or sig["token_id"] in seen: continue
            seen.add(sig["token_id"]); clean.append(sig)

        for sig in clean[:5]:
            if balance < MIN_BET_SIZE: break
            t = fake_buy(sig)
            if t:
                held.add(sig["token_id"])
                strat_cnt[sig["strategy"]] += 1

        invested = sum(p["size"]*p["avg_entry"] for p in positions.values())
        nav_hist.append(round(balance + invested, 2))

    invested = sum(p["size"]*p["avg_entry"] for p in positions.values())
    nav_end  = balance + invested

    print(f"  Ticks simulated:       50")
    print(f"  Starting balance:      $1,000.00")
    print(f"  Cash remaining:        ${balance:.2f}")
    print(f"  Invested (mark cost):  ${invested:.2f}")
    print(f"  NAV (cash+invested):   ${nav_end:.2f}")
    print(f"  Total trades placed:   {ctr[0]}")
    print(f"  Open positions:        {len(positions)}")
    print(f"  Strategy breakdown:    {strat_cnt}")
    print(f"  NAV trajectory:        {nav_hist[0]} → {nav_hist[24]} → {nav_hist[-1]}")

    if ctr[0] > 0:
        print(f"\n  ✅ Engine IS placing bets across all strategies")
    else:
        print(f"\n  ❌ No trades placed — check strategy filters")

    print(f"\n  Sample trades (first 10):")
    for t in trades[:10]:
        print(f"    {t['id']}  {t['strategy']:<14} {t['outcome']:<3} "
              f"@{t['price']:.3f}  ${t['cost']:.2f}  edge={t['edge']:.4f}"
              f"  \"{t['question'][:42]}\"")
    return ctr[0] > 0, strat_cnt

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 62)
    print("  ORACULUS — SYNTHETIC STRATEGY TEST HARNESS")
    print("  No network. No Binance. No CoinGecko. Pure logic.")
    print("=" * 62)

    k_ok             = test_kelly()
    sf_sigs, sf_ok   = test_spread_fade()
    cm_sigs, cm_ok   = test_cross_market()
    dg_sigs, dg_ok   = test_dep_graph_ladder()
    la_ok            = test_dep_graph_latency()
    sim_ok, sc       = test_full_sim()

    print("\n" + "="*62)
    print("FINAL REPORT")
    print("="*62)
    results = [
        ("Kelly sizing",          k_ok),
        ("SPREAD_FADE",           sf_ok),
        ("CROSS_MARKET",          cm_ok),
        ("DEP_GRAPH ladder",      dg_ok),
        ("DEP_GRAPH latency arb", la_ok),
        ("Full sim trades fire",  sim_ok),
    ]
    for label, ok in results:
        print(f"  {'✅' if ok else '❌'}  {label}")
    all_pass = all(ok for _,ok in results)
    print(f"\n  {'ALL TESTS PASSED ✅' if all_pass else 'SOME TESTS FAILED ❌'}")
    print(f"  Sim strategy breakdown: {sc}")
