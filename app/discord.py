"""Posts StockBot news to a Discord channel through a webhook (no Discord bot account needed).

What it posts (each can be turned off in Settings):
- trades:  every filled order on your account, with the bot's reason
- alerts:  approvals waiting, rejected orders, loss halts, kill switch, errors, StockBot coming online
- daily:   after each weekday close: account value, day and total P/L vs SPY, holdings, tournament leader
- weekly:  the tournament report card and any champion change

Messages go out from a background thread so a slow Discord never holds up trading.
"""
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

from . import market, strategy

SITE = "https://eddyarriaga00.github.io/StockBot/"
WEBHOOK_RE = re.compile(r"^https://(?:ptb\.|canary\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
UA = "StockBot (https://github.com/eddyarriaga00/StockBot, 1.0)"
GREEN, RED, BLUE, YELLOW, GREY = 0x22C38E, 0xFF5C5C, 0x4C8DFF, 0xF5B041, 0x8A94A3
DEFAULT_DISCORD = {"trades": True, "alerts": True, "daily": True, "weekly": True}
ALERT_KINDS = {"warn", "error", "approve"}


def valid_webhook(url):
    return bool(WEBHOOK_RE.match((url or "").strip()))


class Discord:
    def __init__(self, data_dir, load_settings, get_broker, tournament):
        self.path = os.path.join(data_dir, "discord.json")
        self.load_settings = load_settings
        self.get_broker = get_broker
        self.tournament = tournament
        self.state = {"seen_orders": [], "last_daily": None, "last_report": None, "champion_changes": 0, "primed": False}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.state.update(json.load(f))
        self.q = queue.Queue(maxsize=200)
        self.recent = {}  # alert text -> last sent, to avoid repeating the same error every 20 seconds
        self.last_error = None
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------- config ----------
    @property
    def url(self):
        return self.load_settings().get("discord_webhook", "")

    def opts(self):
        return {**DEFAULT_DISCORD, **self.load_settings().get("discord", {})}

    def on(self, kind):
        return bool(self.url) and self.opts().get(kind, True)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    # ---------- sending ----------
    def send(self, title, desc="", color=BLUE, fields=None, url=None):
        embed = {"title": title[:256], "description": desc[:4000], "color": color, "timestamp": _iso(time.time()),
                 "footer": {"text": f"StockBot · {self.get_broker().name}"}}
        if fields:
            embed["fields"] = [{"name": n[:256], "value": (v or "—")[:1024], "inline": inline} for n, v, inline in fields][:25]
        if url:
            embed["url"] = url
        try:
            self.q.put_nowait({"username": "StockBot", "embeds": [embed]})
        except queue.Full:
            pass

    def post_now(self, payload, url=None):
        """Send immediately (used by the Test button). Returns None on success or an error message."""
        url = url or self.url
        if not valid_webhook(url):
            return "That isn't a Discord webhook link. It should start with https://discord.com/api/webhooks/"
        for _ in range(3):
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": UA})
            try:
                urllib.request.urlopen(req, timeout=15).read()
                return None
            except urllib.error.HTTPError as e:
                if e.code == 429:  # rate limited: wait as long as Discord asks
                    try:
                        wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                    except ValueError:
                        wait = 2
                    time.sleep(min(wait, 30))
                    continue
                if e.code in (401, 403, 404):
                    return "Discord says this webhook doesn't exist anymore. Make a new one and paste it again."
                return f"Discord error {e.code}."
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                return f"Couldn't reach Discord: {e}"
        return "Discord kept rate-limiting us."

    def _worker(self):
        while True:
            payload = self.q.get()
            if self.url:
                self.last_error = self.post_now(payload)
            time.sleep(1.1)  # stay well under Discord's 30-messages-a-minute webhook limit

    # ---------- events ----------
    def on_log(self, entry):
        """Called for every line in your bot's activity log."""
        kind, msg = entry["kind"], entry["msg"]
        if kind == "info" and msg.startswith("Rebalance"):
            if self.on("trades"):
                self.send("🔄 Rebalance", msg, BLUE, url=SITE)
            return
        if kind not in ALERT_KINDS or not self.on("alerts"):
            return
        if re.search(r"timed out|urlopen error|temporarily|Connection reset", msg):  # brief internet blips fix themselves
            return
        key = re.sub(r"[\d.,$%]+", "#", msg)  # same message with different numbers counts as a repeat
        if time.time() - self.recent.get(key, 0) < 3600:
            return
        self.recent[key] = time.time()
        icon, color = {"warn": ("⚠️", YELLOW), "error": ("❗", RED), "approve": ("🖐️", BLUE)}[kind]
        title = {"warn": "Alert", "error": "Problem", "approve": "Orders need your approval"}[kind]
        self.send(f"{icon} {title}", msg + (f"\n\n[Open StockBot]({SITE})" if kind == "approve" else ""), color)

    def online(self, remote_url=None):
        if self.on("alerts"):
            self.send("🟢 StockBot is online", f"Trading on the {self.get_broker().name} account. [Open StockBot]({SITE})", GREEN)

    def step(self, clock):
        if not self.url:
            return
        try:
            self._check_fills()
            self._check_daily(clock)
            self._check_weekly()
        finally:
            self._save()

    def _check_fills(self):
        orders = [o for o in self.get_broker().orders("closed", 100) if o["status"] == "filled"]
        seen = set(self.state["seen_orders"])
        new = [o for o in orders if o["id"] not in seen]
        if not self.state["primed"]:  # first run: don't replay old history
            new, self.state["primed"] = [], True
        self.state["seen_orders"] = [o["id"] for o in orders][:300]
        if not self.on("trades"):
            return
        for o in reversed(new[-15:]):
            buy = o["side"] == "buy"
            qty, px = o["filled_qty"], o.get("filled_avg_price") or 0
            who = "Bot" if o.get("source") == "bot" else "You"
            fields = [("Shares", f"{qty:,.4g}", True), ("Price", f"${px:,.2f}", True), ("Total", f"${qty * px:,.2f}", True)]
            self.send(f"{'🟢 BUY' if buy else '🔴 SELL'} {o['symbol']}", f"{who}: {o.get('reason') or o['type'] + ' order'}",
                      GREEN if buy else RED, fields, url=SITE)

    def _check_daily(self, clock):
        today = time.strftime("%Y-%m-%d")
        end = clock.get("session_end")
        if clock["is_open"] or not end or time.time() < end or time.localtime().tm_wday >= 5 \
                or self.state["last_daily"] == today or time.time() - end > 6 * 3600:
            return
        self.state["last_daily"] = today
        if self.on("daily"):
            self.daily_summary()

    def daily_summary(self):
        broker = self.get_broker()
        a = broker.account()
        pos = sorted(broker.positions(), key=lambda p: -p["market_value"])
        try:
            spy = market.quote(strategy.BENCHMARK)["change_pct"]
        except market.MarketDataError:
            spy = None
        up = a["day_pl"] >= 0
        holdings = "\n".join(f"**{p['symbol']}** ${p['market_value']:,.0f} ({p.get('unrealized_pl_pct', 0):+.1f}%)"
                             for p in pos[:10]) or "All cash"
        fields = [("Account value", f"${a['equity']:,.2f}", True),
                  ("Today", f"{a['day_pl']:+,.2f} ({a['day_pl_pct']:+.2f}%)", True),
                  ("SPY today", f"{spy:+.2f}%" if spy is not None else "—", True),
                  ("Since start", f"{a['total_pl']:+,.2f} ({a['total_pl_pct']:+.2f}%)", True),
                  ("Cash", f"${a['cash']:,.2f}", True),
                  ("Holdings", holdings, False)]
        try:
            st = self.tournament.standings()
            leader = max(st["rows"], key=lambda r: r["live_return"])
            champ = strategy.STRATEGIES[st["champion"]]["name"]
            fields.append(("Tournament", f"Bot follows **{champ}**. Live leader: **{leader['name']}** {leader['live_return']:+.2f}%", False))
        except Exception:
            pass
        self.send(f"{'📈' if up else '📉'} Daily summary · {time.strftime('%a %b %d')}", "", GREEN if up else RED, fields, url=SITE)

    def _check_weekly(self):
        hist = self.tournament.state.get("history", [])
        if len(hist) > self.state["champion_changes"]:
            if self.state["champion_changes"] and self.on("weekly"):
                h = hist[-1]
                name = lambda k: strategy.STRATEGIES.get(k, {}).get("name", k or "none")
                self.send("🏆 New champion", f"The bot now follows **{name(h['to'])}** (was {name(h['from'])}).\n{h['reason']}", YELLOW)
            self.state["champion_changes"] = len(hist)
        rep = self.tournament.state.get("last_report")
        if rep and rep != self.state["last_report"]:
            first = self.state["last_report"] is None
            self.state["last_report"] = rep
            if not first and self.on("weekly"):
                self.weekly_report()

    def weekly_report(self):
        st = self.tournament.standings()
        rows = sorted(st["rows"], key=lambda r: -r["live_return"])
        lines = [f"`{n}.` {'🏆 ' if r['champion'] else ''}**{r['name']}** {r['live_return']:+.2f}% "
                 f"(vs SPY {r['vs_spy']:+.2f}, worst drop {r['live_max_drawdown']:.1f}%)" for n, r in enumerate(rows, 1)]
        self.send("🗓️ Weekly report card", "\n".join(lines) + "\n\nEach strategy trades its own fake $100,000. "
                  "Under 60 trading days, results are mostly noise.", BLUE, url=SITE)


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
