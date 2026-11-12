"""Scoring rules for the two daily screens.

Pure functions only: no network. Each stock arrives as a dict `s` with
  s["info"]   : current snapshot (price, targets, ratios) as plain floats
  s["inc"], s["bal"], s["cf"] : annual statements, {row_name: [oldest ... newest]}
Rules follow the written spec (see README.md). Where the spec leaves a detail
open, the choice made here is marked ASSUMPTION.
"""
from __future__ import annotations

import math
from statistics import median

# ---------------------------------------------------------------- helpers

def num(x):
    """Float or None (NaN, inf, strings and None all become None)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def lin(x, zero, full):
    """0..1 linear ramp: 0 at `zero`, 1 at `full` (works in either direction)."""
    if x is None:
        return None
    if zero == full:
        return 1.0 if x >= full else 0.0
    t = (x - zero) / (full - zero)
    return max(0.0, min(1.0, t))


def _norm(name):
    return str(name).replace(" ", "").lower()


def series(stmt, *names):
    """First matching row as a list of floats/None, oldest -> newest.
    Matches Yahoo's raw keys ("NetPPE") and pretty ones ("Net PPE") alike."""
    keys = {_norm(k): k for k in stmt}
    for n in names:
        k = keys.get(_norm(n))
        if k is not None:
            return [num(v) for v in stmt[k]]
    return []


def clean(xs):
    return [x for x in xs if x is not None]


def last(xs):
    c = clean(xs)
    return c[-1] if c else None


def first(xs):
    c = clean(xs)
    return c[0] if c else None


def cagr(xs):
    """Compound annual growth, oldest -> newest, None if not computable."""
    c = clean(xs)
    if len(c) < 2 or c[0] <= 0 or c[-1] <= 0:
        return None
    return (c[-1] / c[0]) ** (1 / (len(c) - 1)) - 1


def safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def checklist(checks):
    """checks: list of (name, True/False/None). None = not computable, skipped.
    Returns (score 0-100 or None, detail list)."""
    done = [(n, v) for n, v in checks if v is not None]
    if not done:
        return None, checks
    return 100.0 * sum(1 for _, v in done if v) / len(done), checks


FINANCIAL_SECTORS = {"Financial Services", "Financial"}
UTILITY_SECTORS = {"Utilities"}

# ---------------------------------------------------------------- Screen 1

GATE = {"roe_min": 0.12, "opm_min": 0.10, "de_max": 200.0}

# (key, weight, zero-at, full-at)
VALUE_FACTORS = [
    ("fcf_yield", 25, 2.0, 6.0),
    ("peg", 20, 3.0, 1.0),
    ("fwd_earn_yield", 15, 3.0, 7.0),
    ("off_high", 20, 10.0, 35.0),
    ("upside_mean", 10, 0.0, 30.0),
    ("vs_low_target", 10, 10.0, 2.0),
]

# Tolerance for "at or below the lowest analyst target" on the below-entry list.
# ASSUMPTION: strict (0%). Set to 0.02 to count anything within 2% as "at".
ENTRY_TOLERANCE = 0.0


def quality_gate(info):
    roe, opm, de = num(info.get("returnOnEquity")), num(info.get("operatingMargins")), num(info.get("debtToEquity"))
    reasons = []
    if roe is None or roe < GATE["roe_min"]:
        reasons.append("ROE")
    if opm is None or opm < GATE["opm_min"]:
        reasons.append("op margin")
    # Yahoo reports debt-to-equity as a percentage (150 = 1.5x). Missing D/E
    # (often negative equity) fails the gate.
    if de is None or de > GATE["de_max"]:
        reasons.append("D/E")
    return not reasons, reasons


def value_inputs(info):
    p = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    mcap = num(info.get("marketCap"))
    fcf = num(info.get("freeCashflow"))
    peg = num(info.get("trailingPegRatio")) or num(info.get("pegRatio"))
    fpe = num(info.get("forwardPE"))
    hi = num(info.get("fiftyTwoWeekHigh"))
    mean_t, low_t = num(info.get("targetMeanPrice")), num(info.get("targetLowPrice"))
    out = {
        "fcf_yield": safe_div(fcf, mcap) * 100 if safe_div(fcf, mcap) is not None else None,
        "peg": peg,
        # Negative forward P/E means forecast losses: yield <= 0, scores zero.
        "fwd_earn_yield": (100.0 / fpe) if fpe else None,
        "off_high": (1 - p / hi) * 100 if p and hi else None,
        "upside_mean": (mean_t / p - 1) * 100 if p and mean_t else None,
        "vs_low_target": (p / low_t - 1) * 100 if p and low_t else None,
    }
    return out


