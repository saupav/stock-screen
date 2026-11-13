"""Offline checks of the scoring rules on made-up companies (no network).
Run:  python test_scoring.py
"""
import json
import math
import random
import sys

import pandas as pd

from scoring import (below_entry, lin, piotroski, fundamentals, quality_gate,
                     run_screens, value_score, verdict)


def company(seed, **over):
    r = random.Random(seed)
    rev0 = r.uniform(2e9, 40e9)
    g = r.uniform(-0.05, 0.2)
    rev = [rev0 * (1 + g) ** i for i in range(4)]
    om = r.uniform(0.05, 0.35)
    ni = [x * om * 0.75 for x in rev]
    sh = [1e9 * (1 - 0.01 * i) for i in range(4)]
    eps = [n / s for n, s in zip(ni, sh)]
    eq = [x * r.uniform(2, 6) for x in ni]
    ta = [e * r.uniform(1.5, 3) for e in eq]
    price = eps[-1] * r.uniform(8, 40)
    hi = price * r.uniform(1.0, 1.8)
    info = {
        "shortName": f"Co {seed}", "sector": r.choice(["Technology", "Industrials", "Financial Services", "Utilities", "Healthcare"]),
        "currentPrice": price, "marketCap": price * sh[-1], "enterpriseValue": price * sh[-1] * 1.1,
        "freeCashflow": ni[-1] * r.uniform(0.6, 1.3), "returnOnEquity": ni[-1] / eq[-1],
        "operatingMargins": om, "debtToEquity": r.uniform(10, 250), "trailingPegRatio": r.uniform(0.5, 4),
        "forwardPE": price / (eps[-1] * 1.08), "trailingPE": price / eps[-1], "trailingEps": eps[-1],
        "priceToBook": price / (eq[-1] / sh[-1]), "bookValue": eq[-1] / sh[-1], "dividendYield": r.uniform(0, 3),
        "fiftyTwoWeekHigh": hi, "fiftyTwoWeekLow": price * r.uniform(0.7, 1.0),
        "targetMeanPrice": price * r.uniform(0.9, 1.5), "targetLowPrice": price * r.uniform(0.8, 1.2),
        "sharesOutstanding": sh[-1],
    }
    info.update(over)
    inc = {"Total Revenue": rev, "Gross Profit": [x * r.uniform(0.3, 0.6) for x in rev],
           "Operating Income": [x * om for x in rev], "EBIT": [x * om for x in rev], "Net Income": ni,
           "Diluted EPS": eps, "Research And Development": [x * 0.04 for x in rev],
           "Interest Expense": [x * 0.02 for x in rev]}
    bal = {"Current Assets": [x * 0.3 for x in ta], "Current Liabilities": [x * 0.15 for x in ta],
           "Long Term Debt": [x * 0.5 for x in eq], "Total Debt": [x * 0.6 for x in eq], "Total Assets": ta,
           "Total Liabilities Net Minority Interest": [a - e for a, e in zip(ta, eq)], "Stockholders Equity": eq,
           "Retained Earnings": [x * 0.7 for x in eq], "Inventory": [x * 0.05 for x in rev], "Invested Capital": [x * 1.5 for x in eq],
           "Cash Cash Equivalents And Short Term Investments": [x * 0.1 for x in ta], "Net PPE": [x * 0.3 for x in ta],
           "Ordinary Shares Number": sh}
    cf = {"Operating Cash Flow": [x * 1.2 for x in ni], "Capital Expenditure": [-x * 0.3 for x in ni],
          "Free Cash Flow": [x * 0.9 for x in ni], "Cash Dividends Paid": [-x * 0.3 for x in ni],
          "Depreciation And Amortization": [x * 0.2 for x in ni]}
    return {"info": info, "inc": inc, "bal": bal, "cf": cf}


