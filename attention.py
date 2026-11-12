"""Attention signals for flagged stocks: Reddit mentions and Google search interest.

Both are optional and skip cleanly when unavailable.

Reddit: needs a free Reddit API app (script type) approved by Reddit. Store its
client id and secret as GitHub repository secrets REDDIT_CLIENT_ID and
REDDIT_CLIENT_SECRET; nothing goes in the code. Counts posts in r/wallstreetbets,
r/stocks and r/investing that mention "$TICKER" or the company name, last 7 days
vs the weekly average of the 21 days before.

Google Trends: no open official API (the 2025 Trends API is an invite-only alpha).
Uses the unofficial `trendspy` library; Google rate-limits it, more so from cloud
servers, so expect some days with no data. Searches "<TICKER> stock" over the last
3 months, last 4 weeks vs the 8 weeks before.

attention change = average of the available ratios (1.0 = no change).
"""
from __future__ import annotations

import math
import os
import re
import time

import requests

SUBREDDITS = ["wallstreetbets", "stocks", "investing"]
UA = "stock-screen/1.0 (personal research script)"


def company_words(name):
    name = re.sub(r"[,.]|\b(Inc|Corp|Corporation|Co|Company|Holdings?|Group|plc|Ltd|Limited|N\.?V|S\.?A|Class [A-C])\b", " ",
                  name or "", flags=re.I)
    return " ".join(name.split())


# ---------------------------------------------------------------- Reddit

def reddit_token():
    cid, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not sec:
        return None, "Reddit skipped: REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET are not set."
    try:
        r = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, sec),
                          data={"grant_type": "client_credentials"}, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        return r.json()["access_token"], None
    except Exception as e:
        return None, f"Reddit skipped: sign-in failed ({type(e).__name__})."


def reddit_counts(token, ticker, name, now=None):
    now = now or time.time()
    words = company_words(name)
    q = f'"${ticker}"' + (f' OR "{words}"' if words else "")
    h = {"Authorization": f"bearer {token}", "User-Agent": UA}
    week, prior = 0, 0
    for sub in SUBREDDITS:
        after = None
        for _ in range(3):  # up to 300 posts per subreddit
            params = {"q": q, "restrict_sr": 1, "sort": "new", "t": "month", "limit": 100}
            if after:
                params["after"] = after
            r = requests.get(f"https://oauth.reddit.com/r/{sub}/search", params=params, headers=h, timeout=20)
            if r.status_code == 429:
                time.sleep(10)
                continue
            r.raise_for_status()
            data = r.json()["data"]
            for c in data["children"]:
                age = (now - c["data"]["created_utc"]) / 86400
                if age <= 7:
                    week += 1
                elif age <= 28:
                    prior += 1
            after = data.get("after")
            time.sleep(0.7)  # stay under 100 requests a minute
            if not after:
                break
    base = prior / 3
    return {"mentions_7d": week, "weekly_avg_prior": round(base, 1),
            "change": None if base == 0 else round(week / base, 2)}

# ---------------------------------------------------------------- Google Trends

def trends_changes(tickers):
    try:
        from trendspy import Trends
    except ImportError:
        return {}, "Google Trends skipped: trendspy is not installed."
    tr, out, note = Trends(), {}, None
    for i in range(0, len(tickers), 5):
        batch = tickers[i:i + 5]
        kws = [f"{t} stock" for t in batch]
        try:
            df = tr.interest_over_time(kws, timeframe="today 3-m", geo="US")
        except Exception as e:
            note = (f"Google Trends stopped after {len(out)} tickers ({type(e).__name__}); "
                    "Google often blocks automated requests, so this column comes and goes.")
            break
        for t, kw in zip(batch, kws):
            if kw not in df:
                continue
            s = df[kw].astype(float)
            recent, before = s.iloc[-28:].mean(), s.iloc[-84:-28].mean()
            out[t] = {"interest_recent": round(recent, 1), "interest_before": round(before, 1),
                      "change": None if not before else round(recent / before, 2)}
        time.sleep(6)
    return out, note

# ---------------------------------------------------------------- combined

def combine(reddit, trends):
    vals = [x["change"] for x in (reddit, trends) if x and x.get("change") is not None]
    return None if not vals else round(sum(vals) / len(vals), 2)


def boost(change, weight=0.25):
    """Optional ranking multiplier the page can apply: 1 + 0.25 x clamp(log2(change), -1, 1)."""
    if not change or change <= 0:
        return 1.0
    return 1 + weight * max(-1.0, min(1.0, math.log2(change)))


def run(tickers, names):
    notes = []
    token, why = reddit_token()
    if why:
        notes.append(why)
    reddit = {}
    if token:
        for t in tickers:
            try:
                reddit[t] = reddit_counts(token, t, names.get(t, ""))
            except Exception as e:
                notes.append(f"Reddit stopped at {t} ({type(e).__name__}).")
                break
    trends, why = trends_changes(tickers)
    if why:
        notes.append(why)
    out = {}
    for t in tickers:
        r, g = reddit.get(t), trends.get(t)
        if r or g:
            out[t] = {"reddit": r, "trends": g, "change": combine(r, g)}
    return out, notes
