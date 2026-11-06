# StockBot

A trading app with an auto-trader built in: live market data, real order types, a simulator with fake money,
and a connection to Alpaca for paper trading or real money.

## Start it
- **Double-click `Start StockBot.command`**. The app opens at http://127.0.0.1:8777. Keep the window open while it trades.
- Or double-click **`Run StockBot in Background.command`** once, and it runs in the background whenever you're logged in.
  (`Stop Background StockBot.command` undoes it.) Your Mac must be awake for the bot to trade.

## Beginner lessons
The **Learn** tab includes seven short lessons, retryable quizzes, saved browser progress, a searchable glossary, and a fictional-price practice calculator. Lessons never place orders, change your broker, or enable the bot. Progress is local to this browser and is separate from your trading data.

## Screens
- **Trade**: search, watchlist, live charts (1D to MAX, line or candles), key stats, news, and an order ticket
  (market / limit / stop / stop-limit / trailing stop, shares or dollars, day or GTC, take-profit / stop-loss) with a review step.
- **Portfolio**: account value chart, positions, open orders (cancel), order history.
- **Bot**: on/off switch, full-auto or approve-each mode, kill switch, backtest vs SPY, today's scan with reasons, risk limits, activity log.
- **Settings**: choose Simulator, Alpaca Paper, or Alpaca Live; paste Alpaca keys.

## How the bot decides: a strategy tournament
Six strategies each trade their own fake $100,000 on live prices, side by side:
Trend Momentum, Concentrated Momentum, Dual Momentum, Sector Rotation, Dip Buyer, and Hold SPY (the baseline to beat).
The same code runs the 10-year backtest and the live trading, so there's no gap between what was tested and what trades.

Your account follows the **champion** (or a strategy you pick):
- Until there are 60 trading days of live paper results, the champion is the best held-out backtest
  (the last 40% of the 10 years, which the strategies were never tuned on), ranked by Sharpe ratio.
- After that, the champion is the best live paper performer whose worst drop isn't much worse than SPY's.
- The champion is reviewed at most weekly. A report card is written to `data/reports/` every Friday after the close.

Risk limits on your account: max % per stock, cash reserve, trailing stops, a daily loss halt, and a kill switch.

No strategy is guaranteed to make money. Backtests overstate results (they test today's winners), so
let the paper tournament run for a few months before trusting any of it with real money.

## Simulator rules
Buys fill at the ask and sells at the bid. Orders placed while the market is closed queue until the open. Day orders expire at the close.
It's a cash account: no margin and no shorting. Prices come from Yahoo Finance; bid/ask are estimated.
