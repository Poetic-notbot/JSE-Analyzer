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
                    "Cash Flow", "Ratios", "Valuation"])

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

    # ---- Ratios -----------------------------------------------------------
    with tabs[4]:
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

    # ---- Valuation --------------------------------------------------------
    with tabs[5]:
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
