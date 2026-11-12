"""Historical drop study for the stocks the screen flags.

For each stock, take today's drop from its 52-week high (D, at least 10%) and
look back through the stock's own daily history for earlier times it fell at
least that far below its trailing 52-week high. For each such episode:
  - trigger: the first day the drop reached D
  - depth: the deepest fall below that prior high before it recovered
  - half recovery: trading days from trigger until the price was back within D/2
    of the prior high (it regained half of the trigger drop)
  - full recovery: trading days from trigger until the price was back at the
    prior high; the episode ends there and the next one can start after it
  - forward return: price change 6 and 12 months after the trigger
The episode the stock is in today (the last one, not yet back at its prior
high) is excluded, and its start date is reported as "in this drop since". Mechanical history, not a forecast.
"""
from __future__ import annotations

import math
from statistics import median

WINDOW = 252          # trading days in the trailing 52-week high
MIN_HISTORY = 200     # days of history before a 52-week high counts
MIN_DROP = 0.10       # studies drops of at least 10%, even if today's is smaller
TD_PER_MONTH = 21.0


def rolling_high(prices, window=WINDOW, min_periods=MIN_HISTORY):
    """Trailing max of the last `window` prices including today, None until min_periods."""
    from collections import deque
    out, dq = [], deque()  # monotonic deque of indexes
    for i, p in enumerate(prices):
        while dq and prices[dq[-1]] <= p:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - window:
            dq.popleft()
        out.append(prices[dq[0]] if i + 1 >= min_periods else None)
    return out


def episodes(prices, drop, dates=None):
    """Find past drops of at least `drop` (fraction) from the trailing 52-week high."""
    hi = rolling_high(prices)
    n, eps, i = len(prices), [], 0
    while i < n:
        h = hi[i]
        if h is None or prices[i] > h * (1 - drop):
            i += 1
            continue
        ref, start = h, i
        trough, trough_i = prices[i], i
        half_i = full_i = None
        j = i
        while j < n:
            p = prices[j]
            if p < trough:
                trough, trough_i = p, j
            if half_i is None and j > start and p >= ref * (1 - drop / 2):
                half_i = j
            if j > start and p >= ref:
                full_i = j
                break
            j += 1
        fwd = {}
        for k, lab in ((126, "6m"), (252, "12m")):
            fwd[lab] = (prices[start + k] / prices[start] - 1) if start + k < n else None
        eps.append({
            "start": dates[start] if dates else start,
            "start_i": start,
            "depth": 1 - trough / ref,
            "days_to_trough": trough_i - start,
            "half_days": None if half_i is None else half_i - start,
            "full_days": None if full_i is None else full_i - start,
            "fwd_6m": fwd["6m"],
            "fwd_12m": fwd["12m"],
            "open": full_i is None,
            "end_i": full_i if full_i is not None else n - 1,
        })
        i = (full_i + 1) if full_i is not None else n
    return eps


def _med(xs):
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else None


def study(prices, dates, today_drop):
    """Summary for one stock. prices: adjusted closes oldest -> newest."""
    clean = [(d, p) for d, p in zip(dates, prices) if p is not None and math.isfinite(p) and p > 0]
    if len(clean) < MIN_HISTORY + 60:
        return None
    dates, prices = [d for d, _ in clean], [p for _, p in clean]
    drop = max(MIN_DROP, today_drop or 0)
    eps = episodes(prices, drop, dates)
    # The episode still open at the end of the data is today's own drop: leave it out.
    # (Only the last episode can be open, and an open one means the stock is still below that high.)
    current = eps[-1] if eps and eps[-1]["open"] else None
    past = [e for e in eps if e is not current]
    years = (len(prices) - MIN_HISTORY) / 252.0
    closed = [e for e in past if not e["open"]]
    # Episodes old enough to judge a 12-month recovery (trigger at least a year before today).
    judged = [e for e in past if e["start_i"] <= len(prices) - 1 - 252]
    within_12m = [e for e in judged if e["full_days"] is not None and e["full_days"] <= 252]
    m = lambda d: None if d is None else round(d / TD_PER_MONTH, 1)
    pct = lambda x: None if x is None else round(100 * x, 1)
    worst = max(past, key=lambda e: (e["full_days"] is None, e["full_days"] or 0, e["depth"]), default=None)
    return {
        "drop_studied": round(100 * drop, 1),
        "history_years": round(years, 1),
        "first_date": str(dates[0])[:10],
        "episodes": len(past),
        "per_decade": round(len(past) / years * 10, 1) if years > 0 else None,
        "median_half_months": m(_med([e["half_days"] for e in past])),
        "median_full_months": m(_med([e["full_days"] for e in closed])),
        "longest_full_months": m(max((e["full_days"] for e in closed), default=None)),
        "pct_full_within_12m": None if not judged else round(100 * len(within_12m) / len(judged)),
        "median_depth": pct(_med([e["depth"] for e in past])),
        "max_depth": pct(max((e["depth"] for e in past), default=None)),
        "median_fwd_12m": pct(_med([e["fwd_12m"] for e in past])),
        "pct_fwd_12m_positive": None if not [e for e in past if e["fwd_12m"] is not None] else round(
            100 * sum(1 for e in past if e["fwd_12m"] is not None and e["fwd_12m"] > 0)
            / sum(1 for e in past if e["fwd_12m"] is not None)),
        "worst": None if worst is None else {
            "start": str(worst["start"])[:10],
            "depth": pct(worst["depth"]),
            "full_months": m(worst["full_days"]),
            "recovered": not worst["open"],
        },
        "full_months_all": [m(e["full_days"]) for e in closed],
        "current_since": None if current is None else str(current["start"])[:10],
        "list": [{"start": str(e["start"])[:10], "depth": pct(e["depth"]), "half_months": m(e["half_days"]),
                  "full_months": m(e["full_days"]), "fwd_12m": pct(e["fwd_12m"])} for e in past][-25:],
    }


def run(tickers, today_drops, period="max"):
    """Download daily history for `tickers` and study each. Needs network (Yahoo)."""
    import yfinance as yf
    out = {}
    if not tickers:
        return out
    for chunk_start in range(0, len(tickers), 25):
        chunk = tickers[chunk_start:chunk_start + 25]
        try:
            df = yf.download(chunk, period=period, interval="1d", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"backtest download failed for {chunk}: {e}")
            continue
        for t in chunk:
            try:
                close = df[t]["Close"] if len(chunk) > 1 or t in df.columns.get_level_values(0) else df["Close"]
                close = close.dropna()
                res = study([float(x) for x in close.values], [d.date().isoformat() for d in close.index],
                            (today_drops.get(t) or 0) / 100)
                if res:
                    out[t] = res
            except Exception as e:
                print(f"backtest {t}: {e}")
    return out
