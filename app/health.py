"""Is StockBot actually running when it matters?

Writes a heartbeat every loop. On startup it compares the last heartbeat with the market calendar and warns if
StockBot was off during market hours (missed trading days are the most common way a bot quietly loses money).
"""
import datetime as dt
import json
import os
import subprocess
import time
from zoneinfo import ZoneInfo

from . import calendar_us

ET = ZoneInfo("America/New_York")


class Health:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "heartbeat.json")
        self.started = time.time()
        self.last_loop = 0.0
        self.loop_errors = 0
        self.previous = None
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.previous = json.load(f).get("time")
            except (ValueError, OSError):
                pass

    def beat(self):
        self.last_loop = time.time()
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"time": self.last_loop}, f)
        os.replace(tmp, self.path)

    def missed_message(self):
        """A warning if StockBot was off during market hours since it last ran, else None."""
        if not self.previous:
            return None
        days = missed_sessions(self.previous, self.started)
        if not days:
            return None
        off = dt.datetime.fromtimestamp(self.previous).strftime("%a %b %d %I:%M %p")
        names = ", ".join(d.strftime("%a %b %d") for d in days[:5]) + ("…" if len(days) > 5 else "")
        return (f"StockBot was off from {off} until now and missed {len(days)} trading day(s): {names}. "
                "The bot can only trade while this Mac is on, awake and running StockBot.")

    def status(self):
        return {"started": self.started, "uptime": time.time() - self.started, "last_loop": self.last_loop,
                "loop_ok": time.time() - self.last_loop < 120, "loop_errors": self.loop_errors,
                "awake": _caffeinated(), "now": time.time()}


def missed_sessions(t0, t1):
    """Weekdays whose whole 9:30-16:00 ET session passed while StockBot was off (market holidays excluded)."""
    a, b = dt.datetime.fromtimestamp(t0, ET), dt.datetime.fromtimestamp(t1, ET)
    out, d = [], a.date()
    while d <= b.date():
        if calendar_us.is_trading_day(d):
            open_ = dt.datetime.combine(d, dt.time(9, 30), ET)
            close = dt.datetime.combine(d, dt.time(16, 0), ET)
            if a <= open_ + dt.timedelta(minutes=30) and b >= close:  # off for the whole session: no chance to trade
                out.append(d)
        d += dt.timedelta(days=1)
    return out


def _caffeinated():
    try:
        r = subprocess.run(["pgrep", "-f", f"caffeinate -i -w {os.getpid()}"], capture_output=True, timeout=3)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None
