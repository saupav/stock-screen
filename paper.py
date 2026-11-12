"""Hypothetical (paper) portfolio: pretend money put into each weekday's best five.

Bookkeeping only. Nothing is sent to a broker and no order is placed or simulated anywhere;
this file just records what the money would be worth if it had followed the cards.

Rules (ASSUMPTION: chosen defaults, not from the written screen rules):
- Each weekday, STAKE dollars go into each of that day's "best_today" picks, in the structure the
  card shows, at the mid of each option leg's bid and ask (shares at the run's price).
- STAKE is the most the position can lose: shares buy STAKE of stock; a debit structure buys
  STAKE / (debit x 100) contracts; a cash-secured put sells STAKE / ((strike - credit) x 100).
  Fractional quantities are fine; this is bookkeeping.
- A position closes when the stock gets back to the prior 52-week high it was measured from,
  when its options expire (at intrinsic value), or, for shares, after the holding time the card
  showed. Otherwise it's valued every run from fresh quotes: the mid when there's a live bid and
  ask, else the last trade, else a Black-Scholes estimate (each flagged by its source).
- Positions are only ever added and closed. A past day's record is never rewritten; a second run
  on the same day updates only that day's line.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

STAKE = 1000.0
MULT = {"shares": 1, "call": 100, "spread": 100, "csp": 100, "leaps": 100}
KIND = {"Shares (no options)": "shares", "Long call": "call", "Bull call spread": "spread",
        "Cash-secured put": "csp", "LEAPS call (deep in the money)": "leaps"}
DAYS_PER_MONTH = 30.44


def empty():
    return {"stake": STAKE, "started": None, "positions": [], "history": [], "notes": []}


def load(path: Path):
    if path.exists():
        try:
            s = json.loads(path.read_text())
            if isinstance(s, dict) and "positions" in s:
                return s
        except ValueError:
            pass
    return empty()


def mid(leg):
    b, a = leg.get("bid") or 0, leg.get("ask") or 0
    return (b + a) / 2 if b > 0 and a > 0 else None


def sign(side):
    return 1 if side == "buy" else -1


def open_day(state, best, options, spots, today: dt.date):
    """Add today's positions unless today already has some. Returns how many were added."""
    d = today.isoformat()
    if today.weekday() >= 5 or any(p["opened"] == d for p in state["positions"]):
        return 0
    added = 0
    for c in best:
        t, name = c["ticker"], c.get("structure")
        kind = KIND.get(name)
        o = options.get(t) or {}
        st = next((s for s in o.get("structures", []) if s["structure"] == name), None)
        spot = spots.get(t)
        if not kind or not st or not spot:
            state["notes"].append(f"{d}: skipped {t}, no quote for its structure.")
            continue
        legs = [{"side": l["side"], "type": l["type"], "strike": l["strike"], "mid": mid(l)} for l in st["legs"]]
        if kind == "shares":
            unit, max_loss_unit = spot, spot
        else:
            if any(l["mid"] is None for l in legs):
                state["notes"].append(f"{d}: skipped {t}, an option leg had no live bid and ask.")
                continue
            unit = sum(sign(l["side"]) * l["mid"] for l in legs)
            max_loss_unit = (legs[0]["strike"] + unit) if kind == "csp" else unit  # csp: unit is -credit
        if max_loss_unit <= 0:
            state["notes"].append(f"{d}: skipped {t}, its quotes gave no money at risk.")
            continue
        qty = STAKE / (max_loss_unit * MULT[kind])
        months = st.get("months") or 12
        state["positions"].append({
            "id": f"{d}-{t}", "opened": d, "ticker": t, "name": c.get("name"), "verdict": c.get("verdict"),
            "structure": name, "kind": kind, "legs": [{k: l[k] for k in ("side", "type", "strike")} for l in legs],
            "expiry": st.get("expiry"), "target": o.get("up_price"), "spot_in": round(spot, 4),
            "stop_date": None if st.get("expiry") else (today + dt.timedelta(days=round(months * DAYS_PER_MONTH))).isoformat(),
            "qty": round(qty, 6), "unit_in": round(unit, 4), "invested": STAKE,
            "unit": round(unit, 4), "value": STAKE, "mark_date": d, "mark_source": "entry",
            "status": "open", "closed": None, "exit_reason": None,
        })
        added += 1
    if added and not state["started"]:
        state["started"] = d
    return added


