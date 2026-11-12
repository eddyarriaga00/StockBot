"""The track record: how long the account has been running, how reliably, and how far it is from a paper
record worth taking seriously.

The milestones are StockBot's own checkpoints. Meeting all of them is a track record, not a guarantee:
fake-money results never promise the same results with real money.
"""
import datetime as dt
import time

from . import calendar_us, market, performance

LIVE_DAYS = 60          # trading days before live results mean much (same number the tournament uses)
REBALANCES = 12         # about three months of weekly rebalances
SELLOFF = -10.0         # a market drop this deep is the first real test of a strategy
STICKY = {"live_days", "rebalances", "selloff"}  # milestones that stay met once reached (worth announcing)


def record(broker, tournament=None, now=None):
    now = now or time.time()
    hist = [h for h in broker.equity_history() if h.get("equity")]
    perf = performance.for_broker(broker)
    started = hist[0]["time"] if hist else now
    sess = calendar_us.sessions(started, now)
    begun = [s for s in sess if s[1] <= now]  # sessions that have opened
    out = {"started": started, "days": int((now - started) // 86400) + 1, "weeks": (now - started) / 604800,
           "trading_days": len(begun), "live_days_needed": LIVE_DAYS}

    # Was StockBot running? It records the account value every 5 minutes, so a session with no records was missed.
    times = [h["time"] for h in hist]
    covered, missed, got, expected = 0, [], 0, 0
    recent_missed = []
    for i, (d, open_, close) in enumerate(begun):
        end = min(close, now)
        want = max(1, (end - max(open_, started)) / 300)
        have = sum(1 for t in times if open_ <= t <= end + 300)
        got, expected = got + min(have, want), expected + want
        if have >= 1:
            covered += 1
        else:
            missed.append(d.isoformat())
            if i >= len(begun) - 20:
                recent_missed.append(d)
    out["covered_days"], out["missed_days"] = covered, missed
    out["uptime_pct"] = got / expected * 100 if expected else None

    all_time = next((p for p in perf["periods"] if p["label"] == "All time"), None) or {}
    you, spy, diff = all_time.get("you"), all_time.get("spy"), all_time.get("diff")
    out.update(ret=you, spy_ret=spy, vs_spy=diff, max_drawdown=perf["max_drawdown"], sharpe=perf["sharpe"],
               trades=perf["trades"]["fills"], dividends=perf["dividends"], high=perf["high"])

    # The S&P 500's own worst drop over the same stretch.
    try:
        closes = [c["close"] for c in market.daily_closes("SPY", "2y") if c["time"] >= started - 86400] + [market.quote("SPY")["price"]]
    except market.MarketDataError:
        closes = []
    peak, spy_dd = None, 0.0
    for c in closes:
        peak = max(peak or c, c)
        spy_dd = min(spy_dd, (c / peak - 1) * 100)
    out["spy_max_drawdown"] = spy_dd

    # Weeks that ended higher than they started.
    weeks = {}
    for h in hist:
        weeks[dt.date.fromtimestamp(h["time"]).strftime("%G-W%V")] = h["equity"]
    vals = list(weeks.values())
    out["weeks_done"] = max(0, len(vals) - 1)
    out["weeks_up"] = sum(1 for a, b in zip(vals, vals[1:]) if b > a)

    # Days on which the bot traded: with a weekly strategy that is the number of rebalances.
    bot_days = {dt.date.fromtimestamp(o["filled_at"]).isoformat() for o in broker.orders("closed", 500)
                if o.get("status") == "filled" and o.get("source") == "bot" and o.get("filled_at")}
    out["rebalances"] = len(bot_days)

    if tournament is not None:
        st = tournament.state.get("started") or {}
        out["tournament_days"] = min((len([s for s in calendar_us.sessions(v["time"], now) if s[1] <= now]) for v in st.values()), default=0)

    td = out["trading_days"]
    left = max(0, LIVE_DAYS - td)
    eta = calendar_us.add_trading_days(dt.date.fromtimestamp(now), left) if left else None
    f = lambda v: f"{v:+.2f}%"  # noqa: E731
    ms = [
        {"key": "live_days", "name": f"{LIVE_DAYS} trading days of results", "done": td >= LIVE_DAYS, "progress": min(1.0, td / LIVE_DAYS),
         "detail": f"{td} of {LIVE_DAYS}" + (f" · reaches {LIVE_DAYS} around {eta:%b %d, %Y}" if eta else "")},
        {"key": "beats_spy", "name": "Ahead of the S&P 500 since the start", "done": diff is not None and diff > 0,
         "detail": f"Account {f(you)} · S&P 500 {f(spy)}" if you is not None and spy is not None else "No results yet"},
        {"key": "drawdown", "name": "Worst drop not much deeper than the S&P 500's", "done": perf["max_drawdown"] >= spy_dd - 5,
         "detail": f"Account {perf['max_drawdown']:.1f}% · S&P 500 {spy_dd:.1f}% (passes within 5 points)"},
        {"key": "sharpe", "name": "Return is worth the bumps (Sharpe ratio above 1)", "done": (perf["sharpe"] or 0) > 1,
         "detail": f"Sharpe {perf['sharpe']:.2f}" if perf["sharpe"] is not None else "Needs 20 trading days of results"},
        {"key": "rebalances", "name": f"{REBALANCES} rebalances carried out", "done": out["rebalances"] >= REBALANCES,
         "progress": min(1.0, out["rebalances"] / REBALANCES), "detail": f"{out['rebalances']} of {REBALANCES}"},
        {"key": "selloff", "name": "Came through a real market sell-off", "done": spy_dd <= SELLOFF and perf["max_drawdown"] >= spy_dd - 5,
         "detail": f"The S&P 500's worst drop so far is {spy_dd:.1f}%. A drop of {abs(SELLOFF):.0f}% or more is the real test."},
        {"key": "uptime", "name": "StockBot ran every trading day (last 20)", "done": bool(begun) and not recent_missed,
         "detail": f"Ran on {covered} of {td} trading days" + (f" · missed {', '.join(d.strftime('%b %d') for d in recent_missed[:4])}" if recent_missed else "")},
    ]
    out["milestones"] = ms
    out["milestones_done"] = sum(1 for m in ms if m["done"])
    out["headline"] = (f"Day {out['days']} · {td} of {LIVE_DAYS} trading days · "
                       f"{out['milestones_done']} of {len(ms)} milestones met")
    out["note"] = ("Every milestone is met. That is a solid paper track record, not a guarantee with real money."
                   if out["milestones_done"] == len(ms) else
                   "Checkpoints for a paper track record worth trusting. Meeting them is not a guarantee with real money.")
    return out
