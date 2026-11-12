"""Simulated brokerage: real live prices, realistic order types and fill rules, fake money.

Rules modeled after how US retail brokers behave:
- Buys fill at the ask, sells at the bid (you pay the spread).
- Market orders placed while the market is closed queue until the next open.
- Limit, stop, stop-limit and trailing-stop orders trigger on live prices; stops can fill
  worse than the stop price if the price gaps.
- Day orders expire at the close; GTC orders stay open.
- Cash account: no margin, no short selling, buying power reserved by open buy orders.
- Fractional shares and dollar-amount orders allowed for market orders only.
- Bracket orders attach a take-profit and a stop-loss that cancel each other (OCO).
- Limit orders can opt in to pre-market and after-hours trading (4:00 AM to 8:00 PM ET), where spreads are wider.
- Large market orders move the price against you, in proportion to their share of the day's volume.
- Stop prices must be on the right side of the market, and prices of $1 or more can't use fractions of a cent.
- Open orders can be edited (quantity and prices) without losing their place.
- Dividends are paid in cash on the ex-dividend date (real brokers pay a few weeks later), and stock splits
  change the share count and average cost and cancel that stock's open orders.
"""
import math
import sqlite3
import threading
import time
import uuid

from . import market

TYPES = ("market", "limit", "stop", "stop_limit", "trailing_stop")
OPEN_STATUSES = ("new", "accepted", "held")
ACTIONS_EVERY = 1800  # seconds between dividend / split checks
EXT_STALE = 900       # outside regular hours, only trade against a price from the last 15 minutes


class OrderError(Exception):
    pass


