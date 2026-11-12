"""Price alerts: "tell me when AAPL goes above $250". Checked on every pass of the main loop.

An alert fires once, stays in the list marked as triggered, and can be removed. Nothing here places orders.
"""
import json
import os
import threading
import time
import uuid

from . import market

MAX_ALERTS = 50


class AlertError(Exception):
    pass


class Alerts:
    def __init__(self, data_dir, notify=None):
        self.path = os.path.join(data_dir, "alerts.json")
        self.notify = notify  # called with the alert when it fires
        self.lock = threading.RLock()
        self.items = []
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.items = json.load(f)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.items, f)
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            return sorted(self.items, key=lambda a: (a["fired"] is not None, -a["created"]))

    def add(self, symbol, price, direction=None):
        symbol = (symbol or "").upper().strip()
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise AlertError("Enter a price.")
        if not price > 0:
            raise AlertError("Enter a price above zero.")
        try:
            now_px = market.quote(symbol)["price"]
        except market.MarketDataError:
            raise AlertError(f"Unknown symbol {symbol}.")
        if direction not in ("above", "below"):
            direction = "above" if price >= now_px else "below"
        with self.lock:
            if sum(1 for a in self.items if a["fired"] is None) >= MAX_ALERTS:
                raise AlertError(f"You can have at most {MAX_ALERTS} waiting alerts.")
            alert = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "direction": direction, "price": price,
                     "created": time.time(), "price_then": now_px, "fired": None, "fired_price": None}
            self.items.append(alert)
            self._save()
        return alert

    def remove(self, aid):
        with self.lock:
            before = len(self.items)
            self.items = [a for a in self.items if a["id"] != aid]
            if len(self.items) == before:
                raise AlertError("That alert no longer exists.")
            self._save()

    def step(self):
        with self.lock:
            waiting = [a for a in self.items if a["fired"] is None]
        if not waiting:
            return []
        quotes = market.quotes(list({a["symbol"] for a in waiting}))
        fired = []
        with self.lock:
            for a in waiting:
                q = quotes.get(a["symbol"])
                if not q:
                    continue
                if q["price"] >= a["price"] if a["direction"] == "above" else q["price"] <= a["price"]:
                    a["fired"], a["fired_price"] = time.time(), q["price"]
                    fired.append(a)
            if fired:
                # Keep the list short: only the 20 most recent triggered alerts stay.
                done = sorted((a for a in self.items if a["fired"] is not None), key=lambda a: -a["fired"])
                keep = {a["id"] for a in done[:20]}
                self.items = [a for a in self.items if a["fired"] is None or a["id"] in keep]
                self._save()
        for a in fired:
            if self.notify:
                try:
                    self.notify(a)
                except Exception:
                    pass
        return fired
