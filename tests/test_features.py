"""Offline tests for dividends and splits, performance numbers and price alerts.

Run:  .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import performance  # noqa: E402
from app.alerts import AlertError, Alerts  # noqa: E402
from app.sim_broker import OrderError, SimBroker  # noqa: E402
from tests.test_stockbot import FakeMarket  # noqa: E402


class CorporateActionTests(unittest.TestCase):
    def setUp(self):
        self.m = FakeMarket()
        self.m.install(self)
        self.b = SimBroker(os.path.join(tempfile.mkdtemp(), "sim.db"), 10_000)
        self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 10})

    def test_dividend_is_paid_once_on_shares_held_before_the_ex_date(self):
        cash = self.b.account()["cash"]
        self.m.actions["AAPL"] = [{"kind": "dividend", "time": time.time() - 86400 * 30, "amount": 9.0},  # before the buy
                                  {"kind": "dividend", "time": time.time() + 1, "amount": 0.5}]
        self.assertEqual(self.b.apply_corporate_actions(force=True), [])  # one too old, one not yet due
        time.sleep(1.1)
        paid = self.b.apply_corporate_actions(force=True)
        self.assertEqual([(a["kind"], a["amount"]) for a in paid], [("dividend", 5.0)])
        self.assertEqual(self.b.apply_corporate_actions(force=True), [])
        self.assertAlmostEqual(self.b.account()["cash"], cash + 5.0)
        self.assertEqual(len(self.b.activity()), 1)

    def test_split_keeps_the_position_value_and_cancels_open_orders(self):
        self.b.place_order({"symbol": "AAPL", "side": "sell", "type": "limit", "qty": 5, "limit_price": 150, "time_in_force": "gtc"})
        before = self.b.positions()[0]
        self.m.actions["AAPL"] = [{"kind": "split", "time": time.time() + 0.2, "ratio": 4.0}]
        time.sleep(0.3)
        self.m.prices["AAPL"] = 25.0  # the price drops to a quarter on the split day
        self.b.apply_corporate_actions(force=True)
        after = self.b.positions()[0]
        self.assertAlmostEqual(after["qty"], 40)
        self.assertAlmostEqual(after["avg_price"], before["avg_price"] / 4)
        self.assertAlmostEqual(after["market_value"], before["market_value"])
        self.assertEqual(self.b.orders("open"), [])

    def test_routine_tick_checks_at_most_every_half_hour(self):
        calls = []
        self.m.corporate_actions_for = lambda symbols: calls.append(symbols) or {}
        from app import market
        market.corporate_actions_for = self.m.corporate_actions_for
        self.b.tick()
        self.b.tick()
        self.assertEqual(len(calls), 1)


class PerformanceTests(unittest.TestCase):
    def test_closed_trades_use_average_cost(self):
        orders = [
            {"status": "filled", "symbol": "A", "side": "buy", "filled_qty": 10, "filled_avg_price": 100, "filled_at": 1},
            {"status": "filled", "symbol": "A", "side": "buy", "filled_qty": 10, "filled_avg_price": 120, "filled_at": 2},
            {"status": "filled", "symbol": "A", "side": "sell", "filled_qty": 5, "filled_avg_price": 130, "filled_at": 3},
            {"status": "filled", "symbol": "A", "side": "sell", "filled_qty": 5, "filled_avg_price": 100, "filled_at": 4},
            {"status": "canceled", "symbol": "A", "side": "sell", "filled_qty": 0, "filled_avg_price": None},
            {"status": "filled", "symbol": "B", "side": "sell", "filled_qty": 5, "filled_avg_price": 50, "filled_at": 5},  # no known cost
        ]
        t = performance.closed_trades(orders)
        self.assertEqual((t["count"], t["wins"], t["losses"]), (2, 1, 1))
        self.assertAlmostEqual(t["realized"], 5 * 20 + 5 * -10)
        self.assertEqual(t["win_rate"], 50)

    def test_summary_compares_with_spy_and_only_shows_periods_it_has(self):
        now = 1_800_000_000
        day = 86400
        hist = [{"time": now - 10 * day + i * day, "equity": 100_000 + i * 1000} for i in range(10)]
        hist[5]["equity"] = 95_000  # a dip
        spy = [{"time": now - 400 * day, "close": 400.0}, {"time": now - 10 * day, "close": 500.0}, {"time": now - 7 * day - 5, "close": 520.0}]
        acct = {"equity": 110_000.0, "starting_cash": 100_000.0, "day_pl_pct": 0.9}
        p = performance.summarize(hist, [], acct, spy, 550.0, [{"kind": "dividend", "amount": 12.5}, {"kind": "split", "amount": 0}], now=now)
        rows = {r["label"]: r for r in p["periods"]}
        self.assertEqual(list(rows), ["Today", "1W", "All time"])
        self.assertAlmostEqual(rows["All time"]["you"], 10.0)
        self.assertAlmostEqual(rows["All time"]["spy"], 10.0)
        self.assertAlmostEqual(rows["1W"]["spy"], (550 / 520 - 1) * 100)
        self.assertAlmostEqual(p["max_drawdown"], (95_000 / 104_000 - 1) * 100)
        self.assertEqual(p["dividends"], 12.5)
        self.assertIsNone(p["sharpe"])  # too few days
        import datetime as dt
        weekdays = {dt.date.fromtimestamp(h["time"]) for h in hist if dt.date.fromtimestamp(h["time"]).weekday() < 5}
        self.assertEqual(p["days"], len(weekdays) - 1)

    def test_empty_account_does_not_crash(self):
        p = performance.summarize([], [], {"equity": 100_000.0, "starting_cash": 100_000.0, "day_pl_pct": 0.0}, [], None)
        self.assertEqual([r["label"] for r in p["periods"]], ["Today"])
        self.assertEqual(p["trades"]["count"], 0)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.m = FakeMarket()
        self.m.install(self)
        self.fired = []
        self.dir = tempfile.mkdtemp()
        self.a = Alerts(self.dir, notify=self.fired.append)

    def test_direction_is_worked_out_from_the_current_price(self):
        self.assertEqual(self.a.add("aapl", 120)["direction"], "above")
        self.assertEqual(self.a.add("AAPL", 80)["direction"], "below")

    def test_fires_once_and_is_remembered(self):
        self.a.add("AAPL", 120)
        self.assertEqual(self.a.step(), [])
        self.m.prices["AAPL"] = 121.0
        self.assertEqual(len(self.a.step()), 1)
        self.assertEqual(self.a.step(), [])
        self.assertEqual(len(self.fired), 1)
        again = Alerts(self.dir)
        self.assertIsNotNone(again.list()[0]["fired"])

    def test_bad_input_and_removal(self):
        with self.assertRaises(AlertError):
            self.a.add("NOPE", 10)
        with self.assertRaises(AlertError):
            self.a.add("AAPL", "abc")
        with self.assertRaises(AlertError):
            self.a.add("AAPL", -1)
        aid = self.a.add("AAPL", 200)["id"]
        self.a.remove(aid)
        self.assertEqual(self.a.list(), [])
        with self.assertRaises(AlertError):
            self.a.remove(aid)


class RealisticTradingTests(unittest.TestCase):
    """Extended hours, slippage, broker-style checks and editing orders."""

    def setUp(self):
        self.m = FakeMarket()
        self.m.install(self)
        self.extra = {}
        plain = self.m.quote

        def quote(symbol):
            return {**plain(symbol), **self.extra}
        self.m.quote = quote
        from app import market
        market.quote = quote
        market.quotes = lambda symbols: {s: quote(s) for s in symbols if s in self.m.prices}
        self.b = SimBroker(os.path.join(tempfile.mkdtemp(), "sim.db"), 100_000)

    def after_hours(self, price):
        self.m.open = False
        self.extra = {"session": "post", "ext_price": price, "ext_time": time.time(), "ext_bid": price - 0.1, "ext_ask": price + 0.1,
                      "post_end": time.time() + 3600}

    def test_extended_hours_limit_fills_after_the_close_but_a_normal_one_waits(self):
        self.after_hours(101.0)
        normal = self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 1, "limit_price": 105})
        ext = self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 1, "limit_price": 105, "extended_hours": True})
        self.assertEqual(normal["status"], "accepted")
        self.assertEqual((ext["status"], ext["filled_avg_price"]), ("filled", 101.1))

    def test_extended_hours_needs_a_fresh_price_and_a_limit_order(self):
        self.after_hours(101.0)
        self.extra["ext_time"] = time.time() - 3600  # nobody has traded for an hour
        o = self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 1, "limit_price": 105, "extended_hours": True})
        self.assertEqual(o["status"], "new")
        with self.assertRaises(OrderError):
            self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1, "extended_hours": True})

    def test_big_market_orders_get_a_worse_price_and_small_ones_do_not(self):
        self.extra = {"est_volume": 10_000}
        small = self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1})
        big = self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 400})  # 4% of the day's volume
        self.assertAlmostEqual(small["filled_avg_price"], 100.01)
        self.assertAlmostEqual(big["filled_avg_price"], round(100.01 * (1 + 0.02 * 0.2), 4))
        sold = self.b.place_order({"symbol": "AAPL", "side": "sell", "type": "market", "qty": 400})
        self.assertLess(sold["filled_avg_price"], 99.99)

    def test_stops_on_the_wrong_side_and_sub_penny_prices_are_refused(self):
        self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 10})
        with self.assertRaises(OrderError):
            self.b.place_order({"symbol": "AAPL", "side": "sell", "type": "stop", "qty": 5, "stop_price": 105})
        with self.assertRaises(OrderError):
            self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "stop", "qty": 5, "stop_price": 95})
        with self.assertRaises(OrderError):
            self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 5, "limit_price": 99.123})
        self.assertEqual(self.b.place_order({"symbol": "AAPL", "side": "sell", "type": "stop", "qty": 5, "stop_price": 95})["status"], "new")

    def test_open_orders_can_be_edited_with_the_same_checks(self):
        self.b.place_order({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 10})
        o = self.b.place_order({"symbol": "AAPL", "side": "sell", "type": "limit", "qty": 5, "limit_price": 120, "time_in_force": "gtc"})
        o = self.b.replace_order(o["id"], {"qty": 8, "limit_price": 115})
        self.assertEqual((o["qty"], o["limit_price"], o["status"]), (8, 115, "new"))
        with self.assertRaises(OrderError):
            self.b.replace_order(o["id"], {"qty": 11})  # more than is owned
        filled = self.b.replace_order(o["id"], {"limit_price": 99})  # now below the bid: sells right away
        self.assertEqual(filled["status"], "filled")
        with self.assertRaises(OrderError):
            self.b.replace_order(o["id"], {"qty": 1})  # already done
        buy = self.b.place_order({"symbol": "MSFT", "side": "buy", "type": "limit", "qty": 10, "limit_price": 150, "time_in_force": "gtc"})
        with self.assertRaises(OrderError):
            self.b.replace_order(buy["id"], {"qty": 100_000})  # can't afford it


if __name__ == "__main__":
    unittest.main()
