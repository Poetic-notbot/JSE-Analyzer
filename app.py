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
    ic = (debt[common] + equity[common] - cash[common]).dropna()
    # Guard against a non-positive invested-capital base. Net-cash companies
    # (cash > debt + equity) can drive the denominator <= 0, which otherwise
    # produces nonsensical ROIC/CROIC values (e.g. -578% for Carreras).
    return ic.where(ic > 0).dropna()


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
    equity = (latest(balance, "Total Common Equity")
              or latest(balance, "Shareholders' Equity")
              or latest(balance, "Total Equity"))
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
    equity = series_of(balance, "Total Common Equity", "Shareholders' Equity", "Total Equity")
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
    add("Interest coverage", ratio(ebit, interest.abs()), "x",
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


def decomposition_children(df, parent, aggregates, cascade=False):
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
    parent_s = clean_series(df, parent)
    # Magnitude of the parent on its latest reported year, used to stop the walk
    # before it crosses into a neighbouring section (see _fits below).
    parent_mag = abs(float(parent_s.iloc[-1])) if len(parent_s) else None

    def _fits(name, running):
        """Is this nested subtotal genuinely PART of the parent?

        A real component fits inside the parent: it never exceeds the parent on
        any shared year, and adding it to what we've already collected does not
        overshoot the parent. This stops the upward walk at a section boundary
        (e.g. decomposing 'Total Current Liabilities' must not reach up into the
        asset rows and grab 'Total Current Assets'/'Total Assets').
        """
        s = clean_series(df, name)
        shared = [(s[y], parent_s[y]) for y in s.index if y in parent_s.index]
        if not shared:
            return False
        if any(abs(sv) > abs(pv) + 1e-6 for sv, pv in shared):
            return False
        if parent_mag is not None:
            # If the components gathered so far already account for the whole
            # parent, this subtotal cannot be part of it - we have crossed into
            # the next section (e.g. 'Total Liabilities' sitting just above the
            # equity block on a near-debt-free balance sheet).
            if running >= parent_mag * 0.97 - 1e-6:
                return False
            sv = abs(float(s.iloc[-1]))
            if running + sv > parent_mag * 1.05 + 1e-6:
                return False
        return True

    children = []
    running = 0.0  # accumulated magnitude of collected components (latest year)
    i = idx - 1
    while i >= 0:
        name = order[i]

        def _mag(n):
            s = clean_series(df, n)
            return abs(float(s.iloc[-1])) if len(s) else 0.0

        if name in aggregates:
            if cascade:
                # Cascade (e.g. income statement): this nested subtotal is the
                # running base that already contains everything above it, so it
                # alone partitions the rest. Include it and stop, instead of
                # summing the cumulative subtotals above it (double-counting).
                children.append(name)
                break
            if not _fits(name, running):
                break                             # reached a different section
            children.append(name)                 # nested subtotal as one block
            running += _mag(name)
            j = i - 1
            while j >= 0 and order[j] not in aggregates:
                j -= 1                            # skip the rows it rolls up
            i = j
        else:
            children.append(name)
            running += _mag(name)
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
    cascade = statement == "Income Statement"
    parents = [name for name in df.index                       # statement order
               if name in eff_aggs
               and len(decomposition_children(df, name, eff_aggs, cascade)) >= 2]
    return parents, eff_aggs


def _dedupe_components(df, components, y0, yN):
    """
    Remove redundant rows from a component list so the parts genuinely
    partition the parent (and "explained" doesn't overshoot the total).

    The feed sometimes exposes overlapping rows in the same block - e.g. a
    broad subtotal AND a narrower line nested inside it (the classic
    "Receivables" total alongside the "Accounts Receivable" it contains),
    or two labels carrying the same series. Using only the company's own
    reported values on shared years, we drop, in order:

      1. exact duplicate series (keep the first occurrence);
      2. a relabel/superset of another - a line that is >= another on every
         shared year and coincides with it in the latest year (the same item
         under two labels the feed later merges) - keeping the broader line
         and dropping the nested duplicate, so the part is counted once; and
      3. any remaining component whose series equals the sum of two or more
         OTHER components (a subtotal that slipped in).
    Nothing is keyed to a particular label or company.
    """
    series = {c["name"]: clean_series(df, c["name"]) for c in components}

    def aligned(a, b):
        common = a.index.intersection(b.index)
        return (a[common], b[common]) if len(common) >= 2 else (None, None)

    # 1. exact duplicate series -> keep first
    kept, seen = [], []
    for c in components:
        s = series[c["name"]]
        dup = False
        for s2 in seen:
            x, y = aligned(s, s2)
            if x is not None and bool((x.round(2).values == y.round(2).values).all()):
                dup = True
                break
        if not dup:
            kept.append(c)
            seen.append(s)
    components = kept

    # 2. relabel/superset -> drop the nested duplicate, keep the broader line
    drop = set()
    for a in components:
        for b in components:
            if a["name"] == b["name"] or a["name"] in drop or b["name"] in drop:
                continue
            sa, sb = aligned(series[a["name"]], series[b["name"]])
            if sa is None:
                continue
            tol = 0.01 * (sb.abs().max() or 1.0)
            ge_all = bool(((sb - sa) >= -tol).all())
            same_latest = abs(sb.iloc[-1] - sa.iloc[-1]) <= tol
            gt_some = bool(((sb - sa) > tol).any())
            if ge_all and same_latest and gt_some:
                drop.add(a["name"])
    if drop:
        components = [c for c in components if c["name"] not in drop]

    # 3. drop a component equal to the sum of >=2 others (a leaked subtotal)
    names = [c["name"] for c in components]
    drop2 = set()
    for c in components:
        s = series[c["name"]]
        yrs = list(s.index)
        if len(yrs) < 2:
            continue
        others = [series[n] for n in names if n != c["name"] and n not in drop2]
        if len(others) < 2:
            continue
        total, used = None, 0
        for s2 in others:
            if set(yrs).issubset(set(s2.index)):
                total = s2[yrs] if total is None else total + s2[yrs]
                used += 1
        if total is not None and used >= 2:
            scale = s[yrs].abs().max() or 1.0
            if bool(((s[yrs] - total).abs() <= 0.005 * scale).all()):
                drop2.add(c["name"])
    if drop2:
        components = [c for c in components if c["name"] not in drop2]
    return components


def _component_signs(df, parent, names):
    """
    Infer whether each component ADDS to or SUBTRACTS from its parent, from the
    company's own reported levels. Returns {name: +1 or -1}.

    We choose signs s_i so the identity parent ~= sum(s_i * comp_i) holds best
    across the shared years. With a handful of components we test every sign
    combination and keep the lowest reconciliation residual (a plain greedy
    flip can get trapped in a local optimum, e.g. marking Revenue rather than
    Cost as the subtractive part of Gross Profit). Ties break toward leaving
    the LARGEST component additive, which avoids the mirror solution where
    every sign is flipped. Additive subtotals come back all +1; a subtractive
    one (Gross Profit = Revenue - Cost of Revenue) gives the cost line -1.
    Nothing is keyed to a label.
    """
    import itertools

    p = clean_series(df, parent)
    cols = {}
    for n in names:
        s = clean_series(df, n)
        common = p.index.intersection(s.index)
        if len(common) >= 2:
            cols[n] = s
    if not cols:
        return {n: 1 for n in names}
    yrs = p.index
    for s in cols.values():
        yrs = yrs.intersection(s.index)
    if len(yrs) < 2:
        return {n: 1 for n in names}
    pv = p[yrs]
    keys = list(cols.keys())
    mats = {k: cols[k][yrs] for k in keys}
    scale = float(pv.abs().sum()) or 1.0

    def residual(sign_tuple):
        total = None
        for k, sgn in zip(keys, sign_tuple):
            term = sgn * mats[k]
            total = term if total is None else total + term
        return float((pv - total).abs().sum())

    # Largest component (by typical magnitude) we prefer to keep additive.
    biggest = max(keys, key=lambda k: float(mats[k].abs().mean()))

    best_combo, best_res = None, None
    if len(keys) <= 14:
        for combo in itertools.product((1, -1), repeat=len(keys)):
            res = residual(combo)
            prefer_add = 0 if combo[keys.index(biggest)] == 1 else 1
            score = (round(res / scale, 6), prefer_add)
            if best_res is None or score < best_res:
                best_res, best_combo = score, combo
    else:
        # Greedy fallback for unusually wide statements.
        combo = [1] * len(keys)
        cur = residual(combo)
        improved = True
        while improved:
            improved = False
            for i in range(len(keys)):
                trial = list(combo)
                trial[i] = -trial[i]
                r2 = residual(trial)
                if r2 < cur - 1e-6:
                    combo, cur = trial, r2
                    improved = True
        best_combo = tuple(combo)

    signs = {k: best_combo[i] for i, k in enumerate(keys)}
    # Only trust an inferred -1 when reconciliation is genuinely tight;
    # otherwise treat the parent as additive to avoid spurious sign flips.
    if best_res is not None and best_res[0] > 0.02 and len(keys) <= 14:
        signs = {k: 1 for k in keys}
    return {n: signs.get(n, 1) for n in names}


def decompose(df, parent, aggregates, sig_frac=0.05, max_bars=12, cascade=False):
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

    raw = []
    for name in decomposition_children(df, parent, aggregates, cascade):
        cs = clean_series(df, name)
        first = float(cs[y0]) if y0 in cs.index else None
        last = float(cs[yN]) if yN in cs.index else None
        raw.append({"name": name, "series": cs, "first": first, "last": last,
                    "cagr": cagr(cs)})

    # Some parents are subtractive (e.g. Gross Profit = Revenue - Cost of
    # Revenue): a component can REDUCE the parent. Infer each component's sign
    # (+1 adds, -1 subtracts) from the company's own levels, so contributions
    # and shares reflect the true effect on the parent instead of treating a
    # rising cost as if it lifted the result.
    signs = _component_signs(df, parent, [r["name"] for r in raw])

    components = []
    for r in raw:
        name, first, last = r["name"], r["first"], r["last"]
        sgn = signs.get(name, 1)
        raw_delta = (last - first) if (first is not None and last is not None) else None
        delta = (sgn * raw_delta) if raw_delta is not None else None
        contrib = (delta / parent_delta * 100) if (delta is not None and parent_delta) else None
        share0 = (sgn * first / p0 * 100) if (first is not None and p0) else None
        shareN = (sgn * last / pN * 100) if (last is not None and pN) else None
        components.append({"name": name, "first": first, "last": last,
                           "delta": delta, "raw_delta": raw_delta, "sign": sgn,
                           "cagr": r["cagr"], "contrib_pct": contrib,
                           "share0": share0, "shareN": shareN})

    components = _dedupe_components(df, components, y0, yN)

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
                  "raw_delta": best.get("raw_delta"), "sign": best.get("sign", 1),
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


def _series_lookup(df, *candidates):
    """First non-empty year->value series among candidate row labels, or None.
    Lets the narrative resolve items like Revenue / Cost of Revenue / Inventory
    across the small label variations the feed uses, without hard-coding one."""
    if df is None:
        return None
    for name in candidates:
        if name in df.index:
            s = clean_series(df, name)
            if len(s):
                return s
    return None


def _max_prior_yoy(series):
    """Largest absolute year-over-year change BEFORE the final step, so the
    latest move can be judged against the company's own history."""
    if series is None or len(series) < 3:
        return None
    diffs = series.diff().dropna()
    if len(diffs) < 2:
        return None
    prior = diffs.iloc[:-1]
    return prior.abs().max() if len(prior) else None


def _driver_context(d, drv, income, balance, currency):
    """A short, data-only contextual read of the biggest driver: how its growth
    compares to sales, how efficiently the balance is turning (days outstanding),
    and whether the latest move is unusual for this company. Each clause is
    emitted only when the underlying rows exist, so it degrades gracefully."""
    name = drv["name"]
    low = name.lower()
    bits = []

    rev = _series_lookup(income, "Revenue", "Total Revenue", "Net Sales", "Sales")

    # Growth of the driver vs growth of the business (sales). Skip when the
    # driver IS revenue (comparing it to itself is meaningless) or when it is a
    # subtractive line such as cost (handled by the cost wording below instead).
    comp = next((c for c in d["components"] if c["name"] == name), None)
    drv_cagr = comp["cagr"] if comp else None
    is_revenue = ("revenue" in low or "net sales" in low or low == "sales")
    is_subtractive = bool(comp and comp.get("sign", 1) < 0)
    if (rev is not None and len(rev) >= 2 and drv_cagr is not None
            and not is_revenue and not is_subtractive):
        rev_cagr = cagr(rev)
        if rev_cagr is not None:
            gap = (drv_cagr - rev_cagr) * 100
            if abs(gap) >= 1.0:
                faster = "faster than" if gap > 0 else "slower than"
                bits.append(
                    f"It grew about {drv_cagr * 100:.0f}% a year versus roughly "
                    f"{rev_cagr * 100:.0f}% for revenue, {faster} sales"
                    + (" - a build that can tie up cash and signals stock piling up "
                       "ahead of demand" if (gap > 0 and "inventor" in low) else "")
                    + "."
                )
            else:
                bits.append(
                    f"It grew roughly in line with revenue "
                    f"(about {drv_cagr * 100:.0f}% a year), so its rise looks "
                    f"demand-driven rather than a build-up."
                )

    # Efficiency: days outstanding, end vs start.
    def days_line(numer_df, numer_names, label, last_val, first_val):
        base = _series_lookup(numer_df, *numer_names)
        if base is None or len(base) < 1:
            return None
        out = []
        for tag, bal in (("now", last_val), ("at the start", first_val)):
            yr = base.index[-1] if tag == "now" else base.index[0]
            flow = base.get(yr)
            if flow and bal is not None and flow != 0:
                out.append((tag, bal / flow * 365.0))
        if len(out) == 2:
            (_, d_now), (_, d_start) = out[0], out[1]
            move = "up from" if d_now > d_start else "down from"
            return (f"On the latest figures that is about {d_now:.0f} {label} "
                    f"({move} {d_start:.0f}), the time it ties up.")
        elif len(out) == 1:
            return f"On the latest figures that is about {out[0][1]:.0f} {label}."
        return None

    first_b = comp["first"] if comp else None
    last_b = comp["last"] if comp else None
    if "inventor" in low:
        ln = days_line(income,
                       ["Cost of Revenue", "Cost of Goods Sold", "Cost of Sales", "Revenue"],
                       "days of inventory", last_b, first_b)
        if ln:
            bits.append(ln)
    elif "receivable" in low:
        ln = days_line(income, ["Revenue", "Total Revenue", "Net Sales", "Sales"],
                       "days of sales outstanding", last_b, first_b)
        if ln:
            bits.append(ln + " A rising figure means sales are being booked faster "
                             "than they are being collected.")

    # Subtractive driver (e.g. cost): describe its drag on the parent.
    if is_subtractive and comp is not None and comp.get("raw_delta") is not None:
        if rev is not None and comp.get("first") and comp.get("last"):
            r0 = rev.iloc[0] if len(rev) else None
            rN = rev.iloc[-1] if len(rev) else None
            if r0 and rN:
                c0 = comp["first"] / r0 * 100
                cN = comp["last"] / rN * 100
                moved = "up from" if cN > c0 else "down from"
                bits.append(f"As a share of revenue it ran about {cN:.0f}% "
                            f"({moved} {c0:.0f}%), so its rise is squeezing the margin "
                            f"even as the total still grew.")

    # Is the latest move unusual for this company?
    full = _series_lookup(balance, name)
    if full is None:
        full = _series_lookup(income, name)
    prior_max = _max_prior_yoy(full)
    if full is not None and len(full) >= 3 and prior_max:
        last_step = abs(full.diff().dropna().iloc[-1])
        if last_step >= 1.25 * prior_max:
            bits.append("The most recent year's jump is the largest single-year move "
                        "on record for this company, so it stands out from its own history.")
        else:
            bits.append("The pace is broadly in keeping with prior years rather than "
                        "a break from trend.")
    return " ".join(bits)


def decomposition_narrative(d, currency, income=None, balance=None):
    """A grounded read of a decomposition, from the company's reported data: what
    moved, the biggest driver, the mix shift, how that driver compares to sales,
    how long it ties up cash, whether the move is unusual, and how cleanly the
    parts reconcile to the total."""
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
        # The line's own movement vs its effect on the parent can differ in
        # direction for a subtractive component (a rising cost lowers profit).
        own_delta = drv.get("raw_delta")
        own_delta = own_delta if own_delta is not None else drv["delta"]
        moved = "rose" if own_delta > 0 else "fell"
        dpct = f" ({abs(drv['pct']):.1f}%)" if drv["pct"] is not None else ""
        share = (f", about {abs(drv['delta'] / pd_delta * 100):.0f}% of the net change"
                 if pd_delta else "")
        if drv.get("sign", 1) < 0:
            effect = "added to" if drv["delta"] > 0 else "subtracted from"
            lines.append(f"The single biggest driver was **{drv['name']}**, which {moved} by "
                         f"{fmt_money(abs(own_delta), currency)}{dpct} and {effect} the total"
                         f"{share}.")
        else:
            lines.append(f"The single biggest driver was **{drv['name']}**, which {moved} by "
                         f"{fmt_money(abs(own_delta), currency)}{dpct}{share}.")
        ctx = _driver_context(d, drv, income, balance, currency)
        if ctx:
            lines.append(ctx)
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
                         f"unexplained — the rest sits in rows the feed groups differently.")
        else:
            lines.append("Identified components reconcile closely to the total change, so the "
                         "breakdown captures essentially all of the movement.")
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
# ======================================================================
#  Business Classification Engine
#  ---------------------------------------------------------------------
#  Turns the raw statements into a forensic, financials-justified verdict:
#  a company "type" (industrial / bank / insurer / reit / holding), a set
#  of pillar scores, plain-English risks, a near-term refinancing read,
#  and a final stamp (Cash machine, Wide moat, Compounder, Fortress,
#  Deteriorating, Grenade, ...).  Everything is derived only from the
#  reported figures the feed actually supports - no fabricated maturity
#  ladders, no opinions.
# ======================================================================

