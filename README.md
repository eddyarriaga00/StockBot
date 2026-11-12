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

One webhook is enough, but each kind of message can have its own channel (Settings → Discord → Separate channels):
- **#start-here**: a guide to the server, kept up to date.
- **#dashboard**: one message edited in place every 5 minutes while the market is open (every 30 when closed):
  account value, a 30-day chart, holdings, what the bot does next, the tournament top 5, and whether StockBot is healthy.
- **#trades**: every fill with the bot's reason and how much of the account the position is now.
- **#alerts**: approvals, loss halts, errors, missed trading days, and any holding that moves 4% or more in a day.
- **#daily-summary**: the plan at the open, results with a chart after the close, and new all-time highs.
- **#weekly-report**: the tournament table every Friday and any champion change.

Charts are drawn by `app/chart.py` in pure Python (no extra packages), and nothing is sent anywhere except Discord.

## Screens
- **Trade**: search, watchlist, live charts (1D to MAX, line or candles), key stats, price alerts, news, and an order ticket
  (market / limit / stop / stop-limit / trailing stop, shares or dollars, day or GTC, take-profit / stop-loss) with a review step.
- **Portfolio**: account value chart, performance (returns by period against SPY, worst drop, best and worst day,
  profit on sold shares, win rate, dividends), positions, open orders (cancel), order history, dividends and splits.
- **Bot**: on/off switch, full-auto or approve-each mode, kill switch, what the bot will do next and when, its target portfolio,
  a health line, the strategy tournament, backtest vs SPY, today's scan with reasons, risk limits, activity log.
- **Settings**: choose Simulator, Alpaca Paper, or Alpaca Live; paste Alpaca keys.

## Track record
The point of the simulator is to build a paper record worth trusting before any real money is involved. The **Bot** screen
(and the "Day N" badge in the header, the Discord dashboard, the daily summary and the weekly report) shows how long the
account has been running, how many trading days it has seen, whether StockBot was actually running on each of them,
and seven milestones:
60 trading days of results, ahead of the S&P 500, worst drop not much deeper than the S&P 500's, Sharpe ratio above 1,
12 rebalances, coming through a 10% market sell-off, and no missed trading days in the last 20.
Discord announces the ones that stay met once reached. Meeting all seven is a track record, not a guarantee.
`app/calendar_us.py` knows US market holidays, so they don't count as trading days or as missed days.

## How the bot decides: a strategy tournament
Eleven strategies each trade their own fake $100,000 on live prices, side by side:
Momentum Max, Momentum Weekly, 52-Week High, Core Blend, Nasdaq 2x Trend, Trend Momentum, Concentrated Momentum, Dual Momentum,
Sector Rotation, Dip Buyer, and Hold SPY (the baseline to beat).
- **Momentum Max / Weekly** rank stocks on blended 3/6/12-month risk-adjusted momentum, check every week, and keep current
  winners unless something is clearly stronger.
- **52-Week High** holds the 6 uptrending stocks closest to their 52-week high (George & Hwang, 2004).
- **Core Blend** is equal parts Momentum Max, Trend Momentum and 52-Week High, so one rule's bad luck matters less.

The same code runs the backtest and the live trading, so there's no gap between what was tested and what trades.
Backtests use dividend-adjusted prices (total return). Ignoring dividends used to make SPY look about 2% a year worse than it is.

Your account follows the **champion** (or a strategy you pick):
- Until there are 60 trading days of live paper results, the champion comes from three **honest backtests**. The stock list is
  today's giants, so a plain backtest "knows" who won. Each strategy is re-run (1) without the 6 stocks that rose the most,
  (2) on 20 random halves of the stock list, and (3) over 30 years of history. Its **honest Sharpe** is the weakest of the three.
  Nine years of data can't tell Sharpe ratios within 0.25 of each other apart, so among those tied leaders the champion is the one with the higher return without the winners.
  Momentum Max goes from 25.7%/yr to 16.4%/yr without the winners: still ahead of SPY (14.1%/yr with dividends), but the
  headline number is mostly hindsight. All of these numbers are in the tournament table.
