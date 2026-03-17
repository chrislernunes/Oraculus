import os

STARTING_BALANCE     = 1_000.0
MAX_KELLY_FRACTION   = 0.25
MIN_EDGE             = 0.004       # min net edge to enter a trade
MIN_DEPTH            = 5.0        # min contract depth to trade against
MAX_BET_SIZE         = 50.0
MIN_BET_SIZE         = 1.0
TAKER_FEE            = 0.002
GRAPH_MIN_VIOLATION  = 0.008      # min price ladder violation for DEP_GRAPH
CROSS_MIN_OVERCHARGE = 0.012      # min YES_sum overcharge for CROSS_MARKET

POLYMARKET_GAMMA     = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB      = "https://clob.polymarket.com"
COINGECKO_API        = "https://api.coingecko.com/api/v3"  # replaces Binance (geo-blocked on Render)

MARKET_REFRESH_SEC   = 25
SCAN_INTERVAL_SEC    = 0.5
CRYPTO_POLL_SEC      = 10         # CoinGecko free tier: 30 req/min — 10s = 6/min

STATE_FILE           = "/tmp/oraculus_state.json"
PERSIST_FILE         = "/tmp/oraculus_persist.json"
BOT_NAME             = "ORACULUS"
