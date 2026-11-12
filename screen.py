"""Fetch data from Yahoo Finance (via yfinance) and run both screens.

Usage:  python screen.py [--limit N] [--out docs/results.json]
Writes results.json for the web page. Annual statements are cached in
./cache for 7 days (they only change quarterly), so a normal day costs
about two Yahoo calls per ticker.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

import attention
import backtest
import options_study
import strategies
from scoring import num, run_screens

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
STMT_MAX_AGE_DAYS = 7
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
INFO_KEYS = [
    "shortName", "longName", "sector", "industry", "currentPrice", "regularMarketPrice",
    "marketCap", "enterpriseValue", "freeCashflow", "returnOnEquity", "operatingMargins",
    "debtToEquity", "trailingPegRatio", "pegRatio", "forwardPE", "trailingPE", "trailingEps",
    "priceToBook", "bookValue", "dividendYield", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "targetMeanPrice", "targetLowPrice", "targetHighPrice", "numberOfAnalystOpinions",
    "sharesOutstanding",
]


def sp500():
    try:
        html = requests.get(SP500_URL, headers={"User-Agent": "Mozilla/5.0 (stock-screen)"}, timeout=30).text
        t = pd.read_html(io.StringIO(html))[0]
        syms = [s.replace(".", "-") for s in t["Symbol"].astype(str)]
        (ROOT / "sp500_fallback.txt").write_text("\n".join(syms) + "\n")
        return syms
    except Exception as e:  # keep running on yesterday's list
        print(f"S&P 500 list fetch failed ({e}); using sp500_fallback.txt", file=sys.stderr)
        p = ROOT / "sp500_fallback.txt"
        return read_list(p) if p.exists() else []


def read_list(p):
    out = []
    for line in Path(p).read_text().splitlines():
        line = line.split("#")[0].strip().upper()
        if line:
            out.append(line)
    return out


def frame_to_dict(df):
    """yfinance statement (rows x dates, newest first) -> {row: [oldest..newest]}."""
    if df is None or df.empty:
        return {}
    df = df[sorted(df.columns)]
    return {str(k): [num(v) for v in row] for k, row in df.iterrows()}


def rate_limited(e):
    return "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e)


def with_retry(fn, tries=4):
    """Retry with backoff. Yahoo answers 429 when called too fast from shared cloud IPs,
    so rate-limit errors wait much longer (20 s, 40 s, 80 s) than other errors."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            base = 20 if rate_limited(e) else 3
            time.sleep((2 ** i) * base + random.random() * 2)
            print(f"  retry after {type(e).__name__}: {e}", file=sys.stderr)


PACE_SECONDS = 0.5   # pause after each ticker's fetch, per worker
RETRY_PAUSE = 90     # wait before the second pass over failed tickers


def statements(t, tk):
    CACHE.mkdir(exist_ok=True)
    p = CACHE / f"{t}.json"
    if p.exists() and time.time() - p.stat().st_mtime < STMT_MAX_AGE_DAYS * 86400:
        return json.loads(p.read_text())
    d = {
        "inc": frame_to_dict(with_retry(lambda: tk.get_income_stmt(freq="yearly"))),
        "bal": frame_to_dict(with_retry(lambda: tk.get_balance_sheet(freq="yearly"))),
        "cf": frame_to_dict(with_retry(lambda: tk.get_cashflow(freq="yearly"))),
    }
    if d["inc"]:
        p.write_text(json.dumps(d))
    return d


def insider_summary(tk):
    """Net insider shares bought over the last 6 months, from Yahoo's summary table."""
    try:
        df = tk.insider_purchases
        if df is None or df.empty:
            return None
        lab = df.columns[0]
        row = df[df[lab].astype(str).str.contains("Net Shares Purchased", case=False)]
        if row.empty:
            return None
        shares = num(row.iloc[0].get("Shares"))
        return {"net_shares_6m": shares} if shares else None
    except Exception:
        return None


def fetch_one(t):
    tk = yf.Ticker(t)
    info = with_retry(lambda: tk.info) or {}
    info = {k: info.get(k) for k in INFO_KEYS}
    st = statements(t, tk)
    ins = insider_summary(tk)
    time.sleep(PACE_SECONDS)
    return t, {"info": info, **st}, ins