def _last(series):
    """Latest non-null value of a clean_series (year-ascending)."""
    if series is None or len(series) == 0:
        return None
    return float(series.iloc[-1])


def _vals(series):
    if series is None:
        return []
    return [float(x) for x in series.tolist()]


def _series_cagr(series):
    v = _vals(series)
    if len(v) < 2 or v[0] <= 0 or v[-1] <= 0:
        return None
    try:
        out = (v[-1] / v[0]) ** (1.0 / (len(v) - 1)) - 1.0
        if isinstance(out, complex):
            return None
        return out
    except Exception:
        return None


def _pick(df, *titles):
    """First title present (non-empty) in the dataframe, as a clean series."""
    for t in titles:
        s = clean_series(df, t)
        if len(s) > 0:
            return s
    return pd.Series(dtype=float)


def detect_company_type(income, balance):
    """Classify the reporting template so we score like-for-like."""
    if income is None:
        return "unknown"
    idx = list(income.index)
    has = lambda *names: any(n in idx for n in names)
    if has("Net Interest Income", "Total Interest Income", "Interest Income on Loans"):
        return "bank"
    if has("Premiums & Annuity Revenue", "Policy Benefits", "Total Premiums Earned"):
        return "insurer"
    if has("Rental Revenue", "Tenant Reimbursements"):
        return "reit"
    rev = _last(_pick(income, "Revenue", "Total Revenue"))
    assets = _last(clean_series(balance, "Total Assets")) if balance is not None else None
    if rev is not None and assets and assets > 0 and abs(rev) / assets < 0.15:
        return "holding"
    return "industrial"



