"""Options structures sized to each flagged stock's past recovery times, and a
reward-per-time ranking. A mechanical historical study, not advice: it reads
public option quotes and computes payoffs; it never places or simulates orders.

Horizon. From the stock's past drops of today's size (backtest.py):
  target months T* = 75th percentile of past full-recovery times when there are
  4+ episodes, else 1.5 x the median; at least 1 month. The expiry used is the
  first listed expiry on or after T* (or the longest listed, flagged "short").

Two scenarios at that expiry:
  up   = back at the prior 52-week high
  down = at the median deepest fall of past episodes (prior high x (1 - median depth))
  p    = share of past episodes that got back to the prior high within the expiry's months

Structures compared (fills at the ask when buying, at the bid when selling):
  Shares             buy 100 shares at today's price (baseline, no options)
  Long call          the first strike at or above today's price
  Bull call spread   buy that call, sell the listed strike closest to the prior high
  Cash-secured put   sell the put whose strike is closest to 95% of today's price
  LEAPS call         the longest expiry 9+ months out, strike closest to 80% of price

For each: net cost (debit) or credit, max loss, max gain, breakeven, payoff in
the up and down scenarios, and the widest bid/ask spread of its legs.

Score (per structure, and a stock's score is its best structure's):
  EV        = p x payoff(up) + (1 - p) x payoff(down)
  score     = EV / max loss / years to expiry      (expected return on money at risk, per year)
Best structure = highest score among structures whose legs are all "liquid"
(bid and ask above 0, spread no more than 25% of mid, open interest 100+).
If none is liquid, the shares baseline is used. Opportunities sort by score.
"""
from __future__ import annotations

import datetime as dt
import math

LIQ_MAX_SPREAD = 0.25
LIQ_MIN_OI = 100
CONTRACT = 100


def pctile(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def target_months(bt):
    full = [x for x in (bt.get("full_months_all") or []) if x is not None]
    if len(full) >= 4:
        t = pctile(full, 0.75)
    elif bt.get("median_full_months") is not None:
        t = 1.5 * bt["median_full_months"]
    else:
        t = 12.0  # no past episode of this size: assume a year
    return max(1.0, t)


def p_recover(bt, months):
    """Share of past drops back at the prior high within `months`, smoothed as (k + 1) / (n + 2)
    (Laplace's rule), so one or two lucky past drops don't read as certainty.
    ASSUMPTION: the smoothing is not in the written rules."""
    full = [x for x in (bt.get("full_months_all") or []) if x is not None]
    n = bt.get("episodes") or 0
    if n == 0:
        return None
    k = sum(1 for x in full if x <= months)
    return (k + 1) / (n + 2)


def months_between(today, expiry):
    return (expiry - today).days / 30.44


def leg_quote(row):
    """row: dict with bid, ask, lastPrice, openInterest, strike."""
    bid, ask = row.get("bid") or 0.0, row.get("ask") or 0.0
    last = row.get("lastPrice") or 0.0
    live = bid > 0 and ask > 0
    mid = (bid + ask) / 2 if live else last
    spread = (ask - bid) / mid if live and mid > 0 else None
    oi = row.get("openInterest") or 0
    return {
        "strike": row["strike"], "bid": bid, "ask": ask, "last": last, "oi": int(oi),
        "buy": ask if live else last, "sell": bid if live else last,
        "spread_pct": None if spread is None else round(100 * spread, 1),
        "live": live,
        "liquid": live and spread is not None and spread <= LIQ_MAX_SPREAD and oi >= LIQ_MIN_OI,
    }


def nearest(rows, target, at_or_above=False):
    cands = [r for r in rows if (r["strike"] >= target if at_or_above else True)]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(r["strike"] - target))


