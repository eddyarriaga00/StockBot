#!/bin/bash
PLIST="$HOME/Library/LaunchAgents/com.stockbot.app.plist"
launchctl unload "$PLIST" 2>/dev/null && rm -f "$PLIST"
echo "Background StockBot stopped and removed from login items."
