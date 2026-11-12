"""Live market data from Yahoo Finance's public chart endpoints (no API key needed)."""
import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15"}
BASE = "https://query1.finance.yahoo.com"

_cache: dict = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=12)

# Range -> candle interval, the way most trading apps pair them.
RANGE_INTERVALS = {
    "1d": "5m",
    "5d": "15m",
    "1mo": "60m",
    "3mo": "1d",
    "6mo": "1d",
    "1y": "1d",
    "5y": "1wk",
    "max": "1mo",
}


class MarketDataError(Exception):
    pass


def _get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # network, HTTP 404 for bad symbols, etc.
        raise MarketDataError(str(e)) from e


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (now, value)
    return value


def _chart(symbol: str, rng: str, interval: str, prepost: bool = False) -> dict:
    q = urllib.parse.urlencode({"range": rng, "interval": interval, "includePrePost": str(prepost).lower()})
    data = _get_json(f"{BASE}/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}")
    result = (data.get("chart") or {}).get("result")
    if not result:
        err = (data.get("chart") or {}).get("error") or {}
        raise MarketDataError(err.get("description") or f"No data for {symbol}")
    return result[0]


def _candles(res: dict) -> list:
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c, v = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v or 0})
    return out


def spread_for(price: float, dollar_volume: float = None, widen: float = 1.0) -> float:
    """Estimated half-spread. Yahoo's free feed has no bid/ask, so model it the way real spreads behave:
    a fraction of a cent for the most traded stocks, much wider for thinly traded ones, and wider still
    just after the open and outside regular hours (`widen`)."""
    if dollar_volume is None:
        bps = 2.0
    elif dollar_volume >= 500e6:
        bps = 1.0
    elif dollar_volume >= 50e6:
        bps = 2.5
    elif dollar_volume >= 5e6:
        bps = 6.0
    elif dollar_volume >= 500e3:
        bps = 20.0
    else:
        bps = 50.0
    tick = 0.005 if price >= 1 else 0.0005  # a spread is at least one cent (a hundredth of a cent under $1)
    return max(tick, price * bps / 10_000 * widen)


def _px(v: float) -> float:
    return round(v, 2 if v >= 1 else 4)


def quote(symbol: str) -> dict:
    symbol = symbol.upper().strip()

    def load():
        res = _chart(symbol, "1d", "1m", prepost=True)
        m = res["meta"]
        periods = m.get("currentTradingPeriod") or {}
        period, pre, post = (periods.get(k) or {} for k in ("regular", "pre", "post"))
        now = time.time()
        every = _candles(res)
        r_start, r_end = period.get("start", 0), period.get("end", 0)
        candles = [c for c in every if r_start <= c["time"] < r_end] if period else every
        price = m.get("regularMarketPrice") or (candles[-1]["close"] if candles else None)
        if price is None:
            raise MarketDataError(f"No price for {symbol}")
        prev = m.get("chartPreviousClose") or m.get("previousClose") or price
        is_open = bool(period) and r_start <= now < r_end
        if is_open:
            session = "regular"
        elif pre and pre.get("start", 0) <= now < pre.get("end", 0):
            session = "pre"
        elif post and post.get("start", 0) <= now < post.get("end", 0):
            session = "post"
        else:
            session = "closed"
        volume = m.get("regularMarketVolume") or sum(c["volume"] for c in candles)
        # Early in the day, scale today's volume up to a full day so a stock doesn't look thinly traded at 9:35.
        done = min(1.0, max(0.1, (now - r_start) / (r_end - r_start))) if is_open and r_end > r_start else 1.0
        est_volume = volume / done if volume else 0
        just_opened = is_open and now - r_start < 300
        half = spread_for(price, price * est_volume if est_volume else None, 2.0 if just_opened else 1.0)
        # Pre-market and after-hours: the last trade outside regular hours, if there was one.
        if session == "pre":
            ext = [c for c in every if pre.get("start", 0) <= c["time"] < r_start]
        else:
            ext = [c for c in every if r_end and c["time"] >= r_end] if not is_open else []
        ext_price = ext[-1]["close"] if ext else None
        ext_half = spread_for(ext_price, price * est_volume if est_volume else None, 4.0) if ext_price else None
        return {
            "symbol": symbol,
            "name": m.get("longName") or m.get("shortName") or symbol,
            "exchange": m.get("fullExchangeName") or m.get("exchangeName"),
            "type": m.get("instrumentType"),
            "price": price,
            "bid": _px(price - half),
            "ask": _px(price + half),
            "prev_close": prev,
            "change": price - prev,
            "change_pct": (price - prev) / prev * 100 if prev else 0,
            "open": candles[0]["open"] if candles else price,
            "high": m.get("regularMarketDayHigh") or (max(c["high"] for c in candles) if candles else price),
            "low": m.get("regularMarketDayLow") or (min(c["low"] for c in candles) if candles else price),
            "volume": volume,
            "est_volume": est_volume,
            "week52_high": m.get("fiftyTwoWeekHigh"),
            "week52_low": m.get("fiftyTwoWeekLow"),
            "market_open": is_open,
            "session": session,
            "session_start": period.get("start"),
            "session_end": period.get("end"),
            "pre_start": pre.get("start"),
            "post_end": post.get("end"),
            "ext_price": ext_price,
            "ext_time": ext[-1]["time"] if ext else None,
            "ext_bid": _px(ext_price - ext_half) if ext_price else None,
            "ext_ask": _px(ext_price + ext_half) if ext_price else None,
            "ext_change": ext_price - price if ext_price else None,
            "ext_change_pct": (ext_price - price) / price * 100 if ext_price and price else None,
            "time": m.get("regularMarketTime"),
            "spark": [c["close"] for c in candles[::5]],
        }

    return _cached(f"q:{symbol}", 5, load)


