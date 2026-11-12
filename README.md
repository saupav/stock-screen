# Daily stock screen

Runs the undervalued screen (Screen 1) and the masters screen (Screen 2) on the
S&P 500 plus your extras every weekday morning, and shows the results on a web page.

**How it runs:** a free scheduled GitHub Actions job pulls Yahoo Finance data
(through the `yfinance` library, no API key), scores every stock, and commits
`docs/results.json`. The page (`docs/index.html`) reads that file, so any static
host that serves the `docs/` folder works.

Why not compute everything inside the browser: no free API that a web page can
call directly gives analyst low and mean price targets for 500 stocks a day
(Finnhub's price targets are a paid endpoint; FMP's free plan is 250 calls a day),
and Yahoo doesn't allow browser calls. A scheduled job has none of those limits.

## Files

| File | What it is |
|---|---|
| `scoring.py` | Every rule from both screens and the verdicts. Choices the written rules leave open are marked `ASSUMPTION`. |
| `backtest.py` | The past-drops study for flagged stocks. |
| `options_study.py` | Options structures sized to past recovery times, and the reward-per-time ranking. |
| `strategies.py` | Side-by-side backtests of well-documented strategies on the universe. |
| `attention.py` | Reddit mentions and Google search interest for flagged stocks (optional). |
| `paper.py` | The hypothetical $1,000-a-pick portfolio, kept in `docs/paper.json`. |
| `screen.py` | Fetches the data and writes `docs/results.json` and `docs/summary.json`. |
| `docs/index.html` | The results page. |
| `.github/workflows/daily-screen.yml` | Runs the screens at 12:15 UTC Monday to Friday (6:15 AM Chicago in winter, 7:15 AM in summer), buys the pretend portfolio's picks at 15:00 UTC, and runs on demand. |
| `tickers_extra.txt` | Your hand-picked extras, one per line. |
| `test_scoring.py` | Offline checks of the rules: `python test_scoring.py`. |

## Best opportunities today

The page opens with up to five cards: the ADD-REVIEW stocks with the highest positive reward-per-time
score (see the options study below), each with its company name, verdict (masters-confirmed cards
have a green border), the chosen structure and expiry, cost, max loss, payoff if it gets back to the
prior high, recovery odds in that time, typical recovery time from its past drops, and any risk flags.
The same list is saved as `best_today` in `docs/results.json` and `docs/summary.json`.

## Hypothetical portfolio

Pretend money, bookkeeping only: no real money, no broker and no orders, simulated or otherwise.
Each weekday the 6:15 AM Chicago run picks that day's best five, and a second run just after the
market opens (15:00 UTC: 9:00 AM Chicago in winter, 10:00 AM in summer) buys $1,000 of each
(`STAKE` in `paper.py`) in the structure its card shows: shares at that day's opening price, options
at the mid of each leg's live bid and ask. A pick without live quotes waits for the next after-open
run; after 3 tries it's marked not bought and never counts. Runs started by a code change don't
pick or buy anything. $1,000 is the most each position can lose: shares buy $1,000 of stock; a debit structure buys
$1,000 ÷ (debit × 100) contracts; a cash-secured put sells $1,000 ÷ ((strike − credit) × 100).
A position closes when the stock gets back to its prior 52-week high, when its options expire (at
intrinsic value), or for shares after the card's holding time. Until then each run values it from
fresh quotes: the mid with a live bid and ask, else the last trade, else a Black-Scholes estimate,
and the page says which. Everything is kept in `docs/paper.json`; past days are never rewritten.

The page shows money put in, what it's worth, profit, a chart over time, and a table sortable by
day bought, stock, strategy, profit $, profit % and profit per year in the market, with an
open/closed filter and totals by strategy, stock or day. `docs/summary.json` carries
`paper_total_invested`, `paper_value` and `paper_pl_pct`.

The first five (Nov 12, 2026) were bought at 12:57 PM Chicago on live midday quotes, before this
morning/after-open schedule existed.

## Past drops study

