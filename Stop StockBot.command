#!/bin/bash
# Stops a StockBot that was started with "Start StockBot.command" or in the background.
PIDS=$(lsof -ti tcp:8777 -sTCP:LISTEN)
if [ -n "$PIDS" ]; then kill $PIDS && echo "StockBot stopped."; else echo "StockBot isn't running."; fi