def value_score(info):
    x = value_inputs(info)
    got, wsum, parts = 0.0, 0.0, {}
    for key, w, zero, full in VALUE_FACTORS:
        v = x[key]
        if key == "peg" and v is not None and v <= 0:
            pts = 0.0  # negative PEG = shrinking earnings; ASSUMPTION: zero points
        else:
            pts = lin(v, zero, full)
        if pts is None:
            continue  # missing: left out, remaining weights rescaled
        parts[key] = round(pts * w, 2)
        got += pts * w
        wsum += w
    if wsum == 0:
        return None, x, parts
    score = got / wsum * 100
    # Bonus. ASSUMPTION: ramps linearly, ROE 15% -> 0 to 30% -> 5,
    # op margin 20% -> 0 to 40% -> 5 (spec gives only the start and the max).
    roe, opm = num(info.get("returnOnEquity")), num(info.get("operatingMargins"))
    b_roe = 5 * lin(roe, 0.15, 0.30) if roe is not None else 0
    b_opm = 5 * lin(opm, 0.20, 0.40) if opm is not None else 0
    parts["bonus"] = round(b_roe + b_opm, 2)
    return min(100.0, score + b_roe + b_opm), x, parts


def below_entry(info):
    p = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    low_t = num(info.get("targetLowPrice"))
    return bool(p and low_t and p <= low_t * (1 + ENTRY_TOLERANCE))

# ---------------------------------------------------------------- Screen 2 data prep

def fundamentals(s):
    """Derive the annual series every lens needs."""
    inc, bal, cf, info = s.get("inc", {}), s.get("bal", {}), s.get("cf", {}), s.get("info", {})
    f = {
        "rev": series(inc, "Total Revenue", "Operating Revenue"),
        "gp": series(inc, "Gross Profit"),
        "opinc": series(inc, "Operating Income", "Total Operating Income As Reported"),
        "ebit": series(inc, "EBIT", "Operating Income"),
        "ni": series(inc, "Net Income", "Net Income Common Stockholders"),
        "eps": series(inc, "Diluted EPS", "Basic EPS"),
        "rnd": series(inc, "Research And Development"),
        "int_exp": series(inc, "Interest Expense", "Interest Expense Non Operating"),
        "ca": series(bal, "Current Assets"),
        "cl": series(bal, "Current Liabilities"),
        "ltd": series(bal, "Long Term Debt", "Long Term Debt And Capital Lease Obligation"),
        "total_debt": series(bal, "Total Debt"),
        "ta": series(bal, "Total Assets"),
        "tl": series(bal, "Total Liabilities Net Minority Interest"),
        "eq": series(bal, "Stockholders Equity", "Common Stock Equity"),
        "re": series(bal, "Retained Earnings"),
        "inv": series(bal, "Inventory"),
        "ic": series(bal, "Invested Capital"),
        "cash": series(bal, "Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"),
        "ppe": series(bal, "Net PPE"),
        "shares": series(bal, "Ordinary Shares Number", "Share Issued"),
        "ocf": series(cf, "Operating Cash Flow"),
        "capex": series(cf, "Capital Expenditure"),
        "fcf": series(cf, "Free Cash Flow"),
        "div": series(cf, "Cash Dividends Paid", "Common Stock Dividend Paid"),
        "da": series(cf, "Depreciation And Amortization", "Depreciation Amortization Depletion"),
    }
    f["price"] = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    f["mcap"] = num(info.get("marketCap"))
    f["ev"] = num(info.get("enterpriseValue"))
    f["sector"] = info.get("sector") or ""
    f["industry"] = info.get("industry") or ""
    f["is_fin"] = f["sector"] in FINANCIAL_SECTORS
    f["sh_now"] = num(info.get("sharesOutstanding")) or last(f["shares"])
    return f


def ratio_series(a, b):
    n = min(len(a), len(b))
    return [safe_div(a[i], b[i]) for i in range(n)]

# ---------------------------------------------------------------- lenses

