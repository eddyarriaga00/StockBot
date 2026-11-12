"""Posts StockBot news to Discord through webhooks (no Discord bot account needed).

One webhook is enough: everything goes to that channel. Give a channel its own webhook (Settings → Discord) and
that kind of message moves there instead:

- info:       a "start here" guide that explains the server (posted once, then kept up to date)
- dashboard:  ONE message that is edited in place every few minutes: account value, chart, holdings, what's next
- trades:     every filled order with the bot's reason, and each rebalance
- alerts:     approvals waiting, loss halts, errors, big moves in a holding, StockBot coming online
- daily:      a morning plan at the open and a summary with a chart after the close, new all-time highs
- weekly:     the tournament report card and any champion change

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
import uuid

from . import chart, market, strategy

SITE = "https://eddyarriaga00.github.io/StockBot/"
WEBHOOK_RE = re.compile(r"^https://(?:ptb\.|canary\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
UA = "StockBot (https://github.com/eddyarriaga00/StockBot, 1.0)"
GREEN, RED, BLUE, YELLOW, GREY = 0x22C38E, 0xFF5C5C, 0x4C8DFF, 0xF5B041, 0x8A94A3
DEFAULT_DISCORD = {"trades": True, "alerts": True, "daily": True, "weekly": True, "dashboard": True, "movers": True}
ALERT_KINDS = {"warn", "error", "approve"}
CHANNELS = {  # kind -> (suggested channel name, what shows up there)
    "info": ("start-here", "What this server is and how to read it."),
    "dashboard": ("dashboard", "One live message: account value, chart, holdings and what the bot does next."),
    "trades": ("trades", "Every buy and sell, with the bot's reason."),
    "alerts": ("alerts", "Things that need you: approvals, loss halts, errors, big moves."),
    "daily": ("daily-summary", "The plan at the open and the results after the close."),
    "weekly": ("weekly-report", "The strategy tournament report card, every Friday."),
}
DEDICATED_ONLY = {"info", "dashboard"}  # these would bury a shared channel, so they need their own
MOVE_ALERT_PCT = 4.0


def valid_webhook(url):
    return bool(WEBHOOK_RE.match((url or "").strip()))


class Discord:
    def __init__(self, data_dir, load_settings, get_broker, tournament, start_worker=True):
        self.path = os.path.join(data_dir, "discord.json")
        self.load_settings = load_settings
        self.get_broker = get_broker
        self.tournament = tournament
        self.bot = None     # set by main: used for "what happens next"
        self.health = None  # set by main: used for the dashboard's status line
        self.lock = threading.RLock()
        self.state = {"seen_orders": [], "last_daily": None, "last_report": None, "champion_changes": 0, "primed": False,
                      "seen_activity": None, "messages": {}, "last_dashboard": 0, "last_brief": None, "movers": {}, "ath": None, "ath_day": None}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.state.update(json.load(f))
        self.q = queue.Queue(maxsize=200)
        self.recent = {}  # alert text -> last sent, to avoid repeating the same error every 20 seconds
        self.last_error = None
        if start_worker:
            threading.Thread(target=self._worker, daemon=True).start()

    # ---------- config ----------
    @property
    def url(self):
        return self.load_settings().get("discord_webhook", "")

    def webhooks(self):
        return {k: v for k, v in (self.load_settings().get("discord_webhooks") or {}).items() if k in CHANNELS and v}

    def url_for(self, kind):
        own = self.webhooks().get(kind)
        if own or kind in DEDICATED_ONLY:
            return own or ""
        return self.url

    def connected(self):
        return bool(self.url or self.webhooks())

    def opts(self):
        return {**DEFAULT_DISCORD, **self.load_settings().get("discord", {})}

    def on(self, kind):
        return bool(self.url_for(kind)) and self.opts().get(kind, True)

    def _save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)

    # ---------- sending ----------
    def send(self, title, desc="", color=BLUE, fields=None, url=None, kind="alerts", image=None, edit=None):
        """Queue one message for the `kind` channel. `image` is PNG bytes shown in the embed.
        `edit` is a name: the message is edited in place next time instead of posted again."""
        embed = {"title": title[:256], "description": desc[:4000], "color": color, "timestamp": _iso(time.time()),
                 "footer": {"text": f"StockBot · {self.get_broker().name}"}}
        if fields:
            embed["fields"] = [{"name": n[:256], "value": (v or "—")[:1024], "inline": inline} for n, v, inline in fields][:25]
        if url:
            embed["url"] = url
        if image:
            embed["image"] = {"url": "attachment://chart.png"}
        try:
            self.q.put_nowait({"kind": kind, "payload": {"username": "StockBot", "embeds": [embed]}, "file": image, "edit": edit})
        except queue.Full:
            pass

    def post_now(self, payload, url=None):
        """Send immediately (used by the Test button). Returns None on success or an error message."""
        return self._request(url or self.url, payload)[0]

    def _request(self, url, payload, file=None, message_id=None):
        """POST a new message, or PATCH `message_id`. Returns (error or None, Discord's reply or None).
        The error is "gone" when the message to edit no longer exists."""
        if not valid_webhook(url):
            return "That isn't a Discord webhook link. It should start with https://discord.com/api/webhooks/", None
        target = url + (f"/messages/{message_id}" if message_id else "") + "?wait=true"
        if message_id:
            payload = {k: v for k, v in payload.items() if k not in ("username", "avatar_url")}
        if file:
            body, ctype = _multipart({**payload, "attachments": [{"id": 0, "filename": "chart.png"}]}, file)
        else:
            body, ctype = json.dumps(payload).encode(), "application/json"
        for _ in range(3):
            req = urllib.request.Request(target, data=body, method="PATCH" if message_id else "POST",
                                         headers={"Content-Type": ctype, "User-Agent": UA})
            try:
                raw = urllib.request.urlopen(req, timeout=20).read()
                try:
                    return None, json.loads(raw or b"{}")
                except ValueError:
                    return None, {}
            except urllib.error.HTTPError as e:
                if e.code == 429:  # rate limited: wait as long as Discord asks
                    try:
                        wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                    except ValueError:
                        wait = 2
                    time.sleep(min(wait, 30))
                    continue
                if e.code == 404 and message_id:
                    return "gone", None
                if e.code in (401, 403, 404):
                    return "Discord says this webhook doesn't exist anymore. Make a new one and paste it again.", None
                return f"Discord error {e.code}.", None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                return f"Couldn't reach Discord: {e}", None
        return "Discord kept rate-limiting us.", None

    def deliver(self, item):
        """Send one queued message now. Returns an error message or None."""
        url = self.url_for(item["kind"])
        if not url:
            return None
        name, mid = item.get("edit"), None
        if name:
            saved = self.state["messages"].get(name) or {}
            mid = saved.get("id") if saved.get("hook") == url[-8:] else None  # a new webhook can't edit the old message
        err, reply = self._request(url, item["payload"], item.get("file"), mid)
        if err == "gone":
            err, reply = self._request(url, item["payload"], item.get("file"))
        if name and not err and reply and reply.get("id"):
            with self.lock:
                self.state["messages"][name] = {"id": reply["id"], "hook": url[-8:]}
            self._save()
        return err

    def _worker(self):
        while True:
            item = self.q.get()
            try:
                self.last_error = self.deliver(item)
            except Exception as e:  # never let one bad message stop the rest
                self.last_error = str(e)
            time.sleep(1.1)  # stay well under Discord's 30-messages-a-minute webhook limit

    # ---------- events ----------
    def on_log(self, entry):
        """Called for every line in your bot's activity log."""
        kind, msg = entry["kind"], entry["msg"]
        if kind == "info" and msg.startswith("Rebalance"):
            if self.on("trades"):
                self.send("🔄 Rebalance", msg, BLUE, url=SITE, kind="trades")
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
        if not self.connected():
            return
        try:
            for check in (self._check_fills, self._check_activity, lambda: self._check_brief(clock), lambda: self._check_daily(clock),
                          self._check_weekly, lambda: self._check_movers(clock), self._check_high,
                          lambda: self._check_dashboard(clock)):
                try:
                    check()
                except market.MarketDataError:
                    pass  # prices unavailable for a moment: try again next loop
        finally:
            self._save()

    def refresh(self):
        """Post (or update) the guide and the dashboard right now. Used after connecting channels."""
        self.guide()
        self.dashboard()
        with self.lock:
            self.state["last_dashboard"] = time.time()
        self._save()

    # ---------- helpers ----------
    def _equity_values(self, days):
        try:
            hist = self.get_broker().equity_history()
        except Exception:
            return []
        since = time.time() - days * 86400
        return [h["equity"] for h in hist if h["time"] >= since]

    def _next_action(self):
        try:
            return self.bot.next_action() if self.bot else None
        except Exception:
            return None

    def _targets_text(self):
        try:
            t = self.bot.preview_targets() if self.bot else []
        except Exception:
            t = []
        return " · ".join(f"**{x['symbol']}** {x['weight']:.0f}%" for x in t[:10])

    # ---------- trades ----------
    def _check_fills(self):
        broker = self.get_broker()
        orders = [o for o in broker.orders("closed", 100) if o["status"] == "filled"]
        seen = set(self.state["seen_orders"])
        new = [o for o in orders if o["id"] not in seen]
        if not self.state["primed"]:  # first run: don't replay old history
            new, self.state["primed"] = [], True
        self.state["seen_orders"] = [o["id"] for o in orders][:300]
        if not new or not self.on("trades"):
            return
        equity = broker.account()["equity"]
        held = {p["symbol"]: p["market_value"] for p in broker.positions()}
        for o in reversed(new[-15:]):
            buy = o["side"] == "buy"
            qty, px = o["filled_qty"], o.get("filled_avg_price") or 0
            who = "Bot" if o.get("source") == "bot" else "You"
            now_w = held.get(o["symbol"], 0) / equity * 100 if equity else 0
            fields = [("Shares", f"{qty:,.4g}", True), ("Price", f"${px:,.2f}", True), ("Total", f"${qty * px:,.2f}", True),
                      ("Now holding", f"{now_w:.1f}% of the account" if now_w > 0.05 else "None (sold out)", False)]
            self.send(f"{'🟢 BUY' if buy else '🔴 SELL'} {o['symbol']}", f"{who}: {o.get('reason') or o['type'] + ' order'}",
                      GREEN if buy else RED, fields, url=SITE, kind="trades")

    def _check_activity(self):
        """Dividends and splits (simulator only: Alpaca reports its own)."""
        broker = self.get_broker()
        if not hasattr(broker, "activity"):
            return
        items = broker.activity(50)
        seen = self.state["seen_activity"]
        self.state["seen_activity"] = [a["id"] for a in items]
        if seen is None or not self.on("trades"):  # first run: don't replay old history
            return
        for a in reversed([a for a in items if a["id"] not in seen]):
            if a["kind"] == "dividend":
                self.send(f"💵 Dividend · {a['symbol']}", f"{a['note']}.", GREEN, [("Cash added", f"${a['amount']:,.2f}", True)],
                          url=SITE, kind="trades")
            else:
                self.send(f"✂️ Stock split · {a['symbol']}", f"{a['note']}. The value of the position is unchanged.", BLUE,
                          url=SITE, kind="trades")

    def price_alert(self, a):
        if not self.connected() or not self.on("alerts"):
            return
        up = a["direction"] == "above"
        self.send(f"🔔 {a['symbol']} is {'above' if up else 'below'} ${a['price']:,.2f}",
                  f"Your price alert was reached. {a['symbol']} is now **${a['fired_price']:,.2f}**. No order was placed.",
                  GREEN if up else RED, url=SITE, kind="alerts")

    # ---------- daily ----------
    def _check_brief(self, clock):
        today = time.strftime("%Y-%m-%d")
        if not clock["is_open"] or time.localtime().tm_wday >= 5 or self.state["last_brief"] == today:
            return
        self.state["last_brief"] = today
        if self.on("daily"):
            self.morning_brief()

    def morning_brief(self):
        a = self.get_broker().account()
        fields = [("Account value", f"${a['equity']:,.2f}", True), ("Cash", f"${a['cash']:,.2f}", True)]
        nxt, targets = self._next_action(), self._targets_text()
        if nxt:
            fields.append(("Today's plan", nxt, False))
        if targets:
            fields.append(("Target portfolio", targets, False))
        self.send(f"🔔 Market open · {time.strftime('%a %b %d')}", "", BLUE, fields, url=SITE, kind="daily")

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
        fields = [("Account value", f"${a['equity']:,.2f}", True),
                  ("Today", f"{a['day_pl']:+,.2f} ({a['day_pl_pct']:+.2f}%)", True),
                  ("SPY today", f"{spy:+.2f}%" if spy is not None else "—", True),
                  ("Since start", f"{a['total_pl']:+,.2f} ({a['total_pl_pct']:+.2f}%)", True),
                  ("Cash", f"${a['cash']:,.2f}", True)]
        if spy is not None:
            diff = a["day_pl_pct"] - spy
            fields.append(("vs SPY today", f"{'Beat' if diff >= 0 else 'Trailed'} by {abs(diff):.2f} pts", True))
        fields.append(("Holdings", _holdings(pos, a["equity"]), False))
        movers = sorted(pos, key=lambda p: p.get("day_pl_pct", 0))
        if len(movers) >= 2:
            best, worst = movers[-1], movers[0]
            fields.append(("Best / worst today", f"**{best['symbol']}** {best.get('day_pl_pct', 0):+.2f}% · "
                                                 f"**{worst['symbol']}** {worst.get('day_pl_pct', 0):+.2f}%", False))
        try:
            st = self.tournament.standings()
            leader = max(st["rows"], key=lambda r: r["live_return"])
            champ = strategy.STRATEGIES[st["champion"]]["name"]
            fields.append(("Tournament", f"Bot follows **{champ}**. Live leader: **{leader['name']}** {leader['live_return']:+.2f}%", False))
        except Exception:
            pass
        nxt = self._next_action()
        if nxt:
            fields.append(("Next", nxt, False))
        vals = self._equity_values(30)
        self.send(f"{'📈' if up else '📉'} Daily summary · {time.strftime('%a %b %d')}", "", GREEN if up else RED, fields, url=SITE,
                  kind="daily", image=chart.line_chart(vals, baseline=a.get("starting_cash")) if len(vals) > 1 else None)

    def _check_high(self):
        if not self.on("daily"):
            return
        equity = self.get_broker().account()["equity"]
        today = time.strftime("%Y-%m-%d")
        if self.state["ath"] is None:  # first run: learn the record without announcing it
            self.state["ath"] = max(self._equity_values(36500) + [equity])
            return
        if equity > self.state["ath"] * 1.0005:
            self.state["ath"] = equity
            if self.state["ath_day"] != today:  # at most one a day
                self.state["ath_day"] = today
                self.send("🚀 New all-time high", f"The account just reached **${equity:,.2f}**, its highest value yet.", GREEN,
                          url=SITE, kind="daily")

    # ---------- alerts ----------
    def _check_movers(self, clock):
        if not clock["is_open"] or not self.on("alerts") or not self.opts().get("movers", True):
            return
        today = time.strftime("%Y-%m-%d")
        done = self.state["movers"].get(today, [])
        for p in self.get_broker().positions():
            pct = p.get("day_pl_pct", 0)
            if abs(pct) >= MOVE_ALERT_PCT and p["symbol"] not in done:
                done.append(p["symbol"])
                up = pct > 0
                self.send(f"{'⬆️' if up else '⬇️'} {p['symbol']} is {'up' if up else 'down'} {abs(pct):.1f}% today",
                          f"Your position is worth ${p['market_value']:,.0f} ({p.get('day_pl', 0):+,.0f} today). "
                          "No action needed: the bot rebalances on its own schedule.", GREEN if up else RED, url=SITE)
        self.state["movers"] = {today: done}

    # ---------- dashboard ----------
    def _check_dashboard(self, clock):
        if not self.on("dashboard"):
            return
        every = 300 if clock["is_open"] else 1800
        if time.time() - self.state["last_dashboard"] < every:
            return
        self.state["last_dashboard"] = time.time()
        self.dashboard(clock)

    def dashboard(self, clock=None):
        if not self.url_for("dashboard"):
            return
        broker = self.get_broker()
        a = broker.account()
        pos = sorted(broker.positions(), key=lambda p: -p["market_value"])
        if clock is None:
            try:
                clock = market.market_clock()
            except market.MarketDataError:
                clock = {"is_open": False}
        up = a["total_pl"] >= 0
        fields = [("Account value", f"**${a['equity']:,.2f}**", True),
                  ("Today", f"{a['day_pl']:+,.2f} ({a['day_pl_pct']:+.2f}%)", True),
                  ("Since start", f"{a['total_pl']:+,.2f} ({a['total_pl_pct']:+.2f}%)", True),
                  ("Cash", f"${a['cash']:,.2f}", True)]
        if self.bot:
            cfg = self.bot.cfg
            fields.append(("Strategy", strategy.STRATEGIES[self.bot.strategy_key]["name"], True))
            fields.append(("Bot", ("On · " + ("full auto" if cfg["mode"] == "auto" else "asks before trading")) if cfg["enabled"] else "Off", True))
        fields.append(("Holdings", _holdings(pos, a["equity"]), False))
        nxt = self._next_action()
        if nxt:
            fields.append(("Next", nxt, False))
        try:
            rows = sorted(self.tournament.standings()["rows"], key=lambda r: -r["live_return"])
            fields.append(("Tournament (live, fake $100k each)", "\n".join(
                f"`{n}.` {'🏆 ' if r['champion'] else ''}**{r['name']}** {r['live_return']:+.2f}%" for n, r in enumerate(rows[:5], 1)), False))
        except Exception:
            pass
        status = "🟢 Market open" if clock.get("is_open") else "🌙 Market closed"
        if self.health:
            h = self.health.status()
            status += " · StockBot running" + (" · Mac kept awake" if h.get("awake") else " · ⚠️ Mac may sleep")
        vals = self._equity_values(30)
        self.send("📊 StockBot dashboard", f"{status}\nUpdated <t:{int(time.time())}:R> · [Open StockBot]({SITE})",
                  GREEN if up else RED, fields, kind="dashboard", edit="dashboard",
                  image=chart.line_chart(vals, baseline=a.get("starting_cash")) if len(vals) > 1 else None)

    def guide(self):
        if not self.url_for("info"):
            return
        hooks = self.webhooks()
        lines = [f"**#{name}**: {about}" for k, (name, about) in CHANNELS.items() if k != "info" and (k in hooks or self.url)]
        desc = ("StockBot is an auto-trader practicing with **fake money on real prices**. Nine strategies each trade their own "
                "fake $100,000, and the main account follows the champion.\n\n" + "\n".join(lines) +
                f"\n\n**Control it:** [open StockBot]({SITE}) from any device and enter your passcode to trade, change settings, "
                "or stop the bot.\n**Reading the numbers:** results under about 60 trading days are mostly noise. "
                "One good or bad week doesn't mean much.\n\nNothing here is financial advice, and no real money is involved.")
        self.send("👋 Start here", desc, BLUE, kind="info", edit="guide")

    # ---------- weekly ----------
    def _check_weekly(self):
        hist = self.tournament.state.get("history", [])
        if len(hist) > self.state["champion_changes"]:
            if self.state["champion_changes"] and self.on("weekly"):
                h = hist[-1]
                name = lambda k: strategy.STRATEGIES.get(k, {}).get("name", k or "none")
                self.send("🏆 New champion", f"The bot now follows **{name(h['to'])}** (was {name(h['from'])}).\n{h['reason']}", YELLOW,
                          kind="weekly")
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
        table = [f"{'#':<2} {'Strategy':<22} {'Live':>7} {'vs SPY':>6} {'Worst':>7} {'Honest':>7}", "-" * 56]
        for n, r in enumerate(rows, 1):
            table.append(f"{n:<2} {(r['name'] + (' *' if r['champion'] else ''))[:22]:<22} {r['live_return']:>+6.2f}% {r['vs_spy']:>+6.2f} "
                         f"{r['live_max_drawdown']:>6.1f}% {r['bt_checked']['cagr']:>6.1f}%")
        champ = strategy.STRATEGIES[st["champion"]]["name"]
        desc = ("```\n" + "\n".join(table) + "\n```\n" + f"\\* The bot follows **{champ}**. {st['reason']}\n\n"
                "**Live** = return so far on a fake $100,000. **Worst** = biggest drop from a peak. "
                "**Honest** = yearly return in a 10-year backtest without the biggest hindsight winners.\n"
                f"Under {st['live_days_needed']} trading days, live results are mostly noise.")
        self.send(f"🗓️ Weekly report card · {time.strftime('%b %d')}", desc, BLUE, url=SITE, kind="weekly")


def _holdings(pos, equity):
    if not pos:
        return "All cash"
    return "\n".join(f"**{p['symbol']}** {p['market_value'] / equity * 100 if equity else 0:.0f}% · ${p['market_value']:,.0f} · "
                     f"today {p.get('day_pl_pct', 0):+.2f}% · total {p.get('unrealized_pl_pct', 0):+.1f}%" for p in pos[:10])


def _multipart(payload, png):
    boundary = "----stockbot" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n"
            f"{json.dumps(payload)}\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"chart.png\"\r\n"
            "Content-Type: image/png\r\n\r\n").encode()
    return head + png + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
