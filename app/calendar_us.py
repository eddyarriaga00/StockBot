"""The US stock market calendar: which days the market is open, and when each regular session starts and ends.

Early closes (1:00 PM on a few days a year) are treated as full days.
"""
import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _nth(year, month, weekday, n):
    """The n-th given weekday of a month (n = -1 for the last)."""
    if n > 0:
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
        return d
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return dt.date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


def _observed(d):
    """A holiday on Saturday is observed on Friday, one on Sunday on Monday."""
    return d - dt.timedelta(days=1) if d.weekday() == 5 else d + dt.timedelta(days=1) if d.weekday() == 6 else d


def holidays(year):
    days = {
        _observed(dt.date(year, 1, 1)), _nth(year, 1, 0, 3), _nth(year, 2, 0, 3), _easter(year) - dt.timedelta(days=2),
        _nth(year, 5, 0, -1), _observed(dt.date(year, 6, 19)), _observed(dt.date(year, 7, 4)), _nth(year, 9, 0, 1),
        _nth(year, 11, 3, 4), _observed(dt.date(year, 12, 25)),
    }
    if dt.date(year + 1, 1, 1).weekday() == 5:  # next New Year's Day falls on Saturday: the market does not close Friday
        days.discard(dt.date(year, 12, 31))
    if dt.date(year, 1, 1).weekday() == 5:
        days.discard(dt.date(year - 1, 12, 31))
    return days


def is_trading_day(d):
    return d.weekday() < 5 and d not in holidays(d.year)


def sessions(t0, t1):
    """Regular sessions that overlap the time span t0..t1, as (date, open, close) with times in epoch seconds."""
    a, b = dt.datetime.fromtimestamp(t0, ET), dt.datetime.fromtimestamp(t1, ET)
    out, d = [], a.date()
    while d <= b.date():
        if is_trading_day(d):
            open_ = dt.datetime.combine(d, dt.time(9, 30), ET).timestamp()
            close = dt.datetime.combine(d, dt.time(16, 0), ET).timestamp()
            if close > t0 and open_ < t1:
                out.append((d, open_, close))
        d += dt.timedelta(days=1)
    return out


def add_trading_days(d, n):
    while n > 0:
        d += dt.timedelta(days=1)
        if is_trading_day(d):
            n -= 1
    return d