def lens_graham(f, info):
    eps = clean(f["eps"])
    pe3 = None
    if len(eps) >= 1 and f["price"]:
        avg3 = sum(eps[-3:]) / len(eps[-3:])
        pe3 = f["price"] / avg3 if avg3 > 0 else None
    ca, cl, ltd = last(f["ca"]), last(f["cl"]), last(f["ltd"])
    ncav = ca - cl if ca is not None and cl is not None else None
    pb = num(info.get("priceToBook"))
    pe = num(info.get("trailingPE"))
    bvps = num(info.get("bookValue"))
    teps = num(info.get("trailingEps")) or last(f["eps"])
    gn = math.sqrt(22.5 * teps * bvps) if teps and bvps and teps > 0 and bvps > 0 else None
    g = cagr(f["eps"])
    divs = clean(f["div"])
    checks = [
        ("market cap >= $2B", None if f["mcap"] is None else f["mcap"] >= 2e9),
        ("current ratio >= 2", None if not (ca and cl) else ca / cl >= 2),
        ("LT debt <= net current assets", None if ncav is None else (ltd or 0) <= ncav),
        ("positive earnings every year", None if not clean(f["ni"]) else all(x > 0 for x in clean(f["ni"]))),
        ("dividend every year", None if not f["div"] else (len(divs) == len(f["div"]) and all(abs(x) > 0 for x in divs))),
        ("EPS growth >= 3%/yr", None if g is None else g >= 0.03),
        ("P/E on 3-yr avg EPS <= 15", None if pe3 is None else pe3 <= 15),
        ("P/B <= 1.5 or P/E x P/B <= 22.5", None if pb is None else (pb <= 1.5 or (pe is not None and pe * pb <= 22.5))),
        ("price <= Graham Number", None if gn is None or not f["price"] else f["price"] <= gn),
    ]
    return checklist(checks)


def lens_buffett(f, info, treasury_10y):
    roe = ratio_series(f["ni"], f["eq"])
    roic = ratio_series(f["ni"], f["ic"])
    yearly = []
    for i in range(max(len(roe), len(roic))):
        a = roe[i] if i < len(roe) else None
        b = roic[i] if i < len(roic) else None
        vals = clean([a, b])
        yearly.append(max(vals) if vals else None)
    gm = clean(ratio_series(f["gp"], f["rev"]))
    ni_l, ltd_l = last(f["ni"]), last(f["ltd"])
    capex_l = last(f["capex"])
    fcf = clean(f["fcf"])
    sh = clean(f["shares"])
    re = clean(f["re"])
    oe = None
    if ni_l is not None and capex_l is not None:
        oe = ni_l + (last(f["da"]) or 0) - abs(capex_l)
    oey = safe_div(oe, f["mcap"])
    checks = [
        ("ROE or ROIC >= 15% every year", None if not clean(yearly) else all(x >= 0.15 for x in clean(yearly))),
        ("gross margin >= 40% and within 5 pts", None if not gm else (min(gm) >= 0.40 and max(gm) - min(gm) <= 0.05)),
        ("LT debt <= 4x net income", None if ni_l is None else (ni_l > 0 and (ltd_l or 0) <= 4 * ni_l)),
        ("capex <= 50% of net income", None if ni_l is None or capex_l is None else (ni_l > 0 and abs(capex_l) <= 0.5 * ni_l)),
        ("positive FCF every year", None if not fcf else all(x > 0 for x in fcf)),
        # ASSUMPTION: "flat" allows up to 1% total growth over the period.
        ("share count flat or shrinking", None if len(sh) < 2 else sh[-1] <= sh[0] * 1.01),
        ("retained earnings growing", None if len(re) < 2 else re[-1] > re[0]),
        ("owner-earnings yield >= 10-yr Treasury", None if oey is None or treasury_10y is None else oey >= treasury_10y),
    ]
    return checklist(checks)


def lens_lynch(f, info):
    peg = num(info.get("trailingPegRatio")) or num(info.get("pegRatio"))
    pe = num(info.get("trailingPE"))
    g = cagr(f["eps"])
    dy = num(info.get("dividendYield"))
    # Yahoo's dividendYield moved from fraction to percent in 2025; normalise to percent.
    if dy is not None and dy < 0.5:
        dy *= 100
    de = num(info.get("debtToEquity"))
    cash, debt = last(f["cash"]), last(f["total_debt"])
    net_cash = cash - (debt or 0) if cash is not None else None
    inv_g, rev_g = cagr(f["inv"]), cagr(f["rev"])
    checks = [
        ("PEG <= 1", None if peg is None else 0 < peg <= 1),
        ("(growth + yield) / P/E >= 1.5", None if g is None or not pe or pe <= 0 else (g * 100 + (dy or 0)) / pe >= 1.5),
        ("debt-to-equity <= 35%", None if de is None else de <= 35),
        ("net cash >= 10% of market cap", None if net_cash is None or not f["mcap"] else net_cash >= 0.10 * f["mcap"]),
        ("inventory growing no faster than sales", None if inv_g is None or rev_g is None else inv_g <= rev_g),
        ("P/E <= 40", None if pe is None else 0 < pe <= 40),
    ]
    return checklist(checks)


