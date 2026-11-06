#!/bin/bash
# Double-click once: StockBot starts automatically whenever you log in, with no window to keep open.
# To undo, double-click "Stop Background StockBot.command".
cd "$(dirname "$0")"
DIR="$(pwd)"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p data
PLIST="$HOME/Library/LaunchAgents/com.stockbot.app.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.stockbot.app</string>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>ProgramArguments</key><array>
    <string>$DIR/.venv/bin/python</string><string>-m</string><string>uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>8777</string><string>--log-level</string><string>warning</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$DIR/data/server.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
sleep 3
open "http://127.0.0.1:8777"
echo "StockBot now runs in the background and starts at login. Open http://127.0.0.1:8777 any time."
