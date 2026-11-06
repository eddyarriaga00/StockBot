"""Alpaca brokerage adapter (paper or live). Same interface as SimBroker, so the app and bot don't care which is active."""
import json
import uuid
import urllib.error
import urllib.parse
import urllib.request

from . import market
from .sim_broker import OrderError

PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"


class AlpacaBroker:
    def __init__(self, key_id: str, secret: str, live: bool = False):
        self.name = "alpaca_live" if live else "alpaca_paper"
        self.base = LIVE if live else PAPER
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret, "Content-Type": "application/json"}

    def _req(self, method, path, body=None, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("message")
            except Exception:
                msg = str(e)
            raise OrderError(f"Alpaca: {msg}") from e
        except urllib.error.URLError as e:
            raise OrderError(f"Could not reach Alpaca: {e.reason}") from e

    def check(self):
        return self._req("GET", "/v2/account")

    def account(self):
        a = self._req("GET", "/v2/account")
        eq, last = float(a["equity"]), float(a["last_equity"])
        hist = self._req("GET", "/v2/account/portfolio/history", params={"period": "all", "timeframe": "1D"}) or {}
        base = next((v for v in hist.get("equity") or [] if v), eq)
        return {
            "broker": self.name, "cash": float(a["cash"]), "buying_power": float(a.get("non_marginable_buying_power") or a["buying_power"]),
            "long_market_value": float(a["long_market_value"]), "equity": eq, "last_equity": last,
            "day_pl": eq - last, "day_pl_pct": (eq - last) / last * 100 if last else 0,
            "total_pl": eq - base, "starting_cash": base, "total_pl_pct": (eq - base) / base * 100 if base else 0,
        }

    def positions(self):
        out = []
        for p in self._req("GET", "/v2/positions"):
            qty, price, avg = float(p["qty"]), float(p["current_price"]), float(p["avg_entry_price"])
            prev = float(p.get("lastday_price") or price)
            out.append({
                "symbol": p["symbol"], "name": p["symbol"], "qty": qty, "avg_price": avg, "price": price,
                "market_value": float(p["market_value"]), "cost_basis": float(p["cost_basis"]),
                "unrealized_pl": float(p["unrealized_pl"]), "unrealized_pl_pct": float(p["unrealized_plpc"]) * 100,
                "day_pl": qty * (price - prev), "day_pl_pct": (price - prev) / prev * 100 if prev else 0,
            })
        return out

    def equity_history(self):
        h = self._req("GET", "/v2/account/portfolio/history", params={"period": "1M", "timeframe": "1H"}) or {}
        return [{"time": t, "equity": e} for t, e in zip(h.get("timestamp") or [], h.get("equity") or []) if e]

    def orders(self, status="all", limit=200):
        rows = self._req("GET", "/v2/orders", params={"status": status, "limit": min(limit, 500), "nested": "true"})
        flat = []
        for o in rows:
            flat.append(o)
            flat.extend(o.get("legs") or [])
        return [_map_order(o) for o in flat]

    def get_order(self, oid):
        return _map_order(self._req("GET", f"/v2/orders/{oid}"))

    def place_order(self, req, source="manual", reason=None):
        body = {"symbol": req["symbol"].upper(), "side": req["side"], "type": req.get("type", "market"),
                "time_in_force": req.get("time_in_force", "day")}
        for k in ("qty", "notional", "limit_price", "stop_price", "trail_percent"):
            if req.get(k) not in (None, ""):
                body[k] = str(req[k])
        tp, sl = req.get("take_profit"), req.get("stop_loss")
        if tp or sl:
            body["order_class"] = "bracket" if tp and sl else "oto"
            if tp:
                body["take_profit"] = {"limit_price": str(tp)}
            if sl:
                body["stop_loss"] = {"stop_price": str(sl)}
            body["time_in_force"] = "gtc" if body["time_in_force"] == "gtc" else "day"
        body["client_order_id"] = f"{source[:4]}-{uuid.uuid4().hex[:20]}"
        o = _map_order(self._req("POST", "/v2/orders", body))
        o["source"], o["reason"] = source, reason
        return o

    def cancel_order(self, oid):
        self._req("DELETE", f"/v2/orders/{oid}")
        return self.get_order(oid)

    def cancel_all(self):
        self._req("DELETE", "/v2/orders")

    def close_position(self, symbol, source="manual", reason=None):
        return _map_order(self._req("DELETE", f"/v2/positions/{symbol.upper()}"))

    def tick(self, only=None):
        pass  # Alpaca matches orders on its own servers.

    def reset(self, *_):
        raise OrderError("Reset only applies to the built-in simulator. Reset an Alpaca paper account from its dashboard.")


def _f(v):
    return float(v) if v not in (None, "") else None


def _map_order(o):
    status = {"partially_filled": "new", "pending_new": "new", "done_for_day": "expired", "replaced": "canceled"}.get(o["status"], o["status"])
    cid = o.get("client_order_id") or ""
    return {
        "id": o["id"], "symbol": o["symbol"], "side": o["side"], "type": o["type"], "qty": _f(o.get("qty")),
        "notional": _f(o.get("notional")), "limit_price": _f(o.get("limit_price")), "stop_price": _f(o.get("stop_price")),
        "trail_percent": _f(o.get("trail_percent")), "tif": o.get("time_in_force"), "status": status,
        "filled_qty": _f(o.get("filled_qty")) or 0, "filled_avg_price": _f(o.get("filled_avg_price")),
        "created_at": _ts(o.get("created_at")), "updated_at": _ts(o.get("updated_at")), "filled_at": _ts(o.get("filled_at")),
        "source": "bot" if cid.startswith("bot-") else "manual", "reason": None, "reject_reason": None,
    }


def _ts(s):
    if not s:
        return None
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
