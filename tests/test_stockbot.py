"""Offline tests for StockBot: a fake market feeds the simulator, strategies and bot, so no internet is needed.

Run:  .venv/bin/python -m unittest discover tests
"""
import math
import os
import sys
import tempfile
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import market, strategy  # noqa: E402
from app.bot import Bot  # noqa: E402
from app.sim_broker import OrderError, SimBroker  # noqa: E402


class FakeMarket:
    """Replaces app.market's network calls with prices the test controls."""

    def __init__(self):
        self.prices = {"AAPL": 100.0, "MSFT": 200.0, "SPY": 500.0, "BIL": 91.0}
        self.open = True
        self.actions = {}

    def corporate_actions_for(self, symbols):
        return {s: self.actions[s] for s in symbols if s in self.actions}

    def quote(self, symbol):
        if symbol not in self.prices:
            raise market.MarketDataError("unknown")
        p = self.prices[symbol]
        spread = max(0.01, p * 0.0002)
        now = time.time()
        return {"symbol": symbol, "name": symbol, "exchange": "X", "type": "EQUITY", "price": p, "bid": p - spread / 2,
                "ask": p + spread / 2, "prev_close": p, "change": 0, "change_pct": 0, "market_open": self.open,
                "session_start": now - 7200, "session_end": now + 7200, "time": now}

    def quotes(self, symbols):
        return {s: self.quote(s) for s in symbols if s in self.prices}

    def install(self, test):
        for name in ("quote", "quotes", "corporate_actions_for"):
            orig = getattr(market, name)
            setattr(market, name, getattr(self, name))
            test.addCleanup(setattr, market, name, orig)
        orig_clock = market.market_clock
        market.market_clock = lambda: {"is_open": self.open, "next_open_or_close": 0,
                                       "session_start": time.time() - 7200, "session_end": time.time() + 7200}
        test.addCleanup(setattr, market, "market_clock", orig_clock)


class SimBrokerTests(unittest.TestCase):
    def setUp(self):
        self.m = FakeMarket()
        self.m.install(self)
        self.dir = tempfile.mkdtemp()
        self.b = SimBroker(os.path.join(self.dir, "sim.db"), 10_000)

    def test_market_buy_fills_at_ask_and_sell_at_bid(self):
        o = self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 10})
        self.assertEqual(o["status"], "filled")
        self.assertGreater(o["filled_avg_price"], 100)
        s = self.b.place_order({"symbol": "AAPL", "side": "sell", "type": "market", "qty": 10})
        self.assertLess(s["filled_avg_price"], 100)
        self.assertLess(self.b.account()["cash"], 10_000)  # paid the spread both ways

    def test_dollar_amount_buy_gives_fractional_shares(self):
        o = self.b.place_order({"symbol": "MSFT", "side": "buy", "type": "market", "notional": 1000})
        self.assertAlmostEqual(o["filled_qty"] * o["filled_avg_price"], 1000, places=1)
        self.assertNotEqual(o["filled_qty"], int(o["filled_qty"]))

    def test_cannot_overspend_or_short(self):
        with self.assertRaises(OrderError):
            self.b.place_order({"symbol": "SPY", "side": "buy", "type": "market", "qty": 100})
        with self.assertRaises(OrderError):
            self.b.place_order({"symbol": "AAPL", "side": "sell", "type": "market", "qty": 1})

    def test_orders_queue_while_closed_and_fill_at_open(self):
        self.m.open = False
        o = self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 5, "time_in_force": "gtc"})
        self.assertEqual(o["status"], "accepted")
        self.m.open = True
        self.b.tick()
        self.assertEqual(self.b.get_order(o["id"])["status"], "filled")

    def test_limit_waits_for_price(self):
        o = self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 5, "limit_price": 95, "time_in_force": "gtc"})
        self.assertEqual(o["status"], "new")
        self.m.prices["AAPL"] = 94
        self.b.tick()
        self.assertEqual(self.b.get_order(o["id"])["status"], "filled")

    def test_bracket_legs_cancel_each_other(self):
        self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 5, "take_profit": 110, "stop_loss": 90})
        legs = self.b.orders("open")
        self.assertEqual(len(legs), 2)
        self.m.prices["AAPL"] = 111
        self.b.tick()
        self.assertEqual(self.b.orders("open"), [])
        self.assertEqual(self.b.positions(), [])

    def test_trailing_stop_follows_price_up(self):
        self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 5})
        o = self.b.place_order({"symbol": "AAPL", "side": "sell", "type": "trailing_stop", "qty": 5, "trail_percent": 5,
                                "time_in_force": "gtc"})
        self.m.prices["AAPL"] = 120
        self.b.tick()
        self.m.prices["AAPL"] = 115  # 4.2% off the high: still open
        self.b.tick()
        self.assertEqual(self.b.get_order(o["id"])["status"], "new")
        self.m.prices["AAPL"] = 113  # 5.8% off the high: sells
        self.b.tick()
        self.assertEqual(self.b.get_order(o["id"])["status"], "filled")