def piotroski(f):
    """F-score 0-9 from the latest two years, or None if too little data."""
    def two(xs):
        return (xs[-2], xs[-1]) if len(xs) >= 2 else (None, None)
    ni, ta, ocf = f["ni"], f["ta"], f["ocf"]
    roa = ratio_series(ni, ta)
    lev = ratio_series(f["ltd"] or f["total_debt"], ta)
    cr = ratio_series(f["ca"], f["cl"])
    gm = ratio_series(f["gp"], f["rev"])
    at = ratio_series(f["rev"], ta)
    sh = f["shares"]
    r0, r1 = two(roa); l0, l1 = two(lev); c0, c1 = two(cr)
    g0, g1 = two(gm); a0, a1 = two(at); s0, s1 = two(sh)
    ni_l, ocf_l = last(ni), last(ocf)

    def cmp(a, b, better):
        if a is None or b is None:
            return None
        return b > a if better == "up" else b < a

    checks = [
        ("ROA > 0", None if last(roa) is None else last(roa) > 0),
        ("operating cash flow > 0", None if ocf_l is None else ocf_l > 0),
        ("ROA improving", cmp(r0, r1, "up")),
        ("cash flow > net income", None if ocf_l is None or ni_l is None else ocf_l > ni_l),
        ("leverage falling", None if l0 is None or l1 is None else l1 <= l0),
        ("current ratio rising", cmp(c0, c1, "up")),
        ("no new shares", None if s0 is None or s1 is None else s1 <= s0),
        ("gross margin improving", cmp(g0, g1, "up")),
        ("asset turnover improving", cmp(a0, a1, "up")),
    ]
    done = [v for _, v in checks if v is not None]
    if len(done) < 5:
        return None, checks
    # Not-computable checks count as failed so the score stays on the 0-9 scale.
    return sum(1 for _, v in checks if v), checks


def lens_fisher(f, info):
    rg, eg = cagr(f["rev"]), cagr(f["eps"])
    om = clean(ratio_series(f["opinc"], f["rev"]))
    rnd = safe_div(last(f["rnd"]), last(f["rev"]))
    sh = clean(f["shares"])
    dil = None
    if len(sh) >= 2 and sh[0] > 0:
        dil = (sh[-1] / sh[0]) ** (1 / (len(sh) - 1)) - 1
    checks = [
        ("sales growth >= 10%/yr", None if rg is None else rg >= 0.10),
        ("op margin >= 15% and expanding", None if len(om) < 2 else (om[-1] >= 0.15 and om[-1] > om[0])),
        ("R&D >= 3% of sales", None if not f["rev"] else (rnd is not None and rnd >= 0.03)),
        ("dilution < 2%/yr", None if dil is None else dil < 0.02),
        ("EPS growth >= 10%/yr", None if eg is None else eg >= 0.10),
    ]
    return checklist(checks)


def lens_templeton(f, info, pe_median):
    p = f["price"]
    hi, lo = num(info.get("fiftyTwoWeekHigh")), num(info.get("fiftyTwoWeekLow"))
    mean_t = num(info.get("targetMeanPrice"))
    pe = num(info.get("trailingPE"))
    teps = num(info.get("trailingEps"))
    checks = [
        ("price >= 30% below 52-wk high", None if not (p and hi) else p <= hi * 0.70),
        ("price within 20% of 52-wk low", None if not (p and lo) else p <= lo * 1.20),
        ("mean target >= 25% above price", None if not (p and mean_t) else mean_t >= p * 1.25),
        ("still profitable", None if teps is None and last(f["ni"]) is None else ((teps if teps is not None else last(f["ni"])) > 0)),
        ("P/E below universe median", None if pe is None or pe_median is None else 0 < pe < pe_median),
    ]
    return checklist(checks)


DCF = {"years": 10, "g_cap": 0.12, "g_end": 0.03, "r": 0.10, "g_term": 0.025}


