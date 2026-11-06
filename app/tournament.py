"""Strategy tournament: every strategy trades its own fake $100k account on live prices, side by side.

The champion (the strategy your main account follows when set to Auto) is picked by:
1. Backtest out-of-sample Sharpe ratio, until every strategy has LIVE_DAYS trading days of live paper results.
2. After that, live paper results: highest return, as long as its worst drop isn't more than 10 points worse than SPY's.
The champion is reviewed at most once a week, so it doesn't flip-flop on noise.
"""
import datetime as dt
import json
import os
import threading
import time

from . import market, strategy
from .bot import Bot
from .sim_broker import SimBroker

LIVE_DAYS = 60       # about 3 months of trading days before live results take over
REVIEW_EVERY = 7 * 86400


class Tournament:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "tournament")
        self.reports_dir = os.path.join(data_dir, "reports")
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)
        self.path = os.path.join(self.dir, "tournament.json")
        self.lock = threading.RLock()
        self.state = {"started": {}, "champion": None, "champion_reason": "", "reviewed": 0, "history": [], "last_report": None}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.state.update(json.load(f))
        self.entries = {}
        for key in strategy.STRATEGIES:
            broker = SimBroker(os.path.join(self.dir, f"{key}.db"))
            self.entries[key] = {"broker": broker,
                                 "bot": Bot(self.dir, (lambda b=broker: b), state_name=f"{key}.json", strategy_key=key)}

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)

    # ---------- running ----------
    def step(self, clock):
        for key, e in self.entries.items():
            e["broker"].tick()
            if clock["is_open"] and key not in self.state["started"]:
                self.state["started"][key] = {"time": time.time(), "spy": market.quote(strategy.BENCHMARK)["price"]}
                self.save()
            e["bot"].step(clock)
        self.review()
        self.maybe_write_report(clock)

    def reset(self):
        with self.lock:
            for key, e in self.entries.items():
                e["broker"].reset()
                e["bot"].state.update(owned={}, pending=[], last_rebalance=0, last_strategy=None)
                e["bot"].log("Tournament account reset.")
            self.state.update(started={}, reviewed=0)
            self.save()

    # ---------- scoring ----------
    def standings(self):
        bt = strategy.backtest_all()
        spy_now = market.quote(strategy.BENCHMARK)["price"]
        rows = []
        for key, e in self.entries.items():
            st = strategy.STRATEGIES[key]
            acct = e["broker"].account()
            hist = e["broker"].equity_history()
            started = self.state["started"].get(key)
            mdd, peak = 0.0, None
            for h in hist:
                peak = max(peak or h["equity"], h["equity"])
                mdd = min(mdd, h["equity"] / peak - 1)
            spy_ret = (spy_now / started["spy"] - 1) * 100 if started else 0.0
            b = bt["strategies"][key]
            rows.append({
                "key": key, "name": st["name"], "about": st["about"], "rebalance_days": st["rebalance_days"],
                "equity": acct["equity"], "live_return": acct["total_pl_pct"], "spy_return": spy_ret,
                "vs_spy": acct["total_pl_pct"] - spy_ret, "live_max_drawdown": mdd * 100, "day_pl_pct": acct["day_pl_pct"],
                "trading_days": _trading_days(started["time"]) if started else 0, "started": started["time"] if started else None,
                "trades": sum(1 for o in e["broker"].orders("closed", 10000) if o["status"] == "filled"),
                "holdings": [{"symbol": p["symbol"], "weight": p["market_value"] / acct["equity"] * 100} for p in e["broker"].positions()],
                "bt_oos": b["oos"], "bt_full": b["full"], "beats_spy_oos": b["beats_spy_oos"],
                "champion": key == self.champion(),
            })
        return {"champion": self.champion(), "reason": self.state["champion_reason"], "live_days_needed": LIVE_DAYS,
                "history": self.state["history"][-20:], "rows": rows, "backtest_period": {k: bt[k] for k in ("start", "split", "end")},
                "caveat": bt["caveat"]}

    def champion(self):
        if not self.state["champion"]:
            self.review(force=True)
        return self.state["champion"]

    def review(self, force=False):
        if not force and time.time() - self.state["reviewed"] < REVIEW_EVERY:
            return
        bt = strategy.backtest_all()["strategies"]
        started = self.state["started"]
        live_ready = all(k in started and _trading_days(started[k]["time"]) >= LIVE_DAYS for k in self.entries)
        if live_ready:
            spy = self.entries["spy_hold"]
            spy_dd = _max_dd(spy["broker"].equity_history())
            scored = []
            for k, e in self.entries.items():
                dd = _max_dd(e["broker"].equity_history())
                if dd >= spy_dd - 10:
                    scored.append((e["broker"].account()["total_pl_pct"], k))
            best_ret, best = max(scored)
            reason = f"Best live paper return ({best_ret:+.1f}%) over {LIVE_DAYS}+ trading days without a much worse drop than SPY."
        else:
            best_sharpe, best = max((v["oos"]["sharpe"], k) for k, v in bt.items())
            reason = (f"Highest out-of-sample backtest Sharpe ({best_sharpe:.2f}). "
                      f"Live paper results take over after {LIVE_DAYS} trading days.")
        with self.lock:
            prev = self.state["champion"]
            self.state.update(champion=best, champion_reason=reason, reviewed=time.time())
            if prev != best:
                self.state["history"].append({"time": time.time(), "from": prev, "to": best, "reason": reason})
        self.save()

    # ---------- weekly report card ----------
    def maybe_write_report(self, clock):
        now = dt.datetime.now()
        week = now.strftime("%G-W%V")
        after_close = now.weekday() == 4 and not clock["is_open"] and now.hour >= 16 or now.weekday() >= 5
        if not after_close or self.state["last_report"] == week or not self.state["started"]:
            return
        path = self.write_report()
        self.state["last_report"] = week
        self.save()
        return path

    def write_report(self):
        s = self.standings()
        rows = sorted(s["rows"], key=lambda r: -r["live_return"])
        lines = [f"# StockBot weekly report card: {dt.date.today():%B %d, %Y}", "",
                 f"**Champion:** {strategy.STRATEGIES[s['champion']]['name']}. {s['reason']}", "",
                 "| Rank | Strategy | Live return | vs SPY | Worst drop | Trades | Days live | Backtest (out-of-sample) |",
                 "|---|---|---|---|---|---|---|---|"]
        for n, r in enumerate(rows, 1):
            lines.append(f"| {n} | {r['name']}{' 🏆' if r['champion'] else ''} | {r['live_return']:+.2f}% | {r['vs_spy']:+.2f} pts | "
                         f"{r['live_max_drawdown']:.1f}% | {r['trades']} | {r['trading_days']} | {r['bt_oos']['cagr']:.1f}%/yr |")
        lines += ["", "Each strategy trades its own fake $100,000 on live prices.",
                  f"Results under {LIVE_DAYS} trading days are mostly noise: don't read much into one good or bad week."]
        path = os.path.join(self.reports_dir, f"report-{dt.date.today().isoformat()}.md")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def live_curves(self):
        out = {}
        for key, e in self.entries.items():
            hist = e["broker"].equity_history()
            out[key] = [{"time": int(h["time"]), "value": h["equity"] / 1000} for h in hist]  # $k, comparable since all start at $100k
        return out


def _trading_days(since):
    d, end, n = dt.date.fromtimestamp(since), dt.date.today(), 0
    while d < end:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _max_dd(hist):
    peak, mdd = None, 0.0
    for h in hist:
        peak = max(peak or h["equity"], h["equity"])
        mdd = min(mdd, h["equity"] / peak - 1)
    return mdd * 100
