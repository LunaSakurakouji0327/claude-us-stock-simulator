"""Market data from Yahoo Finance via yfinance (an unofficial API that occasionally fails)."""

import math
import re
import time as _time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_QUOTE_CACHE = {}
QUOTE_TTL = 90
TRADABLE_RE = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}$")


def now_et():
    return datetime.now(ET)


def normalize_symbol(raw):
    if not isinstance(raw, str):
        return ""
    s = raw.strip().upper()
    if not s.startswith("^"):
        s = s.replace(".", "-")     # BRK.B -> BRK-B
    return s


def is_tradable_symbol(sym):
    """Only plain US stocks/ETFs. Indices (^), futures/FX (=) and crypto (-USD) are not tradable."""
    return bool(TRADABLE_RE.match(sym)) and not sym.endswith("-USD")


def get_quotes(tickers, fresh=False):
    import yfinance as yf
    today = now_et().date()
    out = {}
    for raw in tickers:
        sym = normalize_symbol(raw)
        if not sym or sym in out:
            continue
        cached = _QUOTE_CACHE.get(sym)
        if cached and not fresh and _time.time() - cached[0] < QUOTE_TTL:
            out[sym] = cached[1]
            continue
        try:
            tk = yf.Ticker(sym)
            daily = tk.history(period="10d", interval="1d", auto_adjust=False)
            intraday = tk.history(period="1d", interval="1m", auto_adjust=False)
            daily = daily[daily["Close"].notna()] if not daily.empty else daily
            if daily.empty and intraday.empty:
                out[sym] = {"error": "no data (ticker may not exist or is delisted)"}
                continue
            if not intraday.empty and intraday["Close"].notna().any():
                closes = intraday["Close"].dropna()
                price, ts = float(closes.iloc[-1]), closes.index[-1]
            else:
                price, ts = float(daily["Close"].iloc[-1]), daily.index[-1]
            prev_close = None
            if not daily.empty:
                last_day = daily.index[-1].date()
                if last_day == today and len(daily) >= 2:
                    prev_close = float(daily["Close"].iloc[-2])
                elif last_day != today:
                    prev_close = float(daily["Close"].iloc[-1])
            q = {
                "price": round(price, 4),
                "prev_close": round(prev_close, 4) if prev_close else None,
                "day_change_pct": round((price / prev_close - 1) * 100, 2) if prev_close else None,
                "as_of": ts.tz_convert(ET).strftime("%Y-%m-%d %H:%M ET")
                if hasattr(ts, "tz_convert") and ts.tzinfo else str(ts),
            }
            _QUOTE_CACHE[sym] = (_time.time(), q)
            out[sym] = q
        except Exception as e:
            out[sym] = {"error": f"{type(e).__name__}: {e}"}
    return out


def get_history(ticker, period="3mo", interval="1d"):
    import yfinance as yf
    sym = normalize_symbol(ticker)
    if period not in {"5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"}:
        period = "3mo"
    if interval not in {"1h", "1d", "1wk"}:
        interval = "1d"
    if interval == "1h" and period in {"2y", "5y"}:
        period = "1y"
    try:
        df = yf.Ticker(sym).history(period=period, interval=interval)
        df = df[df["Close"].notna()] if not df.empty else df
        if df.empty:
            return {"ticker": sym, "error": "no data"}
        c = df["Close"]
        stats = {
            "first_close": round(float(c.iloc[0]), 2),
            "last_close": round(float(c.iloc[-1]), 2),
            "period_return_pct": round((float(c.iloc[-1]) / float(c.iloc[0]) - 1) * 100, 2),
            "high": round(float(df["High"].max()), 2),
            "low": round(float(df["Low"].min()), 2),
        }
        for n in (20, 50, 200):
            if len(c) >= n:
                stats[f"sma{n}"] = round(float(c.tail(n).mean()), 2)
        if interval == "1d" and len(c) > 10:
            stats["annualized_vol_pct"] = round(float(c.pct_change().std()) * math.sqrt(252) * 100, 1)
        fmt = "%Y-%m-%d %H:%M" if interval == "1h" else "%Y-%m-%d"
        rows = [[idx.strftime(fmt), round(float(r["Close"]), 2), int(r["Volume"]) if r["Volume"] == r["Volume"] else 0]
                for idx, r in df.tail(60).iterrows()]
        return {"ticker": sym, "period": period, "interval": interval, "stats": stats,
                "rows_[time,close,volume]_last60": rows}
    except Exception as e:
        return {"ticker": sym, "error": f"{type(e).__name__}: {e}"}


def market_is_open(now=None):
    now = now or now_et()
    if now.weekday() >= 5 or not (dtime(9, 30) <= now.time() < dtime(16, 0)):
        return False
    # Holiday check: the latest SPY 1-minute bar must be from today and recent.
    try:
        import yfinance as yf
        df = yf.Ticker("SPY").history(period="1d", interval="1m")
        if df.empty:
            return False
        last = df.index[-1].tz_convert(ET)
        return last.date() == now.date() and (now - last) < timedelta(minutes=30)
    except Exception:
        return True