def intrinsic_value(f, info):
    """Per-share intrinsic value, or None."""
    sh = f["sh_now"]
    if not sh:
        return None
    if f["is_fin"]:
        # Justified P/B = (ROE - g) / (r - g). ASSUMPTION: ROE capped at 25%.
        bvps = num(info.get("bookValue"))
        roe = num(info.get("returnOnEquity"))
        if not bvps or roe is None or bvps <= 0:
            return None
        roe = min(roe, 0.25)
        return bvps * max(0.0, (roe - DCF["g_term"]) / (DCF["r"] - DCF["g_term"]))
    fcf = clean(f["fcf"])[-3:]
    if not fcf:
        return None
    base = sum(fcf) / len(fcf)
    if base <= 0:
        return 0.0
    gs = clean([cagr(f["rev"]), cagr(f["eps"])])
    g0 = min(gs + [DCF["g_cap"]]) if gs else DCF["g_end"]
    pv, cash_flow = 0.0, base
    n = DCF["years"]
    for t in range(1, n + 1):
        g = g0 + (DCF["g_end"] - g0) * (t - 1) / (n - 1)  # linear fade to 3%
        cash_flow *= 1 + g
        pv += cash_flow / (1 + DCF["r"]) ** t
    term = cash_flow * (1 + DCF["g_term"]) / (DCF["r"] - DCF["g_term"])
    pv += term / (1 + DCF["r"]) ** n
    # ASSUMPTION: equity value = DCF value + cash - total debt.
    pv += (last(f["cash"]) or 0) - (last(f["total_debt"]) or 0)
    return max(0.0, pv / sh)


def lens_margin_of_safety(f, info):
    iv = intrinsic_value(f, info)
    p = f["price"]
    if iv is None or not p:
        return None, [("intrinsic value", None)]
    disc = 1 - p / iv if iv > 0 else -1.0
    score = 100.0 * lin(disc, 0.0, 0.50)
    return score, [("intrinsic value / share", round(iv, 2)), ("discount", round(disc * 100, 1))]


def altman_z(f):
    ta = last(f["ta"])
    if not ta:
        return None
    ca, cl = last(f["ca"]), last(f["cl"])
    wc = ca - cl if ca is not None and cl is not None else None
    re, ebit, rev, tl = last(f["re"]), last(f["ebit"]), last(f["rev"]), last(f["tl"])
    if None in (wc, re, ebit, rev) or not tl or not f["mcap"]:
        return None
    return 1.2 * wc / ta + 1.4 * re / ta + 3.3 * ebit / ta + 0.6 * f["mcap"] / tl + 1.0 * rev / ta


def risk_flags(f, fscore):
    flags = []
    if not f["is_fin"]:  # ASSUMPTION: Altman Z skipped for financials (it isn't built for banks)
        z = altman_z(f)
        if z is not None and z < 1.8:
            flags.append(f"Altman Z {z:.2f}")
    ebit, ie = last(f["ebit"]), last(f["int_exp"])
    if ebit is not None and ie:
        cover = ebit / abs(ie)
        if cover < 3:
            flags.append(f"interest cover {cover:.1f}x")
    if fscore is not None and fscore <= 3:
        flags.append(f"Piotroski {fscore}")
    fcf = last(f["fcf"])
    if fcf is not None and fcf < 0:
        flags.append("negative FCF")
    eq = last(f["eq"])
    if eq is not None and eq < 0:
        flags.append("negative equity")
    return flags


LENS_WEIGHTS = {
    "Graham": 15, "Buffett": 20, "Greenblatt": 15, "Lynch": 10,
    "Piotroski": 10, "Fisher": 10, "Templeton": 5, "MarginOfSafety": 15,
}
# "Pulled slightly toward 50". ASSUMPTION: keep 85% of the distance from 50.
SHRINK = 0.85
CONSENSUS_MIN = 70


