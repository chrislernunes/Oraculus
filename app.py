"""
Oraculus — Flask Dashboard
Serves the dashboard HTML and /api/state endpoint.
Engine runs alongside in the same process via start.sh.
"""

import json, threading, os
from flask import Flask, jsonify, render_template_string
from config import STATE_FILE, BOT_NAME, STARTING_BALANCE

app = Flask(__name__, static_folder='templates', static_url_path='')

# Start the trading engine in a background thread
def start_engine():
    import asyncio
    from engine import run
    asyncio.run(run())

engine_thread = threading.Thread(target=start_engine, daemon=True)
engine_thread.start()


@app.route("/api/state")
def api_state():
    try:
        with open(STATE_FILE) as f:
            return jsonify(json.load(f))
    except:
        return jsonify({"engine_alive": False, "nav": STARTING_BALANCE,
                        "balance": STARTING_BALANCE, "scan_count": 0,
                        "market_count": 0, "trades": [], "signals": [],
                        "markets": [], "positions": {}, "crypto": {},
                        "nav_history": [], "strat_counts": {},
                        "realized_pnl": 0, "win_rate": 0, "total_fees": 0,
                        "num_trades": 0})


@app.route("/")
def index():
    return app.send_static_file("index.html")



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
