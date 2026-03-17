import os

STARTING_BALANCE     = 1_000.0
MAX_KELLY_FRACTION   = 0.25
MIN_EDGE             = 0.004       # min net edge to generate a signal
MIN_DEPTH            = 5.0        # min $depth to trade against
MAX_BET_SIZE         = 50.0
MIN_BET_SIZE         = 1.0
TAKER_FEE            = 0.002
BTC_MOVE_THRESHOLD   = 0.0008     # 0.08% in 5s triggers latency arb
ETH_MOVE_THRESHOLD   = 0.0015
GRAPH_MIN_VIOLATION  = 0.008      # min price ladder violation
CROSS_MIN_OVERCHARGE = 0.012      # min YES_sum overcharge for cross-market

POLYMARKET_GAMMA     = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB      = "https://clob.polymarket.com"
BINANCE_REST         = "https://api.binance.com"

MARKET_REFRESH_SEC   = 25
SCAN_INTERVAL_SEC    = 0.5

STATE_FILE           = "/tmp/oraculus_state.json"
PERSIST_FILE         = "/tmp/oraculus_persist.json"
BOT_NAME             = "ORACULUS"