def build_metric_panel(income, balance, cashflow):
    """Compute the forensic metric set used by every classifier."""
    I, B, C = income, balance, cashflow
    p = {}

    rev   = _pick(I, "Revenue", "Total Revenue")
    ebit  = clean_series(I, "EBIT")
    oi    = clean_series(I, "Operating Income")
    ebitda = clean_series(I, "EBITDA")
    gp    = clean_series(I, "Gross Profit")
    ni    = clean_series(I, "Net Income")
    nic   = clean_series(I, "Net Income to Common")
    intex = clean_series(I, "Interest Expense")
    if len(intex) == 0:
        intex = _pick(I, "Total Interest Expense", "Net Interest Expenses")

    assets = clean_series(B, "Total Assets") if B is not None else pd.Series(dtype=float)
    equity = _pick(B, "Total Common Equity", "Shareholders' Equity") if B is not None else pd.Series(dtype=float)
    debt   = clean_series(B, "Total Debt") if B is not None else pd.Series(dtype=float)
    cash   = _pick(B, "Cash & Equivalents", "Cash & Cash Equivalents") if B is not None else pd.Series(dtype=float)
    cura   = clean_series(B, "Total Current Assets") if B is not None else pd.Series(dtype=float)
    curl   = clean_series(B, "Total Current Liabilities") if B is not None else pd.Series(dtype=float)
    stdebt = _pick(B, "Short-Term Debt", "Short-Term Borrowings") if B is not None else pd.Series(dtype=float)
    cpltd  = clean_series(B, "Current Portion of Long-Term Debt") if B is not None else pd.Series(dtype=float)
    clease = clean_series(B, "Current Portion of Leases") if B is not None else pd.Series(dtype=float)
    ltdebt = clean_series(B, "Long-Term Debt") if B is not None else pd.Series(dtype=float)
    ltlease = clean_series(B, "Long-Term Leases") if B is not None else pd.Series(dtype=float)

    ocf   = clean_series(C, "Operating Cash Flow") if C is not None else pd.Series(dtype=float)
    fcf   = clean_series(C, "Free Cash Flow") if C is not None else pd.Series(dtype=float)
    capex = clean_series(C, "Capital Expenditures") if C is not None else pd.Series(dtype=float)
    div   = clean_series(C, "Dividends Paid") if C is not None else pd.Series(dtype=float)

    p["rev"] = _last(rev); p["ebit"] = _last(ebit); p["oi"] = _last(oi)
    p["ebitda"] = _last(ebitda); p["gp"] = _last(gp)
    p["ni"] = _last(ni); p["nic"] = _last(nic)
    ie = _last(intex)
    p["intExp"] = abs(ie) if ie is not None else None
    p["assets"] = _last(assets); p["equity"] = _last(equity)
    p["debt"] = _last(debt) or 0.0; p["cash"] = _last(cash) or 0.0
    p["curAssets"] = _last(cura); p["curLiab"] = _last(curl)
    p["stDebt"] = _last(stdebt) or 0.0
    p["curPortLTD"] = _last(cpltd) or 0.0
    p["curLeases"] = _last(clease) or 0.0
    p["ltDebt"] = _last(ltdebt) or 0.0
    p["ltLeases"] = _last(ltlease) or 0.0
    p["ocf"] = _last(ocf); p["fcf"] = _last(fcf)
    p["capex"] = _last(capex); p["div"] = _last(div)

    ebitV = p["ebit"] if p["ebit"] is not None else p["oi"]
    p["ebitUsed"] = ebitV
    rv = p["rev"]
    p["opMargin"]    = (ebitV / rv) if (rv and ebitV is not None) else None
    p["grossMargin"] = (p["gp"] / rv) if (rv and p["gp"] is not None) else None
    p["netMargin"]   = (p["ni"] / rv) if (rv and p["ni"] is not None) else None
    eq = p["equity"]
    earn = p["nic"] if p["nic"] is not None else p["ni"]
    p["roe"] = (earn / eq) if (eq and eq > 0 and earn is not None) else None
    p["roa"] = (p["ni"] / p["assets"]) if (p["assets"] and p["assets"] > 0 and p["ni"] is not None) else None
    p["netDebt"] = p["debt"] - p["cash"]
    if p["ebitda"] and p["ebitda"] > 0:
        p["netDebtToEbitda"] = p["netDebt"] / p["ebitda"]
    elif p["netDebt"] <= 0:
        p["netDebtToEbitda"] = -99.0
    else:
        p["netDebtToEbitda"] = None
    p["debtToEquity"] = (p["debt"] / eq) if (eq and eq > 0) else None
    if p["intExp"] and p["intExp"] > 0 and ebitV is not None:
        p["intCover"] = ebitV / p["intExp"]
    elif not p["intExp"]:
        p["intCover"] = float("inf")
    else:
        p["intCover"] = None
    p["currentRatio"] = (p["curAssets"] / p["curLiab"]) if (p["curLiab"] and p["curLiab"] > 0 and p["curAssets"] is not None) else None
    p["currentDebtDue"] = p["stDebt"] + p["curPortLTD"] + p["curLeases"]
    p["fcfMargin"] = (p["fcf"] / rv) if (rv and p["fcf"] is not None) else None
    p["cashConversion"] = (p["ocf"] / p["ni"]) if (p["ni"] and p["ni"] > 0 and p["ocf"] is not None) else None
    p["revCagr"] = _series_cagr(rev)
    p["ebitCagr"] = _series_cagr(ebit) if len(ebit) else _series_cagr(oi)
    p["niCagr"] = _series_cagr(ni)
    p["fcfPositive"] = (p["fcf"] > 0) if p["fcf"] is not None else None

    niv = _vals(ni); p["lossYears"] = sum(1 for x in niv if x < 0); p["totalYears"] = len(niv)
    p["niLast"] = niv[-1] if niv else None
    p["niPrev"] = niv[-2] if len(niv) > 1 else None
    p["niDeteriorating"] = bool(p["niLast"] is not None and p["niPrev"] is not None
                                and p["niLast"] < p["niPrev"] and p["niLast"] < abs(p["niPrev"]) * 0.5)
    rvv = _vals(rev); ev = _vals(ebit) if len(ebit) else _vals(oi)
    om = [ev[i] / rvv[i] for i in range(min(len(ev), len(rvv))) if rvv[i] and rvv[i] > 0]
    p["omSeries"] = om
    p["omNegYears"] = sum(1 for x in om if x < 0)
    p["recentCollapse"] = bool(len(om) >= 3 and om[-1] < 0 and any(x > 0.03 for x in om[:-1]))
    p["structurallyWeak"] = bool((p["totalYears"] >= 3 and p["lossYears"] >= (p["totalYears"] + 1) // 2)
                                 or (len(om) >= 3 and p["omNegYears"] >= (len(om) + 1) // 2))
    p["opMarginStart"] = om[0] if om else None
    p["opMarginEnd"] = om[-1] if om else None
    dq = 1.0; flags = []
    if any(x is not None and x < 0 for x in rvv):
        dq -= 0.5; flags.append("revenue is negative in some years - figures unreliable")
    if p["totalYears"] < 3:
        dq -= 0.3; flags.append("short reporting history")
    p["dataConfidence"] = max(0.0, dq)
    p["dqFlags"] = flags
    return p



def _refinancing_read(p, currency):
    """Near-term debt obligations vs the resources to meet them.
    Uses only what the feed reports (current portion of debt + leases,
    short-term borrowings, cash, free cash flow). It is a near-term
    refinancing-risk read, NOT a full year-by-year maturity ladder
    (that lives in the audited notes and is not in this data)."""
    due = p["currentDebtDue"]
    cash = p["cash"] or 0.0
    fcf = p["fcf"] if p["fcf"] is not None else 0.0
    breakdown = []
    if p["stDebt"]:     breakdown.append(("Short-term borrowings", p["stDebt"]))
    if p["curPortLTD"]: breakdown.append(("Current portion of long-term debt", p["curPortLTD"]))
    if p["curLeases"]:  breakdown.append(("Current portion of leases", p["curLeases"]))
    resources = cash + (fcf if fcf > 0 else 0.0)
    cover = (resources / due) if due > 0 else None
    if due <= 0:
        level, msg = "none", "No debt or leases falling due within a year that the feed reports."
    elif cover is None:
        level, msg = "unknown", "Near-term maturities could not be assessed."
    elif cover >= 2.0:
        level = "low"
        msg = ("Cash plus free cash flow cover near-term maturities about "
               "{:.1f}x - comfortable.".format(cover))
    elif cover >= 1.0:
        level = "moderate"
        msg = ("Cash plus free cash flow cover near-term maturities about "
               "{:.1f}x - manageable but with little buffer.".format(cover))
    else:
        level = "high"
        msg = ("Cash plus free cash flow cover only {:.0%} of debt falling due "
               "within a year - the company depends on rolling over / "
               "refinancing this debt.".format(cover))
    return {
        "due": due, "breakdown": breakdown, "cash": cash, "fcf": fcf,
        "cover": cover, "level": level, "message": msg,
        "longTermDebt": p["ltDebt"], "longTermLeases": p["ltLeases"],
        "netDebt": p["netDebt"], "netDebtToEbitda": p["netDebtToEbitda"],
        "intCover": p["intCover"],
    }


def score_industrial(p):
    pillars, reasons = {}, {}

    s, r = 0, []
    if p["roe"] is not None:
        if p["roe"] >= 0.20: s += 30; r.append("ROE {:.0%} (excellent)".format(p["roe"]))
        elif p["roe"] >= 0.12: s += 22; r.append("ROE {:.0%} (good)".format(p["roe"]))
        elif p["roe"] >= 0.07: s += 12; r.append("ROE {:.0%} (modest)".format(p["roe"]))
        elif p["roe"] > 0: s += 4; r.append("ROE {:.0%} (weak)".format(p["roe"]))
        else: r.append("ROE is negative")
    if p["roa"] is not None:
        if p["roa"] >= 0.10: s += 20; r.append("ROA {:.0%} (high)".format(p["roa"]))
        elif p["roa"] >= 0.05: s += 14
        elif p["roa"] > 0: s += 6
    if p["opMargin"] is not None:
        if p["opMargin"] >= 0.20: s += 25; r.append("operating margin {:.0%} (strong pricing power)".format(p["opMargin"]))
        elif p["opMargin"] >= 0.10: s += 16; r.append("operating margin {:.0%}".format(p["opMargin"]))
        elif p["opMargin"] >= 0.05: s += 8
        elif p["opMargin"] > 0: s += 3
        else: r.append("operating margin is negative")
    if p["netMargin"] is not None:
        if p["netMargin"] >= 0.10: s += 15
        elif p["netMargin"] >= 0.05: s += 9
        elif p["netMargin"] > 0: s += 3
    if p["grossMargin"] is not None and p["grossMargin"] >= 0.40:
        s += 10; r.append("gross margin {:.0%}".format(p["grossMargin"]))
    pillars["Profitability"] = min(100, s); reasons["Profitability"] = r

    s, r = 0, []
    if p["fcfPositive"] is True: s += 30; r.append("free cash flow is positive")
    elif p["fcfPositive"] is False: r.append("free cash flow is negative")
    if p["fcfMargin"] is not None:
        if p["fcfMargin"] >= 0.12: s += 30; r.append("FCF margin {:.0%} (cash-machine territory)".format(p["fcfMargin"]))
        elif p["fcfMargin"] >= 0.06: s += 20; r.append("FCF margin {:.0%}".format(p["fcfMargin"]))
        elif p["fcfMargin"] > 0: s += 10
    if p["cashConversion"] is not None:
        if p["cashConversion"] >= 1.0: s += 25; r.append("operating cash {:.2f}x of net income (earnings are cash-backed)".format(p["cashConversion"]))
        elif p["cashConversion"] >= 0.7: s += 15
        elif p["cashConversion"] > 0: s += 5; r.append("operating cash only {:.2f}x of net income (earnings not fully cash-backed)".format(p["cashConversion"]))
    if p["fcf"] is not None and p["ocf"] is not None and p["capex"] is not None and p["ocf"] > 0:
        if abs(p["capex"]) / p["ocf"] < 0.4: s += 15; r.append("low capital intensity")
    pillars["Cash generation"] = min(100, s); reasons["Cash generation"] = r

    s, r = 100, []
    nd = p["netDebtToEbitda"]
    if nd is not None:
        if nd <= 0: r.append("net cash position")
        elif nd <= 1.5: s -= 5
        elif nd <= 3: s -= 20; r.append("net debt {:.1f}x EBITDA (moderate leverage)".format(nd))
        elif nd <= 4.5: s -= 40; r.append("net debt {:.1f}x EBITDA (high leverage)".format(nd))
        else: s -= 60; r.append("net debt {:.1f}x EBITDA (dangerous leverage)".format(nd))
    ic = p["intCover"]
    if ic is not None and ic != float("inf"):
        if ic < 1.5: s -= 30; r.append("interest coverage {:.1f}x (earnings barely cover interest)".format(ic))
        elif ic < 3: s -= 15; r.append("interest coverage {:.1f}x (thin)".format(ic))
    if p["currentRatio"] is not None:
        if p["currentRatio"] < 1: s -= 20; r.append("current ratio {:.2f} (current liabilities exceed current assets)".format(p["currentRatio"]))
        elif p["currentRatio"] < 1.2: s -= 8
    if p["currentDebtDue"] > 0:
        resources = (p["cash"] or 0) + (p["fcf"] if (p["fcf"] or 0) > 0 else 0)
        cover = resources / p["currentDebtDue"]
        if cover < 1: s -= 25; r.append("debt due within a year exceeds cash + free cash flow (refinancing-dependent)")
        elif cover < 1.5: s -= 10; r.append("near-term debt only modestly covered by cash + free cash flow")
    pillars["Balance sheet"] = max(0, min(100, s)); reasons["Balance sheet"] = r

    s, r = 0, []
    if p["revCagr"] is not None:
        if p["revCagr"] >= 0.12: s += 35; r.append("revenue compounding {:.0%}/yr".format(p["revCagr"]))
        elif p["revCagr"] >= 0.05: s += 25; r.append("revenue growing {:.0%}/yr".format(p["revCagr"]))
        elif p["revCagr"] >= 0: s += 12
        else: r.append("revenue shrinking ({:.0%}/yr)".format(p["revCagr"]))
    if p["ebitCagr"] is not None:
        if p["ebitCagr"] >= 0.12: s += 35
        elif p["ebitCagr"] >= 0.05: s += 22
        elif p["ebitCagr"] >= 0: s += 10
        else: r.append("operating profit declining")
    if p["opMarginStart"] is not None and p["opMarginEnd"] is not None:
        d = p["opMarginEnd"] - p["opMarginStart"]
        if d >= 0.02: s += 30; r.append("margins expanding")
        elif d >= -0.01: s += 18
        else: r.append("margins compressing")
    pillars["Growth"] = min(100, s); reasons["Growth"] = r

    s, r = 0, []
    high_returns = (p["roe"] is not None and p["roe"] >= 0.15) and (p["roa"] is not None and p["roa"] >= 0.08)
    fat = (p["opMargin"] is not None and p["opMargin"] >= 0.18)
    stable = (p["opMarginStart"] is not None and p["opMarginEnd"] is not None and p["opMarginEnd"] >= p["opMarginStart"] - 0.03)
    if high_returns: s += 40; r.append("consistently high returns on capital")
    if fat: s += 30; r.append("wide operating margins (pricing power)")
    if stable and fat: s += 15; r.append("margins are durable")
    if p["fcfMargin"] is not None and p["fcfMargin"] >= 0.12: s += 15; r.append("strong cash conversion")
    pillars["Moat"] = min(100, s); reasons["Moat"] = r

    return pillars, reasons



def detect_risks(p, ctype):
    """Plain-English, financials-backed risks to watch."""
    risks = []
    if p["recentCollapse"]:
        risks.append(("Earnings collapse", "Operating profit turned negative in the latest year after being positive earlier - something has broken recently and needs explaining before any purchase."))
    elif p["niDeteriorating"]:
        risks.append(("Deteriorating profit", "The latest year's net profit fell sharply versus the prior year - momentum is negative."))
    nd = p["netDebtToEbitda"]
    if nd is not None and nd > 4.5:
        risks.append(("Heavy leverage", "Net debt is more than 4.5x EBITDA - a downturn or rate rise could overwhelm the balance sheet."))
    elif nd is not None and nd > 3:
        risks.append(("Elevated leverage", "Net debt is over 3x EBITDA - manageable in good times, punishing in bad ones."))
    ic = p["intCover"]
    if ic is not None and ic != float("inf") and ic < 2:
        risks.append(("Thin interest cover", "Operating profit covers interest less than 2x - little room for a profit wobble."))
    if p["currentRatio"] is not None and p["currentRatio"] < 1:
        risks.append(("Liquidity squeeze", "Current liabilities exceed current assets - the company relies on continued cash generation or rollovers to pay near-term bills."))
    if p["currentDebtDue"] > 0:
        resources = (p["cash"] or 0) + (p["fcf"] if (p["fcf"] or 0) > 0 else 0)
        if resources / p["currentDebtDue"] < 1:
            risks.append(("Refinancing dependence", "Debt falling due within a year is larger than cash plus free cash flow - the company must refinance, which is risky if credit tightens."))
    if p["fcfPositive"] is False and (p["lossYears"] or 0) == 0:
        risks.append(("Cash burn despite profits", "The company reports profits but free cash flow is negative - profits are not turning into cash (watch working capital or heavy capex)."))
    if p["cashConversion"] is not None and p["cashConversion"] < 0.6 and p["cashConversion"] > 0:
        risks.append(("Low cash conversion", "Operating cash flow is well below reported net income - earnings quality is questionable."))
    if p["opMarginStart"] is not None and p["opMarginEnd"] is not None and (p["opMarginEnd"] - p["opMarginStart"]) < -0.03:
        risks.append(("Margin erosion", "Operating margins have compressed over the period - pricing power or cost control is slipping."))
    if p["revCagr"] is not None and p["revCagr"] < 0:
        risks.append(("Shrinking top line", "Revenue is falling over the period - the business is contracting."))
    if p["structurallyWeak"]:
        risks.append(("Chronic unprofitability", "The company has lost money in most of the years reported - this is a structural problem, not a blip."))
    if ctype == "bank" and p["equity"] and p["assets"] and (p["equity"] / p["assets"]) < 0.06:
        risks.append(("Thin capital buffer", "Equity is a small share of assets - a bank with little cushion against loan losses."))
    if not risks:
        risks.append(("No major red flags", "On the reported figures, no significant financial red flags stand out - keep watching the usual operating metrics."))
    return risks



def classify_business(p, pillars, ctype):
    """Map the evidence to stamps + an overall verdict tier."""
    stamps = []
    weights = {"Profitability": 0.25, "Cash generation": 0.22, "Balance sheet": 0.23, "Growth": 0.15, "Moat": 0.15}
    overall = round(sum(pillars.get(k, 0) * w for k, w in weights.items()))

    prof = pillars.get("Profitability", 0); cashp = pillars.get("Cash generation", 0)
    bal = pillars.get("Balance sheet", 0); grow = pillars.get("Growth", 0); moat = pillars.get("Moat", 0)

    structural = p["structurallyWeak"]
    collapsing = p["recentCollapse"] or p["niDeteriorating"]

    resources = (p["cash"] or 0) + (p["fcf"] if (p["fcf"] or 0) > 0 else 0)
    cant_service = (p["intCover"] is not None and p["intCover"] != float("inf") and p["intCover"] < 1
                    and p["currentDebtDue"] > 0 and resources < p["currentDebtDue"])
    grenade = (structural
               or (cant_service and not collapsing)
               or (p["netDebtToEbitda"] is not None and p["netDebtToEbitda"] > 5 and p["fcfPositive"] is False)
               or bal < 15)

    if grenade:
        stamps.append(("Grenade", "Structural financial damage - lossmaking across most of its history and/or unable to service its debt. The kind of business to stay well away from."))
    if collapsing and not grenade:
        stamps.append(("Falling knife", "Was viable but the latest results show a sharp deterioration. Do not catch it until the bleeding stops and you understand the cause."))
    if bal >= 85 and (p["netDebtToEbitda"] is None or p["netDebtToEbitda"] <= 1):
        stamps.append(("Fortress balance sheet", "Little or no net debt and ample liquidity - built to survive a downturn."))
    if p["fcfMargin"] is not None and p["fcfMargin"] >= 0.12 and cashp >= 60 and not collapsing:
        stamps.append(("Cash machine", "Turns a large slice of revenue into free cash flow - the hallmark of a high-quality, self-funding business."))
    if moat >= 70 and not collapsing:
        stamps.append(("Wide moat", "High, durable returns on capital and fat margins point to a strong competitive advantage."))
    elif moat >= 45 and not collapsing:
        stamps.append(("Narrow moat", "Some evidence of a competitive edge in its returns and margins, but not yet decisive."))
    if grow >= 65 and prof >= 60 and (p["roe"] is not None and p["roe"] >= 0.15) and not collapsing:
        stamps.append(("Compounder", "Growing while earning high returns on capital - the kind of business that builds value year after year."))
    if (not grenade and not collapsing) and (grow < 35 or (p["opMarginStart"] is not None and p["opMarginEnd"] is not None and p["opMarginEnd"] < p["opMarginStart"] - 0.02)):
        stamps.append(("Margins under pressure", "Sluggish growth or compressing margins - the business is running to stand still."))
    if not grenade and not collapsing and prof >= 50 and bal >= 60 and not stamps:
        stamps.append(("Steady operator", "Solidly profitable with a sound balance sheet, without standout growth or a clear moat - a dependable holding."))
    if not stamps:
        stamps.append(("Mixed picture", "The financials send mixed signals - read the detail below before forming a view."))

    if grenade: tier = "Avoid"
    elif collapsing: tier = "Caution - deteriorating"
    elif overall >= 72: tier = "High quality"
    elif overall >= 55: tier = "Solid"
    elif overall >= 40: tier = "Mixed / watch"
    else: tier = "Weak"

    seen = set(); uniq = []
    for s in stamps:
        if s[0] not in seen:
            seen.add(s[0]); uniq.append(s)
    return {"overall": overall, "tier": tier, "stamps": uniq}



def build_financial_panel(income, balance, cashflow):
    """A type-appropriate panel for banks, insurers and holding/investment
    companies, where operating-margin and FCF logic do not apply."""
    I, B = income, balance
    p = {}
    rev   = _pick(I, "Revenue", "Total Revenue")
    nii   = clean_series(I, "Net Interest Income")
    tnie  = clean_series(I, "Total Non-Interest Expense")
    ni    = clean_series(I, "Net Income")
    nic   = clean_series(I, "Net Income to Common")
    assets = clean_series(B, "Total Assets") if B is not None else pd.Series(dtype=float)
    equity = _pick(B, "Total Common Equity", "Shareholders' Equity") if B is not None else pd.Series(dtype=float)
    debt   = clean_series(B, "Total Debt") if B is not None else pd.Series(dtype=float)

    p["rev"] = _last(rev); p["ni"] = _last(ni); p["nic"] = _last(nic)
    p["assets"] = _last(assets); p["equity"] = _last(equity); p["debt"] = _last(debt) or 0.0
    earn = p["nic"] if p["nic"] is not None else p["ni"]
    p["roa"] = (p["ni"] / p["assets"]) if (p["assets"] and p["assets"] > 0 and p["ni"] is not None) else None
    p["roe"] = (earn / p["equity"]) if (p["equity"] and p["equity"] > 0 and earn is not None) else None
    p["equityToAssets"] = (p["equity"] / p["assets"]) if (p["assets"] and p["equity"]) else None
    p["efficiency"] = (abs(_last(tnie)) / p["rev"]) if (p["rev"] and _last(tnie) is not None) else None
    p["debtToEquity"] = (p["debt"] / p["equity"]) if (p["equity"] and p["equity"] > 0) else None
    p["revCagr"] = _series_cagr(rev); p["niCagr"] = _series_cagr(ni)
    p["equityCagr"] = _series_cagr(equity)

    niv = _vals(ni); p["lossYears"] = sum(1 for x in niv if x < 0); p["totalYears"] = len(niv)
    p["niLast"] = niv[-1] if niv else None
    p["niPrev"] = niv[-2] if len(niv) > 1 else None
    p["niDeteriorating"] = bool(p["niLast"] is not None and p["niPrev"] is not None
                                and p["niLast"] < p["niPrev"] and p["niLast"] < abs(p["niPrev"]) * 0.5)
    p["structurallyWeak"] = bool(p["totalYears"] >= 3 and p["lossYears"] >= (p["totalYears"] + 1) // 2)
    p["recentCollapse"] = False
    p["intCover"] = None; p["netDebtToEbitda"] = None; p["currentRatio"] = None
    p["currentDebtDue"] = 0.0; p["cash"] = 0.0; p["fcf"] = None; p["fcfPositive"] = None
    p["cashConversion"] = None; p["opMarginStart"] = None; p["opMarginEnd"] = None
    p["stDebt"] = 0.0; p["curPortLTD"] = 0.0; p["curLeases"] = 0.0; p["ltDebt"] = _last(debt) or 0.0; p["ltLeases"] = 0.0
    p["dataConfidence"] = 1.0 if p["totalYears"] >= 3 else 0.7
    p["dqFlags"] = [] if p["totalYears"] >= 3 else ["short reporting history"]
    return p


def score_financial(p, ctype):
    pillars, reasons = {}, {}
    s, r = 0, []
    if p["roe"] is not None:
        if p["roe"] >= 0.15: s += 45; r.append("ROE {:.0%} (strong for a financial)".format(p["roe"]))
        elif p["roe"] >= 0.10: s += 32; r.append("ROE {:.0%}".format(p["roe"]))
        elif p["roe"] >= 0.06: s += 18; r.append("ROE {:.0%} (modest)".format(p["roe"]))
        elif p["roe"] > 0: s += 6; r.append("ROE {:.0%} (weak)".format(p["roe"]))
        else: r.append("ROE negative")
    if p["roa"] is not None:
        if ctype == "bank":
            if p["roa"] >= 0.02: s += 40; r.append("ROA {:.1%} (excellent for a bank)".format(p["roa"]))
            elif p["roa"] >= 0.012: s += 28; r.append("ROA {:.1%} (good)".format(p["roa"]))
            elif p["roa"] >= 0.006: s += 14
            elif p["roa"] > 0: s += 4
        else:
            if p["roa"] >= 0.06: s += 40
            elif p["roa"] >= 0.03: s += 26
            elif p["roa"] > 0: s += 10
    pillars["Returns"] = min(100, s); reasons["Returns"] = r
    s, r = 0, []
    eta = p["equityToAssets"]
    if eta is not None:
        if ctype == "bank":
            if eta >= 0.12: s += 70; r.append("equity {:.0%} of assets (well capitalised)".format(eta))
            elif eta >= 0.08: s += 50; r.append("equity {:.0%} of assets (adequate)".format(eta))
            elif eta >= 0.06: s += 30; r.append("equity {:.0%} of assets (thin)".format(eta))
            else: s += 10; r.append("equity only {:.0%} of assets (low capital buffer)".format(eta))
        else:
            if eta >= 0.5: s += 70; r.append("equity {:.0%} of assets (lightly geared)".format(eta))
            elif eta >= 0.3: s += 50
            elif eta >= 0.15: s += 30
            else: s += 12; r.append("highly geared")
    if p["debtToEquity"] is not None and p["debtToEquity"] < 0.3:
        s += 30; r.append("little external debt")
    pillars["Capital strength"] = min(100, s); reasons["Capital strength"] = r
    s, r = 0, []
    if p["efficiency"] is not None:
        if p["efficiency"] <= 0.5: s += 50; r.append("cost-to-income {:.0%} (very efficient)".format(p["efficiency"]))
        elif p["efficiency"] <= 0.6: s += 38; r.append("cost-to-income {:.0%} (efficient)".format(p["efficiency"]))
        elif p["efficiency"] <= 0.7: s += 22; r.append("cost-to-income {:.0%}".format(p["efficiency"]))
        else: s += 8; r.append("cost-to-income {:.0%} (high)".format(p["efficiency"]))
    if p["lossYears"] == 0 and p["totalYears"] >= 3:
        s += 50; r.append("profitable every year on record")
    pillars["Quality"] = min(100, s); reasons["Quality"] = r
    s, r = 0, []
    if p["revCagr"] is not None:
        if p["revCagr"] >= 0.12: s += 50; r.append("revenue compounding {:.0%}/yr".format(p["revCagr"]))
        elif p["revCagr"] >= 0.05: s += 35; r.append("revenue growing {:.0%}/yr".format(p["revCagr"]))
        elif p["revCagr"] >= 0: s += 18
        else: r.append("revenue shrinking")
    if p["equityCagr"] is not None and p["equityCagr"] >= 0.08:
        s += 50; r.append("book value compounding {:.0%}/yr".format(p["equityCagr"]))
    elif p["equityCagr"] is not None and p["equityCagr"] >= 0.03:
        s += 30
    pillars["Growth"] = min(100, s); reasons["Growth"] = r
    return pillars, reasons


def classify_financial(p, pillars, ctype):
    stamps = []
    weights = {"Returns": 0.30, "Capital strength": 0.28, "Quality": 0.24, "Growth": 0.18}
    overall = round(sum(pillars.get(k, 0) * w for k, w in weights.items()))
    ret = pillars.get("Returns", 0); cap = pillars.get("Capital strength", 0)
    qual = pillars.get("Quality", 0); grow = pillars.get("Growth", 0)
    structural = p["structurallyWeak"]; collapsing = p["niDeteriorating"]
    thin_cap = (ctype == "bank" and p["equityToAssets"] is not None and p["equityToAssets"] < 0.06)
    grenade = structural or (thin_cap and (p["roe"] is None or p["roe"] < 0))
    if grenade:
        stamps.append(("Grenade", "Lossmaking across most of its history and/or dangerously thin capital. Avoid."))
    if collapsing and not grenade:
        stamps.append(("Falling knife", "Profit fell away sharply in the latest year - understand why before buying."))
    if ctype == "bank" and cap >= 70 and ret >= 45:
        stamps.append(("Fortress financial", "Well capitalised and earning good returns - a sturdy compounder among financials."))
    if ret >= 60 and qual >= 60:
        stamps.append(("High-return franchise", "Earns above-average returns efficiently and consistently."))
    if grow >= 60 and ret >= 45:
        stamps.append(("Compounder", "Growing the book and earnings while keeping returns up."))
    if (p["roe"] is not None and 0 < p["roe"] < 0.07) and not grenade:
        stamps.append(("Low-return financial", "Returns on equity are below the cost of capital most investors require - capital is not working hard here."))
    if not grenade and not collapsing and ret >= 45 and cap >= 50 and not stamps:
        stamps.append(("Steady financial", "Reliably profitable and reasonably capitalised, without standout growth."))
    if not stamps:
        stamps.append(("Mixed picture", "Signals are mixed - read the detail below."))
    if grenade: tier = "Avoid"
    elif collapsing: tier = "Caution - deteriorating"
    elif overall >= 70: tier = "High quality"
    elif overall >= 52: tier = "Solid"
    elif overall >= 38: tier = "Mixed / watch"
    else: tier = "Weak"
    seen = set(); uniq = []
    for s in stamps:
        if s[0] not in seen:
            seen.add(s[0]); uniq.append(s)
    return {"overall": overall, "tier": tier, "stamps": uniq}



_TIER_COLOUR = {
    "High quality": "#1b7f3b", "Solid": "#3a7d44",
    "Mixed / watch": "#b8860b", "Weak": "#b04a2f",
    "Caution - deteriorating": "#c0392b", "Avoid": "#922b21",
}


def assess_business(income, balance, cashflow):
    """Top-level: returns a dict describing the business classification."""
    ctype = detect_company_type(income, balance)
    if ctype in ("bank", "insurer", "holding", "reit"):
        p = build_financial_panel(income, balance, cashflow)
        pillars, reasons = score_financial(p, ctype)
        verdict = classify_financial(p, pillars, ctype)
        family = "financial"
    else:
        p = build_metric_panel(income, balance, cashflow)
        pillars, reasons = score_industrial(p)
        verdict = classify_business(p, pillars, ctype)
        family = "industrial"
    risks = detect_risks(p, ctype)
    refi = _refinancing_read(p, "") if family == "industrial" else None
    return {"type": ctype, "family": family, "panel": p, "pillars": pillars,
            "reasons": reasons, "verdict": verdict, "risks": risks, "refi": refi}


_TYPE_LABEL = {
    "industrial": "Operating company", "bank": "Bank / lender",
    "insurer": "Insurer", "reit": "Property / REIT",
    "holding": "Holding / investment company", "unknown": "Unclassified",
}


def render_verdict(income, balance, cashflow, currency, company_name):
    if income is None or balance is None:
        st.info("Not enough statement data to classify this business.")
        return
    a = assess_business(income, balance, cashflow)
    v = a["verdict"]; p = a["panel"]
    colour = _TIER_COLOUR.get(v["tier"], "#444")

    stamp_names = " &nbsp;&middot;&nbsp; ".join(s[0] for s in v["stamps"])
    st.markdown(
        "<div style='border-left:8px solid " + colour + ";background:#0e1117;"
        "padding:14px 18px;border-radius:6px;margin-bottom:6px'>"
        "<div style='font-size:0.85rem;color:#9aa0a6;text-transform:uppercase;"
        "letter-spacing:.05em'>" + _TYPE_LABEL.get(a["type"], a["type"]) +
        " &nbsp;|&nbsp; overall score " + str(v["overall"]) + "/100</div>"
        "<div style='font-size:1.6rem;font-weight:700;color:" + colour + "'>" + v["tier"] + "</div>"
        "<div style='font-size:1.05rem;color:#e8eaed;margin-top:2px'>" + stamp_names + "</div>"
        "</div>", unsafe_allow_html=True)

    if p.get("dataConfidence", 1.0) < 0.7:
        st.warning("Data quality is low for this company (" + "; ".join(p.get("dqFlags", [])) +
                   "). Treat the classification as indicative only.")

    for name, blurb in v["stamps"]:
        st.markdown("**" + name + ".** " + blurb)

    st.divider()

    st.markdown("#### How the score breaks down")
    cols = st.columns(len(a["pillars"]))
    for col, (k, val) in zip(cols, a["pillars"].items()):
        col.metric(k, str(int(round(val))) + "/100")
    for k, rs in a["reasons"].items():
        if rs:
            st.markdown("**" + k + "** &mdash; " + "; ".join(rs) + ".")

    st.divider()

    refi = a["refi"]
    if refi is not None:
        st.markdown("#### Debt & refinancing risk")
        rc = {"none": "#1b7f3b", "low": "#1b7f3b", "moderate": "#b8860b",
              "high": "#c0392b", "unknown": "#666"}.get(refi["level"], "#666")
        st.markdown("<span style='color:" + rc + ";font-weight:600'>"
                    "Near-term refinancing risk: " + refi["level"].upper() + "</span>",
                    unsafe_allow_html=True)
        st.markdown(refi["message"])
        if refi["breakdown"]:
            due_tbl = {}
            for nm, amt in refi["breakdown"]:
                due_tbl[nm] = fmt_money_compact(amt, currency)
            due_tbl["Total due within ~1 year"] = fmt_money_compact(refi["due"], currency)
            due_tbl["Cash on hand"] = fmt_money_compact(refi["cash"], currency)
            due_tbl["Free cash flow (latest yr)"] = fmt_money_compact(refi["fcf"], currency)
            st.table(pd.Series(due_tbl, name="Amount").to_frame())
        extra = []
        if refi["netDebtToEbitda"] is not None and refi["netDebtToEbitda"] > -90:
            extra.append("Net debt is {:.1f}x EBITDA.".format(refi["netDebtToEbitda"]))
        elif refi["netDebtToEbitda"] is not None:
            extra.append("The company holds more cash than debt (net cash).")
        if refi["intCover"] is not None and refi["intCover"] != float("inf"):
            extra.append("Operating profit covers interest {:.1f}x.".format(refi["intCover"]))
        if refi["longTermDebt"] or refi["longTermLeases"]:
            lt = (refi["longTermDebt"] or 0) + (refi["longTermLeases"] or 0)
            extra.append("Longer-dated debt & leases total " + fmt_money_compact(lt, currency) + ".")
        if extra:
            st.markdown(" ".join(extra))
        st.caption("This is a near-term refinancing read built from the current "
                   "portion of debt and leases the feed reports. A full year-by-year "
                   "maturity ladder is only in the audited notes and is not modelled here.")
        st.divider()

    st.markdown("#### Risks to watch")
    for title, body in a["risks"]:
        st.markdown("**" + title + ".** " + body)

    st.caption("All classifications are derived only from reported figures versus "
               "stockanalysis.com data - they are a disciplined reading of the numbers, "
               "not investment advice.")


# 4. USER INTERFACE
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="JSE Financial Analyzer", layout="wide")
    # Keep metric values (e.g. "JMD 523.7M") from being clipped in the narrow
    # 1/3-width columns used on the Decomposition and Ratios tabs.
    st.markdown(
        """
        <style>
        [data-testid="stMetricValue"] {
            font-size: 1.45rem;
            line-height: 1.2;
            white-space: normal;
            overflow-wrap: anywhere;
        }
        [data-testid="stMetricValue"] > div { overflow: visible; }
        [data-testid="stMetricLabel"] { white-space: normal; }
        </style>
        """,
        unsafe_allow_html=True,
    )
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

    tabs = st.tabs(["Overview", "Verdict", "Income Statement", "Balance Sheet",
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

    # ---- Verdict ----------------------------------------------------------
    with tabs[1]:
        st.subheader(f"{companies.get(ticker, ticker)} \u2014 the verdict")
        st.caption("A disciplined, financials-only reading of the business: "
                   "what kind of company it is, how it scores, what it is, and "
                   "the risks to watch. Reflects the latest reported year.")
        render_verdict(income, balance, cashflow, currency,
                       companies.get(ticker, ticker))

    # ---- One drill-down tab per statement --------------------------------
    statement_tabs = {
        "Income Statement": (tabs[2], income, inc_agg),
        "Balance Sheet": (tabs[3], balance, bal_agg),
        "Cash Flow": (tabs[4], cashflow, cf_agg),
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
    with tabs[5]:
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
                d = decompose(ddf, parent, eff_aggs,
                              cascade=sname == "Income Statement")
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
                    st.markdown(decomposition_narrative(d, currency, income, balance))

    # ---- Ratios -----------------------------------------------------------
    with tabs[6]:
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
    with tabs[7]:
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
