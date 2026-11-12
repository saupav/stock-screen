"""Hypothetical (paper) portfolio: pretend money put into each weekday's best five.

Bookkeeping only. Nothing is sent to a broker and no order is placed or simulated anywhere;
this file just records what the money would be worth if it had followed the cards.

Rules (ASSUMPTION: chosen defaults, not from the written screen rules):
- Picks: the scheduled morning run (6:15 AM Chicago, before the open) records that day's
  "best_today" five as pending buys, in the structure each card shows.
- Fills: a second run shortly after the open (9:00 AM Chicago in winter, 10:00 AM in summer)
  buys them: shares at that day's opening price, options at the mid of each leg's live bid and
  ask at that moment. A pick with no live quote stays pending and is tried again on the next fill
  run; after MAX_FILL_TRIES failed tries it's marked not filled and never counts.
- STAKE dollars go into each, and STAKE is the most each can lose: shares buy STAKE of stock; a
  debit structure buys STAKE / (debit x 100) contracts; a cash-secured put sells
  STAKE / ((strike - credit) x 100). Fractional quantities are fine; this is bookkeeping.
- A position closes when the stock gets back to the prior 52-week high it was measured from,
  when its options expire (at intrinsic value), or, for shares, after the holding time the card
  showed. Otherwise each run values it from fresh quotes: the mid when there's a live bid and
  ask, else the last trade, else a Black-Scholes estimate (each flagged by its source).
- Positions are only ever added, filled and closed. A past day's record is never rewritten; a
  later run on the same day updates only that day's line.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

STAKE = 1000.0
MAX_FILL_TRIES = 3
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


def sign(side):
    return 1 if side == "buy" else -1


def _live_mid(row):
    if not row:
        return None
    b, a = row.get("bid") or 0, row.get("ask") or 0
    return (b + a) / 2 if b > 0 and a > 0 else None


def pick_day(state, best, options, today: dt.date):
    """Record today's five as pending buys, once per weekday. Returns how many were added."""
    d = today.isoformat()
    if today.weekday() >= 5 or any(p["opened"] == d for p in state["positions"]):
        return 0
    added = 0
    for c in best:
        t, name = c["ticker"], c.get("structure")
        kind = KIND.get(name)
        o = options.get(t) or {}
        st = next((s for s in o.get("structures", []) if s["structure"] == name), None)
        if not kind or not st:
            state["notes"].append(f"{d}: skipped {t}, its structure wasn't in the options study.")
            continue
        state["positions"].append({
            "id": f"{d}-{t}", "opened": d, "ticker": t, "name": c.get("name"), "verdict": c.get("verdict"),
            "structure": name, "kind": kind, "legs": [{k: l[k] for k in ("side", "type", "strike")} for l in st["legs"]],
            "expiry": st.get("expiry"), "target": o.get("up_price"), "months": st.get("months") or 12,
            "stop_date": None, "qty": 0.0, "unit_in": None, "invested": 0.0, "unit": None, "value": 0.0,
            "spot_in": None, "filled_at": None, "fill_tries": 0, "mark_date": None, "mark_source": "waiting for the open",
            "status": "pending", "closed": None, "exit_reason": None,
        })
        added += 1
    if added and not state["started"]:
        state["started"] = d
    return added


def fill(state, opens, chains, now: dt.datetime):
    """Buy pending picks. opens: {ticker: today's opening price}; chains: {(ticker, expiry): {(type, strike): row}}.
    Returns how many were filled."""
    today = now.date()
    filled = 0
    for p in state["positions"]:
        if p["status"] != "pending":
            continue
        t, spot = p["ticker"], opens.get(p["ticker"])
        why = None
        if not spot:
            why = "no opening price yet"
        elif p["kind"] == "shares":
            unit = max_loss_unit = spot
        else:
            ch = chains.get((t, p["expiry"])) or {}
            mids = [_live_mid(ch.get((l["type"], float(l["strike"])))) for l in p["legs"]]
            if any(m is None for m in mids):
                why = "an option leg had no live bid and ask"
            else:
                unit = sum(sign(l["side"]) * m for l, m in zip(p["legs"], mids))
                max_loss_unit = (p["legs"][0]["strike"] + unit) if p["kind"] == "csp" else unit  # csp: unit is -credit
        if why is None and max_loss_unit <= 0:
            why = "its quotes gave no money at risk"
        if why:
            p["fill_tries"] = p.get("fill_tries", 0) + 1
            if p["fill_tries"] >= MAX_FILL_TRIES:
                p["status"], p["mark_source"] = "not filled", f"not filled: {why}"
            state["notes"].append(f"{today}: {t} not bought yet, {why}.")
            continue
        p["qty"] = round(STAKE / (max_loss_unit * MULT[p["kind"]]), 6)
        p["unit_in"] = p["unit"] = round(unit, 4)
        p["invested"] = p["value"] = STAKE
        p["spot_in"] = round(spot, 4)
        p["filled_at"] = now.isoformat(timespec="minutes")
        p["mark_date"], p["mark_source"], p["status"] = today.isoformat(), "bought at the open", "open"
        if not p["expiry"]:
            p["stop_date"] = (today + dt.timedelta(days=round(p["months"] * DAYS_PER_MONTH))).isoformat()
        filled += 1
    return filled


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
    row = (chain or {}).get((leg["type"], float(leg["strike"])))
    if row:
        b, a, last = row.get("bid") or 0, row.get("ask") or 0, row.get("lastPrice") or 0
        if b > 0 and a > 0:
            return sign(leg["side"]) * (b + a) / 2, "quotes"
        if last > 0:
            return sign(leg["side"]) * last, "last trade"
    v = bs(spot, leg["strike"], vol, years, leg["type"] == "call", r)
    return sign(leg["side"]) * v, "model"


def mark(state, spots, chains, vols, today: dt.date, r=0.04):
    """Value every open position bought before today. spots: {ticker: price};
    chains: {(ticker, expiry): {(type, strike): row}}; vols: {ticker: annual vol}. Returns how many closed."""
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
    ps = [p for p in state["positions"] if p["status"] in ("open", "closed")]
    inv = sum(p["invested"] for p in ps)
    val = sum(p["value"] for p in ps)
    return {"invested": round(inv, 2), "value": round(val, 2), "pl": round(val - inv, 2),
            "pl_pct": round((val / inv - 1) * 100, 2) if inv else None,
            "open": sum(p["status"] == "open" for p in ps), "closed": sum(p["status"] == "closed" for p in ps),
            "pending": sum(p["status"] == "pending" for p in state["positions"])}


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


def needed_chains(state, today: dt.date, status="open"):
    """(ticker, expiry) pairs that need option quotes: open positions to value, or pending ones to buy."""
    d = today.isoformat()
    return sorted({(p["ticker"], p["expiry"]) for p in state["positions"]
                   if p["status"] == status and p["kind"] != "shares" and p["expiry"] and p["expiry"] > d
                   and (status == "pending" or p["opened"] != d)})


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
