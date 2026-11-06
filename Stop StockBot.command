#!/bin/bash
# Stops a StockBot that was started with "Start StockBot.command" or in the background, plus its website tunnel.
PIDS=$(lsof -ti tcp:8777 -sTCP:LISTEN)
pkill -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8777" 2>/dev/null
if [ -n "$PIDS" ]; then kill $PIDS && echo "StockBot stopped."; else echo "StockBot isn't running."; fi