def intrinsic(leg, spot):
    k = leg["strike"]
    v = max(spot - k, 0) if leg["type"] == "call" else max(k - spot, 0)
    return sign(leg["side"]) * v


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, vol, t, call, r=0.0):
    vol, t = max(vol, 0.10), max(t, 1 / 365)
    sq = vol * math.sqrt(t)
    d1 = (math.log(S / K) + (r + 0.5 * vol * vol) * t) / sq
    d2 = d1 - sq
    if call:
        return S * _ncdf(d1) - K * math.exp(-r * t) * _ncdf(d2)
    return K * math.exp(-r * t) * _ncdf(-d2) - S * _ncdf(-d1)


def leg_now(leg, chain, spot, vol, years, r):
    """(signed per-share value, source) for one option leg."""
    row = (chain or {}).get((leg["type"], leg["strike"]))
    if row:
        b, a, last = row.get("bid") or 0, row.get("ask") or 0, row.get("lastPrice") or 0
        if b > 0 and a > 0:
            return sign(leg["side"]) * (b + a) / 2, "quotes"
        if last > 0:
            return sign(leg["side"]) * last, "last trade"
    v = bs(spot, leg["strike"], vol, years, leg["type"] == "call", r)
    return sign(leg["side"]) * v, "model"


def mark(state, spots, chains, vols, today: dt.date, r=0.04):
    """Value every open position. spots: {ticker: price}; chains: {(ticker, expiry): {(type, strike): row}};
    vols: {ticker: annual vol}. Returns how many closed."""
    d = today.isoformat()
    closed = 0
    for p in state["positions"]:
        if p["status"] != "open" or p["opened"] == d:
            continue
        spot = spots.get(p["ticker"])
        if not spot:
            p["mark_source"] = "no price today (last value kept)"
            continue
        reason = None
        if p["kind"] == "shares":
            unit, src = spot, "price"
            if p["stop_date"] and d >= p["stop_date"]:
                reason = "time limit reached"
        else:
            exp = dt.date.fromisoformat(p["expiry"])
            if today >= exp:
                unit, src, reason = sum(intrinsic(l, spot) for l in p["legs"]), "intrinsic at expiry", "options expired"
            else:
                years = (exp - today).days / 365
                parts = [leg_now(l, chains.get((p["ticker"], p["expiry"])), spot, vols.get(p["ticker"], 0.35), years, r)
                         for l in p["legs"]]
                unit = sum(v for v, _ in parts)
                srcs = {s for _, s in parts}
                src = "model" if "model" in srcs else "last trade" if "last trade" in srcs else "quotes"
        if reason is None and p.get("target") and spot >= p["target"]:
            reason = "back at the prior high"
        p["unit"] = round(unit, 4)
        p["value"] = round(p["invested"] + p["qty"] * MULT[p["kind"]] * (unit - p["unit_in"]), 2)
        p["mark_date"], p["mark_source"], p["spot"] = d, src, round(spot, 4)
        if reason:
            p["status"], p["closed"], p["exit_reason"] = "closed", d, reason
            closed += 1
    return closed


def totals(state):
    ps = state["positions"]
    inv = sum(p["invested"] for p in ps)
    val = sum(p["value"] for p in ps)
    return {"invested": round(inv, 2), "value": round(val, 2), "pl": round(val - inv, 2),
            "pl_pct": round((val / inv - 1) * 100, 2) if inv else None,
            "open": sum(p["status"] == "open" for p in ps), "closed": sum(p["status"] == "closed" for p in ps)}


def record(state, today: dt.date):
    """Today's line in the history: replaced if today already has one, never earlier lines."""
    d = today.isoformat()
    line = {"date": d, **totals(state)}
    if state["history"] and state["history"][-1]["date"] == d:
        state["history"][-1] = line
    elif not state["history"] or state["history"][-1]["date"] < d:
        state["history"].append(line)
    state["notes"] = state["notes"][-50:]
    return line


def needed_chains(state, today: dt.date):
    """(ticker, expiry) pairs that need fresh option quotes for marking."""
    d = today.isoformat()
    return sorted({(p["ticker"], p["expiry"]) for p in state["positions"]
                   if p["status"] == "open" and p["kind"] != "shares" and p["opened"] != d
                   and p["expiry"] and p["expiry"] > d})


def chain_rows(ch):
    """yfinance option_chain result -> {(type, strike): {bid, ask, lastPrice}}."""
    out = {}
    for typ, df in (("call", ch.calls), ("put", ch.puts)):
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            def f(k):
                try:
                    v = float(r.get(k))
                    return v if math.isfinite(v) else 0.0
                except (TypeError, ValueError):
                    return 0.0
            out[(typ, f("strike"))] = {"bid": f("bid"), "ask": f("ask"), "lastPrice": f("lastPrice")}
    return out
