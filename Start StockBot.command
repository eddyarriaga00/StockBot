#!/bin/bash
# Double-click to start StockBot. Leave this window open while you want the bot running.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up (about a minute)..."
  python3 -m venv .venv || { echo "Python 3 is required. Install it from python.org"; read -r; exit 1; }
fi
.venv/bin/pip install -q -r requirements.txt
PORT=8777
if curl -s "http://127.0.0.1:$PORT/api/clock" >/dev/null 2>&1; then
  open "http://127.0.0.1:$PORT"; exit 0
fi
(sleep 2 && open "http://127.0.0.1:$PORT") &
echo "StockBot running at http://127.0.0.1:$PORT  (close this window or press Ctrl+C to stop)"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $PORT --log-level warning
