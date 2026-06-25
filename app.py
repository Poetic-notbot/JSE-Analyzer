"""
Jamaican Stock Financial Statement Analyzer
===========================================

A Streamlit app for analysing companies on the Jamaica Stock Exchange (JSE).

What it does
------------
Pick a company, then drill into ANY line item on its Income Statement, Balance
Sheet or Cash Flow Statement to see:
  * how the item has changed over the last several years (trend chart),
  * the year-over-year growth (growth chart),
  * its share of the relevant total (e.g. a balance-sheet item as a % of Total
    Assets), so you can see the *makeup* of the company and how it is shifting,
  * a plain-language read of WHAT changed and, for subtotals like "Total
    Assets", WHICH underlying components drove the change,
  * the financial ratios that the chosen item feeds into.
There is also a Valuation tab (a simple 2-stage discounted-cash-flow estimate
plus an earnings-multiple cross-check).

Why this version exists
-----------------------
The original (2025) version scraped HTML tables from stockanalysis.com. The site
has since been rebuilt and the data now lives in a JSON payload behind each page
(`__data.json`), so the old HTML scraper no longer works. This version reads
that JSON feed instead, which is far more reliable. Everything else — the idea,
the drill-down, the narrative, the valuation — is preserved.

Run it locally with:
    pip install -r requirements.txt
    streamlit run app.py
"""

import json
import time
import urllib.request
import urllib.error

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# 1. DATA LAYER  — read financial statements from stockanalysis.com's JSON feed
# ---------------------------------------------------------------------------
# The site is built with SvelteKit. Every financials page has a sibling URL
# ending in "/__data.json" that returns the same numbers as structured data.
# That JSON uses a compact "devalue" encoding where integers are *pointers* into
# a flat array (and -1 means "missing"). resolve() below turns it back into
# ordinary nested data.

BASE = "https://stockanalysis.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
}

# Each financial statement and the URL segment it lives under.
STATEMENTS = {
    "Income Statement": "",                       # /financials/
    "Balance Sheet": "balance-sheet/",            # /financials/balance-sheet/
    "Cash Flow": "cash-flow-statement/",          # /financials/cash-flow-statement/
    "Ratios": "ratios/",                          # /financials/ratios/
}

# The "primary total" of each statement. Drill-down components are measured as a
# share of this line, and it anchors the makeup view.
PRIMARY_TOTAL = {
    "Income Statement": ("revenue", "Revenue"),
    "Balance Sheet": ("assets", "Total Assets"),
    "Cash Flow": ("ncfo", "Operating Cash Flow"),
    "Ratios": (None, None),
}

# Companies that report in USD on the site (values must be converted to JMD so
# per-share and valuation figures make sense). This is a small curated list; if a
# company is missing it is simply treated as already-JMD.
USD_REPORTERS = {
    "TJH", "GHL", "PROVEN", "SGJ", "SCIUSD", "FIRSTROCKUSD",
    "MASSY", "SRFUSD", "SILUS", "PULS", "TJHUSD", "SELECTMD",
}
DEFAULT_USD_JMD = 158.0  # rough fallback rate; only used for USD reporters


def _fetch(url, retries=4, delay=1.4):
    """Download a URL's text, retrying politely on transient failures."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(delay * (attempt + 1))
        except Exception:
            time.sleep(delay * (attempt + 1))
    return None


def _resolve(flat):
    """Expand stockanalysis.com's index-pointer ('devalue') array into real data."""
    cache = {}

    def walk(node):
        if not isinstance(node, int):
            return node
        if node < 0:           # -1 is the site's marker for "no value"
            return None
        if node in cache:
            return cache[node]
        value = flat[node]
        if isinstance(value, list):
            out = []
            cache[node] = out
            out.extend(walk(item) for item in value)
            return out
        if isinstance(value, dict):
            out = {}
            cache[node] = out
            for key, item in value.items():
                out[key] = walk(item)
            return out
        cache[node] = value
        return value

    return walk(0)


