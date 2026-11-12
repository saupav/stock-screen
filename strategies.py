"""Side-by-side backtests of well-documented strategies on the screen's universe.

Price-only, because free data has no point-in-time fundamentals or option
history. Daily adjusted closes (dividends included) for today's universe.

Stock-selection strategies (each trade costs 0.2% round trip):
  drop52      Buy when a stock first falls 25% below its 52-week high; sell back at that
              high or after 12 months. The price-only core of this screen.
  momentum    12-1 momentum (Jegadeesh & Titman 1993): each month buy the top 10% by
              return over the past 12 months excluding the last month; hold one month.
  high52      52-week-high proximity (George & Hwang 2004): each month buy the 10% closest
              to their 52-week high; hold one month.
  rsi2        Short-term mean reversion (Connors RSI-2): buy when 2-day RSI < 10 while above
              the 200-day average; sell when the close tops the 5-day average or after 10 days.
  hold        Equal-weight buy and hold of the whole universe, the benchmark.

Option-writing strategies, modelled after the Cboe BXM, PUT and CLL indexes. There is
no free option-price history, so premiums are Black-Scholes prices using the stock's
trailing 21-day realized volatility x 1.1 as implied volatility, less 5% for spreads.
Treat these as approximations.
  buywrite    Own the stock, sell a one-month call 2% above the price, monthly.
  putwrite    Sell a one-month at-the-money put, fully cash-secured (cash earns nothing here).
  collar      Own the stock, buy a one-month put 5% below, sell a call 5% above.

Not studied: post-earnings drift needs years of earnings surprises with dates, which
Yahoo doesn't provide for free. Every figure carries survivorship bias, because only
today's index members are tested.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

COST = 0.002          # round-trip cost per stock trade
OPT_HAIRCUT = 0.05    # share of option premium lost to bid/ask
IV_MULT = 1.1
MONTH = 21

DESCRIPTIONS = {
    "drop52": ("Drop 25% from 52-week high", "Buy the first day 25% below the 52-week high; sell at that high or after 12 months."),
    "momentum": ("12-1 momentum", "Monthly: top 10% by 12-month return skipping the last month; hold 1 month."),
    "high52": ("Near 52-week high", "Monthly: the 10% closest to their 52-week high; hold 1 month."),
    "rsi2": ("Short-term reversal (RSI-2)", "Buy when 2-day RSI < 10 above the 200-day average; sell above the 5-day average or after 10 days."),
    "hold": ("Buy and hold (benchmark)", "Equal weight in every stock, never sold."),
    "buywrite": ("Covered call (buy-write)", "Own the stock and sell a 1-month call 2% above the price, every month. Option prices modelled."),
    "putwrite": ("Cash-secured put-write", "Sell a 1-month at-the-money put each month, fully cash-secured. Option prices modelled."),
    "collar": ("Collar", "Own the stock, buy a 1-month put 5% below and sell a call 5% above, every month. Option prices modelled."),
}

# ---------------------------------------------------------------- metrics

def series_stats(r, per_year):
    r = np.asarray([x for x in r if x is not None and math.isfinite(x)])
    if len(r) < 2:
        return {}
    eq = np.cumprod(1 + r)
    years = len(r) / per_year
    cagr = eq[-1] ** (1 / years) - 1 if eq[-1] > 0 else -1.0
    dd = 1 - eq / np.maximum.accumulate(eq)
    sd = r.std()
    return {"cagr": round(100 * cagr, 1), "max_drawdown": round(100 * dd.max(), 1),
            "sharpe": round(r.mean() / sd * math.sqrt(per_year), 2) if sd > 0 else None,
            "years": round(years, 1)}


def trade_stats(rets, days):
    rets = np.asarray(rets, dtype=float)
    if len(rets) == 0:
        return {"trades": 0}
    return {"trades": int(len(rets)), "hit_rate": round(100 * float((rets > 0).mean()), 1),
            "median_trade": round(100 * float(np.median(rets)), 2),
            "bad_trade_p10": round(100 * float(np.percentile(rets, 10)), 2),
            "median_days": int(np.median(days)) if len(days) else None}


def portfolio_from_trades(P, trades):
    """Daily portfolio: equal weight across open trades, cash (0%) when none.
    trades: list of (col, entry_i, exit_i). Position held over days entry_i+1..exit_i."""
    r = P.pct_change().fillna(0.0).values
    n, m = r.shape
    W = np.zeros((n + 1, m))
    E = np.zeros(n)
    for c, a, b in trades:
        W[a + 1, c] += 1
        W[b + 1, c] -= 1
        E[a] += 1
    W = np.cumsum(W, axis=0)[:n]
    k = W.sum(1)
    daily = np.where(k > 0, (W * r).sum(1) / np.where(k > 0, k, 1), 0.0)
    kk = np.where(k > 0, k, 1)
    daily -= COST * np.roll(E, 1) / kk  # cost charged as positions open
    return daily, float((k > 0).mean())


def finish(key, trades_ret, trades_days, port, per_year, invested):
    label, rule = DESCRIPTIONS[key]
    out = {"key": key, "name": label, "rule": rule}
    out.update(trade_stats(trades_ret, trades_days))
    out.update(series_stats(port, per_year))
    out["time_invested_pct"] = None if invested is None else round(100 * invested, 0)
    return out

# ---------------------------------------------------------------- stock strategies

def month_ends(index):
    s = pd.Series(range(len(index)), index=index)
    return s.groupby([index.year, index.month]).max().values


def run_drop52(P, drop=0.25, max_hold=252):
    V = P.values
    hi = P.rolling(252, min_periods=200).max().values
    trades, rets, days = [], [], []
    for c in range(V.shape[1]):
        v, h = V[:, c], hi[:, c]
        i, n = 0, len(v)
        while i < n:
            if not (np.isfinite(v[i]) and np.isfinite(h[i])) or v[i] > h[i] * (1 - drop):
                i += 1
                continue
            ref, j = h[i], i + 1
            while j < n and j - i < max_hold and not (np.isfinite(v[j]) and v[j] >= ref):
                j += 1
            j = min(j, n - 1)
            if j > i and np.isfinite(v[j]):
                trades.append((c, i, j)); rets.append(v[j] / v[i] - 1 - COST); days.append(j - i)
            # wait for a full recovery before a new episode can start
            while j < n and np.isfinite(v[j]) and v[j] < ref:
                j += 1
            i = j + 1
    port, inv = portfolio_from_trades(P, trades)
    return finish("drop52", rets, days, port, 252, inv)


def run_monthly_rank(P, key):
    V = P.values
    ends = month_ends(P.index)
    hi = P.rolling(252, min_periods=200).max().values
    trades, rets, days = [], [], []
    for a, b in zip(ends[:-1], ends[1:]):
        if a < 252:
            continue
        if key == "momentum":
            sig = V[a - MONTH] / V[a - 252] - 1
        else:
            sig = V[a] / hi[a]
        ok = np.isfinite(sig) & np.isfinite(V[a]) & np.isfinite(V[b])
        if ok.sum() < 20:
            continue
        cut = np.nanpercentile(sig[ok], 90)
        for c in np.where(ok & (sig >= cut))[0]:
            trades.append((c, a, b)); rets.append(V[b, c] / V[a, c] - 1 - COST); days.append(b - a)
    port, inv = portfolio_from_trades(P, trades)
    return finish(key, rets, days, port, 252, inv)


def rsi2(P):
    d = P.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def run_rsi2(P, max_hold=10):
    V = P.values
    R = rsi2(P).values
    ma200 = P.rolling(200).mean().values
    ma5 = P.rolling(5).mean().values
    trades, rets, days = [], [], []
    for c in range(V.shape[1]):
        i, n = 200, len(V)
        while i < n - 1:
            if R[i, c] < 10 and V[i, c] > ma200[i, c]:
                j = i + 1
                while j < n - 1 and j - i < max_hold and not V[j, c] > ma5[j, c]:
                    j += 1
                if np.isfinite(V[j, c]):
                    trades.append((c, i, j)); rets.append(V[j, c] / V[i, c] - 1 - COST); days.append(j - i)
                i = j + 1
            else:
                i += 1
    port, inv = portfolio_from_trades(P, trades)
    return finish("rsi2", rets, days, port, 252, inv)


def run_hold(P):
    r = P.pct_change().mean(axis=1).fillna(0.0).values
    tot = (P.iloc[-1] / P.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else np.nan) - 1).dropna().values
    first = P.apply(lambda s: s.first_valid_index())
    d = [(P.index[-1] - f).days * 252 / 365 for f in first.dropna()]
    return finish("hold", tot, d, r, 252, 1.0)

# ---------------------------------------------------------------- option-writing (modelled)

def _ncdf(x):
    return 0.5 * (1 + np.vectorize(math.erf)(x / math.sqrt(2)))


def bs(S, K, vol, t, call):
    vol = np.maximum(vol, 0.10)
    sq = vol * math.sqrt(t)
    d1 = (np.log(S / K) + 0.5 * vol * vol * t) / sq
    d2 = d1 - sq
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


def run_options(P, key):
    ends = month_ends(P.index)
    lr = np.log(P).diff()
    rv = (lr.rolling(21).std() * math.sqrt(252)).values * IV_MULT
    V = P.values
    t = MONTH / 252
    month_rets, all_rets = [], []
    for a, b in zip(ends[:-1], ends[1:]):
        if a < 30:
            continue
        S0, S1, vol = V[a], V[b], rv[a]
        ok = np.isfinite(S0) & np.isfinite(S1) & np.isfinite(vol)
        if ok.sum() < 5:
            continue
        S0, S1, vol = S0[ok], S1[ok], vol[ok]
        keep = 1 - OPT_HAIRCUT
        if key == "buywrite":
            K = S0 * 1.02
            C = bs(S0, K, vol, t, True) * keep
            r = (np.minimum(S1, K) + C) / S0 - 1
        elif key == "putwrite":
            K = S0
            Pp = bs(S0, K, vol, t, False) * keep
            r = (Pp - np.maximum(K - S1, 0)) / K
        else:  # collar
            Kp, Kc = S0 * 0.95, S0 * 1.05
            net = (bs(S0, Kc, vol, t, True) * keep) - (bs(S0, Kp, vol, t, False) / keep)
            r = (S1 + np.maximum(Kp - S1, 0) - np.maximum(S1 - Kc, 0) + net) / S0 - 1
        r = r - (COST / 2 if key != "putwrite" else 0)  # stock legs pay half the round trip per month
        month_rets.append(float(np.mean(r)))
        all_rets.extend(r.tolist())
    return finish(key, all_rets, [MONTH] * len(all_rets), month_rets, 12, 1.0)

# ---------------------------------------------------------------- today's signals

def signals_today(P, tickers):
    """Which of the stock strategies would hold each ticker today."""
    V = P.iloc[-260:]
    last = V.iloc[-1]
    mom = V.iloc[-1 - MONTH] / V.iloc[-252] - 1 if len(V) >= 252 else pd.Series(dtype=float)
    hi = V.max()
    prox = last / hi
    R = rsi2(P.iloc[-260:]).iloc[-1]
    ma200 = P.iloc[-200:].mean()
    out = {}
    for t in tickers:
        if t not in P.columns:
            continue
        d = {}
        if t in mom and np.isfinite(mom[t]):
            d["momentum_pct_rank"] = round(100 * float((mom.dropna() < mom[t]).mean()))
        if np.isfinite(prox.get(t, np.nan)):
            d["high52_pct_rank"] = round(100 * float((prox.dropna() < prox[t]).mean()))
        d["rsi2"] = None if not np.isfinite(R.get(t, np.nan)) else round(float(R[t]), 1)
        d["rsi2_buy"] = bool(d["rsi2"] is not None and d["rsi2"] < 10 and last[t] > ma200[t])
        out[t] = d
    return out


def run_all(P):
    """P: DataFrame of daily adjusted closes, dates x tickers."""
    P = P.sort_index().astype(float)
    P = P.loc[:, P.notna().sum() > 300]
    results = []
    for fn in (run_hold, run_drop52, lambda p: run_monthly_rank(p, "momentum"),
               lambda p: run_monthly_rank(p, "high52"), run_rsi2,
               lambda p: run_options(p, "buywrite"), lambda p: run_options(p, "putwrite"),
               lambda p: run_options(p, "collar")):
        try:
            results.append(fn(P))
        except Exception as e:  # keep the rest if one fails
            print(f"strategy failed: {e}")
    return {"from": str(P.index[0].date()), "to": str(P.index[-1].date()), "stocks": int(P.shape[1]),
            "strategies": results}
