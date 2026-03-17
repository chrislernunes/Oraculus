# ORACULUS — ARB ENGINE

> Three-strategy prediction market arbitrage engine. Scans binary outcome markets for structural mispricings, sizes positions via fractional Kelly, and executes on a paper account. Real-time crypto prices via CoinGecko. Market data via Polymarket Gamma + CLOB APIs. Deployable on Render in one click.

---

## Table of Contents

1. [Overview](#overview)
2. [How It Works](#how-it-works)
3. [Strategies](#strategies)
   - [SPREAD_FADE](#spread_fade)
   - [CROSS_MARKET](#cross_market)
   - [DEP_GRAPH](#dep_graph)
4. [Architecture](#architecture)
5. [File Structure](#file-structure)
6. [Configuration](#configuration)
7. [Deploy on Render](#deploy-on-render)
8. [Run Locally](#run-locally)
9. [Dashboard](#dashboard)
10. [Backtesting](#backtesting)
11. [Known Constraints](#known-constraints)
12. [FAQ](#faq)

---

## Overview

Oraculus is a paper-trading arbitrage bot for binary prediction markets. It runs three independent scanning strategies simultaneously, looking for moments when the market's quoted prices are inconsistent with what they logically should be. When it finds one, it calculates the optimal bet size and executes immediately on a paper account — no real money.

**What it is:**
- A fully automated trading engine that runs 24/7
- A live dashboard showing NAV, signals, trades, and market scanner
- A research-grade codebase with formal edge conditions and a 2,000+ trade backtest

**What it is not:**
- A live trading bot (paper only — no wallet integration)
- A price predictor or ML model
- Investment advice of any kind

---

## How It Works

Every 500ms the engine:

1. **Refreshes market data** (every 25s) — fetches up to 500 active binary markets from Polymarket Gamma, pulls orderbooks from the CLOB API, and builds a `MarketData` object for each with ask/bid/depth on both YES and NO sides.

2. **Polls crypto prices** (every 10s) — fetches BTC, ETH, SOL spot prices from CoinGecko and calculates a 10-second return for each symbol.

3. **Runs three strategies** — each strategy scans all markets and returns a list of signals: `{ strategy, outcome, price, edge, size, reason }`.

4. **Deduplicates and ranks** — signals are sorted by edge descending, filtered for already-held positions, and the top 5 are executed each cycle.

5. **Sizes via Kelly** — each bet is `min(kelly_fraction × balance, $50)`. Kelly fraction is capped at 25% of balance per trade.

6. **Writes state** — results are written to `/tmp/oraculus_state.json` every 500ms. The Flask `/api/state` endpoint serves this to the dashboard.

---

## Strategies

### SPREAD_FADE

**The idea:** On binary markets, YES ask + NO ask should sum to approximately `1 + 2 × fee`. When a market maker quotes spreads around a stale mid-price, one side becomes underpriced relative to what the other side implies.

**Case 1 — Long NO on near-zero markets:**

When `YES ask < 0.08`, the event is priced as nearly impossible. The NO should trade near `1 - YES_mid`. If the market maker's NO ask falls below that, the gap is exploitable.

```
Edge = (1 - YES_mid) - NO_ask - fee
Fire when: Edge ≥ 0.004  AND  NO_depth ≥ 5 contracts
```

**Case 2 — Long YES on near-certain markets:**

When `YES_mid > 0.92`, the event is priced as near-certain. If the YES ask is below `1 - NO_mid`, buy YES.

```
Edge = (1 - NO_mid) - YES_ask - fee
Fire when: Edge ≥ 0.004  AND  YES_depth ≥ 5 contracts
```

> **Why mid-price matters:** Using the ask price as a probability proxy overstates edge. The mid `(ask + bid) / 2` is the unbiased fair-value estimate. This prevents false signals during wide-spread conditions.

**Typical edge:** 0.5% – 3.5%
**Win rate (backtest):** ~98% — high-frequency small wins on near-certain events

---

### CROSS_MARKET

**The idea:** A set of mutually exclusive outcomes (e.g. "which team wins the championship?") must have YES prices that sum to no more than `1.0 + fees`. When the sum exceeds this, the market is oversubscribed — at least some YES contracts are overpriced. Buy NO on the markets where the overpricing is clearest.

**Detection:**
```
Omega(group) = sum(YES_asks) - 1.0
Fire when: Omega ≥ 0.012  AND  group has ≥ 4 members
```

**Trade selection:**

Sort candidates by `neg_risk_edge = 1 - YES_ask - NO_ask` descending. The markets with the most positive neg-risk edge have the clearest mispricing — typically longshots where `YES + NO` sits furthest below 1.0.

> **Common mistake:** Sorting by `NO_ask` ascending selects tournament favourites (cheap NO = expensive favourite). Their `YES + NO > 1.0` gives negative neg-risk-edge — they never clear the filter. Always sort by `neg_risk_edge` descending.

**Covered groups:** NBA Finals, NHL Stanley Cup, Champions League, Premier League, La Liga, Serie A, Bundesliga, World Series, Super Bowl, Oscars Best Picture/Director, Emmy, Nobel Prize, Presidential nominations, James Bond, Next Pope.

**Typical edge:** 0.5% – 6%
**Win rate (backtest):** 100% (rare but reliable)

---

### DEP_GRAPH

**Sub-strategy 1 — Price ladder consistency:**

For a crypto asset at spot price S, a lower strike must have a higher YES probability than a higher strike (stochastic dominance). When a higher strike YES costs *more* than a lower strike YES, the lower-strike contract is underpriced — buy it.

```
violation = YES_ask(higher_strike) - YES_ask(lower_strike)
Fire when: violation ≥ 0.008
Kelly probability = min(0.95, YES_ask(lower) + violation)
```

**Sub-strategy 2 — Latency arb:**

CoinGecko delivers a crypto price move. Prediction markets referencing that asset haven't repriced yet. Trade in the direction of the move before they do.

```
delta = 3.0 × crypto_return_10s
Edge (up move) = delta - fee
Fire when: |crypto_return| ≥ threshold  AND  edge ≥ 0.004
```

Thresholds: BTC ≥ 0.05%, ETH ≥ 0.10%, SOL ≥ 0.30%

> **Sensitivity calibration:** The multiplier `3.0` means a 0.1% BTC move is expected to produce a 0.3% prediction market reprice. Conservative by design — empirical sensitivity is likely higher but unstable over short windows.

**Typical edge:** 0.5% – 2.5%
**Win rate (backtest):** 33.6% — low win rate, but asymmetric payoffs account for 84% of total PnL

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Render Web Service  (single process)                       │
│                                                             │
│  ┌──────────────┐     ┌───────────────────────────────┐    │
│  │  Flask app   │     │  Engine thread  (daemon)       │    │
│  │  (app.py)    │     │                               │    │
│  │  GET /       │     │  ┌─────────────┐              │    │
│  │  GET /api/   │     │  │ DataFetcher │◄─ Gamma API  │    │
│  │    state     │     │  │             │◄─ CLOB API   │    │
│  └──────┬───────┘     │  └──────┬──────┘              │    │
│         │             │         │ MarketData[]         │    │
│         │ reads       │  ┌──────▼──────┐              │    │
│         │             │  │  Strategies │              │    │
│  ┌──────▼───────┐     │  │  SPREAD_FADE│              │    │
│  │ state.json   │◄────│  │  CROSS_MKT  │              │    │
│  │  /tmp/       │     │  │  DEP_GRAPH  │              │    │
│  └──────────────┘     │  └──────┬──────┘              │    │
│                        │         │ signals              │    │
│  ┌──────────────┐     │  ┌──────▼──────┐              │    │
│  │  index.html  │     │  │PaperAccount │              │    │
│  │  dashboard   │     │  │ Kelly size  │              │    │
│  └──────────────┘     │  │  buy/hold   │              │    │
│                        │  └─────────────┘              │    │
│                        │                               │    │
│                        │  ┌─────────────┐              │    │
│                        │  │ CryptoFeed  │◄─ CoinGecko  │    │
│                        │  │ (10s poll)  │              │    │
│                        │  └─────────────┘              │    │
│                        └───────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

**Threading model:** Flask and the async engine share one process. The engine runs in a daemon thread via `asyncio.run(run())`. State is exchanged via a JSON file in `/tmp/` — no Redis, no shared memory required.

**Data flow:** Engine writes `oraculus_state.json` every 500ms → Flask reads it on `/api/state` → Dashboard polls every 2 seconds → UI updates.

---

## File Structure

```
oraculus/
├── app.py              # Flask server + engine thread launcher
├── engine.py           # Core engine: async main loop, strategies, crypto feed
├── config.py           # All tunable constants
├── index.html          # Single-page dashboard (polls /api/state)
├── requirements.txt    # aiohttp, flask, gunicorn
├── Procfile            # Render start command
├── start.sh            # Alternative start script
├── README.md           # This file

```

---

## Configuration

All constants live in `config.py`. The engine, dashboard, and test files all import from this single source.

| Constant | Default | Description |
|---|---|---|
| `STARTING_BALANCE` | `1_000.0` | Initial paper account balance ($) |
| `MAX_KELLY_FRACTION` | `0.25` | Maximum Kelly bet fraction (25% of balance) |
| `MIN_EDGE` | `0.004` | Minimum net edge to enter a trade (0.4%) |
| `MIN_DEPTH` | `5.0` | Minimum contracts available at best ask levels |
| `MAX_BET_SIZE` | `50.0` | Hard cap per bet ($) |
| `MIN_BET_SIZE` | `1.0` | Discard signals below this size ($) |
| `TAKER_FEE` | `0.002` | Taker fee per contract (0.2%) |
| `GRAPH_MIN_VIOLATION` | `0.008` | Minimum ladder violation for DEP_GRAPH (0.8%) |
| `CROSS_MIN_OVERCHARGE` | `0.012` | Minimum group overcharge for CROSS_MARKET (1.2%) |
| `MARKET_REFRESH_SEC` | `25` | How often to re-fetch market list from Gamma |
| `SCAN_INTERVAL_SEC` | `0.5` | Main loop sleep between strategy scans |
| `CRYPTO_POLL_SEC` | `10` | CoinGecko poll interval (6 req/min on free tier) |

**Tuning tips:**
- Raise `MIN_EDGE` to `0.008` to reduce trade frequency and filter marginal signals
- Lower `MAX_BET_SIZE` to `$20` for more conservative per-trade exposure
- Lower `SCAN_INTERVAL_SEC` to `0.25` for faster scanning (higher CPU usage)
- Raise `CRYPTO_POLL_SEC` to `15` if you see 429 rate-limit errors in the logs


If you see `CryptoFeed CoinGecko → 429`, increase `CRYPTO_POLL_SEC` to `15` in `config.py`.

## Dashboard

The dashboard at `/` polls `/api/state` every 2 seconds and renders:

| Section | Contents |
|---|---|
| **Header** | Scan counter, BTC/ETH live prices, best current edge, live/offline badge |
| **Bayesian panel** | Posterior probability, epoch count, model confidence |
| **Edge + Spread panel** | Current best signal EV, pass/reject status |
| **Execution panel** | Kelly fraction, gamma, sizing calculation |
| **Ray canvas** | Animated background, NAV overlay, total trades |
| **Training stream** | Rolling log of fills, signals, and scan events |
| **Bot metrics** | NAV, ROI %, win rate, Sharpe, max drawdown, strategy status indicators |
| **P&L curve** | NAV history drawn as a line chart |
| **Market Scanner tab** | Up to 150 markets sorted by arbitrage edge, with YES/NO prices and depth |
| **Trade Log tab** | Full trade history: strategy, direction, price, cost, PnL, edge |
| **Positions tab** | All open positions with entry price and cost |

---

## Backtesting

### `test_strategies.py` — Unit tests

Validates each strategy in isolation against hand-crafted scenarios with injected edge cases.

```bash
python test_strategies.py
```

Covers: Kelly sizing (8 cases), SPREAD_FADE (8 markets, 3 should fire / 5 silent), CROSS_MARKET (11 markets, small groups excluded), DEP_GRAPH ladder (2 injected violations, no false positives on clean ladder), DEP_GRAPH latency (3 crypto-move scenarios), 50-tick full simulation.

### `sim500.py` — 500-trade backtest

Full 500-tick simulation over 60 synthetic markets per tick. Markets are generated fresh each tick with realistic spread structure, true probabilities, and periodic injected violations. Positions settle probabilistically against true probability every 8 ticks, recycling capital.

```bash
python sim500.py
```

**Benchmark results (seed=42, $10k starting balance):**

| Metric | Value |
|---|---|
| Ticks simulated | 500 |
| Trades placed | 2,223 |
| Win rate | 73.2% |
| Realized PnL | +$2,784.58 |
| ROI on capital staked | +4.24% |
| Max drawdown | 13.77% |
| Sharpe (trade-level) | 0.670 |

| Strategy | Trades | Win% | PnL | Notes |
|---|---|---|---|---|
| SPREAD_FADE | 1,368 | 97.6% | +$389 | Small consistent wins |
| CROSS_MARKET | 8 | 100.0% | +$53 | Rare but reliable |
| DEP_GRAPH | 847 | 33.6% | +$2,343 | Asymmetric — 84% of PnL |

---

## Known Constraints

**No live execution:** The engine is paper-only. `PaperAccount` has `buy()` but no `sell()` or position-close logic. Realized PnL from resolved markets is not tracked. A full position management layer with mark-to-market tracking and auto-close at resolution is the main missing piece.
