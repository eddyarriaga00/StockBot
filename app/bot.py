"""The auto-trader: turns a strategy's target weights into orders, with risk limits and an approval mode.

One Bot runs your main account (following the tournament champion or a strategy you pick), and one Bot
per strategy runs that strategy's own fake account in the tournament.
"""
import json
import os
import threading
import time
import uuid

from . import market, strategy
from .sim_broker import OrderError

DEFAULT_BOT = {
    "enabled": False,            # master switch (the kill switch turns this off)
    "mode": "auto",              # "auto" places orders, "approve" waits for you to tap Approve
    "strategy": "auto",          # "auto" follows the tournament champion, or a strategy key
    "max_position_pct": 25.0,    # hard cap on any single stock, % of the bot's money
    "budget_pct": 100.0,         # % of account equity the bot may manage
    "cash_reserve_pct": 2.0,
    "stop_atr": 0.0,             # trailing stop distance in ATRs; 0 = off (backtests: stops cut every strategy's return)
    "daily_loss_limit_pct": 3.0, # halt trading for the day if the account falls this much
}

TOURNAMENT_BOT = {**DEFAULT_BOT, "enabled": True, "mode": "auto", "max_position_pct": 100.0}


class Bot:
    def __init__(self, data_dir, get_broker, get_settings=None, state_name="bot_state.json",
                 strategy_key=None, get_champion=None):
        self.path = os.path.join(data_dir, state_name)
        self.get_broker = get_broker
        self.get_settings = get_settings
        self.fixed_strategy = strategy_key   # set for tournament bots
        self.get_champion = get_champion
        self.lock = threading.RLock()
        self.state = {"owned": {}, "pending": [], "log": [], "last_rebalance": 0, "halted_day": None,
                      "last_scan": None, "last_stops": 0, "last_strategy": None}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.state.update(json.load(f))

    # ---------- persistence / logging ----------
    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)

    on_log = None  # optional hook, e.g. Discord alerts

    def log(self, msg, kind="info"):
        entry = {"time": time.time(), "kind": kind, "msg": msg}
        with self.lock:
            self.state["log"].insert(0, entry)
            del self.state["log"][300:]
        self.save()
        if self.on_log:
            try:
                self.on_log(entry)
            except Exception:
                pass

    @property
    def cfg(self):
        if self.fixed_strategy:
            return TOURNAMENT_BOT
        return {**DEFAULT_BOT, **self.get_settings().get("bot", {})}

    @property
    def strategy_key(self):
        if self.fixed_strategy:
            return self.fixed_strategy
        key = self.cfg["strategy"]
        if key == "auto" or key not in strategy.STRATEGIES:
            return self.get_champion() if self.get_champion else "trend_momentum"
        return key

    def scan(self):
        s = strategy.scan()
        with self.lock:
            self.state["last_scan"] = {"time": time.time(), **s}
        self.save()
        return s

    def status(self):
        key = self.strategy_key
        return {
            "config": self.cfg, "owned": self.state["owned"], "pending": self.state["pending"], "log": self.state["log"][:100],
            "last_rebalance": self.state["last_rebalance"], "halted_today": self.state["halted_day"] == _today(),
            "strategy": key, "strategy_name": strategy.STRATEGIES[key]["name"],
            "next_rebalance": self.state["last_rebalance"] + strategy.STRATEGIES[key]["rebalance_days"] * 86400 * 7 / 5,
            "last_scan": self.state.get("last_scan"),
            "next_action": self.next_action(),
            "targets": self.preview_targets(),
        }

    def next_action(self):
        """Plain-English answer to 'what will the bot do next, and when?'."""
        cfg = self.cfg
        if not cfg["enabled"]:
            return "The bot is off. Turn it on to trade."
        if self._halted_today_flag():
            return "Paused for the rest of today (daily loss limit). Resumes tomorrow."
        key = self.strategy_key
        name = strategy.STRATEGIES[key]["name"]
        days = strategy.STRATEGIES[key]["rebalance_days"]
        due_at = self.state["last_rebalance"] + days * 86400 * 7 / 5 - 3600
        switching = self.state["last_strategy"] != key
        try:
            clock = market.market_clock()
        except market.MarketDataError:
            clock = {"is_open": False, "session_start": None}
        what = f"switch the account into {name}" if switching else f"rebalance {name}"
        if switching or time.time() >= due_at:
            if clock["is_open"]:
                start = clock["session_start"] or time.time()
                if time.time() - start > 1800:
                    return f"Will {what} within a minute."
                return f"Will {what} at {time.strftime('%I:%M %p', time.localtime(start + 1800))} (30 min after the open)."
            return f"Will {what} 30 minutes after the next market open (about 10:00 AM ET)."
        return f"Holding. Next {name} check {time.strftime('%a %b %d', time.localtime(due_at))}, 30 min after the open."

    def _halted_today_flag(self):
        return self.state["halted_day"] == _today()

    def preview_targets(self):
        try:
            cfg = self.cfg
            owned = list(self.state["owned"])
            w, notes = strategy.targets(self.strategy_key, owned, reserve=cfg["cash_reserve_pct"] / 100,
                                        max_stock_w=cfg["max_position_pct"] / 100)
            return [{"symbol": s, "weight": x * 100, "why": notes.get(s, "")} for s, x in sorted(w.items(), key=lambda kv: -kv[1])]
        except Exception:
            return []

    # ---------- the plan ----------
    def plan_orders(self):
        cfg = self.cfg
        broker = self.get_broker()
        acct = broker.account()
        positions = {p["symbol"]: p for p in broker.positions()}
        owned = self.state["owned"]
        manual_mv = sum(p["market_value"] for s, p in positions.items() if s not in owned)
        budget = max(0.0, acct["equity"] * cfg["budget_pct"] / 100 - manual_mv)
        key = self.strategy_key
        weights, notes = strategy.targets(key, [s for s in owned if s in positions],
                                          reserve=cfg["cash_reserve_pct"] / 100, max_stock_w=cfg["max_position_pct"] / 100)
        open_syms = {o["symbol"] for o in broker.orders("open") if o.get("source") == "bot"}
        orders = []
        # Sells first so the cash is there for the buys.
        for s in list(owned):
            if s not in weights and s in positions and s not in open_syms:
                orders.append({"symbol": s, "side": "sell", "type": "market", "qty": positions[s]["qty"],
                               "reason": "No longer a pick", "close": True})
        buys = []
        for s, w in weights.items():
            if s in open_syms:
                continue
            have = positions[s]["market_value"] if s in positions and s in owned else 0
            diff = w * budget - have
            if abs(diff) < max(50, budget * 0.01):
                continue
            why = notes.get(s) or ("Parking spare money in SPY" if s == strategy.BENCHMARK else "Target")
            if diff > 0:
                buys.append({"symbol": s, "side": "buy", "type": "market", "notional": round(diff, 2),
                             "reason": f"{why} · target {w * 100:.1f}%"})
            else:
                px = positions[s]["price"]
                orders.append({"symbol": s, "side": "sell", "type": "market", "qty": round(min(-diff / px, positions[s]["qty"]), 6),
                               "reason": f"Trim to {w * 100:.1f}% target"})
        summary = f"{strategy.STRATEGIES[key]['name']}: " + (", ".join(f"{s} {w * 100:.0f}%" for s, w in
                                                                       sorted(weights.items(), key=lambda x: -x[1])) or "all cash")
        return orders + buys, summary

    # ---------- execution ----------
    def submit(self, req):
        broker = self.get_broker()
        reason = req.pop("reason", None)
        close = req.pop("close", False)
        try:
            o = broker.place_order(req, source="bot", reason=reason)
        except OrderError as e:
            self.log(f"Order rejected: {req['side']} {req['symbol']}: {e}", "error")
            return None
        with self.lock:
            if req["side"] == "buy":
                self.state["owned"].setdefault(req["symbol"], {"since": time.time(), "stop": None})
            elif close:
                self.state["owned"].pop(req["symbol"], None)
        amt = f"${req['notional']:,.2f}" if req.get("notional") else f"{req['qty']:g} sh"
        self.log(f"{req['side'].upper()} {req['symbol']} {amt}: {reason} [{o['status']}]", "trade")
        return o

    def execute(self, orders):
        if self.cfg["mode"] == "approve":
            with self.lock:
                for o in orders:
                    self.state["pending"].append({"id": str(uuid.uuid4()), "created": time.time(), **o})
            self.log(f"{len(orders)} order(s) waiting for your approval.", "approve")
            return
        for o in orders:
            self.submit(dict(o))

    def approve(self, pid):
        with self.lock:
            item = next((p for p in self.state["pending"] if p["id"] == pid), None)
            self.state["pending"] = [p for p in self.state["pending"] if p["id"] != pid]
        if not item:
            raise OrderError("That approval no longer exists.")
        item.pop("id"), item.pop("created")
        return self.submit(item)

    def reject(self, pid):
        with self.lock:
            self.state["pending"] = [p for p in self.state["pending"] if p["id"] != pid]
        self.save()

    def rebalance(self, force=False):
        if not self.cfg["enabled"] and not force:
            return
        if self._halted():
            return
        settle = getattr(self.get_broker(), "apply_corporate_actions", None)
        if settle:  # a split that isn't applied yet would look like a half-sized position
            try:
                settle(force=True)
            except market.MarketDataError:
                pass
        orders, summary = self.plan_orders()
        with self.lock:
            self.state["last_rebalance"] = time.time()
            self.state["last_strategy"] = self.strategy_key
        self.log(f"Rebalance → {summary}. {len(orders)} order(s).", "info")
        self.execute(orders)

    def check_stops(self):
        """Trailing stop per stock: exit if price falls stop_atr x ATR below its highest level since entry.
        Off by default: in 10-year backtests stops sold winners on normal dips and lowered every strategy's return."""
        broker = self.get_broker()
        stops_on = self.cfg["stop_atr"] > 0
        positions = {p["symbol"]: p for p in broker.positions()}
        with self.lock:
            for s in list(self.state["owned"]):  # forget positions that were sold elsewhere
                if s not in positions and time.time() - self.state["owned"][s]["since"] > 600:
                    self.state["owned"].pop(s)
        for s, info in list(self.state["owned"].items()):
            if not stops_on or s not in positions or s in strategy.ETFS:
                continue
            candles = strategy._safe_daily(s)
            a = strategy.atr(candles) if candles else None
            if not a:
                continue
            price = positions[s]["price"]
            level = price - self.cfg["stop_atr"] * a
            info["stop"] = max(info.get("stop") or level, level)
            if price <= info["stop"]:
                self.log(f"Stop hit on {s} at ${price:,.2f} (stop ${info['stop']:,.2f}).", "warn")
                self.execute([{"symbol": s, "side": "sell", "type": "market", "qty": positions[s]["qty"],
                               "reason": "Trailing stop hit", "close": True}])
        self.save()

    def _halted(self):
        if self.state["halted_day"] == _today():
            return True
        acct = self.get_broker().account()
        if acct["day_pl_pct"] <= -self.cfg["daily_loss_limit_pct"]:
            with self.lock:
                self.state["halted_day"] = _today()
            self.log(f"Daily loss limit hit ({acct['day_pl_pct']:.2f}%). Paused until tomorrow.", "warn")
            return True
        return False

    def kill(self):
        broker = self.get_broker()
        try:
            for o in broker.orders("open"):
                if o.get("source") == "bot":
                    broker.cancel_order(o["id"])
        except OrderError:
            pass
        with self.lock:
            self.state["pending"] = []
        self.log("KILL SWITCH: bot disabled and its open orders canceled.", "warn")

    # ---------- scheduling (called by the app's loop every ~20s) ----------
    def step(self, clock):
        if not self.cfg["enabled"] or not clock["is_open"]:
            return
        now = time.time()
        if now - self.state["last_stops"] > 900:
            self.state["last_stops"] = now
            self.check_stops()
        settled = now - (clock["session_start"] or now) > 1800  # skip the volatile first 30 minutes
        days = strategy.STRATEGIES[self.strategy_key]["rebalance_days"]
        # Trading days -> calendar seconds, minus an hour of slack so a daily strategy runs every session.
        due = now - self.state["last_rebalance"] > days * 86400 * 7 / 5 - 3600 if days > 1 else \
            _day(self.state["last_rebalance"]) != _today()
        if settled and (due or self.state["last_strategy"] != self.strategy_key):
            self.rebalance()


def _today():
    return time.strftime("%Y-%m-%d")


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else None