class SimBroker:
    name = "sim"

    def __init__(self, db_path: str, starting_cash: float = 100_000.0):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._actions_checked = 0
        self._init(starting_cash)

    # ---------- storage ----------
    def _init(self, starting_cash):
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK (id = 1), cash REAL, starting_cash REAL, created_at REAL);
                CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, qty REAL, avg_price REAL, opened_at REAL);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, symbol TEXT, side TEXT, type TEXT, qty REAL, notional REAL,
                    limit_price REAL, stop_price REAL, trail_percent REAL, hwm REAL, triggered INTEGER DEFAULT 0,
                    tif TEXT, status TEXT, filled_qty REAL DEFAULT 0, filled_avg_price REAL,
                    created_at REAL, updated_at REAL, filled_at REAL, expires_at REAL,
                    parent_id TEXT, oco_group TEXT, take_profit REAL, stop_loss REAL,
                    source TEXT DEFAULT 'manual', reason TEXT, reject_reason TEXT);
                CREATE TABLE IF NOT EXISTS equity (time REAL PRIMARY KEY, equity REAL);
                CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, time REAL, kind TEXT, symbol TEXT, amount REAL, qty REAL, note TEXT);
                """
            )
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(orders)")}
            if "extended_hours" not in cols:  # databases from before extended-hours trading
                self.db.execute("ALTER TABLE orders ADD COLUMN extended_hours INTEGER DEFAULT 0")
            if not self.db.execute("SELECT 1 FROM account").fetchone():
                self.db.execute("INSERT INTO account VALUES (1, ?, ?, ?)", (starting_cash, starting_cash, time.time()))
            self.db.commit()

    def reset(self, starting_cash: float = 100_000.0):
        with self.lock:
            self.db.executescript("DELETE FROM positions; DELETE FROM orders; DELETE FROM equity; DELETE FROM account; DELETE FROM activity;")
            self.db.execute("INSERT INTO account VALUES (1, ?, ?, ?)", (starting_cash, starting_cash, time.time()))
            self.db.commit()

    def _cash(self) -> float:
        return self.db.execute("SELECT cash FROM account").fetchone()["cash"]

    # ---------- reads ----------
    def positions(self) -> list:
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE qty > 1e-9 ORDER BY symbol")]
        qs = market.quotes([r["symbol"] for r in rows])
        out = []
        for r in rows:
            q = qs.get(r["symbol"])
            price = q["price"] if q else r["avg_price"]
            prev = q["prev_close"] if q else price
            mv = r["qty"] * price
            cost = r["qty"] * r["avg_price"]
            out.append({
                "symbol": r["symbol"], "name": q["name"] if q else r["symbol"], "qty": r["qty"],
                "avg_price": r["avg_price"], "price": price, "market_value": mv, "cost_basis": cost,
                "unrealized_pl": mv - cost, "unrealized_pl_pct": (mv - cost) / cost * 100 if cost else 0,
                "day_pl": r["qty"] * (price - prev), "day_pl_pct": (price - prev) / prev * 100 if prev else 0,
            })
        return out

    def account(self) -> dict:
        with self.lock:
            acct = dict(self.db.execute("SELECT * FROM account").fetchone())
            reserved = self._reserved_cash()
        pos = self.positions()
        mv = sum(p["market_value"] for p in pos)
        equity = acct["cash"] + mv
        last_equity = self._equity_at_day_start(equity)
        return {
            "broker": self.name, "cash": acct["cash"], "buying_power": max(0.0, acct["cash"] - reserved),
            "long_market_value": mv, "equity": equity, "last_equity": last_equity,
            "day_pl": equity - last_equity, "day_pl_pct": (equity - last_equity) / last_equity * 100 if last_equity else 0,
            "total_pl": equity - acct["starting_cash"], "starting_cash": acct["starting_cash"],
            "total_pl_pct": (equity - acct["starting_cash"]) / acct["starting_cash"] * 100,
        }

    def _equity_at_day_start(self, fallback):
        try:
            start = market.market_clock()["session_start"] or 0
        except market.MarketDataError:
            return fallback
        row = self.db.execute("SELECT equity FROM equity WHERE time < ? ORDER BY time DESC LIMIT 1", (start,)).fetchone()
        if row:
            return row["equity"]
        return self.db.execute("SELECT starting_cash FROM account").fetchone()["starting_cash"]

    def equity_history(self) -> list:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT time, equity FROM equity ORDER BY time")]

    def orders(self, status: str = "all", limit: int = 200) -> list:
        with self.lock:
            if status == "open":
                rows = self.db.execute(f"SELECT * FROM orders WHERE status IN {OPEN_STATUSES} ORDER BY created_at DESC")
            elif status == "closed":
                rows = self.db.execute(f"SELECT * FROM orders WHERE status NOT IN {OPEN_STATUSES} ORDER BY updated_at DESC LIMIT ?", (limit,))
            else:
                rows = self.db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def activity(self, limit: int = 200) -> list:
        """Dividends received and splits applied, newest first."""
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM activity ORDER BY time DESC LIMIT ?", (limit,))]

    # ---------- dividends and splits ----------
    def apply_corporate_actions(self, force: bool = False) -> list:
        """Pay dividends and apply splits for stocks held since before the ex-date. Safe to call often."""
        now = time.time()
        if not force and now - self._actions_checked < ACTIONS_EVERY:
            return []
        self._actions_checked = now
        with self.lock:
            held = [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE qty > 1e-9")]
        if not held:
            return []
        actions = market.corporate_actions_for([p["symbol"] for p in held])
        applied = []
        with self.lock:
            for p in held:
                for a in actions.get(p["symbol"], []):
                    aid = f"{a['kind']}:{p['symbol']}:{int(a['time'])}"
                    if not p["opened_at"] < a["time"] <= now or self.db.execute("SELECT 1 FROM activity WHERE id=?", (aid,)).fetchone():
                        continue
                    row = self.db.execute("SELECT qty, avg_price FROM positions WHERE symbol=?", (p["symbol"],)).fetchone()
                    if not row or row["qty"] <= 1e-9:
                        continue
                    qty = row["qty"]
                    if a["kind"] == "dividend":
                        amount = round(qty * a["amount"], 2)
                        self.db.execute("UPDATE account SET cash=cash+?", (amount,))
                        note = f"{p['symbol']} paid ${a['amount']:,.4g} a share on {qty:,.6g} shares"
                    else:
                        amount, ratio = 0.0, a["ratio"]
                        self.db.execute("UPDATE positions SET qty=?, avg_price=? WHERE symbol=?", (qty * ratio, row["avg_price"] / ratio, p["symbol"]))
                        self.db.execute(f"UPDATE orders SET status='canceled', updated_at=?, reject_reason='Canceled by a stock split.' "
                                        f"WHERE symbol=? AND status IN {OPEN_STATUSES}", (now, p["symbol"]))
                        note = f"{p['symbol']} split {ratio:g}-for-1: {qty:,.6g} shares became {qty * ratio:,.6g}"
                    entry = {"id": aid, "time": a["time"], "kind": a["kind"], "symbol": p["symbol"], "amount": amount, "qty": qty, "note": note}
                    self.db.execute("INSERT INTO activity VALUES (:id, :time, :kind, :symbol, :amount, :qty, :note)", entry)
                    applied.append(entry)
            self.db.commit()
        return applied

    def _reserved_cash(self) -> float:
        total = 0.0
        for o in self.db.execute(f"SELECT * FROM orders WHERE side='buy' AND status IN {OPEN_STATUSES}"):
            if o["notional"]:
                total += o["notional"]
            else:
                px = o["limit_price"] or o["stop_price"] or self._safe_price(o["symbol"])
                total += (o["qty"] - o["filled_qty"]) * px * 1.002
        return total

    def _reserved_shares(self, symbol: str) -> float:
        row = self.db.execute(
            f"SELECT COALESCE(SUM(qty - filled_qty), 0) s FROM orders WHERE side='sell' AND symbol=? AND status IN {OPEN_STATUSES} AND oco_group IS NULL",
            (symbol,),
        ).fetchone()
        # OCO pairs (bracket exits) reserve the shares once, not twice.
        oco = self.db.execute(
            f"SELECT oco_group, MAX(qty - filled_qty) s FROM orders WHERE side='sell' AND symbol=? AND status IN {OPEN_STATUSES} AND oco_group IS NOT NULL GROUP BY oco_group",
            (symbol,),
        ).fetchall()
        return row["s"] + sum(r["s"] for r in oco)

    def _safe_price(self, symbol):
        try:
            return market.quote(symbol)["price"]
        except market.MarketDataError:
            return 0.0

    # ---------- order entry ----------
    def place_order(self, req: dict, source: str = "manual", reason: str = None) -> dict:
        symbol = (req.get("symbol") or "").upper().strip()
        side = req.get("side")
        otype = req.get("type", "market")
        tif = req.get("time_in_force", "day")
        qty = float(req["qty"]) if req.get("qty") not in (None, "") else None
        notional = float(req["notional"]) if req.get("notional") not in (None, "") else None
        limit_price = _num(req.get("limit_price"))
        stop_price = _num(req.get("stop_price"))
        trail_percent = _num(req.get("trail_percent"))
        take_profit = _num(req.get("take_profit"))
        stop_loss = _num(req.get("stop_loss"))
        ext = bool(req.get("extended_hours"))

        if side not in ("buy", "sell"):
            raise OrderError("Side must be buy or sell.")
        if otype not in TYPES:
            raise OrderError(f"Unsupported order type {otype}.")
        if tif not in ("day", "gtc"):
            raise OrderError("Time in force must be day or gtc.")
        if (qty is None) == (notional is None):
            raise OrderError("Enter either a share quantity or a dollar amount.")
        if qty is not None and qty <= 0 or notional is not None and notional <= 0:
            raise OrderError("Quantity must be greater than zero.")
        if notional is not None and otype != "market":
            raise OrderError("Dollar-amount orders must be market orders.")
        if qty is not None and qty != int(qty) and otype != "market":
            raise OrderError("Fractional shares are only allowed on market orders.")
        if otype in ("limit", "stop_limit") and not limit_price:
            raise OrderError("Limit price required.")
        if otype in ("stop", "stop_limit") and not stop_price:
            raise OrderError("Stop price required.")
        if otype == "trailing_stop" and not trail_percent:
            raise OrderError("Trail percent required.")
        if (take_profit or stop_loss) and side != "buy":
            raise OrderError("Brackets can only be attached to buy orders.")
        if ext and (otype != "limit" or take_profit or stop_loss):
            raise OrderError("Pre-market and after-hours orders must be plain limit orders.")
        if trail_percent is not None and not 0 < trail_percent < 100:
            raise OrderError("Trail percent must be between 0 and 100.")
        for label, v in (("Limit price", limit_price), ("Stop price", stop_price), ("Take profit", take_profit), ("Stop loss", stop_loss)):
            _check_price(label, v)

        try:
            q = market.quote(symbol)
        except market.MarketDataError:
            raise OrderError(f"Unknown symbol {symbol}.")

        _check_stop_side(side, otype, stop_price, q)
        with self.lock:
            if side == "buy":
                est_px = limit_price if otype in ("limit", "stop_limit") else max(q["ask"], stop_price or 0)
                cost = notional if notional is not None else qty * est_px
                bp = self._cash() - self._reserved_cash()
                if cost > bp + 1e-6:
                    raise OrderError(f"Insufficient buying power. Need ${cost:,.2f}, have ${bp:,.2f}.")
            else:
                held = self._held(symbol) - self._reserved_shares(symbol)
                want = qty if qty is not None else notional / q["bid"]
                if want > held + 1e-9:
                    raise OrderError(f"You can sell at most {held:g} shares of {symbol} (no short selling).")

            now = time.time()
            oid = str(uuid.uuid4())
            self.db.execute(
                """INSERT INTO orders (id, symbol, side, type, qty, notional, limit_price, stop_price, trail_percent, hwm,
                   tif, status, created_at, updated_at, expires_at, take_profit, stop_loss, source, reason, extended_hours)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (oid, symbol, side, otype, qty, notional, limit_price, stop_price, trail_percent,
                 q["price"] if otype == "trailing_stop" else None, tif,
                 "new" if q["market_open"] or (ext and _ext_active(q, now)) else "accepted",
                 now, now, _day_expiry(q, ext, now) if tif == "day" else None,
                 take_profit, stop_loss, source, reason, int(ext)),
            )
            self.db.commit()
        self.tick(only=oid)
        return self.get_order(oid)

    def get_order(self, oid: str) -> dict:
        with self.lock:
            r = self.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not r:
            raise OrderError("Order not found.")
        return dict(r)

    def cancel_order(self, oid: str) -> dict:
        with self.lock:
            o = self.get_order(oid)
            if o["status"] not in OPEN_STATUSES:
                raise OrderError(f"Order is already {o['status']}.")
            self._set_status(oid, "canceled")
            self.db.commit()
        return self.get_order(oid)

    def replace_order(self, oid: str, changes: dict) -> dict:
        """Edit an open order's quantity or prices, with the same checks as a new order."""
        with self.lock:
            o = self.get_order(oid)
            if o["status"] not in OPEN_STATUSES:
                raise OrderError(f"Order is already {o['status']}.")
            if o["notional"]:
                raise OrderError("Dollar-amount orders can't be edited. Cancel it and place a new one.")
            new = {k: (_num(changes.get(k)) if changes.get(k) not in (None, "") else o[k])
                   for k in ("qty", "limit_price", "stop_price", "trail_percent")}
            if not new["qty"] or new["qty"] <= 0:
                raise OrderError("Quantity must be greater than zero.")
            if o["type"] != "market" and new["qty"] != int(new["qty"]):
                raise OrderError("Fractional shares are only allowed on market orders.")
            if o["type"] in ("limit", "stop_limit") and not new["limit_price"]:
                raise OrderError("Limit price required.")
            if o["type"] in ("stop", "stop_limit") and not new["stop_price"]:
                raise OrderError("Stop price required.")
            if o["type"] == "trailing_stop" and not 0 < (new["trail_percent"] or 0) < 100:
                raise OrderError("Trail percent must be between 0 and 100.")
            _check_price("Limit price", new["limit_price"])
            _check_price("Stop price", new["stop_price"])
            try:
                q = market.quote(o["symbol"])
            except market.MarketDataError:
                raise OrderError("Prices aren't available right now. Try again in a moment.")
            if not o["triggered"]:
                _check_stop_side(o["side"], o["type"], new["stop_price"], q)
            mine = o["qty"] - o["filled_qty"]
            if o["side"] == "buy":
                own_px = o["limit_price"] or o["stop_price"] or q["price"]
                px = new["limit_price"] if o["type"] in ("limit", "stop_limit") else max(q["ask"], new["stop_price"] or 0)
                bp = self._cash() - self._reserved_cash() + mine * own_px * 1.002
                if new["qty"] * px > bp + 1e-6:
                    raise OrderError(f"Insufficient buying power. Need ${new['qty'] * px:,.2f}, have ${bp:,.2f}.")
            else:
                free = self._held(o["symbol"]) - self._reserved_shares(o["symbol"]) + mine
                if new["qty"] > free + 1e-9:
                    raise OrderError(f"You can sell at most {free:g} shares of {o['symbol']} (no short selling).")
            self.db.execute("UPDATE orders SET qty=?, limit_price=?, stop_price=?, trail_percent=?, updated_at=? WHERE id=?",
                            (new["qty"], new["limit_price"], new["stop_price"], new["trail_percent"], time.time(), oid))
            self.db.commit()
        self.tick(only=oid)
        return self.get_order(oid)

    def cancel_all(self):
        with self.lock:
            self.db.execute(f"UPDATE orders SET status='canceled', updated_at=? WHERE status IN {OPEN_STATUSES}", (time.time(),))
            self.db.commit()

    def close_position(self, symbol: str, source="manual", reason=None):
        symbol = symbol.upper()
        with self.lock:
            for o in self.db.execute(f"SELECT id FROM orders WHERE symbol=? AND side='sell' AND status IN {OPEN_STATUSES}", (symbol,)).fetchall():
                self._set_status(o["id"], "canceled")
            self.db.commit()
            held = self._held(symbol)
        if held <= 0:
            raise OrderError(f"No position in {symbol}.")
        return self.place_order({"symbol": symbol, "side": "sell", "type": "market", "qty": held}, source, reason)

    def _held(self, symbol):
        r = self.db.execute("SELECT qty FROM positions WHERE symbol=?", (symbol,)).fetchone()
        return r["qty"] if r else 0.0

    def _set_status(self, oid, status, reject_reason=None):
        self.db.execute("UPDATE orders SET status=?, updated_at=?, reject_reason=COALESCE(?, reject_reason) WHERE id=?",
                        (status, time.time(), reject_reason, oid))

    # ---------- matching engine ----------
    def tick(self, only: str = None):
        """Check every open order against live prices and fill/expire as a real broker would."""
        if not only:
            try:
                self.apply_corporate_actions()
            except market.MarketDataError:
                pass
        with self.lock:
            sql = f"SELECT * FROM orders WHERE status IN {OPEN_STATUSES}" + (" AND id=?" if only else "")
            open_orders = [dict(r) for r in self.db.execute(sql, (only,) if only else ())]
        if not open_orders:
            self._snapshot()
            return
        qs = market.quotes(list({o["symbol"] for o in open_orders}))
        now = time.time()
        with self.lock:
            for o in open_orders:
                q = qs.get(o["symbol"])
                if not q:
                    continue
                if o["expires_at"] and now >= o["expires_at"]:
                    self._set_status(o["id"], "expired")
                    continue
                ext = bool(o.get("extended_hours")) and _ext_active(q, now)
                if not q["market_open"] and not ext:
                    if o["status"] == "new":
                        self._set_status(o["id"], "accepted")
                    continue
                if o["tif"] == "day" and not o["expires_at"]:
                    self.db.execute("UPDATE orders SET expires_at=? WHERE id=?", (_day_expiry(q, o.get("extended_hours"), now), o["id"]))
                if not q["market_open"]:  # pre-market or after hours: limit orders only, against the wider quote
                    stale = now - (q.get("ext_time") or 0) > EXT_STALE
                    px = None if stale else q["ext_ask"] if o["side"] == "buy" and q["ext_ask"] <= o["limit_price"] else \
                        q["ext_bid"] if o["side"] == "sell" and q["ext_bid"] >= o["limit_price"] else None
                    if px is not None:
                        self._fill(o, px)
                    elif o["status"] == "accepted":
                        self._set_status(o["id"], "new")
                    continue
                fill_px = self._fill_price(o, q)
                if fill_px is not None:
                    self._fill(o, fill_px)
                elif o["status"] == "accepted":
                    self._set_status(o["id"], "new")
            self.db.commit()
        self._snapshot()

    def _fill_price(self, o, q):
        bid, ask = q["bid"], q["ask"]
        buy = o["side"] == "buy"
        t = o["type"]
        if t == "trailing_stop":
            # Sell trailing stop follows the high-water mark up; buy trailing stop follows the low down.
            hwm = o["hwm"] or q["price"]
            hwm = max(hwm, q["price"]) if not buy else min(hwm, q["price"])
            self.db.execute("UPDATE orders SET hwm=? WHERE id=?", (hwm, o["id"]))
            trigger = hwm * (1 - o["trail_percent"] / 100) if not buy else hwm * (1 + o["trail_percent"] / 100)
            hit = q["price"] <= trigger if not buy else q["price"] >= trigger
            return self._slip(o, q, ask if buy else bid) if hit else None
        if t in ("stop", "stop_limit") and not o["triggered"]:
            hit = ask >= o["stop_price"] if buy else bid <= o["stop_price"]
            if not hit:
                return None
            self.db.execute("UPDATE orders SET triggered=1 WHERE id=?", (o["id"],))
            if t == "stop":
                return self._slip(o, q, ask if buy else bid)
        if t == "market" or (t == "stop" and o["triggered"]):
            return self._slip(o, q, ask if buy else bid)
        # limit, or triggered stop-limit
        if buy and ask <= o["limit_price"]:
            return ask
        if not buy and bid >= o["limit_price"]:
            return bid
        return None

    @staticmethod
    def _slip(o, q, px):
        """Price impact for orders that take whatever the market offers: the bigger the order is next to the
        day's volume, the worse the price (square-root rule, capped at 3%). Small orders are unaffected."""
        volume = q.get("est_volume") or q.get("volume")
        qty = o["qty"] if o["qty"] is not None else (o["notional"] or 0) / px
        if not volume or qty / volume < 0.0005:
            return px
        move = min(0.03, 0.02 * math.sqrt(qty / volume))
        return round(px * (1 + move if o["side"] == "buy" else 1 - move), 4)

    def _fill(self, o, px):
        qty = o["qty"] if o["qty"] is not None else round(o["notional"] / px, 6)
        symbol = o["symbol"]
        cash = self._cash()
        pos = self.db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        now = time.time()
        if o["side"] == "buy":
            cost = qty * px
            if cost > cash + 1e-6:
                self._set_status(o["id"], "rejected", "Insufficient cash at fill time.")
                return
            if pos:
                new_qty = pos["qty"] + qty
                avg = (pos["qty"] * pos["avg_price"] + cost) / new_qty
                self.db.execute("UPDATE positions SET qty=?, avg_price=? WHERE symbol=?", (new_qty, avg, symbol))
            else:
                self.db.execute("INSERT INTO positions VALUES (?,?,?,?)", (symbol, qty, px, now))
            self.db.execute("UPDATE account SET cash=cash-?", (cost,))
        else:
            held = pos["qty"] if pos else 0
            qty = min(qty, held)
            if qty <= 0:
                self._set_status(o["id"], "rejected", "No shares to sell.")
                return
            self.db.execute("UPDATE account SET cash=cash+?", (qty * px,))
            left = held - qty
            if left <= 1e-9:
                self.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
            else:
                self.db.execute("UPDATE positions SET qty=? WHERE symbol=?", (left, symbol))
        self.db.execute("UPDATE orders SET status='filled', filled_qty=?, filled_avg_price=?, filled_at=?, updated_at=? WHERE id=?",
                        (qty, px, now, now, o["id"]))
        if o["oco_group"]:
            self.db.execute(f"UPDATE orders SET status='canceled', updated_at=? WHERE oco_group=? AND id!=? AND status IN {OPEN_STATUSES}",
                            (now, o["oco_group"], o["id"]))
        if o["side"] == "buy" and (o["take_profit"] or o["stop_loss"]):
            self._attach_bracket(o, qty, now)

    def _attach_bracket(self, parent, qty, now):
        group = str(uuid.uuid4()) if parent["take_profit"] and parent["stop_loss"] else None
        legs = []
        if parent["take_profit"]:
            legs.append(("limit", parent["take_profit"], None))
        if parent["stop_loss"]:
            legs.append(("stop", None, parent["stop_loss"]))
        for otype, lp, sp in legs:
            self.db.execute(
                """INSERT INTO orders (id, symbol, side, type, qty, limit_price, stop_price, tif, status, created_at, updated_at,
                   parent_id, oco_group, source, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), parent["symbol"], "sell", otype, qty, lp, sp, "gtc", "new",
                 now, now, parent["id"], group, parent["source"],
                 "Bracket take-profit" if otype == "limit" else "Bracket stop-loss"),
            )

    def _snapshot(self):
        with self.lock:
            last = self.db.execute("SELECT time FROM equity ORDER BY time DESC LIMIT 1").fetchone()
            if last and time.time() - last["time"] < 300:
                return
        try:
            eq = self.account()["equity"]
        except market.MarketDataError:
            return
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO equity VALUES (?, ?)", (time.time(), eq))
            self.db.commit()


def _ext_active(q, now):
    return q.get("session") in ("pre", "post") and bool(q.get("ext_price"))


def _day_expiry(q, ext, now):
    """When a day order stops working: the close, or the end of after-hours trading for extended-hours orders."""
    if ext:
        return q.get("post_end") if (q["market_open"] or q.get("session") in ("pre", "post")) else None
    return q["session_end"] if q["market_open"] else None


def _check_price(label, v):
    if v is None:
        return
    if v <= 0:
        raise OrderError(f"{label} must be greater than zero.")
    if round(v, 2 if v >= 1 else 4) != round(v, 8):
        raise OrderError(f"{label}: prices of $1 or more can have at most 2 decimal places (4 below $1).")


def _check_stop_side(side, otype, stop_price, q):
    if otype not in ("stop", "stop_limit") or not stop_price:
        return
    if side == "sell" and stop_price >= q["bid"]:
        raise OrderError(f"A sell stop must be below the current price (${q['bid']:,.2f}). To sell now, use a market or limit order.")
    if side == "buy" and stop_price <= q["ask"]:
        raise OrderError(f"A buy stop must be above the current price (${q['ask']:,.2f}). To buy now, use a market or limit order.")


def _num(v):
    return float(v) if v not in (None, "") else None