def check(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("ok  ", msg)


# ramps
check(lin(2, 2, 6) == 0 and lin(6, 2, 6) == 1 and lin(4, 2, 6) == 0.5, "linear ramp upward")
check(lin(1, 3, 1) == 1 and lin(3, 3, 1) == 0 and lin(2, 3, 1) == 0.5, "linear ramp for lower-is-better (PEG)")

# gate
ok, why = quality_gate({"returnOnEquity": 0.12, "operatingMargins": 0.10, "debtToEquity": 200})
check(ok, "gate passes exactly at the limits")
ok, why = quality_gate({"returnOnEquity": 0.30, "operatingMargins": 0.25, "debtToEquity": None})
check(not ok and why == ["D/E"], "missing D/E fails the gate")

# a perfect value stock scores 100 (capped)
perfect = {"currentPrice": 60, "marketCap": 1e10, "freeCashflow": 7e8, "trailingPegRatio": 0.8, "forwardPE": 12,
           "fiftyTwoWeekHigh": 100, "targetMeanPrice": 90, "targetLowPrice": 61, "returnOnEquity": 0.4, "operatingMargins": 0.5}
vs, x, parts = value_score(perfect)
check(abs(vs - 100) < 1e-9, f"perfect stock scores 100 (got {vs:.1f})")
# missing factors rescale
partial = dict(perfect); partial.pop("trailingPegRatio"); partial.pop("freeCashflow")
partial.update(returnOnEquity=0.13, operatingMargins=0.12)
vs2, _, parts2 = value_score(partial)
check(abs(vs2 - 100) < 1e-9 and "peg" not in parts2, "missing factors are left out and weights rescaled")
check(below_entry({"currentPrice": 50, "targetLowPrice": 50}) and not below_entry({"currentPrice": 50.5, "targetLowPrice": 50}), "below entry is at-or-below the low target")

# Piotroski on an improving company
s = company(7)
fs, _ = piotroski(fundamentals(s))
check(fs is not None and 0 <= fs <= 9, f"Piotroski in range (got {fs})")

# verdict logic
check(verdict(True, True, {"flags": [], "consensus": ["a", "b", "c", "d"]}) == "ADD-REVIEW (masters-confirmed)", "4 consensus + no flags = confirmed")
check(verdict(True, True, {"flags": ["negative FCF"], "consensus": ["a", "b", "c", "d"]}) == "ADD-REVIEW (flagged)", "flag wins over consensus")
check(verdict(True, True, {"flags": [], "consensus": ["a"]}) == "ADD-REVIEW", "plain ADD-REVIEW")
check(verdict(False, True, None) is None, "no verdict without the gate")

# yfinance-shaped frame -> dict, oldest first
try:
    from screen import frame_to_dict
    df = pd.DataFrame({pd.Timestamp("2025-12-31"): [3.0], pd.Timestamp("2023-12-31"): [1.0], pd.Timestamp("2024-12-31"): [2.0]}, index=["Net Income"])
    check(frame_to_dict(df)["Net Income"] == [1.0, 2.0, 3.0], "statement columns reordered oldest -> newest")
except ImportError as e:
    print("skip frame_to_dict check:", e)

# full pipeline on 60 made-up companies
stocks = {f"T{i:02d}": company(i) for i in range(60)}
stocks["NEG"] = company(99, debtToEquity=None)
stocks["NEG"]["bal"]["Stockholders Equity"] = [-1e9] * 4
rows, pe_med = run_screens(stocks, 0.0425)
check(len(rows) == 61, "every stock gets a row")
check(all(r["masters_score"] is None or 0 <= r["masters_score"] <= 100 for r in rows), "masters scores in 0-100")
check(all(r["value_score"] is None or 0 <= r["value_score"] <= 100 for r in rows), "value scores in 0-100")
neg = next(r for r in rows if r["ticker"] == "NEG")
check("negative equity" in neg["flags"] and not neg["gate"], "negative equity flagged and gate failed")
check(all(r["lenses"]["Greenblatt"] is None for r in rows if r["sector"] in ("Financial Services", "Utilities")), "Greenblatt skips financials and utilities")
json.dumps(rows, allow_nan=False)  # page needs strict JSON
gate = sum(r["gate"] for r in rows)
print(f"\n{gate} of {len(rows)} passed the gate; {sum(r['below_entry'] for r in rows)} below entry; "
      f"verdicts: {sorted(set(r['verdict'] for r in rows if r['verdict']))}")

# ---- historical drop study
from backtest import episodes, rolling_high, study

check(rolling_high([1, 3, 2, 5, 4], window=2, min_periods=1) == [1, 3, 3, 5, 5], "rolling 52-week high is a trailing max")
p = [100.0] * 300
p += [100 - 1.5 * i for i in range(1, 21)]          # down to 70 over 20 days
p += [70 + 0.75 * i for i in range(1, 41)]          # back to 100 over 40 days
p += [100.0] * 300
p += [100 - 1.25 * i for i in range(1, 21)] + [75.0] * 30   # today's drop: 25%, not recovered
d = list(range(len(p)))
eps = episodes(p, 0.20, d)
check(len(eps) == 2 and eps[1]["open"] and not eps[0]["open"], "finds one past episode and today's open one")
e = eps[0]
check(p[e["start_i"]] <= 80 and p[e["start_i"] - 1] > 80, "episode starts the first day the drop reaches 20%")
check(abs(e["depth"] - 0.30) < 1e-9, "depth is the deepest fall below the prior high")
check(p[e["start_i"] + e["half_days"]] >= 90 and p[e["start_i"] + e["half_days"] - 1] < 90, "half recovery = back within 10% (half of 20%)")
check(p[e["start_i"] + e["full_days"]] >= 100, "full recovery = back at the prior high")
s2 = study(p, [f"2000-01-{i}" for i in d], 0.25)
check(s2["episodes"] == 1 and s2["current_since"] is not None, "study leaves today's drop out of the history")
check(s2["drop_studied"] == 25.0, "studies today's drop size")
check(study(p, d, 0.05)["drop_studied"] == 10.0, "small drops are studied at the 10% minimum")
json.dumps(s2, allow_nan=False)

# ---- options study
import datetime as _dt
from options_study import payoff, study as opt_study, target_months

check(target_months({"full_months_all": [2, 4, 6, 8], "episodes": 4}) == 6.5, "target months = 75th percentile of recoveries")
check(target_months({"full_months_all": [4], "median_full_months": 4, "episodes": 1}) == 6.0, "few episodes: 1.5 x median")
L1, L2 = {"strike": 100}, {"strike": 120}
check(payoff("spread", [L1, L2], 5, 130) == 15 and payoff("spread", [L1, L2], 5, 90) == -5, "bull call spread payoff capped and floored")
check(payoff("csp", [{"strike": 95}], -3, 100) == 3 and payoff("csp", [{"strike": 95}], -3, 85) == -7, "cash-secured put payoff")
def q(k, bid, ask, oi=500):
    return {"strike": k, "bid": bid, "ask": ask, "lastPrice": (bid + ask) / 2, "openInterest": oi}
today = _dt.date(2026, 11, 12)
chains = {"2027-06-18": {"calls": [q(95, 12, 12.6), q(100, 9, 9.5), q(110, 5, 5.4), q(125, 1.9, 2.1)],
                         "puts": [q(90, 3, 3.3), q(95, 4.6, 5)]},
          "2028-01-21": {"calls": [q(80, 25, 26), q(100, 14, 15)], "puts": []}}
bt = {"full_months_all": [3, 4, 5, 6], "episodes": 4, "median_full_months": 4.5, "median_depth": 30.0}
o = opt_study(100.0, 125.0, bt, chains, today)
names = [x["structure"] for x in o["structures"]]
check(o["expiry"] == "2027-06-18", f"expiry fits the recovery target ({o['target_months']} mo)")
check(names == ["Shares (no options)", "Long call", "Bull call spread", "Cash-secured put", "LEAPS call (deep in the money)"], "all five structures built")
sp = next(x for x in o["structures"] if x["kind"] == "spread")
check(sp["cost_per_contract"] == 760.0 and sp["max_gain"] == 1740.0 and sp["breakeven"] == 107.6, "spread cost, max gain and breakeven (buy at ask, sell at bid)")
check(o["p_recover"] == round(5 / 6, 3) and o["best"] in names, "recovery odds smoothed (4 of 4 -> 5/6) and a best structure picked")
check(all("p" in x for x in o["structures"]), "each structure carries its own recovery odds")
wide = opt_study(100.0, 125.0, bt, {"2027-06-18": {"calls": [q(100, 1, 9), q(125, 0, 2, 5)], "puts": []}}, today)
check(all(not x["liquid"] for x in wide["structures"] if x["kind"] != "shares") and wide["best"] == "Shares (no options)", "wide or empty quotes are illiquid; falls back to shares")
json.dumps(o, allow_nan=False)

# ---- best today
import screen as _screen
ops = [{"ticker": "A", "verdict": "ADD-REVIEW", "score": 0.5, "best": "Shares (no options)", "best_detail": {"cost_per_contract": 100}},
       {"ticker": "B", "verdict": None, "score": 2.0, "best": "Long call", "best_detail": {}},
       {"ticker": "C", "verdict": "ADD-REVIEW (flagged)", "score": -0.1, "best": "Long call", "best_detail": {}},
       {"ticker": "D", "verdict": "ADD-REVIEW (masters-confirmed)", "score": 0.3, "best": "Bull call spread", "best_detail": {}}]
bt_ = _screen.best_today(ops, {"A": {"name": "Alpha"}}, {"A": {"episodes": 4, "median_full_months": 6}})
check([x["ticker"] for x in bt_] == ["A", "D"] and bt_[0]["name"] == "Alpha" and bt_[0]["median_recovery_months"] == 6,
      "best today: ADD-REVIEW only, positive scores, in ranking order, with name and recovery time")

# ---- paper portfolio
import paper as _paper
_d1 = _dt.date(2026, 11, 12)
_opts = {"S": {"up_price": 120.0, "structures": [{"structure": "Shares (no options)", "legs": [], "months": 6, "expiry": None}]},
         "B": {"up_price": 130.0, "structures": [{"structure": "Bull call spread", "months": 7, "expiry": "2027-06-17",
               "legs": [{"side": "buy", "type": "call", "strike": 100.0, "bid": 9.0, "ask": 11.0},
                        {"side": "sell", "type": "call", "strike": 130.0, "bid": 2.0, "ask": 4.0}]}]}}
_best = [{"ticker": "S", "structure": "Shares (no options)"}, {"ticker": "B", "structure": "Bull call spread"}]
st = _paper.empty()
check(_paper.pick_day(st, _best, _opts, _d1) == 2 and all(p["status"] == "pending" and p["invested"] == 0 for p in st["positions"]),
      "paper: the morning run records the picks as pending buys, with nothing put in yet")
check(_paper.pick_day(st, _best, _opts, _d1) == 0, "paper: a second run the same day picks nothing")
check(_paper.totals(st)["invested"] == 0 and _paper.totals(st)["pending"] == 2, "paper: pending picks don't count in the totals")
_open_ch = {("B", "2027-06-17"): {("call", 100.0): {"bid": 9.0, "ask": 11.0}, ("call", 130.0): {"bid": 2.0, "ask": 4.0}}}
_now = _dt.datetime(2026, 11, 12, 9, 0)
check(_paper.fill(st, {}, {}, _now) == 0 and st["positions"][0]["status"] == "pending" and st["positions"][0]["fill_tries"] == 1,
      "paper: no opening price yet leaves the pick pending")
check(_paper.fill(st, {"S": 100.0, "B": 100.0}, _open_ch, _now) == 2, "paper: the after-open run buys the pending picks")
sh, sp = st["positions"]
check(abs(sh["qty"] - 10) < 1e-9 and sh["spot_in"] == 100.0 and abs(sp["unit_in"] - 7.0) < 1e-9 and abs(sp["qty"] - 1000 / 700) < 1e-6
      and sh["stop_date"] == "2027-05-14" and sp["filled_at"].startswith("2026-11-12T09:00"),
      "paper: $1,000 at risk each (10 shares at the $100 open; spread at its $7 mid), fill time kept")
_paper.record(st, _d1)
_d2 = _dt.date(2026, 11, 13)
_ch = {("B", "2027-06-17"): {("call", 100.0): {"bid": 14, "ask": 16, "lastPrice": 15}, ("call", 130.0): {"bid": 4, "ask": 6, "lastPrice": 5}}}
_paper.mark(st, {"S": 110.0, "B": 110.0}, _ch, {}, _d2)
check(abs(sh["value"] - 1100) < 0.01 and abs(sp["value"] - 1000 / 700 * 100 * 10) < 0.01 and sp["mark_source"] == "quotes",
      "paper: marked from price and from leg mids")
_paper.mark(st, {"S": 121.0, "B": 110.0}, {}, {"B": 0.3}, _dt.date(2026, 11, 16))
check(sh["status"] == "closed" and sh["exit_reason"] == "back at the prior high" and sp["mark_source"] == "model",
      "paper: closes at the prior high; no quotes falls back to the model, flagged")
_v = sh["value"]
_paper.mark(st, {"S": 50.0, "B": 140.0}, {}, {}, _dt.date(2027, 6, 18))
check(sh["value"] == _v and sp["status"] == "closed" and abs(sp["value"] - 1000 / 700 * 100 * 30) < 0.01,
      "paper: closed positions stay frozen; expired spread pays its intrinsic value")
_paper.record(st, _d2); _h = list(st["history"]); _paper.record(st, _d2)
check(len(st["history"]) == 2 and st["history"][0] == _h[0], "paper: history keeps earlier days and replaces only today's line")
check(_paper.pick_day(_paper.empty(), _best, _opts, _dt.date(2026, 11, 14)) == 0, "paper: no picks on a weekend")
_s3 = _paper.empty(); _paper.pick_day(_s3, _best[1:], _opts, _d1)
for _ in range(3):
    _paper.fill(_s3, {"B": 100.0}, {}, _now)
check(_s3["positions"][0]["status"] == "not filled" and _paper.totals(_s3)["invested"] == 0,
      "paper: a pick with no live option quotes after 3 tries is marked not filled and never counts")
_s4 = _paper.empty()
check(_paper.pick_day(_s4, [], _opts, _d1) == 0 and _paper.picked_today(_s4, _d1) and not _paper.picked_today(_s4, _d2),
      "paper: a day with no picks still counts as done, so backup runs don't redo it")
_s5 = _paper.empty(); _paper.pick_day(_s5, _best[:1], _opts, _d1, "2026-11-12T08:45-06:00")
check(_s5["positions"][0]["picked_at"] == "2026-11-12T08:45-06:00", "paper: records when a pick was made")
import screen as _screen
_ch_tz = _dt.timezone(_dt.timedelta(hours=-6))
check(not _screen.after_open(_dt.datetime(2026, 11, 13, 8, 20, tzinfo=_ch_tz))
      and _screen.after_open(_dt.datetime(2026, 11, 13, 8, 45, tzinfo=_ch_tz))
      and not _screen.after_open(_dt.datetime(2026, 11, 14, 10, 0, tzinfo=_ch_tz)),
      "catch-up: buys wait for the open on weekdays")
json.dumps(st, allow_nan=False)

# ---- strategies and attention
import os as _os
import numpy as _np
import attention
import strategies

_P = pd.DataFrame({"A": [100, 110, 121, 121], "B": [50, 50, 25, 25]}, index=pd.bdate_range("2020-01-01", periods=4), dtype=float)
_daily, _inv = strategies.portfolio_from_trades(_P, [(0, 0, 2), (1, 1, 2)])
check(abs(_daily[1] - (0.10 - strategies.COST)) < 1e-9, "portfolio day 1: only A held, minus entry cost")
check(abs(_daily[2] - ((0.10 + -0.5) / 2 - strategies.COST / 2)) < 1e-9, "portfolio day 2: A and B equally weighted")
check(_daily[3] == 0.0 and abs(_inv - 0.5) < 1e-9, "cash after exits; invested half the days")
st = strategies.series_stats([0.1, -0.5, 0.2], 12)
check(st["max_drawdown"] == 50.0, "max drawdown from the equity curve")
check(abs(float(strategies.bs(100.0, 100.0, 0.2, 1.0, True)) - 7.9656) < 1e-3 and abs(float(strategies.bs(100.0, 100.0, 0.2, 1.0, True) - strategies.bs(100.0, 100.0, 0.2, 1.0, False))) < 1e-9, "Black-Scholes at the money: 7.97, call = put with no rates")
check(attention.boost(2) == 1.25 and attention.boost(0.5) == 0.75 and attention.boost(16) == 1.25 and attention.boost(None) == 1.0, "attention boost is capped at 0.75 to 1.25")
_os.environ.pop("REDDIT_CLIENT_ID", None)
tok, why = attention.reddit_token()
check(tok is None and "not set" in why, "Reddit skips cleanly without keys")
check(attention.company_words("Alphabet Inc. Class A") == "Alphabet", "company name stripped for searches")
print("all checks passed")