def payoff(kind, legs, net, price):
    """Per-share P&L at expiry for a structure. net: debit (>0) or credit (<0)."""
    if kind == "shares":
        return price - net
    if kind in ("call", "leaps"):
        return max(price - legs[0]["strike"], 0) - net
    if kind == "spread":
        k1, k2 = legs[0]["strike"], legs[1]["strike"]
        return min(max(price - k1, 0), k2 - k1) - net
    if kind == "csp":
        return -net - max(legs[0]["strike"] - price, 0)
    raise ValueError(kind)


def build(kind, name, legs, net, spot, up, down, p, months, expiry):
    if kind == "shares":
        max_loss, max_gain, be = spot, None, spot
    elif kind in ("call", "leaps"):
        max_loss, max_gain, be = net, None, legs[0]["strike"] + net
    elif kind == "spread":
        w = legs[1]["strike"] - legs[0]["strike"]
        max_loss, max_gain, be = net, w - net, legs[0]["strike"] + net
    else:  # csp, net is negative (credit)
        credit = -net
        max_loss, max_gain, be = legs[0]["strike"] - credit, credit, legs[0]["strike"] - credit
    if max_loss is None or max_loss <= 0:
        return None
    pu, pdn = payoff(kind, legs, net, up), payoff(kind, legs, net, down)
    years = max(months, 0.25) / 12
    ev = None if p is None else p * pu + (1 - p) * pdn
    score = None if ev is None else ev / max_loss / years
    r2 = lambda x: None if x is None else round(x, 2)
    return {
        "structure": name, "kind": kind, "expiry": expiry, "months": round(months, 1),
        "p": None if p is None else round(p, 3),
        "legs": [{"side": l.get("side"), "type": l.get("type"), "strike": l["strike"], "bid": l["bid"],
                  "ask": l["ask"], "oi": l["oi"], "spread_pct": l["spread_pct"], "live": l["live"]} for l in legs],
        "net_per_share": r2(net), "cost_per_contract": r2(net * CONTRACT),
        "max_loss": r2(max_loss * CONTRACT), "max_gain": None if max_gain is None else r2(max_gain * CONTRACT),
        "breakeven": r2(be), "payoff_up": r2(pu * CONTRACT), "payoff_down": r2(pdn * CONTRACT),
        "ev": None if ev is None else r2(ev * CONTRACT),
        "score": None if score is None else round(score, 3),
        "liquid": all(l["liquid"] for l in legs) if legs else True,
        "widest_spread_pct": max((l["spread_pct"] for l in legs if l["spread_pct"] is not None), default=None),
    }


