"""Strategy library, shared by the backtester and live trading.

Every strategy is a function (ctx, i, holdings) -> (weights, notes) that looks only at data up to day i,
so the exact same code produces the backtest and the live orders. No look-ahead.

Strategies (all well-known, published styles, not secret edges):
- trend_momentum:   strongest uptrending large caps, sized by volatility, only while SPY is above its 200-day avg.
- concentrated:     top 4 stocks by 12-month momentum (skipping the last month). Bigger swings both ways.
- dual_momentum:    100% in whichever of US large/tech/small/intl/emerging is strongest, or bonds if none beat T-bills.
- sector_rotation:  the 3 strongest sector ETFs that are above their 200-day avg.
- dip_buyer:        buys short, sharp pullbacks (2-day RSI < 10) in uptrending stocks, sells on the bounce.
- momentum_weekly:  6 stocks with the best blended 3/6/12-month risk-adjusted momentum, checked every week,
                    with a "keep what's working" bonus so it doesn't churn. T-bills when SPY < 200-day.
- momentum_max:     same engine, only the top 4. Highest backtested return, bigger single-stock risk.
- nasdaq_2x:        2x Nasdaq-100 (QLD) while the Nasdaq is above its 200-day avg and calm, T-bills otherwise.
- spy_hold:         just hold SPY. The baseline every other strategy has to beat.
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor

from . import market

STOCKS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK-B", "JPM", "V", "MA", "LLY", "UNH",
    "XOM", "COST", "WMT", "HD", "PG", "JNJ", "ORCL", "NFLX", "AMD", "CRM", "ADBE", "KO", "PEP", "MRK", "ABBV",
    "CAT", "GE", "BAC", "CVX", "TMO", "MCD", "INTU", "QCOM",
]
SECTORS = ["XLK", "XLE", "XLV", "XLF", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
ASSET_CLASSES = ["SPY", "QQQ", "IWM", "EFA", "EEM"]
BENCHMARK, SAFE, BONDS = "SPY", "BIL", "IEF"
NASDAQ, NASDAQ_2X = "QQQ", "QLD"
ETFS = set(SECTORS + ASSET_CLASSES + [SAFE, BONDS, "GLD", "TLT", NASDAQ_2X])
ALL_SYMBOLS = list(dict.fromkeys(STOCKS + SECTORS + ASSET_CLASSES + [SAFE, BONDS, NASDAQ_2X]))
# Kept for older imports.
UNIVERSE = STOCKS


# ---------- data context ----------
class Context:
    """Daily closes for every symbol aligned to SPY's trading calendar, with fast rolling helpers."""

    def __init__(self, raw: dict):
        spy = raw[BENCHMARK]
        self.dates = [c["time"] // 86400 * 86400 for c in spy]
        self.n = len(self.dates)
        self.closes, self.first, self.cum = {}, {}, {}
        for s, candles in raw.items():
            by_day = {c["time"] // 86400 * 86400: c["close"] for c in candles}
            col, last, first = [], None, None
            for i, d in enumerate(self.dates):
                v = by_day.get(d)
                if v is not None:
                    last = v
                    if first is None:
                        first = i
                col.append(last)
            if first is None:
                continue
            self.closes[s], self.first[s] = col, first
            cum, run = [], 0.0
            for v in col:
                run += v or 0.0
                cum.append(run)
            self.cum[s] = cum

    @property
    def last(self):
        return self.n - 1

    def has(self, s, i, n=0):
        return s in self.first and i - n >= self.first[s]

    def px(self, s, i):
        return self.closes[s][i] if self.has(s, i) else None

    def sma(self, s, i, n):
        if not self.has(s, i, n - 1):
            return None
        c = self.cum[s]
        return (c[i] - (c[i - n] if i - n >= 0 else 0)) / n

    def ret(self, s, i, n, skip=0):
        if not self.has(s, i, n):
            return None
        return self.closes[s][i - skip] / self.closes[s][i - n] - 1

    def rsi(self, s, i, n=14):
        if not self.has(s, i, n):
            return None
        col = self.closes[s]
        gains = losses = 0.0
        for k in range(i - n + 1, i + 1):
            d = col[k] - col[k - 1]
            if d > 0:
                gains += d
            else:
                losses -= d
        if losses == 0:
            return 100.0
        return 100 - 100 / (1 + gains / losses)

    def vol(self, s, i, n=63):
        if not self.has(s, i, n):
            return None
        col = self.closes[s]
        rets = [col[k] / col[k - 1] - 1 for k in range(i - n + 1, i + 1)]
        m = sum(rets) / n
        return math.sqrt(sum((r - m) ** 2 for r in rets) / n) * math.sqrt(252) or 1e-9

    def risk_on(self, i):
        s200 = self.sma(BENCHMARK, i, 200)
        return s200 is None or self.px(BENCHMARK, i) > s200


def load_context(rng: str) -> Context:
    with ThreadPoolExecutor(max_workers=12) as pool:
        raw = dict(zip(ALL_SYMBOLS, pool.map(lambda s: _safe_daily(s, rng), ALL_SYMBOLS)))
    return Context({s: c for s, c in raw.items() if c})


def _safe_daily(s, rng="2y"):
    try:
        return market.daily_closes(s, rng)
    except market.MarketDataError:
        return []


_live = {"ctx": None, "time": 0}


def live_context() -> Context:
    if not _live["ctx"] or time.time() - _live["time"] > 900:
        _live["ctx"], _live["time"] = load_context("2y"), time.time()
    return _live["ctx"]


# ---------- strategies ----------
def trend_momentum(ctx, i, holdings):
    if not ctx.risk_on(i):
        return {SAFE: 1.0}, {SAFE: "SPY below its 200-day average: risk-off, holding T-bills"}
    scored = []
    for s in STOCKS:
        p, s50, s200, r = ctx.px(s, i), ctx.sma(s, i, 50), ctx.sma(s, i, 200), ctx.rsi(s, i)
        m, v = ctx.ret(s, i, 126), ctx.vol(s, i)
        if None in (p, s50, s200, r, m, v) or not (p > s50 > s200) or r >= 80:
            continue
        scored.append((m / v, s, m, v))
    scored.sort(reverse=True)
    top = scored[:8]
    inv = {s: 1 / v for _, s, _, v in top}
    total = sum(inv.values()) or 1
    weights = {s: x / total for s, x in inv.items()}
    notes = {s: f"Uptrend, 6-mo {m * 100:+.0f}%, vol {v * 100:.0f}%" for _, s, m, v in top}
    return weights, notes


def concentrated(ctx, i, holdings):
    if not ctx.risk_on(i):
        return {SAFE: 1.0}, {SAFE: "SPY below its 200-day average: risk-off"}
    scored = []
    for s in STOCKS:
        p, s200, m = ctx.px(s, i), ctx.sma(s, i, 200), ctx.ret(s, i, 252, skip=21)
        if None in (p, s200, m) or p < s200:
            continue
        scored.append((m, s))
    scored.sort(reverse=True)
    top = scored[:4]
    return {s: 1 / len(top) for _, s in top}, {s: f"12-1 month momentum {m * 100:+.0f}%" for m, s in top}


def dual_momentum(ctx, i, holdings):
    scored = [(ctx.ret(s, i, 252), s) for s in ASSET_CLASSES if ctx.ret(s, i, 252) is not None]
    if not scored:
        return {}, {}
    best_ret, best = max(scored)
    cash_ret = ctx.ret(SAFE, i, 252) or 0
    if best_ret <= cash_ret:
        return {BONDS: 1.0}, {BONDS: f"Best asset ({best} {best_ret * 100:+.0f}%) not beating T-bills: bonds"}
    return {best: 1.0}, {best: f"Strongest asset class, 12-mo {best_ret * 100:+.0f}%"}


def sector_rotation(ctx, i, holdings):
    scored = []
    for s in SECTORS:
        p, s200 = ctx.px(s, i), ctx.sma(s, i, 200)
        rs = [ctx.ret(s, i, n) for n in (21, 63, 126)]
        if None in (p, s200) or None in rs or p < s200:
            continue
        scored.append((sum(rs) / 3, s))
    scored.sort(reverse=True)
    top = scored[:3]
    weights = {s: 1 / 3 for _, s in top}
    if len(top) < 3:
        weights[SAFE] = (3 - len(top)) / 3
    return weights, {s: f"Sector momentum {m * 100:+.1f}% avg" for m, s in top}


def dip_buyer(ctx, i, holdings):
    weights, notes = {}, {}
    for s in holdings:
        if s in STOCKS and ctx.has(s, i, 5) and ctx.px(s, i) <= ctx.sma(s, i, 5):
            weights[s] = 0.10
            notes[s] = "Holding: waiting for the bounce above the 5-day avg"
    cands = []
    for s in STOCKS:
        if s in weights:
            continue
        p, s200, r2 = ctx.px(s, i), ctx.sma(s, i, 200), ctx.rsi(s, i, 2)
        if None in (p, s200, r2) or p < s200 or r2 >= 10:
            continue
        cands.append((r2, s))
    for r2, s in sorted(cands)[: 10 - len(weights)]:
        weights[s] = 0.10
        notes[s] = f"Pullback in an uptrend (2-day RSI {r2:.0f})"
    return weights, notes


def _blended_momentum(ctx, s, i):
    """Risk-adjusted momentum blended over 3, 6 and 12 months, skipping the last week (short-term noise)."""
    rs = [ctx.ret(s, i, n, skip=5) for n in (63, 126, 252)]
    v = ctx.vol(s, i)
    if None in rs or v is None:
        return None, None
    return (0.2 * rs[0] + 0.4 * rs[1] + 0.4 * rs[2]) / v, rs


def _momentum_top(ctx, i, holdings, n):
    if not ctx.risk_on(i):
        return {SAFE: 1.0}, {SAFE: "SPY below its 200-day average: risk-off, holding T-bills"}
    scored = []
    for s in STOCKS:
        p, s200 = ctx.px(s, i), ctx.sma(s, i, 200)
        score, rs = _blended_momentum(ctx, s, i)
        if None in (p, s200, score) or p < s200:
            continue
        # Stocks we already own get a 25% bonus: only swap when something is clearly stronger (less churn).
        scored.append((score * (1.25 if s in holdings else 1.0), s, rs))
    scored.sort(reverse=True)
    top = scored[:n]
    notes = {s: f"Momentum 3/6/12-mo {rs[0] * 100:+.0f}% / {rs[1] * 100:+.0f}% / {rs[2] * 100:+.0f}%"
             + (" (kept)" if s in holdings else "") for _, s, rs in top}
    return {s: 1 / n for _, s, _ in top}, notes


def momentum_weekly(ctx, i, holdings):
    return _momentum_top(ctx, i, holdings, 6)


def momentum_max(ctx, i, holdings):
    return _momentum_top(ctx, i, holdings, 4)


def nasdaq_2x(ctx, i, holdings):
    p, s200, v = ctx.px(NASDAQ, i), ctx.sma(NASDAQ, i, 200), ctx.vol(NASDAQ, i, 21)
    if None in (p, s200, v) or not ctx.has(NASDAQ_2X, i):
        return {SAFE: 1.0}, {SAFE: "Not enough Nasdaq data"}
    # 2% band around the 200-day so it doesn't flip in and out on small wiggles.
    on = p > s200 * (0.98 if NASDAQ_2X in holdings else 1.02)
    if on and v < 0.35:
        return {NASDAQ_2X: 1.0}, {NASDAQ_2X: f"Nasdaq {p / s200 * 100 - 100:+.1f}% vs 200-day, volatility {v * 100:.0f}%"}
    why = "Nasdaq below its 200-day average" if not on else f"Nasdaq too volatile ({v * 100:.0f}%)"
    return {SAFE: 1.0}, {SAFE: why + ": holding T-bills"}


def spy_hold(ctx, i, holdings):
    return {BENCHMARK: 1.0}, {BENCHMARK: "Baseline: buy and hold the S&P 500"}


STRATEGIES = {
    "trend_momentum": {"name": "Trend Momentum", "fn": trend_momentum, "rebalance_days": 21, "fill_spy": True, "max_w": 0.10,
                       "about": "Strongest uptrending large caps (vol-sized, max 10% each), rest in SPY. T-bills when SPY < 200-day."},
    "concentrated": {"name": "Concentrated Momentum", "fn": concentrated, "rebalance_days": 21, "fill_spy": False, "max_w": 0.25,
                     "about": "Top 4 stocks by 12-month momentum, 25% each. Biggest upside, biggest swings."},
    "dual_momentum": {"name": "Dual Momentum", "fn": dual_momentum, "rebalance_days": 21, "fill_spy": False, "max_w": 1.0,
                      "about": "All-in on the strongest of US large, tech, small, international, emerging; bonds when none beat T-bills."},
    "sector_rotation": {"name": "Sector Rotation", "fn": sector_rotation, "rebalance_days": 21, "fill_spy": False, "max_w": 0.34,
                        "about": "Three strongest sector ETFs above their 200-day average."},
    "dip_buyer": {"name": "Dip Buyer", "fn": dip_buyer, "rebalance_days": 1, "fill_spy": True, "max_w": 0.10,
                  "about": "Buys sharp 2-day pullbacks in uptrending stocks, sells on the bounce. Rest in SPY."},
    "momentum_weekly": {"name": "Momentum Weekly", "fn": momentum_weekly, "rebalance_days": 5, "fill_spy": False, "max_w": 0.17,
                        "about": "6 stocks with the best 3/6/12-month momentum, checked weekly, keeps winners. T-bills when SPY < 200-day."},
    "momentum_max": {"name": "Momentum Max", "fn": momentum_max, "rebalance_days": 5, "fill_spy": False, "max_w": 0.25,
                     "about": "Top 4 momentum stocks, checked weekly. Highest backtested return; each stock is 25% of the account."},
    "nasdaq_2x": {"name": "Nasdaq 2x Trend", "fn": nasdaq_2x, "rebalance_days": 5, "fill_spy": False, "max_w": 1.0,
                  "about": "2x Nasdaq-100 (QLD) while the Nasdaq is above its 200-day and calm; T-bills otherwise. High risk."},
    "spy_hold": {"name": "Hold SPY (baseline)", "fn": spy_hold, "rebalance_days": 63, "fill_spy": False, "max_w": 1.0,
                 "about": "Buy and hold the S&P 500. Every strategy must beat this to be worth it."},
}


def finalize(key, weights, reserve=0.02, max_stock_w=None):
    """Apply position caps, keep a cash reserve, and park leftover money in SPY if the strategy does that."""
    st = STRATEGIES[key]
    cap = min(st["max_w"], max_stock_w) if max_stock_w else st["max_w"]
    out = {s: (min(w, cap) if s not in ETFS else w) for s, w in weights.items() if w > 0}
    total = sum(out.values())
    if total > 1:
        out = {s: w / total for s, w in out.items()}
        total = 1.0
    if st["fill_spy"] and total < 0.99:
        out[BENCHMARK] = out.get(BENCHMARK, 0) + (1 - total)
    return {s: w * (1 - reserve) for s, w in out.items()}


def targets(key, holdings, reserve=0.02, max_stock_w=None):
    """Today's target weights for a strategy, plus a note per symbol explaining why."""
    ctx = live_context()
    weights, notes = STRATEGIES[key]["fn"](ctx, ctx.last, holdings)
    return finalize(key, weights, reserve, max_stock_w), notes


# ---------- backtest ----------
_bt_cache = {"time": 0, "result": None}


def backtest_all(years="10y", cost=0.0005, force=False):
    """Backtest every strategy on the same data. The last 40% of the period is reported separately as
    out-of-sample: it's the closest thing to 'how would this have done on data it wasn't tuned on'."""
    if not force and _bt_cache["result"] and time.time() - _bt_cache["time"] < 86400:
        return _bt_cache["result"]
    ctx = load_context(years)
    start = 252
    split = start + int((ctx.n - start) * 0.6)
    out = {}
    for key in STRATEGIES:
        out[key] = _run(ctx, key, start, split, cost)
    spy = out["spy_hold"]
    for key, r in out.items():
        r["beats_spy_oos"] = r["oos"]["cagr"] > spy["oos"]["cagr"] and key != "spy_hold"
    result = {"strategies": out, "start": ctx.dates[start], "split": ctx.dates[split], "end": ctx.dates[-1],
              "caveat": "Backtests use today's well-known large caps (survivorship bias) and can't capture everything, "
                        "so real results will likely be worse. Live paper results decide the champion once they exist."}
    _bt_cache.update(time=time.time(), result=result)
    return result


def _run(ctx, key, start, split, cost):
    st = STRATEGIES[key]
    holdings, equity, trades = {}, [1.0], 0
    for i in range(start, ctx.n - 1):
        if (i - start) % st["rebalance_days"] == 0:
            w, _ = st["fn"](ctx, i, holdings)
            new = finalize(key, w)
            turnover = sum(abs(new.get(s, 0) - holdings.get(s, 0)) for s in set(new) | set(holdings))
            trades += len(set(new) ^ set(holdings))
            equity[-1] *= 1 - turnover * cost
            holdings = new
        r = 0.0
        for s, w in holdings.items():
            a, b = ctx.px(s, i), ctx.px(s, i + 1)
            if a and b:
                r += w * (b / a - 1)
        equity.append(equity[-1] * (1 + r))
        # Let weights drift with prices between rebalances, like a real account.
        if holdings and r != -1:
            grown = {s: w * ((ctx.px(s, i + 1) or 1) / (ctx.px(s, i) or 1)) for s, w in holdings.items()}
            tot = sum(grown.values()) + (1 - sum(holdings.values()))
            holdings = {s: v / tot for s, v in grown.items()}
    k = split - start
    return {
        "key": key, "name": st["name"], "about": st["about"], "trades": trades,
        "full": _stats(equity), "in_sample": _stats(equity[: k + 1]), "oos": _stats(equity[k:]),
        "curve": [{"time": ctx.dates[start + j], "value": v} for j, v in enumerate(equity)][::3],
    }


def _stats(curve):
    if len(curve) < 3:
        return {"total_return": 0, "cagr": 0, "max_drawdown": 0, "sharpe": 0, "years": 0}
    years = len(curve) / 252
    total = curve[-1] / curve[0]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [b / a - 1 for a, b in zip(curve, curve[1:])]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) or 1e-9
    return {"total_return": (total - 1) * 100, "cagr": (total ** (1 / years) - 1) * 100, "max_drawdown": mdd * 100,
            "sharpe": mean / sd * math.sqrt(252), "years": years}


# ---------- scan (for the UI) ----------
def atr(candles, n=14):
    if len(candles) <= n:
        return None
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for p, c in zip(candles[-n - 1:-1], candles[-n:])]
    return sum(trs) / n


