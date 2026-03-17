#!/bin/bash
# Start the trading engine in the background
python engine.py &

# Start the Flask web server (this is what Render binds to $PORT)
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
