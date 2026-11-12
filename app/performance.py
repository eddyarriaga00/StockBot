"""Account performance numbers: returns by period against SPY, drawdown, day-by-day stats and closed-trade results.

Everything is computed from what the broker already records (account value snapshots, filled orders,
dividends), so it works the same for the simulator and for Alpaca.
"""
import datetime as dt
import math
import time

from . import market

PERIODS = [("1W", 7), ("1M", 30), ("3M", 91), ("1Y", 365)]


def for_broker(broker):
    """Performance of the account behind `broker` (simulator or Alpaca)."""
    try:
        spy_candles, spy_price = market.daily_closes("SPY", "2y"), market.quote("SPY")["price"]
    except market.MarketDataError:
        spy_candles, spy_price = [], None
    activity = broker.activity() if hasattr(broker, "activity") else []
    return summarize(broker.equity_history(), broker.orders("closed", 500), broker.account(), spy_candles, spy_price, activity)


def summarize(equity_hist, orders, account, spy_candles, spy_price, activity=(), now=None):
    now = now or time.time()
    hist = [h for h in equity_hist if h.get("equity")]
    equity = account["equity"]
    out = {"since": hist[0]["time"] if hist else None, "periods": [], "trades": closed_trades(orders),
           "dividends": round(sum(a["amount"] for a in activity if a["kind"] == "dividend"), 2)}

    def equity_at(t):
        prior = [h["equity"] for h in hist if h["time"] <= t]
        return prior[-1] if prior else None

    def spy_at(t):
        prior = [c["close"] for c in spy_candles if c["time"] <= t]
        return prior[-1] if prior else None

    def row(label, start_time, base):
        spy_base = spy_at(start_time)
        you = (equity / base - 1) * 100 if base else None
        spy = (spy_price / spy_base - 1) * 100 if spy_base and spy_price else None
        return {"label": label, "you": you, "spy": spy, "diff": you - spy if you is not None and spy is not None else None}

    out["periods"].append({"label": "Today", "you": account.get("day_pl_pct"), "spy": None, "diff": None})
    for label, days in PERIODS:
        start = now - days * 86400
        if hist and hist[0]["time"] <= start:  # only periods the account has actually lived through
            out["periods"].append(row(label, start, equity_at(start)))
    year_start = dt.datetime(dt.datetime.fromtimestamp(now).year, 1, 1).timestamp()
    if hist and hist[0]["time"] <= year_start:
        out["periods"].append(row("This year", year_start, equity_at(year_start)))
    if hist:
        out["periods"].append(row("All time", hist[0]["time"], account.get("starting_cash") or hist[0]["equity"]))

    # Worst drop from a high, over every snapshot.
    peak, mdd = None, 0.0
    for h in hist + [{"equity": equity}]:
        peak = max(peak or h["equity"], h["equity"])
        mdd = min(mdd, h["equity"] / peak - 1)
    out["max_drawdown"] = mdd * 100
    out["from_high"] = (equity / peak - 1) * 100 if peak else 0.0
    out["high"] = peak

    # Day-by-day: the last snapshot of each weekday.
    days = {}
    for h in hist:
        d = dt.date.fromtimestamp(h["time"])
        if d.weekday() < 5:  # the market is closed on weekends, so they aren't flat days
            days[d.isoformat()] = h["equity"]
    closes = list(days.items())
    rets = [(d, (e / closes[i][1] - 1) * 100) for i, (d, e) in enumerate(closes[1:]) if closes[i][1]]
    out["days"] = len(rets)
    out["up_days"] = sum(1 for _, r in rets if r > 0)
    out["best_day"] = max(rets, key=lambda x: x[1]) if rets else None
    out["worst_day"] = min(rets, key=lambda x: x[1]) if rets else None
    out["sharpe"] = None
    if len(rets) >= 20:  # fewer days than this is noise
        vals = [r for _, r in rets]
        mean = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
        out["sharpe"] = mean / sd * math.sqrt(252) if sd else None
    return out


def closed_trades(orders):
    """Profit and loss on shares that were sold, matched against the average cost of what was bought."""
    fills = sorted((o for o in orders if o.get("status") == "filled" and o.get("filled_qty") and o.get("filled_avg_price")),
                   key=lambda o: o.get("filled_at") or o.get("updated_at") or 0)
    book, results = {}, []
    for o in fills:
        qty, px = float(o["filled_qty"]), float(o["filled_avg_price"])
        have, avg = book.get(o["symbol"], (0.0, 0.0))
        if o["side"] == "buy":
            book[o["symbol"]] = (have + qty, (have * avg + qty * px) / (have + qty))
        elif have > 1e-9:  # sells of shares bought before the history starts can't be scored
            sold = min(qty, have)
            results.append({"symbol": o["symbol"], "pl": sold * (px - avg), "pct": (px / avg - 1) * 100 if avg else 0.0,
                            "time": o.get("filled_at")})
            book[o["symbol"]] = (have - sold, avg)
    wins = [r for r in results if r["pl"] > 0]
    losses = [r for r in results if r["pl"] < 0]
    return {"count": len(results), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(results) * 100 if results else None,
            "realized": round(sum(r["pl"] for r in results), 2),
            "avg_win": sum(r["pl"] for r in wins) / len(wins) if wins else None,
            "avg_loss": sum(r["pl"] for r in losses) / len(losses) if losses else None,
            "best": max(results, key=lambda r: r["pl"]) if results else None,
            "worst": min(results, key=lambda r: r["pl"]) if results else None,
            "fills": len(fills)}
