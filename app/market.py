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


def spread_for(price: float) -> float:
    """Estimated half-spread. Yahoo's free feed has no bid/ask, so model a tight large-cap spread."""
    return max(0.01, round(price * 0.0002, 2))


def quote(symbol: str) -> dict:
    symbol = symbol.upper().strip()

    def load():
        res = _chart(symbol, "1d", "1m")
        m = res["meta"]
        candles = _candles(res)
        price = m.get("regularMarketPrice") or (candles[-1]["close"] if candles else None)
        if price is None:
            raise MarketDataError(f"No price for {symbol}")
        prev = m.get("chartPreviousClose") or m.get("previousClose") or price
        half = spread_for(price)
        period = (m.get("currentTradingPeriod") or {}).get("regular") or {}
        now = time.time()
        return {
            "symbol": symbol,
            "name": m.get("longName") or m.get("shortName") or symbol,
            "exchange": m.get("fullExchangeName") or m.get("exchangeName"),
            "type": m.get("instrumentType"),
            "price": price,
            "bid": round(price - half, 2),
            "ask": round(price + half, 2),
            "prev_close": prev,
            "change": price - prev,
            "change_pct": (price - prev) / prev * 100 if prev else 0,
            "open": candles[0]["open"] if candles else price,
            "high": m.get("regularMarketDayHigh") or (max(c["high"] for c in candles) if candles else price),
            "low": m.get("regularMarketDayLow") or (min(c["low"] for c in candles) if candles else price),
            "volume": m.get("regularMarketVolume") or sum(c["volume"] for c in candles),
            "week52_high": m.get("fiftyTwoWeekHigh"),
            "week52_low": m.get("fiftyTwoWeekLow"),
            "market_open": bool(period) and period.get("start", 0) <= now < period.get("end", 0),
            "session_start": period.get("start"),
            "session_end": period.get("end"),
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
            "session_start": q["session_start"], "session_end": q["session_end"]}
