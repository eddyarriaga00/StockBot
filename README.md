# StockBot

A trading app with an auto-trader built in: live market data, real order types, a simulator with fake money,
and a connection to Alpaca for paper trading or real money.

## Start it
- **Double-click `Start StockBot.command`**. The app opens at http://127.0.0.1:8777. Keep the window open while it trades.
- Or double-click **`Run StockBot in Background.command`** once, and it runs in the background whenever you're logged in.
  (`Stop Background StockBot.command` undoes it.)
- The bot can only trade while the Mac is on, logged in and running StockBot. StockBot keeps the Mac from idle-sleeping
  while it runs (macOS `caffeinate`, released when StockBot stops), but closing the lid or logging out still stops it.
  The Bot screen shows a health line, and StockBot warns you (and Discord, if connected) about trading days it missed.

## Beginner lessons
The **Learn** tab includes seven short lessons, retryable quizzes, saved browser progress, a searchable glossary, and a fictional-price practice calculator. Lessons never place orders, change your broker, or enable the bot. Progress is local to this browser and is separate from your trading data.

## The website
**https://eddyarriaga00.github.io/StockBot/** is the same app.
- When the StockBot computer is on, enter its passcode (Settings → Control from the website → Show passcode on the Mac)
  and everything works from any device: trading, the bot, the tournament, settings.
- Otherwise it's a view-only mirror of the latest results, refreshed about every 2 hours during market hours.
- How it connects: the Mac opens an encrypted Cloudflare tunnel (`brew install cloudflared`) and publishes its address.
  Tunnel requests need a login token, the passcode can only be seen or changed on the Mac, wrong guesses lock out
  for 15 minutes, and only the StockBot website may call the API. Turn it off any time in Settings.

## Discord

StockBot can post to a Discord channel: every trade with the bot's reason, alerts (approvals waiting, loss halts, errors),
a daily summary after the close (account value, day P/L vs SPY, holdings), and the weekly tournament report card.
In Discord: Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Paste it in StockBot's
Settings → Discord and click Connect. The link is stored only on this Mac.

## Screens
- **Trade**: search, watchlist, live charts (1D to MAX, line or candles), key stats, news, and an order ticket
  (market / limit / stop / stop-limit / trailing stop, shares or dollars, day or GTC, take-profit / stop-loss) with a review step.
- **Portfolio**: account value chart, positions, open orders (cancel), order history.
- **Bot**: on/off switch, full-auto or approve-each mode, kill switch, what the bot will do next and when, its target portfolio,
  a health line, the strategy tournament, backtest vs SPY, today's scan with reasons, risk limits, activity log.
- **Settings**: choose Simulator, Alpaca Paper, or Alpaca Live; paste Alpaca keys.

## How the bot decides: a strategy tournament
Nine strategies each trade their own fake $100,000 on live prices, side by side:
Momentum Max, Momentum Weekly, Nasdaq 2x Trend, Trend Momentum, Concentrated Momentum, Dual Momentum, Sector Rotation, Dip Buyer, and Hold SPY (the baseline to beat). Momentum Max and Momentum Weekly rank stocks on blended 3/6/12-month risk-adjusted momentum, check every week, and keep current winners unless something is clearly stronger.
The same code runs the 10-year backtest and the live trading, so there's no gap between what was tested and what trades.

Your account follows the **champion** (or a strategy you pick):
- Until there are 60 trading days of live paper results, the champion is the best **honest backtest**, ranked by Sharpe ratio.
  The stock list is today's giants, so a plain backtest "knows" who won. The honest backtest re-runs every strategy
  without the 6 stocks that rose the most (picked by the data, e.g. NVDA, AVGO, AMD, TSLA, LLY, AAPL).
  Momentum Max goes from 26.7%/yr to 15.0%/yr under that check: still ahead of SPY (12.4%/yr) with a much smaller
  worst drop (−19% vs −33%), but the headline number is mostly hindsight. Both numbers are in the tournament table.
- After that, the champion is the best live paper performer whose worst drop isn't much worse than SPY's.
- The champion is reviewed at most weekly. A report card is written to `data/reports/` every Friday after the close.

Risk limits on your account: max % per stock, cash reserve, a daily loss halt, and a kill switch. Automatic trailing stops are off by default: in 10-year backtests they sold winners on normal dips and lowered every strategy's return (Momentum Weekly 22% → 17%/yr). Set "Trailing stop" above 0 in Settings to turn them back on.

Research notes (Nov 2026), so they aren't repeated: a bigger 100-stock list, momentum across ~50 ETFs, gold or bonds
instead of T-bills when risk-off, faster market filters, and mixing in the 2x Nasdaq strategy all did worse once hindsight
was accounted for.

No strategy is guaranteed to make money. Backtests overstate results (they test today's winners), so
let the paper tournament run for a few months before trusting any of it with real money.

## Tests
`.venv/bin/python -m unittest discover tests` runs offline tests of the simulator (fills, queuing, limits, brackets,
trailing stops), the strategies (picks, risk-off, no look-ahead, caps) and the bot (rebalancing, approvals, leaving your
own trades alone) against a fake market.

## Simulator rules
Buys fill at the ask and sells at the bid. Orders placed while the market is closed queue until the open. Day orders expire at the close.
It's a cash account: no margin and no shorting. Prices come from Yahoo Finance; bid/ask are estimated.