def treasury_10y():
    try:
        h = with_retry(lambda: yf.Ticker("^TNX").history(period="5d"))
        y = float(h["Close"].dropna().iloc[-1])
        return (y / 10 if y > 20 else y) / 100  # ^TNX quotes the yield in percent
    except Exception as e:
        print(f"10-yr Treasury fetch failed ({e}); using 4.25%", file=sys.stderr)
        return 0.0425


PRICES_MAX_AGE_DAYS = 7


def universe_prices(tickers):
    """20 years of daily adjusted closes for the whole universe, cached for a week.
    Returns (prices DataFrame or None, refreshed: bool)."""
    CACHE.mkdir(exist_ok=True)
    p = CACHE / "prices.pkl"
    if p.exists() and time.time() - p.stat().st_mtime < PRICES_MAX_AGE_DAYS * 86400:
        P = pd.read_pickle(p)
        if len(set(tickers) & set(P.columns)) >= 0.9 * len(tickers):
            return P, False
    frames = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            df = with_retry(lambda: yf.download(chunk, period="20y", interval="1d", auto_adjust=True,
                                                group_by="column", threads=True, progress=False))
            c = df["Close"] if "Close" in df else df
            if isinstance(c, pd.Series):
                c = c.to_frame(chunk[0])
            frames.append(c)
        except Exception as e:
            print(f"price history failed for a chunk: {e}", file=sys.stderr)
    if not frames:
        return None, False
    P = pd.concat(frames, axis=1)
    P = P.loc[:, ~P.columns.duplicated()]
    P.to_pickle(p)
    return P, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N tickers (testing)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-backtest", action="store_true", help="skip the historical drop study")
    ap.add_argument("--no-strategies", action="store_true", help="skip the side-by-side strategy backtests")
    ap.add_argument("--no-attention", action="store_true", help="skip Reddit and Google Trends")
    ap.add_argument("--no-options", action="store_true", help="skip the options study")
    ap.add_argument("--out", default=str(ROOT / "docs" / "results.json"))
    a = ap.parse_args()

    extras = read_list(ROOT / "tickers_extra.txt") if (ROOT / "tickers_extra.txt").exists() else []
    universe = list(dict.fromkeys(sp500() + extras))
    if a.limit:
        universe = universe[: a.limit]
    print(f"Universe: {len(universe)} tickers")

    stocks, insider, failed = {}, {}, []

    def take(t, fu):
        try:
            _, s, ins = fu.result() if hasattr(fu, "result") else fu
            if s["info"].get("currentPrice") or s["info"].get("regularMarketPrice"):
                stocks[t] = s
                if ins:
                    insider[t] = ins
                return True
        except Exception as e:
            print(f"{t}: {e}", file=sys.stderr)
        return False

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch_one, t): t for t in universe}
        for i, fu in enumerate(as_completed(futs), 1):
            t = futs[fu]
            if not take(t, fu):
                failed.append(t)
            if i % 50 == 0:
                print(f"  {i}/{len(universe)}")

    # Second pass, one at a time after a pause, for tickers Yahoo turned away.
    if failed:
        print(f"Retrying {len(failed)} failed tickers after {RETRY_PAUSE}s")
        time.sleep(RETRY_PAUSE)
        still = []
        for t in failed:
            try:
                ok = take(t, fetch_one(t))
            except Exception as e:
                print(f"{t}: {e}", file=sys.stderr)
                ok = False
            if not ok:
                still.append(t)
            time.sleep(2)
        failed = still
        print(f"  {len(failed)} still failed")

    ty = treasury_10y()
    rows, pe_median = run_screens(stocks, ty, insider)

    passed = [r for r in rows if r["gate"]]
    top10 = sorted([r for r in passed if r["value_score"] is not None], key=lambda r: -r["value_score"])[:10]
    below = sorted([r for r in rows if r["below_entry"]], key=lambda r: -(r["value_score"] or 0))
    masters_top = sorted([r for r in rows if r["masters_score"] is not None], key=lambda r: -r["masters_score"])[:20]
    confirmed = [r["ticker"] for r in below if r["verdict"] == "ADD-REVIEW (masters-confirmed)"]

    # Historical drop study for the names the screen flags (below entry + top 10).
    bt = {}
    if not a.no_backtest:
        flagged = list(dict.fromkeys([r["ticker"] for r in below] + [r["ticker"] for r in top10]))
        drops = {r["ticker"]: r["value_inputs"].get("off_high") for r in rows}
        print(f"Backtesting {len(flagged)} flagged tickers")
        bt = backtest.run(flagged, drops)

    # Options structures sized to past recovery times, and the reward-per-time ranking.
    opt, opportunities = {}, []
    if bt and not a.no_options:
        cands = {}
        for t in bt:
            info = stocks[t]["info"]
            spot = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
            hi = num(info.get("fiftyTwoWeekHigh"))
            if spot and hi:
                cands[t] = (spot, hi, bt[t])
        print(f"Options study for {len(cands)} tickers")
        opt = options_study.run(cands, retry=with_retry)
        by = {r["ticker"]: r for r in rows}
        for t, o in opt.items():
            if not o:
                continue
            best = next((x for x in o["structures"] if x["structure"] == o["best"]), None)
            opportunities.append({"ticker": t, "verdict": by[t]["verdict"], "value_score": by[t]["value_score"],
                                  "masters_score": by[t]["masters_score"], "flags": by[t]["flags"],
                                  "score": o["score"], "best": o["best"], "best_detail": best,
                                  "p_recover": (best or {}).get("p", o.get("p_recover")),
                                  "expiry": (best or {}).get("expiry") if best else o.get("expiry")})
        opportunities.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))

    # Side-by-side strategy backtests (recomputed when the weekly price cache refreshes).
    strat, signals = None, {}
    flagged_all = list(dict.fromkeys([r["ticker"] for r in below] + [r["ticker"] for r in top10]))
    if not a.no_strategies:
        P, refreshed = universe_prices(list(stocks))
        cached = CACHE / "strategies.json"
        if P is not None:
            if refreshed or not cached.exists():
                print("Backtesting strategies on the universe")
                strat = strategies.run_all(P)
                cached.write_text(json.dumps(strat))
            else:
                strat = json.loads(cached.read_text())
            signals = strategies.signals_today(P, flagged_all)

    # Attention: Reddit mentions and Google search interest (each skips cleanly when unavailable).
    att, att_notes = {}, []
    if not a.no_attention and flagged_all:
        names = {t: stocks[t]["info"].get("shortName") or "" for t in flagged_all}
        att, att_notes = attention.run(flagged_all, names)
    for o in opportunities:
        o["signals"] = signals.get(o["ticker"])
        o["attention"] = att.get(o["ticker"])
        o["attention_boost"] = round(attention.boost((att.get(o["ticker"]) or {}).get("change")), 3)

    result = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "universe": len(universe),
        "scored": len(stocks),
        "failed": sorted(failed),
        "gate_passed": len(passed),
        "below_entry_count": len(below),
        "treasury_10y": ty,
        "pe_median": pe_median,
        "top10": [r["ticker"] for r in top10],
        "below_entry": [r["ticker"] for r in below],
        "masters_top": [r["ticker"] for r in masters_top],
        "masters_confirmed": confirmed,
        "backtest": bt,
        "options": opt,
        "opportunities": opportunities,
        "strategies": strat,
        "signals": signals,
        "attention": att,
        "attention_notes": att_notes,
        "rows": sorted(rows, key=lambda r: r["ticker"]),
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, separators=(",", ":"), allow_nan=False, default=str))
    summary = {k: result[k] for k in ("generated_at", "universe", "scored", "gate_passed",
                                      "below_entry_count", "top10", "masters_confirmed")}
    summary["below_entry"] = [{"ticker": r["ticker"], "verdict": r["verdict"],
                               "value_score": r["value_score"], "masters_score": r["masters_score"],
                               "flags": r["flags"],
                               "past_drops": {k: bt[r["ticker"]].get(k) for k in (
                                   "drop_studied", "episodes", "per_decade", "median_full_months",
                                   "pct_full_within_12m", "longest_full_months")} if r["ticker"] in bt else None}
                              for r in below]
    summary["opportunities"] = [{k: o[k] for k in ("ticker", "verdict", "score", "best", "expiry", "p_recover")}
                                | {"cost": (o["best_detail"] or {}).get("cost_per_contract"),
                                   "max_loss": (o["best_detail"] or {}).get("max_loss"),
                                   "payoff_up": (o["best_detail"] or {}).get("payoff_up")}
                                for o in opportunities[:10]]
    (out.parent / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    if len(stocks) < 0.8 * len(universe):
        print(f"WARNING: only {len(stocks)}/{len(universe)} tickers fetched", file=sys.stderr)


if __name__ == "__main__":
    main()