def make_ctx(series):
    """Build a strategy Context from {symbol: [closes]} on consecutive days."""
    raw = {}
    for s, closes in series.items():
        raw[s] = [{"time": 86400 * (20000 + i), "close": c} for i, c in enumerate(closes)]
    return strategy.Context(raw)


class StrategyTests(unittest.TestCase):
    N = 400

    def trend(self, start, daily, wiggle=0.01, seed=1):
        out, p = [], start
        for i in range(self.N):
            p *= 1 + daily + wiggle * math.sin(i * 0.7 + seed)
            out.append(p)
        return out

    def test_momentum_picks_the_strongest_and_goes_to_tbills_in_bear_market(self):
        series = {s: self.trend(100, 0.0002 * (k + 1), seed=k) for k, s in enumerate(strategy.STOCKS)}
        series["SPY"] = self.trend(100, 0.0005)
        series["BIL"] = [100 + i * 0.01 for i in range(self.N)]
        ctx = make_ctx(series)
        w, notes = strategy.momentum_max(ctx, ctx.last, {})
        self.assertEqual(len(w), 4)
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertTrue(set(w) <= set(strategy.STOCKS[-8:]), w)  # the steepest uptrends
        series["SPY"] = self.trend(100, -0.001)
        ctx = make_ctx(series)
        w, _ = strategy.momentum_max(ctx, ctx.last, {})
        self.assertEqual(w, {strategy.SAFE: 1.0})

    def test_keeps_current_holdings_unless_clearly_beaten(self):
        series = {s: self.trend(100, 0.001 + k * 1e-6) for k, s in enumerate(strategy.STOCKS)}  # near-identical
        series["SPY"] = self.trend(100, 0.0005)
        ctx = make_ctx(series)
        fresh, _ = strategy.momentum_max(ctx, ctx.last, {})
        held = [s for s in strategy.STOCKS if s not in fresh][:4]
        kept, _ = strategy.momentum_max(ctx, ctx.last, held)
        self.assertEqual(set(kept), set(held))  # near-ties: the bonus keeps what we own, so no churn

    def test_no_lookahead(self):
        series = {s: self.trend(100, 0.0003 * (k % 5), seed=k) for k, s in enumerate(strategy.STOCKS)}
        series["SPY"] = self.trend(100, 0.0005)
        full = make_ctx(series)
        cut = make_ctx({s: v[:300] for s, v in series.items()})
        for key in ("momentum_max", "momentum_weekly", "trend_momentum", "concentrated", "high_52w", "core_blend"):
            a, _ = strategy.STRATEGIES[key]["fn"](full, 299, {})
            b, _ = strategy.STRATEGIES[key]["fn"](cut, 299, {})
            self.assertEqual(a, b, key)

    def test_52_week_high_picks_stocks_at_their_highs_and_keeps_holdings(self):
        series = {s: self.trend(100, 0.001, wiggle=0.0) for s in strategy.STOCKS}
        fallen = strategy.STOCKS[:25]  # leaves 12 stocks at their highs
        for s in fallen:  # still above the 200-day average, but 8% off the high
            series[s] = series[s][:-10] + [series[s][-11] * 0.92] * 10
        series["SPY"] = self.trend(100, 0.0005)
        ctx = make_ctx(series)
        w, notes = strategy.high_52w(ctx, ctx.last, {})
        self.assertEqual(len(w), 6)
        self.assertFalse(set(w) & set(fallen), w)
        self.assertTrue(all("52-week high" in n for n in notes.values()))
        held = [s for s in strategy.STOCKS if s not in fallen and s not in w][:6]
        kept, _ = strategy.high_52w(ctx, ctx.last, held)
        self.assertEqual(set(kept), set(held))  # all at their highs: no reason to swap

    def test_core_blend_mixes_three_strategies_and_stays_fully_invested(self):
        series = {s: self.trend(100, 0.0002 * (k + 1), seed=k) for k, s in enumerate(strategy.STOCKS)}
        series["SPY"] = self.trend(100, 0.0005)
        series["BIL"] = [100 + i * 0.01 for i in range(self.N)]
        ctx = make_ctx(series)
        w, notes = strategy.core_blend(ctx, ctx.last, {})
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)
        self.assertGreater(len(w), 4)  # more names than Momentum Max alone
        final = strategy.finalize("core_blend", w)
        self.assertTrue(all(v <= 0.25 for s, v in final.items() if s not in strategy.ETFS))
        self.assertTrue(any("+" in n for n in notes.values()))  # some stocks are picked by more than one signal
        series["SPY"] = self.trend(100, -0.001)
        ctx = make_ctx(series)
        w, _ = strategy.core_blend(ctx, ctx.last, {})
        self.assertEqual(set(w), {strategy.SAFE})

    def test_adjusted_prices_are_used_when_present(self):
        candles = [{"time": 86400 * (20000 + i), "close": 100.0, "adj": 50.0 + i} for i in range(5)]
        ctx = strategy.Context({"SPY": candles})
        self.assertEqual(ctx.px("SPY", 4), 54.0)

    def test_finalize_respects_caps_and_reserve(self):
        w = strategy.finalize("momentum_max", {"AAPL": 0.5, "MSFT": 0.5}, reserve=0.02, max_stock_w=0.25)
        self.assertTrue(all(v <= 0.25 for v in w.values()))
        self.assertLessEqual(sum(w.values()), 0.98 + 1e-9)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.m = FakeMarket()
        self.m.install(self)
        self.dir = tempfile.mkdtemp()
        self.broker = SimBroker(os.path.join(self.dir, "sim.db"), 10_000)
        self.settings = {"bot": {"enabled": True, "mode": "auto", "strategy": "momentum_max", "max_position_pct": 25,
                                 "budget_pct": 100, "cash_reserve_pct": 2, "stop_atr": 0, "daily_loss_limit_pct": 3}}
        self.bot = Bot(self.dir, lambda: self.broker, lambda: self.settings)
        orig = strategy.targets
        strategy.targets = lambda key, holdings, reserve=0.02, max_stock_w=None: (
            {"AAPL": 0.25 * (1 - reserve), "MSFT": 0.25 * (1 - reserve)}, {"AAPL": "test", "MSFT": "test"})
        self.addCleanup(setattr, strategy, "targets", orig)

    def test_rebalance_buys_targets(self):
        self.bot.rebalance(force=True)
        pos = {p["symbol"]: p["market_value"] for p in self.broker.positions()}
        self.assertEqual(set(pos), {"AAPL", "MSFT"})
        self.assertAlmostEqual(pos["AAPL"], 2450, delta=30)

    def test_rebalance_sells_dropped_picks(self):
        self.bot.rebalance(force=True)
        strategy.targets = lambda key, holdings, reserve=0.02, max_stock_w=None: ({"MSFT": 0.25 * (1 - reserve)}, {})
        self.bot.rebalance(force=True)
        self.assertEqual({p["symbol"] for p in self.broker.positions()}, {"MSFT"})

    def test_approve_mode_waits_for_you(self):
        self.settings["bot"]["mode"] = "approve"
        self.bot.rebalance(force=True)
        self.assertEqual(self.broker.positions(), [])
        self.assertEqual(len(self.bot.state["pending"]), 2)
        for p in list(self.bot.state["pending"]):
            self.bot.approve(p["id"])
        self.assertEqual(len(self.broker.positions()), 2)

    def test_manual_positions_are_left_alone(self):
        self.broker.place_order({"symbol": "SPY", "side": "buy", "type": "market", "qty": 2})
        self.bot.rebalance(force=True)
        strategy.targets = lambda key, holdings, reserve=0.02, max_stock_w=None: ({}, {})
        self.bot.rebalance(force=True)
        self.assertEqual({p["symbol"] for p in self.broker.positions()}, {"SPY"})


if __name__ == "__main__":
    unittest.main()