def greenblatt_scores(fund_by_ticker):
    """Magic Formula percentile (0-100, 100 best) across the universe."""
    rows = []
    for t, f in fund_by_ticker.items():
        if f["sector"] in FINANCIAL_SECTORS | UTILITY_SECTORS:
            continue
        ebit = last(f["ebit"])
        ca, cl, ppe = last(f["ca"]), last(f["cl"]), last(f["ppe"])
        cap = None
        if ca is not None and cl is not None and ppe is not None:
            cap = (ca - cl) + ppe
        ey = safe_div(ebit, f["ev"]) if f["ev"] and f["ev"] > 0 else None
        roc = safe_div(ebit, cap) if cap and cap > 0 else None
        if ey is None or roc is None:
            continue
        rows.append((t, ey, roc))
    n = len(rows)
    if n < 2:
        return {}
    ey_rank = {t: r for r, (t, _, _) in enumerate(sorted(rows, key=lambda x: -x[1]))}
    roc_rank = {t: r for r, (t, _, _) in enumerate(sorted(rows, key=lambda x: -x[2]))}
    combined = sorted(rows, key=lambda x: ey_rank[x[0]] + roc_rank[x[0]])
    return {t: 100.0 * (1 - i / (n - 1)) for i, (t, _, _) in enumerate(combined)}


def masters(stocks, treasury_10y):
    """stocks: {ticker: s}. Returns {ticker: result dict}."""
    fund = {t: fundamentals(s) for t, s in stocks.items()}
    pes = [num(s["info"].get("trailingPE")) for s in stocks.values()]
    pe_median = median([p for p in pes if p is not None and p > 0]) if any(p and p > 0 for p in pes) else None
    gb = greenblatt_scores(fund)
    out = {}
    for t, s in stocks.items():
        f, info = fund[t], s["info"]
        fscore, pio_checks = piotroski(f)
        lenses = {
            "Graham": lens_graham(f, info),
            "Buffett": lens_buffett(f, info, treasury_10y),
            "Greenblatt": (gb.get(t), []),
            "Lynch": lens_lynch(f, info),
            "Piotroski": (None if fscore is None else fscore / 9 * 100, pio_checks),
            "Fisher": lens_fisher(f, info),
            "Templeton": lens_templeton(f, info, pe_median),
            "MarginOfSafety": lens_margin_of_safety(f, info),
        }
        raw = {k: (None if v[0] is None else round(v[0], 1)) for k, v in lenses.items()}
        num_, den = 0.0, 0.0
        for k, v in raw.items():
            if v is None:
                continue  # lens not computable: left out, weights rescaled
            num_ += LENS_WEIGHTS[k] * (50 + SHRINK * (v - 50))
            den += LENS_WEIGHTS[k]
        score = round(num_ / den, 1) if den else None
        out[t] = {
            "masters_score": score,
            "lenses": raw,
            "consensus": [k for k, v in raw.items() if v is not None and v >= CONSENSUS_MIN],
            "flags": risk_flags(f, fscore),
            "piotroski": fscore,
            "checks": {k: [[n, v] for n, v in lv[1]] for k, lv in lenses.items()},
        }
    return out, pe_median

# ---------------------------------------------------------------- verdicts

def verdict(gate_ok, is_below_entry, m):
    if not (gate_ok and is_below_entry):
        return None
    if m and m["flags"]:
        return "ADD-REVIEW (flagged)"
    if m and len(m["consensus"]) >= 4:
        return "ADD-REVIEW (masters-confirmed)"
    return "ADD-REVIEW"


def run_screens(stocks, treasury_10y, insider=None):
    """Full pipeline. stocks: {ticker: {"info", "inc", "bal", "cf"}}."""
    insider = insider or {}
    m_all, pe_median = masters(stocks, treasury_10y)
    rows = []
    for t, s in stocks.items():
        info = s["info"]
        ok, why = quality_gate(info)
        vs, inputs, parts = value_score(info) if ok else (None, value_inputs(info), {})
        be = ok and below_entry(info)
        m = m_all[t]
        rows.append({
            "ticker": t,
            "name": info.get("shortName") or info.get("longName") or t,
            "sector": info.get("sector") or "",
            "price": num(info.get("currentPrice")) or num(info.get("regularMarketPrice")),
            "target_low": num(info.get("targetLowPrice")),
            "target_mean": num(info.get("targetMeanPrice")),
            "gate": ok,
            "gate_fail": why,
            "value_score": None if vs is None else round(vs, 1),
            "value_inputs": {k: (None if v is None else round(v, 2)) for k, v in inputs.items()},
            "value_points": parts,
            "below_entry": be,
            "masters_score": m["masters_score"],
            "lenses": m["lenses"],
            "consensus": m["consensus"],
            "flags": m["flags"],
            "piotroski": m["piotroski"],
            "checks": m["checks"],
            "insider": insider.get(t),
            "verdict": verdict(ok, be, m),
        })
    return rows, pe_median