def quotes(symbols: list) -> dict:
    out = {}
    futures = {s: _pool.submit(quote, s) for s in symbols}
    for s, f in futures.items():
        try:
            out[s.upper()] = f.result()
        except MarketDataError:
            pass
    return out


def history(symbol: str, rng: str = "1d") -> dict:
    symbol = symbol.upper()
    interval = RANGE_INTERVALS.get(rng, "1d")

    def load():
        res = _chart(symbol, rng, interval)
        return {"symbol": symbol, "range": rng, "interval": interval,
                "gmtoffset": res["meta"].get("gmtoffset", 0), "candles": _candles(res)}

    return _cached(f"h:{symbol}:{rng}", 30 if rng in ("1d", "5d") else 600, load)


def daily_closes(symbol: str, rng: str = "2y") -> list:
    """Daily candles used by the strategy engine and backtester."""
    symbol = symbol.upper()
    return _cached(f"d:{symbol}:{rng}", 3600, lambda: _candles(_chart(symbol, rng, "1d")))


def corporate_actions(symbol: str) -> list:
    """Recent dividends and splits, oldest first: {"kind": "dividend", "time", "amount"} (dollars per share)
    or {"kind": "split", "time", "ratio"} (new shares per old share). Times are the ex-date's market open."""
    symbol = symbol.upper()

    def load():
        q = urllib.parse.urlencode({"range": "6mo", "interval": "1d", "events": "div,splits"})
        data = _get_json(f"{BASE}/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}")
        result = (data.get("chart") or {}).get("result")
        if not result:
            raise MarketDataError(f"No data for {symbol}")
        ev = result[0].get("events") or {}
        out = [{"kind": "dividend", "time": d["date"], "amount": float(d["amount"])}
               for d in (ev.get("dividends") or {}).values() if d.get("amount")]
        out += [{"kind": "split", "time": x["date"], "ratio": float(x["numerator"]) / float(x["denominator"])}
                for x in (ev.get("splits") or {}).values() if x.get("numerator") and x.get("denominator")]
        return sorted(out, key=lambda a: a["time"])

    return _cached(f"ca:{symbol}", 900, load)


def corporate_actions_for(symbols: list) -> dict:
    out = {}
    futures = {s: _pool.submit(corporate_actions, s) for s in symbols}
    for s, f in futures.items():
        try:
            out[s.upper()] = f.result()
        except MarketDataError:
            pass
    return out


def search(q: str) -> list:
    if not q.strip():
        return []
    url = f"{BASE}/v1/finance/search?" + urllib.parse.urlencode({"q": q, "quotesCount": 8, "newsCount": 0})
    data = _cached(f"s:{q.lower()}", 300, lambda: _get_json(url))
    return [
        {"symbol": x["symbol"], "name": x.get("shortname") or x.get("longname") or x["symbol"],
         "exchange": x.get("exchDisp"), "type": x.get("quoteType")}
        for x in data.get("quotes", [])
        if x.get("quoteType") in ("EQUITY", "ETF") and "." not in x.get("symbol", ".")
    ]


def news(symbol: str) -> list:
    url = f"{BASE}/v1/finance/search?" + urllib.parse.urlencode({"q": symbol, "quotesCount": 0, "newsCount": 8})
    data = _cached(f"n:{symbol.upper()}", 600, lambda: _get_json(url))
    return [
        {"title": n.get("title"), "publisher": n.get("publisher"), "link": n.get("link"),
         "time": n.get("providerPublishTime")}
        for n in data.get("news", [])
    ]


def market_clock() -> dict:
    q = quote("SPY")
    return {"is_open": q["market_open"], "next_open_or_close": q["session_end"] if q["market_open"] else q["session_start"],
            "session_start": q["session_start"], "session_end": q["session_end"], "session": q["session"],
            "pre_start": q["pre_start"], "post_end": q["post_end"]}