@st.cache_data(show_spinner=False, ttl=60 * 60 * 6)
def get_statement(ticker, statement):
    """
    Return one financial statement for a ticker as a tidy table.

    Output: (dataframe, aggregates, currency)
      * dataframe : rows = line-item names, columns = fiscal years
                    (oldest -> newest, left to right). Annual figures only.
      * aggregates: the set of row names that are subtotals/totals
                    (e.g. "Total Assets") rather than raw components.
      * currency  : "JMD" or "USD" as reported.
    Returns (None, None, None) if the statement could not be loaded.
    """
    seg = STATEMENTS[statement]
    url = f"{BASE}/quote/jmse/{ticker}/financials/{seg}__data.json"
    raw = _fetch(url)
    if raw is None:
        return None, None, None
    try:
        obj = json.loads(raw)
    except Exception:
        return None, None, None

    # Find the node that actually carries the financial data.
    node = None
    for n in obj.get("nodes", []):
        if isinstance(n, dict) and isinstance(n.get("data"), list):
            resolved = _resolve(n["data"])
            if isinstance(resolved, dict) and isinstance(resolved.get("financialData"), dict):
                node = resolved
                break
    if node is None:
        return None, None, None

    fdata = node["financialData"]
    layout = node.get("map", [])           # ordered list of {id, title, class}
    details = node.get("details", {}) or {}
    currency = "JMD"
    curr = details.get("currency") or node.get("curr", {})
    if isinstance(curr, dict):
        currency = curr.get("financial") or curr.get("main") or "JMD"
    if ticker.upper() in USD_REPORTERS:
        currency = "USD"

    fiscal_years = fdata.get("fiscalYear") or fdata.get("datekey") or []

    # De-duplicate restatement columns that repeat a fiscal year, and drop TTM
    # so trends are clean annual figures.
    keep_cols, seen_years = [], set()
    for i, yr in enumerate(fiscal_years):
        if str(yr).upper() == "TTM":
            continue
        if yr in seen_years:
            continue
        seen_years.add(yr)
        keep_cols.append(i)
    keep_cols = keep_cols[:6]                       # most recent 6 years max
    year_labels = [str(fiscal_years[i]) for i in keep_cols][::-1]  # oldest->newest

    rows, aggregates = {}, set()
    for entry in layout:
        mid = entry.get("id")
        title = entry.get("title")
        if not mid or not title or mid not in fdata:
            continue
        fmt = entry.get("format", "")
        if fmt == "growth":                          # site's own % rows; we compute our own
            continue
        arr = fdata.get(mid) or []
        values = []
        for i in keep_cols:
            v = arr[i] if i < len(arr) else None
            values.append(v)
        values = values[::-1]                        # align oldest->newest
        if all(v is None for v in values):
            continue
        rows[title] = values
        cls = entry.get("class", "") or ""
        # Subtotals are bold/bordered, but exclude ratio-style rows (margins,
        # per-share, growth) which are also styled but are not "makeup" totals.
        if ("bold" in cls or "border" in cls) and fmt not in ("pershare", "margin", "growth"):
            aggregates.add(title)

    if not rows:
        return None, None, None

    df = pd.DataFrame(rows, index=year_labels).T      # rows=line items, cols=years
    df = df.apply(pd.to_numeric, errors="coerce")
    return df, aggregates, currency


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def get_companies():
    """Return {ticker: name} for all JSE companies, with a fallback list."""
    url = f"{BASE}/list/jamaica-stock-exchange/__data.json"
    raw = _fetch(url)
    out = {}
    if raw:
        try:
            obj = json.loads(raw)
            for n in obj.get("nodes", []):
                if isinstance(n, dict) and isinstance(n.get("data"), list):
                    resolved = _resolve(n["data"])
                    data = resolved.get("data") if isinstance(resolved, dict) else None
                    rowset = None
                    if isinstance(data, dict):
                        rowset = data.get("data") or data.get("stockData")
                    if isinstance(resolved, dict) and not rowset:
                        rowset = resolved.get("stockData")
                    if isinstance(rowset, list):
                        for row in rowset:
                            if not isinstance(row, dict):
                                continue
                            sym = (row.get("s") or "").split("/")[-1].upper()
                            name = row.get("n") or sym
                            if sym:
                                out[sym] = name
        except Exception:
            pass
    if not out:
        out = {t: t for t in FALLBACK_TICKERS}
    return dict(sorted(out.items()))


# Used only if the live company list cannot be fetched.
FALLBACK_TICKERS = [
    "NCBFG", "JMMBGL", "SGJ", "GHL", "PROVEN", "SJ", "BNS", "JBG", "GK", "WIG",
    "CAR", "DOLLA", "LASD", "LASM", "LASF", "SVL", "FOSRICH", "WISYNCO", "MASSY",
    "PJAM", "KW", "138SL", "MJE", "JP", "SEP", "CPJ", "HONBUN", "PURITY", "SALF",
    "SCIJA", "TJH", "MDS", "CABROKERS", "BIL", "JETCON", "ELITE", "ISP", "AFS",
    "ROC", "KEX", "KLE", "PAL", "PTL", "QWI", "RAWILL", "SOS", "TROPICAL",
]


# ---------------------------------------------------------------------------
# 2. ANALYSIS HELPERS  — components, drivers, growth, narrative, ratios
# ---------------------------------------------------------------------------

def clean_series(df, item):
    """Return one line item as a year->value Series, dropping empty years."""
    if item not in df.index:
        return pd.Series(dtype=float)
    return df.loc[item].dropna()


def pct_change_total(series):
    """Total % change from first to last available year."""
    if len(series) < 2 or series.iloc[0] == 0:
        return None
    return (series.iloc[-1] - series.iloc[0]) / abs(series.iloc[0]) * 100


