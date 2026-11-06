"""Publishes a read-only snapshot of results to docs/data.json and pushes it to GitHub, where GitHub Pages
serves the public report card. Contains only fake-money results: no keys, no settings, no way to trade."""
import json
import os
import subprocess
import time

from . import strategy

PUBLISH_EVERY_OPEN = 2 * 3600   # while the market is open
DOCS = "docs"


class Publisher:
    def __init__(self, root, get_broker, bot, tournament, log):
        self.root = root
        self.get_broker = get_broker
        self.bot = bot
        self.tournament = tournament
        self.log = log
        self.path = os.path.join(root, "data", "publish.json")
        self.state = {"last": 0, "last_close": None}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.state.update(json.load(f))

    def snapshot(self):
        broker = self.get_broker()
        acct = broker.account()
        bt = strategy.backtest_all()
        trades = [
            {"time": o["filled_at"] or o["created_at"], "symbol": o["symbol"], "side": o["side"], "qty": o["filled_qty"],
             "price": o["filled_avg_price"], "source": o.get("source"), "reason": o.get("reason")}
            for o in broker.orders("closed", 100) if o["status"] == "filled"
        ][:40]
        return {
            "updated": time.time(),
            "broker": broker.name,
            "account": {k: acct[k] for k in ("equity", "cash", "day_pl", "day_pl_pct", "total_pl", "total_pl_pct", "starting_cash")},
            "positions": [{k: p[k] for k in ("symbol", "qty", "price", "market_value", "unrealized_pl_pct", "day_pl_pct")}
                          for p in broker.positions()],
            "equity_curve": [{"time": int(h["time"]), "value": h["equity"]} for h in broker.equity_history()][-2000:],
            "bot": {"strategy": self.bot.status()["strategy_name"],
                    "enabled": self.bot.cfg["enabled"], "log": [l for l in self.bot.state["log"] if l["kind"] in ("trade", "warn", "info")][:30]},
            "trades": trades,
            "tournament": self.tournament.standings(),
            "tournament_curves": self.tournament.live_curves(),
            "backtest": {"start": bt["start"], "split": bt["split"], "end": bt["end"], "caveat": bt["caveat"],
                         "strategies": {k: {"name": v["name"], "oos": v["oos"], "full": v["full"], "curve": v["curve"]}
                                        for k, v in bt["strategies"].items()}},
        }

    def publish(self, push=True):
        os.makedirs(os.path.join(self.root, DOCS), exist_ok=True)
        out = os.path.join(self.root, DOCS, "data.json")
        with open(out, "w") as f:
            json.dump(self.snapshot(), f, separators=(",", ":"))
        self.state["last"] = time.time()
        self._save()
        if not push:
            return "written"
        msg = time.strftime("Report card update %Y-%m-%d %H:%M")
        cmds = [["git", "add", f"{DOCS}/data.json"], ["git", "commit", "-q", "-m", msg, "--", f"{DOCS}/data.json"],
                ["git", "push", "-q", "origin", "HEAD"]]
        for c in cmds:
            r = subprocess.run(c, cwd=self.root, capture_output=True, text=True, timeout=120)
            if r.returncode and not (c[1] == "commit" and "nothing" in (r.stdout + r.stderr)):
                self.log(f"Publishing to GitHub failed at '{' '.join(c[:2])}': {(r.stderr or r.stdout).strip()[:200]}", "error")
                return "failed"
        self.log("Report card published to GitHub Pages.", "info")
        return "pushed"

    def step(self, clock):
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        if clock["is_open"] and now - self.state["last"] > PUBLISH_EVERY_OPEN:
            self.publish()
        elif not clock["is_open"] and clock.get("session_end") and now > clock["session_end"] and \
                self.state["last_close"] != today and time.localtime().tm_wday < 5:
            self.state["last_close"] = today
            self.publish()

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.state, f)
