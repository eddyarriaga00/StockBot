# StockBot

A trading app with an auto-trader built in: live market data, real order types, a simulator with fake money,
and a connection to Alpaca for paper trading or real money.

## Start it
- **Double-click `Start StockBot.command`**. The app opens at http://127.0.0.1:8777. Keep the window open while it trades.
- Or double-click **`Run StockBot in Background.command`** once, and it runs in the background whenever you're logged in.
  (`Stop Background StockBot.command` undoes it.) Your Mac must be awake for the bot to trade.

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

## Screens
- **Trade**: search, watchlist, live charts (1D to MAX, line or candles), key stats, news, and an order ticket
  (market / limit / stop / stop-limit / trailing stop, shares or dollars, day or GTC, take-profit / stop-loss) with a review step.
- **Portfolio**: account value chart, positions, open orders (cancel), order history.
- **Bot**: on/off switch, full-auto or approve-each mode, kill switch, backtest vs SPY, today's scan with reasons, risk limits, activity log.
- **Settings**: choose Simulator, Alpaca Paper, or Alpaca Live; paste Alpaca keys.

## How the bot decides: a strategy tournament
Six strategies each trade their own fake $100,000 on live prices, side by side:
Momentum Max, Momentum Weekly, Nasdaq 2x Trend, Trend Momentum, Concentrated Momentum, Dual Momentum, Sector Rotation, Dip Buyer, and Hold SPY (the baseline to beat). Momentum Max and Momentum Weekly rank stocks on blended 3/6/12-month risk-adjusted momentum, check every week, and keep current winners unless something is clearly stronger.
The same code runs the 10-year backtest and the live trading, so there's no gap between what was tested and what trades.

Your account follows the **champion** (or a strategy you pick):
- Until there are 60 trading days of live paper results, the champion is the best held-out backtest
  (the last 40% of the 10 years, which the strategies were never tuned on), ranked by Sharpe ratio.
- After that, the champion is the best live paper performer whose worst drop isn't much worse than SPY's.
- The champion is reviewed at most weekly. A report card is written to `data/reports/` every Friday after the close.

Risk limits on your account: max % per stock, cash reserve, a daily loss halt, and a kill switch. Automatic trailing stops are off by default: in 10-year backtests they sold winners on normal dips and lowered every strategy's return (Momentum Weekly 22% → 17%/yr). Set "Trailing stop" above 0 in Settings to turn them back on.

No strategy is guaranteed to make money. Backtests overstate results (they test today's winners), so
let the paper tournament run for a few months before trusting any of it with real money.

## Simulator rules
Buys fill at the ask and sells at the bid. Orders placed while the market is closed queue until the open. Day orders expire at the close.
It's a cash account: no margin and no shorting. Prices come from Yahoo Finance; bid/ask are estimated.