def cagr(series):
    """Compound annual growth rate over the available years (decimal)."""
    if len(series) < 2 or series.iloc[0] <= 0:
        return None
    years = len(series) - 1
    try:
        return (series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1
    except Exception:
        return None


def components_of(df, item, aggregates):
    """
    For a subtotal (e.g. "Total Assets"), the components are the raw line items
    that appear above it and below the previous subtotal, in statement order.
    Returns a list of component row names.
    """
    if item not in df.index:
        return []
    order = list(df.index)
    idx = order.index(item)
    comps = []
    for name in reversed(order[:idx]):           # walk upward from the subtotal
        if name in aggregates:                   # stop at the previous subtotal
            break
        comps.append(name)
    return list(reversed(comps))


def largest_mover(df, names):
    """Among the given rows, find the one with the biggest absolute change."""
    best, best_abs = None, -1
    for name in names:
        s = clean_series(df, name)
        if len(s) < 2:
            continue
        change = s.iloc[-1] - s.iloc[0]
        if abs(change) > best_abs:
            best, best_abs = name, abs(change)
            best_change = change
            best_pct = pct_change_total(s)
    if best is None:
        return None
    return {"name": best, "change": best_change, "pct": best_pct}


def fmt_money(value, currency="JMD"):
    """Human-friendly money formatting (values arrive in the reported units)."""
    if value is None or pd.isna(value):
        return "n/a"
    sign = "-" if value < 0 else ""
    v = abs(value)
    for unit, label in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= unit:
            return f"{sign}{currency} {v / unit:,.2f}{label}"
    return f"{sign}{currency} {v:,.0f}"


def fmt_money_compact(value, currency="JMD"):
    """Like fmt_money but tuned to fit in a narrow metric card: keeps the
    B/M/K abbreviation, drops unnecessary decimals (so 6.10B -> '6.1B',
    43.0 -> '43')."""
    if value is None or pd.isna(value):
        return "n/a"
    sign = "-" if value < 0 else ""
    v = abs(value)
    for unit, label in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= unit:
            num = f"{v / unit:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{currency} {num}{label}"
    return f"{sign}{currency} {v:,.0f}"


def build_narrative(df, item, statement, aggregates, currency):
    """Plain-language read of how an item changed and what drove it."""
    s = clean_series(df, item)
    if len(s) < 2:
        return "Not enough years of data to describe a trend for this item."

    total = pct_change_total(s)
    growth = cagr(s)
    direction = "increased" if s.iloc[-1] > s.iloc[0] else "decreased"
    span = f"{s.index[0]}\u2013{s.index[-1]}"      # en-dash between years

    lines = []
    if total is not None:
        cagr_txt = f", about {growth * 100:.1f}% a year" if growth is not None else ""
        lines.append(
            f"**{item}** {direction} by {abs(total):.1f}% over {span} "
            f"({fmt_money(s.iloc[0], currency)} \u2192 {fmt_money(s.iloc[-1], currency)}){cagr_txt}."
        )

    if item in aggregates:
        comps = components_of(df, item, aggregates)
        mover = largest_mover(df, comps)
        if mover:
            md = "rose" if mover["change"] > 0 else "fell"
            pct_txt = f" ({abs(mover['pct']):.1f}%)" if mover["pct"] is not None else ""
            lines.append(
                f"The biggest driver was **{mover['name']}**, which {md} by "
                f"{fmt_money(abs(mover['change']), currency)}{pct_txt} over the same period."
            )
            lines.append(
                "_This tells you where the change in this total is really coming "
                "from, rather than just that the total moved._"
            )
    else:
        # For a raw item, show how its weight in the statement's primary total shifted.
        key, base_name = PRIMARY_TOTAL.get(statement, (None, None))
        if base_name and base_name in df.index:
            base = clean_series(df, base_name)
            common = s.index.intersection(base.index)
            if len(common) >= 2:
                w0 = s[common[0]] / base[common[0]] * 100 if base[common[0]] else None
                w1 = s[common[-1]] / base[common[-1]] * 100 if base[common[-1]] else None
                if w0 is not None and w1 is not None:
                    shift = "a bigger" if w1 > w0 else "a smaller"
                    lines.append(
                        f"As a share of **{base_name}**, it went from {w0:.1f}% to "
                        f"{w1:.1f}% \u2014 {shift} part of the company over time."
                    )
    return "\n\n".join(lines)


def invested_capital(balance):
    """
    Invested Capital per fiscal year, computed (it is not a line item in the
    data feed) using the excluding-cash definition:

        Total Debt + Total Equity - Cash & Equivalents

    The equity term prefers 'Total Common Equity' and falls back to
    "Shareholders' Equity". The three rows are aligned on the fiscal years they
    have in common; an empty Series is returned if any required row is missing.
    """
    if balance is None:
        return pd.Series(dtype=float)

    def row(*items):
        for it in items:
            if it in balance.index:
                s = clean_series(balance, it)
                if len(s):
                    return s
        return pd.Series(dtype=float)

    debt = row("Total Debt")
    equity = row("Total Common Equity", "Shareholders' Equity")
    cash = row("Cash & Equivalents", "Cash & Cash Equivalents")
    if not len(debt) or not len(equity) or not len(cash):
        return pd.Series(dtype=float)
    common = debt.index.intersection(equity.index).intersection(cash.index)
    if len(common) == 0:
        return pd.Series(dtype=float)
    return (debt[common] + equity[common] - cash[common]).dropna()


def compute_ratios(income, balance, cashflow):
    """A compact set of decision-useful ratios for the latest common year."""
    ratios = []

    def latest(df, item):
        if df is None or item not in df.index:
            return None
        s = clean_series(df, item)
        return s.iloc[-1] if len(s) else None

    rev = latest(income, "Revenue")
    ni = latest(income, "Net Income") or latest(income, "Net Income Common")
    ebit = latest(income, "Operating Income") or latest(income, "EBIT")
    assets = latest(balance, "Total Assets")
    equity = latest(balance, "Shareholders' Equity") or latest(balance, "Total Equity")
    debt = latest(balance, "Total Debt")
    ocf = latest(cashflow, "Operating Cash Flow") if cashflow is not None else None

    def add(name, value, desc):
        if value is not None:
            ratios.append({"name": name, "value": value, "desc": desc})

    if ni and rev:
        add("Net margin", ni / rev * 100, "Profit kept from each dollar of sales (%)")
    if ebit and rev:
        add("Operating margin", ebit / rev * 100, "Operating profit per dollar of sales (%)")
    if ni and assets:
        add("Return on assets", ni / assets * 100, "Profit per dollar of assets (%)")
    if ni and equity:
        add("Return on equity", ni / equity * 100, "Profit per dollar of owners' capital (%)")
    if rev and assets:
        add("Asset turnover", rev / assets, "Sales generated per dollar of assets")
    if debt and equity:
        add("Debt-to-equity", debt / equity, "Borrowings relative to owners' capital")
    if ocf and ni:
        add("Cash conversion", ocf / ni, "Operating cash vs reported profit (>1 is healthy)")
    return ratios


def ratio_timeseries(income, balance, cashflow):
    """
    Per-year values for each headline ratio, aligned across fiscal years.

    Returns {name: {"series": Series, "unit": "%"|"x"}}. Mirrors the definitions
    in compute_ratios so the trend charts agree with the latest-year numbers.
    """
    def series_of(df, *items):
        if df is None:
            return pd.Series(dtype=float)
        for it in items:
            if it in df.index:
                s = clean_series(df, it)
                if len(s):
                    return s
        return pd.Series(dtype=float)

    def ratio(num, den, scale=1.0):
        common = num.index.intersection(den.index)
        if len(common) == 0:
            return pd.Series(dtype=float)
        d = den[common]
        return (num[common] / d.where(d != 0) * scale).dropna()

    rev = series_of(income, "Revenue")
    ni = series_of(income, "Net Income", "Net Income Common")
    ebit = series_of(income, "Operating Income", "EBIT")
    assets = series_of(balance, "Total Assets")
    equity = series_of(balance, "Shareholders' Equity", "Total Equity")
    debt = series_of(balance, "Total Debt")
    ocf = series_of(cashflow, "Operating Cash Flow")

    out = {}

    def add(name, series, unit):
        if len(series):
            out[name] = {"series": series, "unit": unit}

    add("Net margin", ratio(ni, rev, 100), "%")
    add("Operating margin", ratio(ebit, rev, 100), "%")
    add("Return on assets", ratio(ni, assets, 100), "%")
    add("Return on equity", ratio(ni, equity, 100), "%")
    add("Asset turnover", ratio(rev, assets), "x")
    add("Debt-to-equity", ratio(debt, equity), "x")
    add("Cash conversion", ratio(ocf, ni), "x")
    return out


def extra_ratio_timeseries(income, balance, cashflow):
    """
    Additional high-signal ratios as per-year series, aligned across fiscal
    years. Returns an ordered list of {name, series, unit, desc}. A ratio whose
    source line items are unavailable (missing rows, None, or no overlapping
    years) is simply omitted, so callers can render whatever is present.
    """
    def series_of(df, *items):
        if df is None:
            return pd.Series(dtype=float)
        for it in items:
            if it in df.index:
                s = clean_series(df, it)
                if len(s):
                    return s
        return pd.Series(dtype=float)

    def ratio(num, den, scale=1.0):
        if not len(num) or not len(den):
            return pd.Series(dtype=float)
        common = num.index.intersection(den.index)
        if len(common) == 0:
            return pd.Series(dtype=float)
        d = den[common]
        return (num[common] / d.where(d != 0) * scale).dropna()

    ic = invested_capital(balance)
    fcf = series_of(cashflow, "Free Cash Flow")
    ebit = series_of(income, "Operating Income", "EBIT")
    rev = series_of(income, "Revenue")
    gross = series_of(income, "Gross Profit")
    cur_assets = series_of(balance, "Total Current Assets")
    cur_liab = series_of(balance, "Total Current Liabilities")
    interest = series_of(income, "Interest Expense", "Interest Expense / Income")

    out = []

    def add(name, series, unit, desc):
        if len(series) > 1:                      # need at least two years for a trend
            out.append({"name": name, "series": series, "unit": unit, "desc": desc})

    add("CROIC", ratio(fcf, ic, 100), "%",
        "Free cash flow as a % of invested capital")
    add("ROIC", ratio(ebit, ic, 100), "%",
        "Operating income as a % of invested capital")
    add("Current ratio", ratio(cur_assets, cur_liab), "x",
        "Current assets vs current liabilities")
    add("Interest coverage", ratio(ebit, interest), "x",
        "Operating income vs interest expense")
    add("Gross margin", ratio(gross, rev, 100), "%",
        "Gross profit per dollar of sales")
    return out


# Well-known additive subtotal row NAMES per statement (structural identifiers,
# not values). Used only to RECOGNISE subtotals the feed may not have flagged;
# the actual parents offered for a ticker are still taken from that statement's
# own dataframe (a name here that the ticker doesn't report is ignored).
KNOWN_SUBTOTALS = {
    "Income Statement": [
        "Revenue", "Total Revenue", "Gross Profit",
        "Operating Income", "Operating Profit", "Operating Expenses",
        "Total Operating Expenses", "Pretax Income", "Pre-Tax Income",
        "Net Income", "Net Income Common",
        # financial-sector subtotals
        "Net Interest Income", "Total Interest Income", "Total Interest Expense",
        "Revenue After Provisions", "Net Revenue After Provisions",
        "Net Premiums Earned", "Total Premiums Earned",
    ],
    "Balance Sheet": [
        "Total Assets", "Total Current Assets", "Total Non-Current Assets",
        "Total Liabilities", "Total Current Liabilities",
        "Total Non-Current Liabilities", "Total Equity", "Shareholders' Equity",
        "Total Common Equity", "Total Liabilities & Equity",
        "Total Liabilities and Equity",
    ],
    "Cash Flow": [
        "Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow",
        "Net Cash Flow", "Net Change in Cash",
    ],
}


def decomposition_children(df, parent, aggregates):
    """
    Components of a parent subtotal, derived from the statement's own ordering
    and the `aggregates` set (an extension of components_of that handles nested
    subtotals). Walking upward from the parent: a raw row is a component; a
    nested subtotal counts as ONE component and its own sub-block is skipped, so
    the returned components partition the parent. Returns names in statement
    order (top -> bottom).
    """
    if df is None or parent not in df.index:
        return []
    order = list(df.index)
    idx = order.index(parent)
    children = []
    i = idx - 1
    while i >= 0:
        name = order[i]
        if name in aggregates:
            children.append(name)                # nested subtotal as one block
            j = i - 1
            while j >= 0 and order[j] not in aggregates:
                j -= 1                            # skip the rows it rolls up
            i = j
        else:
            children.append(name)
            i -= 1
    return list(reversed(children))


def decomposable_parents(df, aggregates, statement):
    """
    Parent metrics offered for decomposition, sourced ONLY from THIS statement's
    own dataframe. A row qualifies if it is a subtotal — either flagged by the
    feed (`aggregates`) or a well-known additive subtotal name for `statement`
    that the ticker actually reports — and it has >=2 decomposable components.

    Returns (parents_in_statement_order, effective_aggregates). The effective
    aggregates (feed flags unioned with recognised subtotal names present in the
    data) are returned so the caller decomposes children against the same set.
    """
    if df is None:
        return [], set()
    known = {n for n in KNOWN_SUBTOTALS.get(statement, []) if n in df.index}
    eff_aggs = set(aggregates) | known
    parents = [name for name in df.index                       # statement order
               if name in eff_aggs
               and len(decomposition_children(df, name, eff_aggs)) >= 2]
    return parents, eff_aggs


def decompose(df, parent, aggregates, sig_frac=0.05, max_bars=12):
    """
    Decompose a parent subtotal's change over its full available window into
    component contributions, using only the company's reported data.

    Significance is gated on the LARGER of |parent change| and the gross
    component movement (sum of |component change|), so large offsetting
    components that net near zero still surface instead of collapsing into
    'Other'. Returns a dict, or None if the parent lacks two years of data.
    """
    if df is None or parent not in df.index:
        return None
    p = clean_series(df, parent)
    if len(p) < 2:
        return None
    y0, yN = p.index[0], p.index[-1]
    p0, pN = float(p.iloc[0]), float(p.iloc[-1])
    parent_delta = pN - p0

    components = []
    for name in decomposition_children(df, parent, aggregates):
        cs = clean_series(df, name)
        first = float(cs[y0]) if y0 in cs.index else None
        last = float(cs[yN]) if yN in cs.index else None
        delta = (last - first) if (first is not None and last is not None) else None
        contrib = (delta / parent_delta * 100) if (delta is not None and parent_delta) else None
        share0 = (first / p0 * 100) if (first is not None and p0) else None
        shareN = (last / pN * 100) if (last is not None and pN) else None
        components.append({"name": name, "first": first, "last": last, "delta": delta,
                           "cagr": cagr(cs), "contrib_pct": contrib,
                           "share0": share0, "shareN": shareN})

    deltas = [c["delta"] for c in components if c["delta"] is not None]
    explained = sum(deltas)
    unexplained = parent_delta - explained
    gross = sum(abs(x) for x in deltas)
    threshold = sig_frac * max(abs(parent_delta), gross)

    significant = [c for c in components
                   if c["delta"] is not None and threshold > 0
                   and abs(c["delta"]) >= threshold]
    # Cap the number of waterfall bars for readability; any dropped components
    # fold into 'Other / unexplained' automatically (it balances to the total).
    if len(significant) > max_bars:
        keep = sorted(significant, key=lambda c: abs(c["delta"]), reverse=True)[:max_bars]
        kept = {id(c) for c in keep}
        significant = [c for c in significant if id(c) in kept]

    best = None
    for c in components:
        if c["delta"] is None:
            continue
        if best is None or abs(c["delta"]) > abs(best["delta"]):
            best = c
    driver = None
    if best is not None:
        driver = {"name": best["name"], "delta": best["delta"],
                  "pct": pct_change_total(clean_series(df, best["name"]))}

    leader = None
    for c in components:
        if c["share0"] is None or c["shareN"] is None:
            continue
        shift = abs(c["shareN"] - c["share0"])
        if leader is None or shift > leader["_shift"]:
            leader = {"name": c["name"], "from_pct": c["share0"],
                      "to_pct": c["shareN"], "_shift": shift}
    mix_shift_leader = ({"name": leader["name"], "from_pct": leader["from_pct"],
                         "to_pct": leader["to_pct"]} if leader else None)

    return {"parent": parent, "year0": y0, "yearN": yN, "span": f"{y0}–{yN}",
            "p0": p0, "pN": pN, "parent_delta": parent_delta,
            "parent_cagr": cagr(p), "parent_pct": pct_change_total(p),
            "components": components, "significant": significant,
            "explained": explained, "unexplained": unexplained,
            "driver": driver, "mix_shift_leader": mix_shift_leader}


def decomposition_narrative(d, currency):
    """A grounded 2-4 sentence read of a decomposition, from the data only."""
    if d is None or d["parent_delta"] is None:
        return "Not enough data to decompose this metric."
    p = d["parent"]
    pd_delta = d["parent_delta"]
    direction = "rose" if pd_delta > 0 else ("fell" if pd_delta < 0 else "was roughly flat")
    pct_txt = f" ({abs(d['parent_pct']):.1f}%)" if d["parent_pct"] is not None else ""
    cagr_txt = (f", about {d['parent_cagr'] * 100:.1f}% a year"
                if d["parent_cagr"] is not None else "")
    lines = [
        f"**{p}** {direction} by {fmt_money(abs(pd_delta), currency)}{pct_txt} over "
        f"{d['span']} ({fmt_money(d['p0'], currency)} → {fmt_money(d['pN'], currency)}){cagr_txt}."
    ]
    drv = d["driver"]
    if drv and drv["delta"] is not None:
        moved = "rose" if drv["delta"] > 0 else "fell"
        dpct = f" ({abs(drv['pct']):.1f}%)" if drv["pct"] is not None else ""
        share = (f", about {abs(drv['delta'] / pd_delta * 100):.0f}% of the net change"
                 if pd_delta else "")
        lines.append(f"The single biggest driver was **{drv['name']}**, which {moved} by "
                     f"{fmt_money(abs(drv['delta']), currency)}{dpct}{share}.")
    ms = d["mix_shift_leader"]
    if ms:
        moved = "a bigger" if ms["to_pct"] > ms["from_pct"] else "a smaller"
        lines.append(f"In the mix, **{ms['name']}** became {moved} share of {p}, moving from "
                     f"{ms['from_pct']:.1f}% to {ms['to_pct']:.1f}%.")
    if pd_delta:
        exp_pct = d["explained"] / pd_delta * 100
        if abs(d["unexplained"]) > 0.05 * abs(pd_delta):
            lines.append(f"Identified components account for {fmt_money(d['explained'], currency)} "
                         f"of the change ({exp_pct:.0f}%); {fmt_money(d['unexplained'], currency)} is "
                         f"unexplained — check which line items the feed groups differently before "
                         f"relying on this split.")
        else:
            lines.append("Identified components reconcile closely to the total change, so the "
                         "breakdown captures essentially all of the movement; next, check whether "
                         "the biggest driver's trend is likely to persist.")
    return " ".join(lines)


def estimate_valuation(income, balance, cashflow, shares, price,
                       discount_rate, terminal_growth):
    """
    Two-stage discounted-cash-flow estimate plus an earnings-multiple cross-check.
    Returns a dict of results (any field may be None if inputs are missing).
    """
    out = {}

    def latest(df, *items):
        for it in items:
            if df is not None and it in df.index:
                s = clean_series(df, it)
                if len(s):
                    return s
        return pd.Series(dtype=float)

    fcf_series = latest(cashflow, "Free Cash Flow")
    if len(fcf_series) < 2:
        # Fall back to operating cash flow minus capex if FCF row is absent.
        ocf = latest(cashflow, "Operating Cash Flow")
        capex = latest(cashflow, "Capital Expenditures")
        if len(ocf) and len(capex):
            common = ocf.index.intersection(capex.index)
            fcf_series = (ocf[common] + capex[common]).dropna()  # capex is negative

    debt = latest(balance, "Total Debt")
    cash = latest(balance, "Cash & Equivalents", "Cash & Cash Equivalents")
    debt_v = debt.iloc[-1] if len(debt) else 0
    cash_v = cash.iloc[-1] if len(cash) else 0

    if len(fcf_series) >= 2 and fcf_series.iloc[-1] > 0 and shares:
        base_fcf = fcf_series.iloc[-1]
        g = cagr(fcf_series) or 0.0
        g = max(min(g, 0.20), -0.10)               # keep growth sane
        terminal_growth = min(terminal_growth, discount_rate - 0.01)

        pv = 0.0
        cf = base_fcf
        for yr in range(1, 6):
            cf *= (1 + g)
            pv += cf / ((1 + discount_rate) ** yr)
        terminal = cf * (1 + terminal_growth) / (discount_rate - terminal_growth)
        pv += terminal / ((1 + discount_rate) ** 5)

        equity_value = pv - debt_v + cash_v
        out["dcf_per_share"] = equity_value / shares
        out["fcf_growth_used"] = g

    # Earnings-multiple cross-check using a conservative market P/E.
    ni = latest(income, "Net Income", "Net Income Common")
    if len(ni) and shares:
        eps = ni.iloc[-1] / shares
        out["eps"] = eps
        out["pe_value_12x"] = eps * 12

    out["price"] = price
    return out


# ---------------------------------------------------------------------------
# 3. CHARTS
# ---------------------------------------------------------------------------

NAVY = "#16425B"
GOLD = "#8A6A1E"
GREEN = "#2E6E4E"
RED = "#B14A45"


def bar_chart(series, title, ylab, color=NAVY):
    fig = go.Figure(go.Bar(x=list(series.index), y=list(series.values),
                           marker_color=color))
    fig.update_layout(title=title, xaxis_title="Fiscal year", yaxis_title=ylab,
                      height=340, margin=dict(l=10, r=10, t=50, b=10))
    return fig


def growth_chart(series, title):
    growth = series.pct_change() * 100
    growth = growth.dropna()
    colors = [GREEN if v >= 0 else RED for v in growth.values]
    fig = go.Figure(go.Bar(x=list(growth.index), y=list(growth.values),
                           marker_color=colors))
    fig.update_layout(title=title, xaxis_title="Fiscal year",
                      yaxis_title="Year-over-year change (%)",
                      height=340, margin=dict(l=10, r=10, t=50, b=10))
    return fig


def share_chart(series, base, title):
    common = series.index.intersection(base.index)
    if len(common) < 1:
        return None
    pct = (series[common] / base[common] * 100).dropna()
    fig = go.Figure(go.Scatter(x=list(pct.index), y=list(pct.values),
                               mode="lines+markers", line=dict(color=GOLD, width=3)))
    fig.update_layout(title=title, xaxis_title="Fiscal year",
                      yaxis_title="Share (%)",
                      height=340, margin=dict(l=10, r=10, t=50, b=10))
    return fig


def line_chart(series, title, ylab, color=NAVY):
    fig = go.Figure(go.Scatter(x=list(series.index), y=list(series.values),
                               mode="lines+markers", line=dict(color=color, width=3)))
    fig.update_layout(title=title, xaxis_title="Fiscal year", yaxis_title=ylab,
                      height=340, margin=dict(l=10, r=10, t=50, b=10))
    return fig


# A small, consistent palette for stacked component areas (re-uses the app's
# core colours, then a few muted tones); 'Other' is rendered in grey.
MIX_PALETTE = [NAVY, GOLD, GREEN, RED, "#5B8A72", "#A0522D", "#4C6E91",
               "#9C7BB5", "#B08D57", "#3E7C7B", "#7D5BA6", "#6B7280"]
OTHER_GREY = "#9AA0A6"


def waterfall_chart(d, title, currency):
    """Contribution waterfall: start -> significant components -> Other -> end."""
    x = [f"Start ({d['year0']})"]
    measure = ["absolute"]
    y = [d["p0"]]
    for c in d["significant"]:
        x.append(c["name"])
        measure.append("relative")
        y.append(c["delta"])
    other = d["parent_delta"] - sum(c["delta"] for c in d["significant"])
    x.append("Other / unexplained")
    measure.append("relative")
    y.append(other)
    x.append(f"End ({d['yearN']})")
    measure.append("total")
    y.append(d["pN"])
    fig = go.Figure(go.Waterfall(
        orientation="v", measure=measure, x=x, y=y,
        connector=dict(line=dict(color="#BBBBBB")),
        increasing=dict(marker=dict(color=GREEN)),
        decreasing=dict(marker=dict(color=RED)),
        totals=dict(marker=dict(color=NAVY))))
    fig.update_layout(title=title, yaxis_title=currency, xaxis_title="",
                      height=380, margin=dict(l=10, r=10, t=50, b=10))
    return fig


def mix_shift_chart(df, parent, comp_names, title):
    """100% stacked area: each component's share of the parent over time."""
    p = clean_series(df, parent)
    if len(p) < 2:
        return None
    years = list(p.index)
    fig = go.Figure()
    sig_sum = pd.Series(0.0, index=years)
    for k, name in enumerate(comp_names):
        cs = clean_series(df, name)
        shares = []
        for yr in years:
            pv = p.get(yr)
            cv = cs.get(yr)
            ok = (pv not in (None, 0) and cv is not None
                  and not pd.isna(pv) and not pd.isna(cv))
            shares.append(cv / pv * 100 if ok else None)
        ser = pd.Series(shares, index=years, dtype="float")
        sig_sum = sig_sum.add(ser.fillna(0.0))
        fig.add_trace(go.Scatter(
            x=years, y=list(ser.values), name=name, mode="lines", stackgroup="one",
            line=dict(width=0.5, color=MIX_PALETTE[k % len(MIX_PALETTE)])))
    other = 100.0 - sig_sum                       # remainder + unexplained -> 100%
    fig.add_trace(go.Scatter(
        x=years, y=list(other.values), name="Other / unexplained", mode="lines",
        stackgroup="one", line=dict(width=0.5, color=OTHER_GREY)))
    fig.update_layout(title=title, xaxis_title="Fiscal year",
                      yaxis_title="Share of parent (%)",
                      height=380, margin=dict(l=10, r=10, t=50, b=10))
    return fig


# ---------------------------------------------------------------------------
# 4. USER INTERFACE
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="JSE Financial Analyzer", layout="wide")
    st.title("Jamaican Stock Financial Statement Analyzer")
    st.caption(
        "Pick a company, then drill into any line item to see how the business "
        "is built, what has changed, and what it has delivered. Data: "
        "stockanalysis.com (S&P Global)."
    )

    companies = get_companies()
    labels = [f"{t} \u2014 {n}" for t, n in companies.items()]
    sym_by_label = {f"{t} \u2014 {n}": t for t, n in companies.items()}

    with st.sidebar:
        st.header("Company")
        default = next((l for l in labels if l.startswith("NCBFG")), labels[0])
        chosen = st.selectbox("Select a ticker", labels,
                              index=labels.index(default) if default in labels else 0)
        ticker = sym_by_label[chosen]

        st.header("Valuation assumptions")
        discount = st.slider("Discount rate (%)", 6.0, 20.0, 12.0, 0.5) / 100
        term_g = st.slider("Long-term growth (%)", 0.0, 5.0, 2.0, 0.5) / 100

    # Load the three core statements once.
    income, inc_agg, currency = get_statement(ticker, "Income Statement")
    balance, bal_agg, _ = get_statement(ticker, "Balance Sheet")
    cashflow, cf_agg, _ = get_statement(ticker, "Cash Flow")

    if income is None and balance is None:
        st.error(
            f"No financial data could be loaded for {ticker}. Some JSE listings "
            "(funds, very new listings) are not covered by the data source. Try "
            "another ticker."
        )
        return

    tabs = st.tabs(["Overview", "Income Statement", "Balance Sheet",
                    "Cash Flow", "Decomposition", "Ratios", "Valuation"])

    # ---- Overview ---------------------------------------------------------
    with tabs[0]:
        st.subheader(f"{companies.get(ticker, ticker)} — overview")
        st.write(f"Reporting currency: **{currency}**")
        col1, col2 = st.columns(2)

        # Invested Capital is not provided as a line item, so compute it:
        # Total Debt + Total Equity - Cash & Equivalents.
        ic_s = invested_capital(balance)
        if len(ic_s):
            col1.plotly_chart(bar_chart(ic_s, "Invested Capital", currency),
                              use_container_width=True)

        if cashflow is not None and "Free Cash Flow" in cashflow.index:
            fcf = clean_series(cashflow, "Free Cash Flow")
        else:
            fcf = pd.Series(dtype=float)
        if len(fcf):
            col2.plotly_chart(bar_chart(fcf, "Free Cash Flow", currency, color=GREEN),
                              use_container_width=True)

        # CROIC trend (full width, under the two charts above):
        # Free Cash Flow / Invested Capital per fiscal year, on common years.
        if len(fcf) and len(ic_s):
            common = fcf.index.intersection(ic_s.index)
            denom = ic_s[common]
            croic = (fcf[common] / denom.where(denom != 0) * 100).dropna()
            if len(croic) > 1:
                st.plotly_chart(
                    line_chart(croic, "CROIC (FCF / Invested Capital)", "%"),
                    use_container_width=True)

        ratios = compute_ratios(income, balance, cashflow)
        headline = list(ratios[:4])
        # ROIC (Return on Invested Capital): operating-based definition,
        # Operating Income / Invested Capital.
        op_name = "Operating Income" if (income is not None and "Operating Income" in income.index) else "EBIT"
        op_s = clean_series(income, op_name) if income is not None else pd.Series(dtype=float)
        if len(op_s) and len(ic_s) and ic_s.iloc[-1]:
            headline.append({"name": "ROIC",
                             "value": op_s.iloc[-1] / ic_s.iloc[-1] * 100,
                             "desc": "Operating income as a % of invested capital"})
        if headline:
            st.markdown("**Key ratios (latest year)**")
            # Short labels so all five fit on one row; full name kept in tooltip.
            abbrev = {"Net margin": "NM", "Operating margin": "OM",
                      "Return on assets": "ROA", "Return on equity": "ROE",
                      "ROIC": "ROIC"}
            cols = st.columns(min(5, len(headline)))
            for i, r in enumerate(headline[:5]):
                cols[i].metric(abbrev.get(r["name"], r["name"]), f"{r['value']:.1f}",
                               help=f"{r['name']} — {r.get('desc', '')}")

    # ---- One drill-down tab per statement --------------------------------
    statement_tabs = {
        "Income Statement": (tabs[1], income, inc_agg),
        "Balance Sheet": (tabs[2], balance, bal_agg),
        "Cash Flow": (tabs[3], cashflow, cf_agg),
    }
    for sname, (tab, df, aggs) in statement_tabs.items():
        with tab:
            if df is None:
                st.warning(f"{sname} is not available for this company.")
                continue
            st.subheader(sname)
            item = st.selectbox("Choose a line item to analyse",
                                list(df.index), key=f"sel_{sname}")
            s = clean_series(df, item)

            c1, c2 = st.columns(2)
            c1.plotly_chart(bar_chart(s, f"{item}", currency), use_container_width=True)
            if len(s) > 1:
                c2.plotly_chart(growth_chart(s, f"{item} — yearly growth"),
                                use_container_width=True)

            key, base_name = PRIMARY_TOTAL.get(sname, (None, None))
            if base_name and base_name in df.index and item != base_name:
                base = clean_series(df, base_name)
                fig = share_chart(s, base, f"{item} as a share of {base_name}")
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True)

            st.markdown("### What this shows")
            st.markdown(build_narrative(df, item, sname, aggs, currency))

            with st.expander("See the underlying numbers"):
                st.dataframe(df.loc[[item]].style.format("{:,.0f}"))

    # ---- Decomposition ----------------------------------------------------
    with tabs[4]:
        st.subheader("Decomposition")
        st.caption(
            "For a composite metric, see how it changed over the available years "
            "and which components drove it — using only this company's reported data."
        )
        decomp_sources = {
            "Income Statement": (income, inc_agg),
            "Balance Sheet": (balance, bal_agg),
            "Cash Flow": (cashflow, cf_agg),
        }
        avail = {k: v for k, v in decomp_sources.items() if v[0] is not None}
        if not avail:
            st.info("No statements are available to decompose for this company.")
        else:
            sname = st.selectbox("Statement", list(avail.keys()), key="decomp_stmt")
            ddf, daggs = avail[sname]
            # Parents come from THIS statement's own dataframe (per-statement key
            # so switching statements never carries stale options/selection).
            parents, eff_aggs = decomposable_parents(ddf, daggs, sname)
            if not parents:
                st.info(f"No decomposable parent metrics with components were found in "
                        f"the {sname} for this company.")
            else:
                parent = st.selectbox("Parent metric", parents,
                                      key=f"decomp_parent::{sname}")
                d = decompose(ddf, parent, eff_aggs)
                if d is None:
                    st.info("Not enough years of data to decompose this metric.")
                else:
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total change", fmt_money_compact(d["parent_delta"], currency),
                              help=f"{parent} change over {d['span']}")
                    m2.metric("Explained", fmt_money_compact(d["explained"], currency),
                              help="Sum of component changes that have both endpoints")
                    m3.metric("Unexplained", fmt_money_compact(d["unexplained"], currency),
                              help="Parent change minus identified component changes")
                    if d["driver"] and d["driver"]["delta"] is not None:
                        drv = d["driver"]
                        sign = "+" if drv["delta"] >= 0 else "−"
                        st.markdown(f"**Biggest driver:** {drv['name']} "
                                    f"({sign}{fmt_money_compact(abs(drv['delta']), currency)})")

                    st.plotly_chart(
                        waterfall_chart(d, f"{parent}: contribution to change", currency),
                        use_container_width=True)

                    sig_names = [c["name"] for c in d["significant"]]
                    if sig_names:
                        msf = mix_shift_chart(ddf, parent, sig_names,
                                              f"{parent}: component mix over time")
                        if msf is not None:
                            st.plotly_chart(msf, use_container_width=True)

                    table = []
                    for c in d["components"]:
                        table.append({
                            "Component": c["name"],
                            "First": fmt_money_compact(c["first"], currency) if c["first"] is not None else "n/a",
                            "Last": fmt_money_compact(c["last"], currency) if c["last"] is not None else "n/a",
                            "Change": fmt_money_compact(c["delta"], currency) if c["delta"] is not None else "n/a",
                            "CAGR": f"{c['cagr'] * 100:.1f}%" if c["cagr"] is not None else "n/a",
                            "% of parent change": f"{c['contrib_pct']:.1f}%" if c["contrib_pct"] is not None else "n/a",
                        })
                    st.dataframe(pd.DataFrame(table), use_container_width=True,
                                 hide_index=True)

                    st.markdown("### What this shows")
                    st.markdown(decomposition_narrative(d, currency))

    # ---- Ratios -----------------------------------------------------------
    with tabs[5]:
        st.subheader("Financial ratios")
        ratios = compute_ratios(income, balance, cashflow)
        if not ratios:
            st.info("Not enough data to compute ratios for this company.")
        series_by_name = ratio_timeseries(income, balance, cashflow)
        for r in ratios:
            st.metric(r["name"], f"{r['value']:.2f}", help=r["desc"])
            info = series_by_name.get(r["name"])
            if info is not None and len(info["series"]) > 1:
                ylab = "%" if info["unit"] == "%" else "Ratio (×)"
                st.plotly_chart(
                    line_chart(info["series"], f"{r['name']} over time", ylab),
                    use_container_width=True)

        # Additional high-signal ratios as trend charts. Each renders only when
        # its source line items are available; missing ones are skipped silently.
        for r in extra_ratio_timeseries(income, balance, cashflow):
            s = r["series"]
            ylab = "%" if r["unit"] == "%" else "x"
            st.metric(r["name"], f"{s.iloc[-1]:.2f}", help=r["desc"])
            st.plotly_chart(
                line_chart(s, f"{r['name']} over time", ylab),
                use_container_width=True)

    # ---- Valuation --------------------------------------------------------
    with tabs[6]:
        st.subheader("Valuation (estimate)")
        st.caption(
            "A simple 2-stage discounted-cash-flow model with an earnings-multiple "
            "cross-check. Treat as a starting point for your own judgement, not a "
            "target price."
        )
        shares = None
        if balance is not None:
            for nm in ("Total Common Shares Outstanding", "Shares Outstanding",
                       "Filing Date Shares Outstanding"):
                if nm in balance.index:
                    sh = clean_series(balance, nm)
                    if len(sh):
                        shares = sh.iloc[-1]
                        break
        val = estimate_valuation(income, balance, cashflow, shares, None,
                                 discount, term_g)
        if "dcf_per_share" in val:
            st.metric("DCF value per share",
                      f"{currency} {val['dcf_per_share']:,.2f}",
                      help=f"FCF growth assumed: {val.get('fcf_growth_used', 0)*100:.1f}%")
        else:
            st.info("Free cash flow was not positive/available, so a DCF estimate "
                    "could not be produced. The earnings cross-check below may still help.")
        if "pe_value_12x" in val:
            st.metric("Value at 12x earnings",
                      f"{currency} {val['pe_value_12x']:,.2f}",
                      help=f"Latest EPS: {currency} {val.get('eps', 0):,.2f}")
        st.caption("Adjust the discount rate and long-term growth in the sidebar "
                   "to test how sensitive the estimate is.")


if __name__ == "__main__":
    main()
