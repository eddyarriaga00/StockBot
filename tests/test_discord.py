"""Offline tests for the Discord messages and the chart renderer: nothing is sent to Discord.

Run:  .venv/bin/python -m unittest discover tests
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import chart  # noqa: E402
from app.discord import Discord, valid_webhook  # noqa: E402

HOOK = "https://discord.com/api/webhooks/123/abc-DEF_1"
HOOK2 = "https://discord.com/api/webhooks/456/xyz-XYZ_2"


class FakeBroker:
    name = "Simulator"

    def __init__(self):
        self.pos = [{"symbol": "AAPL", "market_value": 6000.0, "day_pl_pct": 5.2, "day_pl": 300.0, "unrealized_pl_pct": 8.0},
                    {"symbol": "KO", "market_value": 3000.0, "day_pl_pct": -0.4, "day_pl": -12.0, "unrealized_pl_pct": 1.0}]
        self.filled = []

    def account(self):
        return {"equity": 10_000.0, "cash": 1000.0, "day_pl": 288.0, "day_pl_pct": 2.9, "total_pl": 0.0, "total_pl_pct": 0.0,
                "starting_cash": 10_000.0}

    def positions(self):
        return self.pos

    def orders(self, status, limit=100):
        return self.filled

    def equity_history(self):
        import time
        return [{"time": time.time() - 100 + i, "equity": 10_000 + i * 3} for i in range(50)]


class FakeTournament:
    state = {"history": [], "last_report": None}

    def standings(self):
        raise RuntimeError("not needed")


class DiscordTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"discord_webhook": HOOK, "discord_webhooks": {}, "discord": {}}
        self.broker = FakeBroker()
        self.d = Discord(tempfile.mkdtemp(), lambda: self.settings, lambda: self.broker, FakeTournament(), start_worker=False)
        self.sent = []

        def fake_request(url, payload, file=None, message_id=None):
            self.sent.append({"url": url, "payload": payload, "file": file, "message_id": message_id})
            return None, {"id": "m1"}
        self.d._request = fake_request

    def drain(self):
        while not self.d.q.empty():
            self.d.deliver(self.d.q.get())
        return self.sent

    def test_webhook_links_are_validated(self):
        self.assertTrue(valid_webhook(HOOK))
        self.assertFalse(valid_webhook("https://evil.example.com/api/webhooks/1/abc"))
        self.assertFalse(valid_webhook("https://discord.com/api/webhooks/1/abc?x=1"))

    def test_messages_go_to_their_own_channel_or_fall_back(self):
        self.settings["discord_webhooks"] = {"trades": HOOK2}
        self.d.send("t", kind="trades")
        self.d.send("a", kind="alerts")
        sent = self.drain()
        self.assertEqual([m["url"] for m in sent], [HOOK2, HOOK])

    def test_dashboard_needs_its_own_channel_and_is_edited_in_place(self):
        self.d.dashboard(clock={"is_open": True})
        self.assertEqual(self.drain(), [])  # no dedicated channel: nothing posted to the shared one
        self.settings["discord_webhooks"] = {"dashboard": HOOK2}
        self.d.dashboard(clock={"is_open": True})
        self.d.dashboard(clock={"is_open": True})
        sent = self.drain()
        self.assertEqual([m["message_id"] for m in sent], [None, "m1"])  # second time edits the first message
        self.assertTrue(sent[0]["file"].startswith(b"\x89PNG"))
        fields = {f["name"]: f["value"] for f in sent[0]["payload"]["embeds"][0]["fields"]}
        self.assertIn("AAPL", fields["Holdings"])

    def test_deleted_dashboard_message_is_posted_again(self):
        self.settings["discord_webhooks"] = {"dashboard": HOOK2}
        self.d.state["messages"]["dashboard"] = {"id": "old", "hook": HOOK2[-8:]}
        calls = []

        def gone_then_ok(url, payload, file=None, message_id=None):
            calls.append(message_id)
            return ("gone", None) if message_id else (None, {"id": "new"})
        self.d._request = gone_then_ok
        self.d.dashboard(clock={"is_open": False})
        self.d.deliver(self.d.q.get())
        self.assertEqual(calls, ["old", None])
        self.assertEqual(self.d.state["messages"]["dashboard"]["id"], "new")

    def test_big_move_alert_fires_once_per_stock_per_day(self):
        self.d._check_movers({"is_open": True})
        self.d._check_movers({"is_open": True})
        sent = self.drain()
        self.assertEqual(len(sent), 1)
        self.assertIn("AAPL is up 5.2%", sent[0]["payload"]["embeds"][0]["title"])

    def test_old_fills_are_not_replayed_but_new_ones_post(self):
        self.broker.filled = [{"id": "o1", "status": "filled", "side": "buy", "symbol": "KO", "filled_qty": 1, "filled_avg_price": 60,
                               "type": "market"}]
        self.d._check_fills()
        self.assertEqual(self.drain(), [])
        self.broker.filled.insert(0, {"id": "o2", "status": "filled", "side": "sell", "symbol": "AAPL", "filled_qty": 2,
                                      "filled_avg_price": 100, "type": "market", "source": "bot", "reason": "dropped"})
        self.d._check_fills()
        sent = self.drain()
        self.assertEqual(len(sent), 1)
        self.assertIn("SELL AAPL", sent[0]["payload"]["embeds"][0]["title"])

    def test_all_time_high_is_announced_once_a_day(self):
        self.d._check_high()  # learns the current record quietly
        self.assertEqual(self.drain(), [])
        self.broker.account = lambda: {"equity": 12_000.0}
        self.d._check_high()
        self.broker.account = lambda: {"equity": 12_500.0}
        self.d._check_high()
        self.assertEqual(len(self.drain()), 1)


class ChartTests(unittest.TestCase):
    def test_png_is_valid_and_right_size(self):
        png = chart.line_chart([100, 101, 99, 104, 108], width=300, height=120)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (300, 120))
        idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
        self.assertEqual(len(zlib.decompress(idat)), 120 * (300 * 3 + 1))

    def test_needs_two_points_and_survives_flat_data(self):
        self.assertIsNone(chart.line_chart([5]))
        self.assertIsNotNone(chart.line_chart([5, 5, 5], width=200, height=100))


if __name__ == "__main__":
    unittest.main()