For every stock on the ADD-REVIEW list or in the top 10, the job downloads its full daily price
history (adjusted for splits and dividends) and finds each earlier time it fell at least as far
below its trailing 52-week high as it is today (10% minimum). Per stock it reports how many such
drops there were and how often (per decade), the median time to regain half the drop and to get
back to the prior high, the longest recovery, the share back at the high within 12 months, the
median return 12 months after the drop began, and since when it has been in today's drop.
Click a row on the page to see every episode.

Limits: it only studies companies in today's S&P 500, which leaves out ones that never recovered
and dropped out of the index, so recovery figures look better than a full history would. Months
are trading months (21 trading days). A history study, not a forecast.

## Options study and ranking

For each flagged stock with a past-drops study, the job reads Yahoo's option chains and compares
five structures at an expiry that fits the stock's own recovery history:

- **Target time:** the 75th percentile of its past times back to the prior high (1.5 x the median
  with fewer than 4 past drops; 12 months with none), at least 1 month. The expiry is the first
  listed on or after that; if none is long enough, the longest listed, with a note.
- **Shares:** 100 shares at today's price, the no-options baseline.
- **Long call:** the first strike at or above today's price.
- **Bull call spread:** buy that call, sell the listed strike closest to the prior 52-week high.
- **Cash-secured put:** sell the put closest to 95% of today's price.
- **LEAPS call:** the longest expiry 9+ months out, strike closest to 80% of today's price.

Buys fill at the ask and sells at the bid. For each it shows net cost or credit, max loss, max
gain, breakeven, each leg's bid/ask with its spread and open interest, and the payoff in two cases:
back at the prior high, and at the usual low (prior high x (1 - median deepest fall of past drops)).

**Score** (expected return on money at risk, per year in the market):

```
p      = (k + 1) / (n + 2), where k of the n past drops got back to the prior high within the months held
EV     = p x payoff(back at prior high) + (1 - p) x payoff(at usual low)
score  = EV / max loss / years to expiry
```

