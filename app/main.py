"""StockBot web server: serves the trading app and its API on http://127.0.0.1:8777."""
import json
import os
import threading
import time

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import market, strategy
from .alpaca_broker import AlpacaBroker
from .bot import DEFAULT_BOT, Bot
from .sim_broker import OrderError, SimBroker
from .publish import Publisher
from .tournament import Tournament

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATIC = os.path.join(ROOT, "static")
SETTINGS = os.path.join(DATA, "settings.json")
os.makedirs(DATA, exist_ok=True)

DEFAULT_SETTINGS = {
    "broker": "sim",
    "alpaca_paper_key": "", "alpaca_paper_secret": "",
    "alpaca_live_key": "", "alpaca_live_secret": "",
    "watchlist": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"],
    "bot": DEFAULT_BOT,
}
_settings_lock = threading.Lock()


def load_settings():
    if not os.path.exists(SETTINGS):
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    with open(SETTINGS) as f:
        s = json.load(f)
    if "strategy" not in s.get("bot", {}):  # settings from before the strategy tournament
        s.setdefault("bot", {}).update(strategy="auto", max_position_pct=DEFAULT_BOT["max_position_pct"])
    return {**DEFAULT_SETTINGS, **s, "bot": {**DEFAULT_BOT, **s.get("bot", {})}}


def save_settings(s):
    with _settings_lock:
        tmp = SETTINGS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.chmod(tmp, 0o600)  # API keys live here: owner-only
        os.replace(tmp, SETTINGS)


sim = SimBroker(os.path.join(DATA, "sim.db"))
_brokers = {}


def get_broker():
    s = load_settings()
    kind = s["broker"]
    if kind == "sim":
        return sim
    live = kind == "alpaca_live"
    key, secret = (s["alpaca_live_key"], s["alpaca_live_secret"]) if live else (s["alpaca_paper_key"], s["alpaca_paper_secret"])
    cache_key = (kind, key)
    if cache_key not in _brokers:
        _brokers[cache_key] = AlpacaBroker(key, secret, live=live)
    return _brokers[cache_key]


tournament = Tournament(DATA)
bot = Bot(DATA, get_broker, load_settings, get_champion=tournament.champion)
publisher = Publisher(ROOT, get_broker, bot, tournament, load_settings, bot.log)
app = FastAPI(title="StockBot")


@app.middleware("http")
async def _no_cache(request, call_next):
    # Always serve the latest app files after an update (local app, so caching buys nothing).
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _loop():
    """Drives everything: fills simulator orders, runs your bot and every tournament bot."""
    while True:
        try:
            get_broker().tick()
            clock = market.market_clock()
            bot.step(clock)
            tournament.step(clock)
            publisher.step(clock)
        except Exception as e:  # keep running no matter what
            bot.log(f"Loop error: {e}", "error")
            time.sleep(30)
        time.sleep(20)


def _err(e):
    raise HTTPException(status_code=400, detail=str(e))


@app.on_event("startup")
def _startup():
    threading.Thread(target=_loop, daemon=True).start()


# ---------- market data ----------
@app.get("/api/quote/{symbol}")
def api_quote(symbol: str):
    try:
        return market.quote(symbol)
    except market.MarketDataError as e:
        _err(e)


@app.get("/api/quotes")
def api_quotes(symbols: str):
    return market.quotes([s for s in symbols.split(",") if s])


@app.get("/api/history/{symbol}")
def api_history(symbol: str, range: str = "1d"):
    try:
        return market.history(symbol, range)
    except market.MarketDataError as e:
        _err(e)


@app.get("/api/search")
def api_search(q: str):
    try:
        return market.search(q)
    except market.MarketDataError:
        return []


@app.get("/api/news/{symbol}")
def api_news(symbol: str):
    try:
        return market.news(symbol)
    except market.MarketDataError:
        return []


@app.get("/api/clock")
def api_clock():
    return market.market_clock()


# ---------- account / trading ----------
@app.get("/api/account")
def api_account():
    try:
        b = get_broker()
        return {"account": b.account(), "positions": b.positions()}
    except OrderError as e:
        _err(e)


@app.get("/api/equity")
def api_equity():
    try:
        return get_broker().equity_history()
    except OrderError as e:
        _err(e)


@app.get("/api/orders")
def api_orders(status: str = "all"):
    try:
        b = get_broker()
        b.tick()
        return b.orders(status)
    except OrderError as e:
        _err(e)


@app.post("/api/orders")
def api_place(req: dict = Body(...)):
    try:
        return get_broker().place_order(req)
    except OrderError as e:
        _err(e)


@app.delete("/api/orders/{oid}")
def api_cancel(oid: str):
    try:
        return get_broker().cancel_order(oid)
    except OrderError as e:
        _err(e)


@app.delete("/api/orders")
def api_cancel_all():
    get_broker().cancel_all()
    return {"ok": True}


