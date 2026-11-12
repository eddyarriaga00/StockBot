"""Publishes the StockBot app to GitHub Pages as a view-only mirror.

It copies the real front end (static/) into docs/ and writes the data its screens need into docs/data/:
core.json for the account, orders, bot and tournament, plus one file per symbol with quotes, charts and news.
On Pages the app reads those files instead of the API and turns off every trading control.
Only fake-money results are published: no keys, no settings, no way to place orders.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import market, performance, strategy, track

PUBLISH_EVERY_OPEN = 2 * 3600   # while the market is open
RANGES = ["1d", "5d", "1mo", "1y", "5y"]
DOCS = "docs"


class Publisher:
    def __init__(self, root, get_broker, bot, tournament, load_settings, log, get_remote_url=lambda: None):
        self.root = root
        self.get_broker = get_broker
        self.bot = bot
        self.tournament = tournament
        self.load_settings = load_settings
        self.log = log
        self.get_remote_url = get_remote_url
        self.path = os.path.join(root, "data", "publish.json")
        self.state = {"last": 0, "last_close": None}
        self.lock = threading.Lock()  # the loop and the API button must not push at the same time
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.state.update(json.load(f))

    # ---------- build ----------
    def build(self):
        docs = os.path.join(self.root, DOCS)
        broker = self.get_broker()
        tourney = self.tournament.standings()
        positions = broker.positions()
        closed = broker.orders("closed", 500)
        open_ = broker.orders("open")
        watchlist = self.load_settings()["watchlist"]
        symbols = list(dict.fromkeys(
            watchlist + [p["symbol"] for p in positions] + [o["symbol"] for o in closed if o["status"] == "filled"]
            + [h["symbol"] for r in tourney["rows"] for h in r["holdings"]] + strategy.STOCKS))
        quotes = market.quotes(symbols)
        symbols = [s for s in symbols if s in quotes]

        with ThreadPoolExecutor(max_workers=8) as pool:
            sym_data = dict(zip(symbols, pool.map(self._symbol_file, symbols, [quotes[s] for s in symbols])))

        bot_status = self.bot.status()
        bot_status["pending"] = []
        core = {
            "updated": time.time(),
            "remote_url": self.get_remote_url(),  # where the website can reach this Mac (login required)
            "account": {"account": broker.account(), "positions": positions},
            "equity": broker.equity_history()[-3000:],
            "orders": {"open": open_, "closed": closed, "all": open_ + closed},
            "performance": performance.for_broker(broker),
            "track": track.record(broker, self.tournament),
            "activity": broker.activity() if hasattr(broker, "activity") else [],
            "watchlist": watchlist,
            "quotes": quotes,
            "clock": market.market_clock(),
            "bot": bot_status,
            "tournament": tourney,
            "tournament_curves": self.tournament.live_curves(),
            "backtest": strategy.backtest_all(),
            "settings": {"broker": broker.name},
            "strategies": {k: {"name": v["name"], "about": v["about"]} for k, v in strategy.STRATEGIES.items()},
            "symbols": [{"symbol": s, "name": quotes[s]["name"], "exchange": quotes[s]["exchange"], "type": quotes[s]["type"]} for s in symbols],
        }

        # Rebuild docs/ from scratch so removed files don't linger.
        for name in ("static", "data", "index.html", "data.json", "vendor"):
            p = os.path.join(docs, name)
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
        os.makedirs(os.path.join(docs, "data", "sym"), exist_ok=True)
        static = os.path.join(self.root, "static")
        shutil.copytree(static, os.path.join(docs, "static"), ignore=shutil.ignore_patterns("index.html"))
        shutil.copy(os.path.join(static, "index.html"), os.path.join(docs, "index.html"))
        open(os.path.join(docs, ".nojekyll"), "w").close()
        _write(os.path.join(docs, "data", "core.json"), core)
        for s, d in sym_data.items():
            _write(os.path.join(docs, "data", "sym", f"{s}.json"), d)
        return len(symbols)

    def _symbol_file(self, symbol, quote):
        history = {}
        for rng in RANGES:
            try:
                h = market.history(symbol, rng)
            except market.MarketDataError:
                continue
            history[rng] = {"gmtoffset": h["gmtoffset"],
                            "c": [[c["time"], round(c["open"], 2), round(c["high"], 2), round(c["low"], 2), round(c["close"], 2), c["volume"]]
                                  for c in h["candles"]]}
        try:
            news = market.news(symbol)
        except market.MarketDataError:
            news = []
        return {"quote": quote, "history": history, "news": news}

    # ---------- publish ----------
    def publish(self, push=True):
        with self.lock:
            return self._publish(push)

    def _publish(self, push):
        n = self.build()
        self.state["last"] = time.time()
        self._save()
        if not push:
            return f"built ({n} symbols)"
        msg = time.strftime("Website update %Y-%m-%d %H:%M")
        cmds = [["git", "add", "-A", DOCS], ["git", "commit", "-q", "-m", msg, "--", DOCS], ["git", "push", "-q", "origin", "HEAD"]]
        for c in cmds:
            r = subprocess.run(c, cwd=self.root, capture_output=True, text=True, timeout=180)
            if r.returncode and not (c[1] == "commit" and "nothing" in (r.stdout + r.stderr)):
                self.log(f"Publishing to GitHub failed at '{' '.join(c[:2])}': {(r.stderr or r.stdout).strip()[:200]}", "error")
                return "failed"
        self.log(f"Website updated on GitHub Pages ({n} symbols).", "info")
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


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