The `+1 / +2` (Laplace's rule) keeps one or two past drops from reading as certain recovery:
1 of 1 counts as 67%, 9 of 10 as 83%. The odds shown are for the best structure's own holding time.

The best structure for a stock is the one with the highest score among those whose legs are all
liquid (live bid and ask, spread no more than 25% of mid, open interest 100+). The shares baseline
always qualifies. Opportunities are sorted by that score. A stock with no past drop of today's size
has no recovery odds, so it gets no score and sorts last.

Limits:
- The job runs before the market opens, so option quotes are the prior session's. When Yahoo has
  no live bid/ask, the last trade price is used and the leg counts as illiquid.
- Recovery odds count from the start of each past drop, not from where the stock is today.
- Only today's S&P 500 members are studied, which flatters the odds.
- Max loss on a cash-secured put assumes the stock goes to zero; the cash it ties up is the strike x 100.
- Wide bid/ask spreads can make the spread and LEAPS numbers unrealistic; check the widths shown.

A mechanical historical study, not advice. Nothing here places or simulates orders.

## Strategies compared

Backtested weekly on 20 years of daily prices for today's universe, with 0.2% cost per stock
trade. For each: trades, hit rate, median and 10th-percentile trade, median days held, yearly
return, max drawdown, Sharpe ratio and share of time invested.

| Strategy | Why it's here | Rule |
|---|---|---|
| Buy and hold | Benchmark | Equal weight in every stock. |
| Drop 25% from 52-week high | The price-only core of this screen | Buy on the first day 25% below the high; sell at that high or after 12 months. |
| 12-1 momentum | Jegadeesh & Titman (1993), the most replicated stock anomaly | Monthly top 10% by 12-month return skipping the last month; hold 1 month. |
| Near 52-week high | George & Hwang (2004) | Monthly top 10% by price ÷ 52-week high; hold 1 month. |
| Short-term reversal (RSI-2) | Connors' widely tested mean-reversion rule | Buy when 2-day RSI < 10 above the 200-day average; sell above the 5-day average or after 10 days. |
| Covered call (buy-write) | Cboe BXM index | Own the stock, sell a 1-month call 2% above, monthly. |
| Cash-secured put-write | Cboe PUT index | Sell a 1-month at-the-money put, cash-secured, monthly. |
| Collar | Cboe CLL index | Own the stock, buy a 5% lower put, sell a 5% higher call, monthly. |

The option rows use modelled prices (Black-Scholes, implied volatility = 1.1 x the trailing
21-day realized volatility, less 5% of premium for spreads), because free option history doesn't
exist. Post-earnings drift is not included: it needs years of dated earnings surprises, which
Yahoo doesn't give for free. Every row shares the survivorship bias of testing only today's members.

The page also shows, for each ranked stock, whether momentum, near-high or RSI-2 would hold it today.

## Attention signals (optional)

- **Reddit:** posts in r/wallstreetbets, r/stocks and r/investing naming "$TICKER" or the company,
  last 7 days vs the weekly average of the 21 days before. Needs a free Reddit API app, which Reddit
  now approves case by case (state a personal, non-commercial research use). Then in the repository,
  **Settings → Secrets and variables → Actions**, add `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`.
  Without them the job skips Reddit and says so on the page. Free limit: 100 requests a minute;
  the job uses about 3 to 9 per stock.
- **Google Trends:** search interest in "TICKER stock" in the US, last 4 weeks vs the 8 before.
  Google's official Trends API is an invite-only alpha, so this uses the unofficial `trendspy`
  library. Google often blocks these requests, especially from cloud servers like GitHub's, so
  expect days with no search data; the job carries on without it.
- **Change** = the average of the available ratios. A page checkbox can boost the ranking by
  `1 + 0.25 x log2(change)`, limited to 0.75 to 1.25. It is off by default.

Attention shows what people are talking about, not whether a stock will recover.

## Setup (about 10 minutes)

1. Create a new GitHub repository (public is simplest for GitHub Pages).
2. Upload everything in this folder, keeping the folder layout (`.github/workflows/` included).
3. Add your extras to `tickers_extra.txt`.
4. In the repository: **Settings → Pages → Build and deployment → Deploy from a branch**, branch `main`, folder `/docs`.
5. **Actions → Daily stock screen → Run workflow** for the first run. It takes roughly 15 to 30 minutes the first time and much less after, because annual reports are cached for a week.
6. Open `https://<your-user>.github.io/<repo>/`.

Prefer a private repo? Connect it to Cloudflare Pages instead (build command empty, output folder `docs`); every commit the job makes redeploys the page.

Run it locally instead: `pip install -r requirements.txt && python screen.py`, then open `docs/index.html` and use "Open a results.json".

## What is exact and what is approximated

- **Exact as written:** the quality gate, all six value factors with their zero and full points, missing-factor rescaling, the 100 cap, the below-entry list, all eight lens checklists, the Magic Formula ranking, the DCF assumptions, the risk flags, consensus (lenses at 70+) and the verdict order (flagged beats masters-confirmed).
- **Choices the rules leave open** (`ASSUMPTION` in `scoring.py`):
  - Bonus ramps: ROE 15% → 0 to 30% → 5 points; operating margin 20% → 0 to 40% → 5 points.
  - "Pulled slightly toward 50": each lens keeps 85% of its distance from 50.
  - Below entry uses price ≤ lowest target exactly (`ENTRY_TOLERANCE = 0.0`; set 0.02 to include anything within 2%).
  - Lenses score the share of their checks passed; a check without data is skipped. A lens with no computable check is left out of the MASTERS average.
  - DCF equity value adds cash and subtracts total debt; justified P/B caps ROE at 25%.
  - Negative PEG scores zero; "flat" share count allows up to 1%; Altman Z is not applied to financials.
- **Data limits:** Yahoo gives four annual reports, so "every year" means the last four. Yahoo's debt-to-equity is a percentage (150 = 1.5×). Insider buying is Yahoo's six-month net purchases and is shown only as a supporting fact.
- Counts won't match the original scripts exactly (for example the 226 gate passes) because data sources and rounding differ. Compare the first run against a recent brief.

Mechanical rules for research, not advice.