@app.post("/api/positions/{symbol}/close")
def api_close(symbol: str):
    try:
        return get_broker().close_position(symbol)
    except OrderError as e:
        _err(e)


@app.post("/api/sim/reset")
def api_reset(body: dict = Body(default={})):
    try:
        get_broker().reset(float(body.get("starting_cash") or 100_000))
    except OrderError as e:
        _err(e)
    bot.state.update(owned={}, pending=[], last_rebalance=0, last_strategy=None)
    bot.log("Simulator account reset.")
    return {"ok": True}


# ---------- watchlist / settings ----------
@app.get("/api/watchlist")
def api_watchlist():
    return load_settings()["watchlist"]


@app.post("/api/watchlist")
def api_set_watchlist(symbols: list = Body(...)):
    s = load_settings()
    s["watchlist"] = [x.upper() for x in dict.fromkeys(symbols)]
    save_settings(s)
    return s["watchlist"]


def _mask(v):
    return (v[:4] + "…" + v[-4:]) if v and len(v) > 10 else ("set" if v else "")


@app.get("/api/settings")
def api_settings():
    s = load_settings()
    return {"broker": s["broker"], "bot": s["bot"],
            **{k: _mask(s[k]) for k in ("alpaca_paper_key", "alpaca_paper_secret", "alpaca_live_key", "alpaca_live_secret")}}


@app.post("/api/settings")
def api_save_settings(body: dict = Body(...)):
    s = load_settings()
    for k in ("alpaca_paper_key", "alpaca_paper_secret", "alpaca_live_key", "alpaca_live_secret"):
        if body.get(k):
            s[k] = body[k].strip()
    if "bot" in body:
        s["bot"] = {**s["bot"], **{k: v for k, v in body["bot"].items() if k in DEFAULT_BOT}}
    if "broker" in body and body["broker"] != s["broker"]:
        target = body["broker"]
        if target not in ("sim", "alpaca_paper", "alpaca_live"):
            _err("Unknown broker.")
        if target == "alpaca_live" and body.get("confirm_live") != "REAL MONEY":
            _err('Type REAL MONEY to confirm switching to live trading.')
        if target != "sim":
            live = target == "alpaca_live"
            key = s["alpaca_live_key" if live else "alpaca_paper_key"]
            secret = s["alpaca_live_secret" if live else "alpaca_paper_secret"]
            if not key or not secret:
                _err("Paste your Alpaca API key and secret first.")
            try:
                AlpacaBroker(key, secret, live=live).check()
            except OrderError as e:
                _err(f"Keys didn't work: {e}")
            if live:
                s["bot"]["mode"] = "approve"  # real money: every bot order needs your tap until you change it
        s["broker"] = target
        bot.state.update(owned={}, pending=[])
        bot.log(f"Switched broker to {target}.")
    save_settings(s)
    return api_settings()


# ---------- bot ----------
@app.get("/api/bot")
def api_bot():
    return bot.status()


@app.post("/api/bot/enable")
def api_bot_enable(body: dict = Body(...)):
    s = load_settings()
    s["bot"]["enabled"] = bool(body.get("enabled"))
    save_settings(s)
    bot.log("Bot turned ON." if s["bot"]["enabled"] else "Bot turned OFF.")
    return bot.status()


@app.post("/api/bot/kill")
def api_bot_kill():
    s = load_settings()
    s["bot"]["enabled"] = False
    save_settings(s)
    bot.kill()
    return bot.status()


@app.post("/api/bot/scan")
def api_bot_scan():
    return bot.scan()


@app.post("/api/bot/backtest")
def api_bot_backtest():
    return strategy.backtest_all(force=True)


@app.get("/api/bot/backtest")
def api_bot_backtest_get():
    return strategy.backtest_all()


@app.get("/api/strategies")
def api_strategies():
    return {k: {"name": v["name"], "about": v["about"]} for k, v in strategy.STRATEGIES.items()}


# ---------- tournament ----------
@app.get("/api/tournament")
def api_tournament():
    return tournament.standings()


@app.get("/api/tournament/curves")
def api_tournament_curves():
    return tournament.live_curves()


@app.post("/api/tournament/report")
def api_tournament_report():
    path = tournament.write_report()
    with open(path) as f:
        return {"path": os.path.relpath(path, ROOT), "markdown": f.read()}


@app.post("/api/publish")
def api_publish():
    return {"result": publisher.publish()}


@app.post("/api/tournament/reset")
def api_tournament_reset():
    tournament.reset()
    return {"ok": True}


@app.post("/api/bot/rebalance")
def api_bot_rebalance():
    try:
        bot.rebalance(force=True)
    except OrderError as e:
        _err(e)
    return bot.status()


@app.post("/api/bot/approve/{pid}")
def api_bot_approve(pid: str):
    try:
        bot.approve(pid)
    except OrderError as e:
        _err(e)
    return bot.status()


@app.post("/api/bot/reject/{pid}")
def api_bot_reject(pid: str):
    bot.reject(pid)
    return bot.status()


# ---------- app ----------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