- After that, the champion is the best live paper performer whose worst drop isn't much worse than SPY's.
- The champion is reviewed at most weekly. A report card is written to `data/reports/` every Friday after the close.
- The Bot screen has a **crash test**: every strategy replayed through the dot-com bust, 2008, the Covid crash and 2022.
  The momentum strategies lost 3-13% in 2000-02 and 2008 while SPY lost 45-52%, but all of them fell about 19% in the
  five-week Covid crash, because a 200-day trend filter is too slow for a crash that fast.

Risk limits on your account: max % per stock, cash reserve, a daily loss halt, and a kill switch. Automatic trailing stops are off by default: in 10-year backtests they sold winners on normal dips and lowered every strategy's return (Momentum Weekly 22% → 17%/yr). Set "Trailing stop" above 0 in Settings to turn them back on.

Research notes (Nov 2026), so they aren't repeated. All tested on 11 years and on 30 years of dividend-adjusted prices,
without the biggest winners and on 40 random halves of the stock list:
- Did worse or no better once hindsight was accounted for: a bigger 100-stock list, momentum across ~50 ETFs, gold or bonds
  instead of T-bills when risk-off, faster market filters, and mixing in the 2x Nasdaq strategy.
- Volatility targeting (Barroso & Santa-Clara 2015; Moreira & Muir 2017): cut returns by 2-6 points a year with no gain in
  Sharpe, matching the out-of-sample critique by Cederburg et al. (2020).
- Residual momentum (Blitz, Huij & Martens 2011): lower return and deeper drops than plain momentum on this stock list.
- Breadth-based crash protection (Keller & Keuning): too much time in cash, 8-10%/yr without the winners.
- Momentum plus a low-volatility screen: steady (Sharpe about 1.1) but only matches SPY's return.
- "Frog in the pan" smooth momentum (Da, Gurun & Warachka 2014) and a graded 7-signal trend filter: no better than the simple versions.
- Concentration and timing: 3 to 8 stocks all beat SPY on random halves; weekly checks beat every-2-weeks and monthly.
- Held up and were added: 52-Week High, and the three-signal Core Blend.
- Nasdaq 2x Trend made 22%/yr over the last decade but only 12%/yr since 2006 with a 47% drop: its recent record is mostly the era.

No strategy is guaranteed to make money. Backtests overstate results (they test today's winners), so
let the paper tournament run for a few months before trusting any of it with real money.

## Tests
`.venv/bin/python -m unittest discover tests` runs offline tests of the simulator (fills, queuing, limits, brackets,
trailing stops, dividends, splits), the strategies (picks, risk-off, no look-ahead, caps) and the bot (rebalancing, approvals, leaving your
own trades alone) against a fake market.

## Simulator rules
Buys fill at the ask and sells at the bid. Orders placed while the market is closed queue until the open. Day orders expire at the close.
- **Spread**: estimated from how heavily the stock trades (about a cent for the busiest stocks, up to 1% for thin ones), doubled in
  the first five minutes after the open and four times wider outside regular hours.
- **Pre-market and after hours** (4:00 AM to 8:00 PM ET): the quote shows the extended-hours price, and a limit order can opt in
  to trade then. It fills only at its limit or better, and only against a price from the last 15 minutes.
- **Slippage**: a market, stop or trailing-stop order bigger than 0.05% of the day's volume gets a worse price, growing with
  the square root of its share of volume and capped at 3%.
- **Order checks**: a sell stop must be below the market and a buy stop above it; prices of $1 or more can't use fractions of a cent.
- **Editing**: open orders can have their quantity and prices changed (Portfolio → Open orders → Edit), with the same checks as a new order.
- The order ticket shows live bid, ask and spread, 25/50/75/Max buttons, and cautions before you submit (order too big for the
  stock, limit that fills at once, likely typo, wide spread, too much in one stock). The app shows a notice when any order fills, expires or is rejected.
It's a cash account: no margin and no shorting. Prices come from Yahoo Finance; bid/ask are estimated.
Dividends are added as cash on the ex-dividend date (a real broker pays a few weeks later). Stock splits change the share count
and average cost and cancel that stock's open orders. Both are checked every 30 minutes and before every bot rebalance.

## Price alerts
On the Trade screen, enter a price and click Alert me. When the stock gets there you get a notice in the app, a line in the
bot's activity log, and a Discord message in #alerts. An alert fires once and never places an order.