def study(spot, prior_high, bt, chains, today=None):
    """chains: {expiry 'YYYY-MM-DD': {"calls": [rows], "puts": [rows]}} (rows as dicts)."""
    today = today or dt.date.today()
    if not spot or not prior_high or not bt:
        return None
    want = target_months(bt)
    depth = (bt.get("median_depth") or 0) / 100
    up, down = prior_high, prior_high * (1 - depth) if depth else spot
    down = min(down, spot)
    exps = sorted((dt.date.fromisoformat(e), e) for e in chains)
    exps = [(d, e) for d, e in exps if d > today]
    fit = next(((d, e) for d, e in exps if months_between(today, d) >= want), exps[-1] if exps else None)
    out = {"target_months": round(want, 1), "up_price": round(up, 2), "down_price": round(down, 2),
           "structures": [], "notes": []}
    # Baseline: shares held for the target time.
    p_t = p_recover(bt, want)
    out["structures"].append(build("shares", "Shares (no options)", [], spot, spot, up, down, p_t, want, None))
    if not fit:
        out["notes"].append("No listed options.")
    else:
        d, e = fit
        m = months_between(today, d)
        if m < want:
            out["notes"].append(f"Longest listed expiry is {m:.0f} months, shorter than the {want:.0f}-month target.")
        p = p_recover(bt, m)
        out["expiry"], out["expiry_months"], out["p_recover"] = e, round(m, 1), None if p is None else round(p, 3)
        calls = [leg_quote(r) for r in chains[e].get("calls", [])]
        puts = [leg_quote(r) for r in chains[e].get("puts", [])]
        atm = nearest(calls, spot, at_or_above=True)
        if atm:
            atm = {**atm, "side": "buy", "type": "call"}
            out["structures"].append(build("call", "Long call", [atm], atm["buy"], spot, up, down, p, m, e))
            short = nearest([c for c in calls if c["strike"] > atm["strike"]], prior_high)
            if short:
                short = {**short, "side": "sell", "type": "call"}
                net = atm["buy"] - short["sell"]
                if net > 0:
                    out["structures"].append(build("spread", "Bull call spread", [atm, short], net, spot, up, down, p, m, e))
        put = nearest([q for q in puts if q["strike"] < spot], spot * 0.95)
        if put and put["sell"] > 0:
            put = {**put, "side": "sell", "type": "put"}
            out["structures"].append(build("csp", "Cash-secured put", [put], -put["sell"], spot, up, down, p, m, e))
    leaps = [(d, e) for d, e in exps if months_between(today, d) >= 9]
    if leaps:
        d, e = leaps[-1]
        m = months_between(today, d)
        calls = [leg_quote(r) for r in chains[e].get("calls", [])]
        k = nearest(calls, spot * 0.80)
        if k:
            k = {**k, "side": "buy", "type": "call"}
            out["structures"].append(build("leaps", "LEAPS call (deep in the money)", [k], k["buy"], spot, up, down,
                                           p_recover(bt, m), m, e))
    out["structures"] = [s for s in out["structures"] if s]
    if any(not l["live"] for s in out["structures"] for l in s["legs"]):
        out["notes"].append("Some legs had no live bid/ask, so the last trade price was used; those legs count as illiquid.")
    ranked = sorted([s for s in out["structures"] if s["score"] is not None and s["liquid"]],
                    key=lambda s: -s["score"])
    out["best"] = ranked[0]["structure"] if ranked else None
    out["score"] = ranked[0]["score"] if ranked else None
    if bt.get("episodes", 0) < 3:
        out["notes"].append(f"Only {bt.get('episodes', 0)} past drops this size, so the recovery odds are thin.")
    return out


def _rows(df):
    if df is None or df.empty:
        return []
    keep = ["strike", "bid", "ask", "lastPrice", "openInterest"]
    out = []
    for _, r in df.iterrows():
        d = {}
        for k in keep:
            v = r.get(k)
            try:
                v = float(v)
                v = v if math.isfinite(v) else 0.0
            except (TypeError, ValueError):
                v = 0.0
            d[k] = v
        out.append(d)
    return out


START_PAUSE = 30   # let Yahoo's rate limit cool down after the main fetch
GAP_SECONDS = 1.5  # between tickers


def run(candidates, max_months_needed=None, retry=None):
    """candidates: {ticker: (spot, prior_high, bt)}. Needs network (Yahoo)."""
    import time
    import yfinance as yf
    retry = retry or (lambda fn: fn())
    today = dt.date.today()
    out = {}
    if candidates:
        time.sleep(START_PAUSE)
    for t, (spot, hi, bt) in candidates.items():
        time.sleep(GAP_SECONDS)
        try:
            tk = yf.Ticker(t)
            exps = list(retry(lambda: tk.options) or [])
            if not exps:
                out[t] = study(spot, hi, bt, {}, today)
                continue
            want = target_months(bt)
            dates = sorted(dt.date.fromisoformat(e) for e in exps)
            fit = next((d for d in dates if months_between(today, d) >= want), dates[-1])
            pick = {fit.isoformat()}
            leaps = [d for d in dates if months_between(today, d) >= 9]
            if leaps:
                pick.add(leaps[-1].isoformat())
            chains = {}
            for e in pick:
                ch = retry(lambda: tk.option_chain(e))
                chains[e] = {"calls": _rows(ch.calls), "puts": _rows(ch.puts)}
            out[t] = study(spot, hi, bt, chains, today)
        except Exception as e:
            print(f"options {t}: {e}")
    return out