def scan():
    ctx = live_context()
    i = ctx.last
    rows = []
    for s in STOCKS:
        p, s50, s200, r, m, v = ctx.px(s, i), ctx.sma(s, i, 50), ctx.sma(s, i, 200), ctx.rsi(s, i), ctx.ret(s, i, 126), ctx.vol(s, i)
        if None in (p, s50, s200, r, m, v):
            continue
        uptrend = p > s50 > s200
        reasons = [f"{'Above' if p > s50 else 'Below'} 50-day avg (${s50:,.2f})",
                   f"50-day {'above' if s50 > s200 else 'below'} 200-day",
                   f"6-mo {m * 100:+.1f}%, vol {v * 100:.0f}%",
                   f"RSI {r:.0f}" + (" (overbought)" if r >= 80 else ""),
                   f"2-day RSI {ctx.rsi(s, i, 2):.0f}"]
        rows.append({"symbol": s, "price": p, "sma50": s50, "sma200": s200, "rsi": r, "momentum": m, "volatility": v,
                     "score": m / v, "eligible": uptrend and r < 80, "reasons": reasons})
    rows.sort(key=lambda x: (x["eligible"], x["score"]), reverse=True)
    holds = {}
    for key, st in STRATEGIES.items():
        w, notes = st["fn"](ctx, i, {})
        holds[key] = {"name": st["name"], "weights": finalize(key, w), "notes": notes}
    tm = set(holds["trend_momentum"]["weights"])
    for x in rows:
        x["action"] = "BUY / HOLD" if x["symbol"] in tm else ("AVOID" if not x["eligible"] else "WATCH")
    return {"regime": {"risk_on": ctx.risk_on(i), "spy": ctx.px(BENCHMARK, i), "spy_sma200": ctx.sma(BENCHMARK, i, 200)},
            "rows": rows, "strategies": holds}
