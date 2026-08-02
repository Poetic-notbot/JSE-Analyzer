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
There is also a Valuation tab that builds a fair value from several methods that
have held up for a very long time - a Buffett owner-earnings DCF, two-stage
dividend discounting, Earnings Power Value, justified price-to-book for
financials, Graham's Number and a fair-P/E reversion - each applied only where
it suits the kind of business, then blended into one central estimate with a
range and a margin-of-safety buy-below line, and compared to the live price.

Why this version exists
-----------------------
The original (2025) version scraped HTML tables from stockanalysis.com. The site
has since been rebuilt and the data now lives in a JSON payload behind each page
(`__data.json`), so the old HTML scraper no longer works. This version reads
that JSON feed instead, which is far more reliable. Everything else \u2014 the idea,
the drill-down, the narrative, the valuation \u2014 is preserved.

Run it locally with:
    pip install -r requirements.txt
    streamlit run app.py
"""

import json
import math
import time
import urllib.request
import urllib.error

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# 1. DATA LAYER  \u2014 read financial statements from stockanalysis.com's JSON feed
# ---------------------------------------------------------------------------
# The site is built with SvelteKit. Every financials page has a sibling URL
# ending in "/__data.json" that returns the same numbers as structured data.
# That JSON uses a compact "devalue" encoding where integers are *pointers* into
# a flat array (and -1 means "missing"). resolve() below turns it back into
# ordinary nested data.

# Bump this whenever the data-fetching behaviour changes. It is shown in the
# sidebar so it is unambiguous which build is actually running (a reboot that
# doesn't change this string means the deployment is not on the latest commit).
APP_BUILD = "2026-08-02s dedupe receivables subtotal"

BASE = "https://stockanalysis.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
}

# Exchanges to try, in order. Most names live under jmse (Jamaica), but some JSE
# listings are cross-listed and carried on the source under their PRIMARY market -
# e.g. Guardian Holdings (GHL) is a Trinidad name (ttse). We try jmse first, then
# ttse, and remember whichever returned data so the other feeds use the same one.
_EXCHANGES = ["jmse", "ttse"]
_TICKER_EXCHANGE = {}
_EXCHANGE_OVERRIDE = {"GHL": "ttse"}          # known cross-listings (try this first)


def _exchanges_for(ticker):
    # Conservative: unknown tickers try ONLY jmse, so a JSE name that is simply not
    # covered never accidentally loads a different company that shares the ticker on
    # another exchange. Cross-listings are handled by the explicit override map.
    t = ticker.upper()
    if t in _TICKER_EXCHANGE:
        return [_TICKER_EXCHANGE[t]]
    if t in _EXCHANGE_OVERRIDE:
        pref = _EXCHANGE_OVERRIDE[t]
        return [pref, "jmse"] if pref != "jmse" else ["jmse"]
    return ["jmse"]


def _exchange_of(ticker):
    """The exchange segment that last worked for this ticker (jmse by default)."""
    return _TICKER_EXCHANGE.get(ticker.upper(),
                                _EXCHANGE_OVERRIDE.get(ticker.upper(), "jmse"))

# Each financial statement and the URL segment it lives under. The income
# statement used to live at the bare /financials/ path; the site has since moved
# it to the named /financials/income-statement/ path (matching the others), so
# the old empty segment now 404s for every ticker.
STATEMENTS = {
    "Income Statement": "income-statement/",      # /financials/income-statement/
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
    "TJH", "GHL", "PROVEN", "SCIUSD", "FIRSTROCKUSD",
    "SRFUSD", "SILUS", "TJHUSD", "SELECTMD",
    # NB: SGJ (Scotia Group Jamaica) reports in JMD - do not add it here, or every
    # figure gets multiplied by the USD/JMD rate. MASSY (TTD) and PULS were also
    # removed pending verification of their reporting currency.
}
DEFAULT_USD_JMD = 158.0  # rough fallback rate; only used for USD reporters

# Cache of FX context per ticker so the UI can show the conversion that was
# applied. Keeps the get_statement return signature unchanged.
_FX_CONTEXT = {}


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def get_usd_jmd_rate():
    """Live USD->JMD rate (cached 6h), falling back to DEFAULT_USD_JMD."""
    try:
        raw = _fetch("https://open.er-api.com/v6/latest/USD")
        rate = float(json.loads(raw)["rates"]["JMD"])
        if 100 < rate < 250:          # sanity band for USD/JMD
            return rate
    except Exception:
        pass
    return DEFAULT_USD_JMD


def _apply_fx(ticker, df, aggregates, currency):
    """Convert a USD-reported statement into JMD so figures are comparable.
    Per-share values are unaffected (numerator and denominator scale together);
    absolute JMD figures become comparable across the exchange. Same 3-tuple
    shape is returned, currency relabelled to JMD once converted."""
    reported = currency
    fx = 1.0
    if currency == "USD":
        fx = get_usd_jmd_rate()
        if df is not None:
            df = df * fx
        if isinstance(aggregates, dict):
            aggregates = {k: (v * fx if isinstance(v, (int, float)) else v)
                          for k, v in aggregates.items()}
        currency = "JMD"
    _FX_CONTEXT[ticker] = {"reported": reported, "rate": fx}
    return df, aggregates, currency



# Process-wide cache that keeps GOOD fetch results for a long time but remembers
# FAILURES only briefly - so a transient block on stockanalysis.com clears on its
# own within a couple of minutes, instead of a 6-hour st.cache_data entry locking
# a ticker out long after the source has recovered.
_FETCH_MEMO = {}

# Records, per (ticker, statement), the URLs the loader actually tried and how
# each turned out - surfaced in the UI when a statement fails to load, so it is
# obvious which path the running code used (and therefore which build is live).
_STMT_ATTEMPTS = {}


def _memo(key, producer, is_ok, ttl_ok=60 * 60 * 6, ttl_bad=120):
    now = time.time()
    hit = _FETCH_MEMO.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    value = producer()
    _FETCH_MEMO[key] = (now + (ttl_ok if is_ok(value) else ttl_bad), value)
    return value


def _fetch(url, retries=4, delay=1.4):
    """Download a URL's text, retrying politely on transient failures. A 404 is a
    definitive 'not covered' and returns immediately; other errors get a short,
    escalating backoff. Deliberately gentle - hammering a throttling source only
    deepens the throttle."""
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


def get_statement(ticker, statement):
    """Cached wrapper: keeps a good statement 6h, retries a failed one after ~2min."""
    return _memo(("stmt", ticker, statement),
                 lambda: _load_statement(ticker, statement),
                 lambda r: r is not None and r[0] is not None)


def _statement_segments(statement):
    """URL segment(s) to try for a statement, in order. The income statement is
    served at BOTH the bare /financials/ path (historically) and the named
    /financials/income-statement/ path; the site has flipped between them, so we
    try both and use whichever actually parses. The others have one stable path."""
    if statement == "Income Statement":
        return ["income-statement/", ""]
    return [STATEMENTS[statement]]


def get_quarterly(ticker, statement):
    """Cached wrapper for the quarterly view of a statement (up to ~12 quarters)."""
    return _memo(("qtr", ticker, statement),
                 lambda: _load_quarterly(ticker, statement),
                 lambda r: r is not None and r[0] is not None, ttl_ok=60 * 60)


def _load_quarterly(ticker, statement):
    """Quarterly statement columns (period-end dated), for TTM and momentum. Same
    page as the annual view with ?p=quarterly. Returns (df, aggregates, currency)
    or (None, None, None)."""
    for exch in _exchanges_for(ticker):
        for seg in _statement_segments(statement):
            url = (f"{BASE}/quote/{exch}/{ticker}/financials/{seg}"
                   "__data.json?p=quarterly")
            result = _parse_statement(_fetch(url), ticker, quarterly=True, max_cols=13)
            if result[0] is not None:
                _TICKER_EXCHANGE[ticker.upper()] = exch
                return result
    return None, None, None


def _load_statement(ticker, statement):
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
    attempts = []
    for exch in _exchanges_for(ticker):
        for seg in _statement_segments(statement):
            url = f"{BASE}/quote/{exch}/{ticker}/financials/{seg}__data.json"
            raw = _fetch(url)
            result = _parse_statement(raw, ticker)
            attempts.append((url, "no response" if raw is None else
                             ("parsed OK" if result[0] is not None else "no data in page")))
            if result[0] is not None:
                _TICKER_EXCHANGE[ticker.upper()] = exch   # reuse for the other feeds
                _STMT_ATTEMPTS[(ticker, statement)] = attempts
                return result
    _STMT_ATTEMPTS[(ticker, statement)] = attempts
    return None, None, None


def _parse_statement(raw, ticker, quarterly=False, max_cols=6):
    """Turn one statement's raw __data.json text into (df, aggregates, currency),
    or (None, None, None) if it is missing/unparseable/empty. For quarterly data
    the fiscal year repeats across quarters, so columns are keyed by period-end
    date instead, and more columns are kept."""
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

    # Annual columns are identified by fiscal year; quarterly columns repeat the
    # year, so use the unique period-end date instead.
    if quarterly:
        labels_src = fdata.get("datekey") or fdata.get("fiscalYear") or []
    else:
        labels_src = fdata.get("fiscalYear") or fdata.get("datekey") or []

    # De-duplicate restatement columns and drop TTM so trends are clean.
    keep_cols, seen = [], set()
    for i, yr in enumerate(labels_src):
        if str(yr).upper() == "TTM":
            continue
        if yr in seen:
            continue
        seen.add(yr)
        keep_cols.append(i)
    keep_cols = keep_cols[:max_cols]                # most recent columns
    year_labels = [str(labels_src[i]) for i in keep_cols][::-1]  # oldest->newest
    fiscal_years = labels_src

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
    return _apply_fx(ticker, df, aggregates, currency)


def get_ratios(ticker):
    """Cached wrapper: keeps good ratios 6h, retries an empty result after ~2min."""
    return _memo(("ratios", ticker), lambda: _load_ratios(ticker), lambda r: bool(r))


def _load_ratios(ticker):
    """Current valuation ratios from the ratios feed, read from its 'Current'
    column. Handles both the current row-based (statement-style) layout and the
    legacy flat layout. Returns {} on failure (callers degrade gracefully)."""
    full = _ratios_rows(ticker)
    if not full or not full.get("rows"):
        return {}

    def cur(*titles, pct=False):
        _by, vals, c = _ratio_series(full, *titles)
        if c is None:
            c = vals[-1] if vals else None            # fall back to latest year
        if c is not None and pct and abs(c) > 1:      # percent -> fraction
            c = c / 100.0
        return c

    return {
        "marketcap":     cur("marketcap", "Market Capitalization"),
        "ev":            cur("ev", "Enterprise Value"),
        "pe":            cur("pe", "PE Ratio", "P/E Ratio"),
        "pb":            cur("pb", "PB Ratio", "P/B Ratio"),
        "ptbv":          cur("ptbvRatio", "P/TBV Ratio"),
        "evEbit":        cur("evebit", "EV/EBIT Ratio"),
        "evEbitda":      cur("evebitda", "EV/EBITDA Ratio"),
        "earningsYield": cur("earningsyield", "Earnings Yield", pct=True),
        "dividendYield": cur("dividendyield", "Dividend Yield", pct=True),
        "roicSite":      cur("roic", "Return on Invested Capital (ROIC)", "ROIC", pct=True),
        "currentRatio":  cur("currentratio", "Current Ratio"),
    }


def get_ratios_history(ticker):
    """Per-year P/E, P/B and dividend yield straight from the ratios feed's own
    history arrays (more reliable than reconstructing them from prices). Cached 6h.
    Returns {'pe':{year:val}, 'pb':{year:val}, 'yield':{year:val}} or {}."""
    return _memo(("rhist", ticker), lambda: _load_ratios_history(ticker),
                 lambda r: bool(r and any((r.get(k) or {}).get("vals")
                                          for k in ("pe", "pb", "yield"))))


def _ratios_rows(ticker):
    """The ratios page as {labels:[...], rows:{title:[values]}, flat:bool}. The site
    now serves it as a normal statement (rows like 'PE Ratio' keyed by title in a
    'financialData' node); older/other layouts stored the values flat on the node
    (node['pe'] = array). Handle both. NO FX is applied - ratios are dimensionless."""
    url = f"{BASE}/quote/{_exchange_of(ticker)}/{ticker}/financials/ratios/__data.json"
    raw = _fetch(url)
    if raw is None:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    for n in obj.get("nodes", []):
        if not (isinstance(n, dict) and isinstance(n.get("data"), list)):
            continue
        rr = _resolve(n["data"])
        if not isinstance(rr, dict):
            continue
        fd = rr.get("financialData")
        if isinstance(fd, dict):                       # statement-style (current site)
            labels = fd.get("fiscalYear") or fd.get("datekey") or []
            rows = {}
            for entry in (rr.get("map") or []):
                mid, title = entry.get("id"), entry.get("title")
                if mid and title and isinstance(fd.get(mid), list):
                    rows[title] = fd[mid]
            if rows:
                return {"labels": labels if isinstance(labels, list) else [],
                        "rows": rows, "flat": False}
        if "marketcap" in rr and "evebit" in rr:       # legacy flat layout
            labels = rr.get("fiscalYear") or rr.get("datekey") or []
            return {"labels": labels if isinstance(labels, list) else [],
                    "rows": rr, "flat": True}
    return {}


def _ratio_row(full, *titles):
    """A metric's raw value array, matched by exact key (flat) or by title contains
    (statement-style, case-insensitive)."""
    rows, flat = full.get("rows") or {}, full.get("flat")
    for t in titles:
        v = rows.get(t)
        if isinstance(v, list):
            return v
    if not flat:
        for t in titles:
            if len(t) <= 4:                     # short keys (ev/pe/pb) are exact-only
                continue                        # to avoid matching e.g. "EV/EBIT Ratio"
            for title, arr in rows.items():
                if isinstance(arr, list) and t.lower() in str(title).lower():
                    return arr
    return []


def _ratio_series(full, *titles):
    """(by_year{yr:val}, vals[historical], current) for one metric. The TTM/Current
    column becomes `current`; year-labelled columns become the historical band."""
    labels = full.get("labels") or []
    arr = _ratio_row(full, *titles)
    by_year, vals, current = {}, [], None
    for i, v in enumerate(arr):
        if not _fnum(v):
            continue
        v = float(v)
        lab = str(labels[i]).upper() if i < len(labels) else ""
        if lab in ("TTM", "CURRENT") or (not labels and i == 0):
            if current is None:
                current = v
            continue
        vals.append(v)
        if i < len(labels):
            try:
                by_year[int(str(labels[i])[:4])] = v
            except Exception:
                pass
    return by_year, vals, current


def _load_ratios_history(ticker):
    full = _ratios_rows(ticker)
    if not full or not full.get("rows"):
        return {}

    def band(titles, is_yield=False):
        by_year, vals, _cur = _ratio_series(full, *titles)
        vals = [v for v in vals if v > 0]
        by_year = {k: v for k, v in by_year.items() if v > 0}
        if is_yield:                                   # percent -> fraction if needed
            allv = vals + list(by_year.values())
            if allv and _median([abs(x) for x in allv]) > 0.5:
                vals = [v / 100 for v in vals]
                by_year = {k: v / 100 for k, v in by_year.items()}
        return {"vals": vals, "byYear": by_year}

    return {
        "pe":    band(["pe", "peRatio", "PE Ratio", "P/E Ratio", "PE"]),
        "pb":    band(["pb", "pbRatio", "PB Ratio", "P/B Ratio", "PB"]),
        "yield": band(["dividendyield", "dividendYield", "Dividend Yield"], is_yield=True),
    }


# Candidate key names the price-history feed might use for the date and the
# closing price. The feed is a SvelteKit "__data.json" like the statements, but
# its exact field names are not documented, so we accept any of these.
_DATE_KEYS  = ("t", "d", "date", "datekey", "timestamp", "dateFormatted", "dt")
_CLOSE_KEYS = ("c", "close", "Close", "adjClose", "adjclose", "adj_close", "a", "cl")


def _find_price_series(node):
    """Best-effort: dig an ascending [(date, close), ...] series out of a resolved
    price-history payload, tolerant of the exact shape/keys the feed uses. Handles
    both row-of-objects and columnar (parallel-array) layouts. Returns None if no
    plausible series is found, so callers fall back to other price sources."""

    def rows_to_series(rows):
        ck = None
        for k in _CLOSE_KEYS:
            if any(isinstance(r, dict) and k in r for r in rows):
                ck = k
                break
        if ck is None:
            return None
        dk = next((k for k in _DATE_KEYS
                   if any(isinstance(r, dict) and k in r for r in rows)), None)
        out = []
        for r in rows:
            if isinstance(r, dict) and _fnum(r.get(ck)):
                out.append((r.get(dk) if dk else None, float(r[ck])))
        return out if len(out) >= 2 else None

    def columnar(d):
        ck = next((k for k in _CLOSE_KEYS if isinstance(d.get(k), list)), None)
        if not ck:
            return None
        closes = d[ck]
        if sum(1 for x in closes if _fnum(x)) < 2:
            return None
        dk = next((k for k in _DATE_KEYS
                   if isinstance(d.get(k), list) and len(d[k]) == len(closes)), None)
        dates = d[dk] if dk else [None] * len(closes)
        return [(dt, float(c)) for dt, c in zip(dates, closes) if _fnum(c)]

    best = None

    def walk(x):
        nonlocal best
        if best is not None:
            return
        if isinstance(x, list):
            if x and any(isinstance(e, dict) for e in x):
                s = rows_to_series(x)
                if s:
                    best = s
                    return
            for e in x:
                walk(e)
                if best is not None:
                    return
        elif isinstance(x, dict):
            s = columnar(x)
            if s:
                best = s
                return
            for v in x.values():
                walk(v)
                if best is not None:
                    return

    walk(node)
    if not best:
        return None
    # Normalise to ascending (oldest -> newest). If dates are present and the
    # first is later than the last, the feed is newest-first, so reverse it.
    dated = [t for t in best if t[0] is not None]
    if len(dated) >= 2 and str(dated[0][0]) > str(dated[-1][0]):
        best = list(reversed(best))
    return best


def get_price_history(ticker):
    """The full ascending [(date, close), ...] daily price series, for historical
    valuation bands. Cached 6h. Empty list on failure."""
    return _memo(("phist", ticker), lambda: _load_price_history(ticker),
                 lambda r: bool(r))


def _load_price_history(ticker):
    url = f"{BASE}/quote/{_exchange_of(ticker)}/{ticker}/history/__data.json"
    raw = _fetch(url)
    if raw is None:
        return []
    try:
        obj = json.loads(raw)
    except Exception:
        return []
    for n in obj.get("nodes", []):
        if isinstance(n, dict) and isinstance(n.get("data"), list):
            s = _find_price_series(_resolve(n["data"]))
            if s:
                return s
    return []


def get_price(ticker):
    """Cached wrapper: keeps a good price 1h (kept fresh through the day), retries
    an empty result after ~2min."""
    return _memo(("price", ticker), lambda: _load_price(ticker), lambda r: bool(r),
                 ttl_ok=60 * 60)


_PRICE_KEYS = ("price", "last", "close", "c", "cl", "regularMarketPrice",
               "lastPrice", "priceClose", "p")


def _find_scalar_price(node, ref):
    """Best-effort: pull a single 'current price' scalar out of a resolved payload,
    accepting it only if it is within a tight band of the series' last close `ref`
    (so we never grab an unrelated number). Returns a float or None."""
    if not (_fnum(ref) and ref > 0):
        return None
    best = [None]

    def ok(v):
        return _fnum(v) and 0.7 * ref <= v <= 1.5 * ref

    def walk(x):
        if best[0] is not None:
            return
        if isinstance(x, dict):
            for k in _PRICE_KEYS:
                if k in x and ok(x[k]):
                    best[0] = float(x[k])
                    return
            for v in x.values():
                walk(v)
                if best[0] is not None:
                    return
        elif isinstance(x, list):
            for v in x:
                walk(v)
                if best[0] is not None:
                    return

    walk(node)
    return best[0]


def _load_price(ticker):
    """Latest traded price and a 52-week range from the site's price-history feed.
    Returns {"price", "date", "low52", "high52", "currency"} or {} on failure.
    `currency` is the raw quote currency BEFORE any JMD conversion. The price is a
    delayed last close, so callers should label it as such."""
    url = f"{BASE}/quote/{_exchange_of(ticker)}/{ticker}/history/__data.json"
    raw = _fetch(url)
    if raw is None:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    series, resolved_nodes = None, []
    for n in obj.get("nodes", []):
        if isinstance(n, dict) and isinstance(n.get("data"), list):
            res = _resolve(n["data"])
            resolved_nodes.append(res)
            if series is None:
                series = _find_price_series(res)
    if not series:
        return {}
    last_date, last_close = series[-1]
    # A feed may carry a fresher 'current price' scalar than the last daily row.
    scalar = None
    for res in resolved_nodes:
        scalar = _find_scalar_price(res, last_close)
        if scalar is not None:
            break
    price = scalar if scalar is not None else last_close
    window = [c for _, c in series][-252:]          # ~ one trading year
    return {
        "price": price,
        "date": last_date,
        "low52": min(window + [price]) if window else price,
        "high52": max(window + [price]) if window else price,
        "currency": "USD" if ticker.upper() in USD_REPORTERS else "JMD",
    }


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
# 2. ANALYSIS HELPERS  \u2014 components, drivers, growth, narrative, ratios
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
    own dataframe. A row qualifies if it is a subtotal \u2014 either flagged by the
    feed (`aggregates`) or a well-known additive subtotal name for `statement`
    that the ticker actually reports \u2014 and it has >=2 decomposable components.

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


def _shared_stem(a, b, n=6):
    """True if the two line-item names share a run of >= n letters (case-folded,
    letters only) - i.e. they belong to the same family, like 'Receivables' and
    'Accounts Receivable' (both contain 'receiv'/'receivabl'). Used to keep the
    subtotal-collapse from firing on numerically-coincident but unrelated lines."""
    aa = "".join(ch for ch in a.lower() if ch.isalpha())
    bb = "".join(ch for ch in b.lower() if ch.isalpha())
    if len(aa) < n or len(bb) < n:
        return False
    return any(aa[i:i + n] in bb for i in range(len(aa) - n + 1))


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

    # 3. drop a component equal to the sum of a SUBSET (2-3) of the other
    #    components AND in the same naming family - a leaked subtotal nested among
    #    siblings, e.g. a "Receivables" total next to the "Accounts Receivable" +
    #    "Other Receivables" it contains. Keep the granular parts, drop the
    #    redundant subtotal so it isn't double-counted. The shared-stem guard stops
    #    an unrelated line (say Cash) being dropped because it happens to equal the
    #    sum of two others by coincidence.
    from itertools import combinations
    names = [c["name"] for c in components]
    drop2 = set()
    for c in components:
        s = series[c["name"]]
        yrs = list(s.index)
        if len(yrs) < 2:
            continue
        scale = s[yrs].abs().max() or 1.0
        others = [(n, series[n]) for n in names
                  if n != c["name"] and n not in drop2 and set(yrs).issubset(set(series[n].index))]
        matched = False
        for k in (2, 3):
            if matched:
                break
            for combo in combinations(others, k):
                total = None
                for _n, s2 in combo:
                    total = s2[yrs] if total is None else total + s2[yrs]
                if (total is not None
                        and bool(((s[yrs] - total).abs() <= 0.005 * scale).all())
                        and all(_shared_stem(c["name"], nm) for nm, _ in combo)):
                    matched = True
                    break
        if matched:
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

    return {"parent": parent, "year0": y0, "yearN": yN, "span": f"{y0}\u2013{yN}",
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
        f"{d['span']} ({fmt_money(d['p0'], currency)} \u2192 {fmt_money(d['pN'], currency)}){cagr_txt}."
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
                         f"unexplained \u2014 the rest sits in rows the feed groups differently.")
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


def value_profile(p, ratios):
    """Fundamentals panel p + TTM site ratios -> valuation anchors + clues."""
    v = dict(ratios) if ratios else {}
    mcap = v.get("marketcap")
    # The feed reports market cap "in millions", an ambiguous scale. Reconstruct it
    # unit-safe from P/E x net income (P/E is dimensionless, net income full-unit)
    # so the yield/multiple clues below can't be thrown off by a units mismatch.
    if _fnum(v.get("pe")) and v["pe"] > 0 and _fnum(p.get("ni")) and p["ni"] > 0:
        mcap = v["pe"] * p["ni"]
        v["marketcap"] = mcap

    v["fcfYield"]  = _safe_div(p.get("fcf"), mcap)
    v["priceToNi"] = _safe_div(mcap, p.get("ni"))                 # years of net income
    total_liab = (p.get("assets") or 0) - (p.get("equity") or 0)
    ncav = (p.get("curAssets") or 0) - total_liab                 # Graham net-net
    v["ncav"] = ncav
    v["priceToNcav"] = _safe_div(mcap, ncav) if (ncav and ncav > 0) else None

    healthy = (p.get("fcfPositive") and p.get("lossYears", 9) <= 1
               and (p.get("netDebtToEbit") is None or p.get("netDebtToEbit") < 3))

    clues = []
    g = lambda t: clues.append(("good", t))
    c = lambda t: clues.append(("caution", t))
    if v.get("priceToNcav") is not None and v["priceToNcav"] <= 1.0 and healthy:
        g("Market value is at or below net current assets — fixed assets and earnings "
          "power are effectively in for free (a Graham net-net).")
    if v.get("priceToNi") is not None and v["priceToNi"] <= 6 and healthy:
        g(f"Priced at about {v['priceToNi']:.1f} years of current net income while "
          "fundamentals look healthy.")
    if v.get("pb") is not None and v["pb"] < 1.0 and healthy:
        g(f"Trading at {v['pb']:.2f}x book value.")
    if v.get("evEbit") is not None and v["evEbit"] <= 8:
        g(f"EV/EBIT of {v['evEbit']:.1f}x — cheap on a leverage-neutral basis.")
    if v.get("fcfYield") is not None and v["fcfYield"] >= 0.10 and (p.get("netDebtToEbit") or 0) < 2:
        g(f"Free-cash-flow yield of {v['fcfYield']*100:.0f}% with modest leverage.")
    if v.get("dividendYield") and v["dividendYield"] >= 0.06 and healthy:
        g(f"Dividend yield of {v['dividendYield']*100:.1f}% covered by a healthy business.")
    if v.get("priceToNi") is not None and v["priceToNi"] > 25:
        c(f"Priced at ~{v['priceToNi']:.0f} years of earnings — much growth assumed.")
    if v.get("evEbit") is not None and v["evEbit"] > 20:
        c(f"EV/EBIT of {v['evEbit']:.0f}x is demanding.")
    v["clues"] = clues
    return v


# ---------------------------------------------------------------------------
# 2b. INTRINSIC VALUE  — a fair value built from methods that have held up for
#     a very long time. Each is applied only where it makes sense for the kind
#     of business, then the sensible ones are blended into one central estimate
#     with a range and a margin-of-safety buy-below line.
# ---------------------------------------------------------------------------
#
# The methods, and why each has earned its place:
#   * Owner-earnings DCF (Buffett)  — a business is worth the cash it can be
#     taken out of it over its life, discounted back. Owner earnings strip out
#     the accounting noise: net income + depreciation/amortisation (non-cash)
#     minus the capital spending needed just to stand still.
#   * Dividend discount / Gordon growth — the oldest rigorous method (John Burr
#     Williams, 1938; dividends have anchored value for centuries). A share is
#     worth the dividends it will pay, growing, discounted back.
#   * Justified price-to-book, (ROE - g)/(r - g) — for banks and insurers value
#     is driven by book equity and the return earned on it, not by capex/FCF.
#   * Earnings Power Value (Greenwald) — capitalise today's normalised earnings
#     assuming *no* growth. A deliberately conservative "what is it worth if it
#     never grows again" number.
#   * Graham Number, sqrt(22.5 x EPS x book value/share) — Benjamin Graham's
#     defensive ceiling (a fair price pays no more than 15x earnings and 1.5x
#     book). A blunt but durable sanity bound.
#   * Fair P/E reversion — long-run multiples mean-revert; anchor earnings to a
#     sober multiple scaled a little for growth and quality.
#   * Graham's revised formula, EPS x (8.5 + 2g) x 4.4/Y — a growth-and-interest
#     cross-check, kept off to the side because it is the most assumption-heavy.
#   * Net current asset value (Graham net-net) — a liquidation FLOOR, never a
#     target: current assets minus ALL liabilities, per share.

def _fnum(x):
    """True only for a real, finite number. Accepts both Python and NumPy numeric
    scalars - pandas frequently yields numpy int64/float64, and an earlier
    isinstance(x, (int, float)) check silently rejected numpy int64, which blanked
    per-share figures for whole-number rows like share counts. Rejects None, bool,
    strings, NaN and infinities."""
    if isinstance(x, bool):
        return False
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return False
    return xf == xf and xf not in (float("inf"), float("-inf"))


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _median(vals):
    xs = sorted(v for v in vals if _fnum(v))
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def owner_earnings_base(p):
    """Buffett 'owner earnings' for the latest year:
        net income + depreciation & amortisation (non-cash) - maintenance capex.
    Maintenance capex is approximated as the smaller of actual capex and D&A, so
    that spending *above* replacement (growth capex) is not charged against the
    owner, and a company under-spending relative to depreciation is not
    flattered. Returns (owner_earnings, detail_dict) or (None, {})."""
    ni = p.get("ni")
    if not _fnum(ni):
        return None, {}
    da = p.get("da") if _fnum(p.get("da")) else 0.0
    capex = abs(p.get("capex")) if _fnum(p.get("capex")) else 0.0
    if da and capex:
        maint = min(capex, da)
    elif capex:
        maint = capex
    else:
        maint = da                      # no capex figure -> assume ~= depreciation
    return ni + da - maint, {"ni": ni, "da": da, "capex": capex, "maint_capex": maint}


def two_stage_pv(base, g1, term_g, discount, years=10):
    """Present value of a cash-flow stream that starts at `base` (year 0), grows
    at g1 in year 1 and fades linearly to `term_g` by the final explicit year,
    then continues at term_g forever (Gordon terminal value)."""
    if not _fnum(base) or discount is None:
        return None
    if discount <= term_g:
        term_g = discount - 0.01
    pv, cf = 0.0, base
    for yr in range(1, years + 1):
        g = g1 + (term_g - g1) * (yr - 1) / max(years - 1, 1)
        cf *= (1 + g)
        pv += cf / ((1 + discount) ** yr)
    terminal = cf * (1 + term_g) / (discount - term_g)
    pv += terminal / ((1 + discount) ** years)
    return pv


def finite_pv(base, g1, term_g, discount, years, residual=0.0):
    """Present value of a cash-flow stream over a FIXED life - no perpetual
    terminal value. For concession/finite-life assets (a toll road, a mine, a
    single lease) whose cash flows stop when the asset expires or reverts.
    `residual` is a one-off lump sum received in the final year (e.g. a reverting
    asset's scrap/book value); usually zero for build-operate-transfer deals."""
    years = int(years)
    if not _fnum(base) or discount is None or years < 1:
        return None
    if discount <= term_g:
        term_g = discount - 0.01
    pv, cf = 0.0, base
    for yr in range(1, years + 1):
        g = g1 + (term_g - g1) * (yr - 1) / max(years - 1, 1)
        cf *= (1 + g)
        pv += cf / ((1 + discount) ** yr)
    if residual:
        pv += residual / ((1 + discount) ** years)
    return pv


def implied_growth(base_ps, price, term_g, discount, finite=False, years=10, residual=0.0):
    """Reverse DCF: solve for the stage-1 growth rate the CURRENT price implies,
    holding the discount rate and terminal growth fixed. Present value rises with
    growth, so a simple bisection inverts it. Returns (status, g):
      'solved' -> g is the growth the price bakes in;
      'below'  -> the price is covered even if the cash flows shrink;
      'above'  -> even the search ceiling won't justify the price."""
    if not (_fnum(base_ps) and base_ps > 0 and _fnum(price) and price > 0
            and _fnum(discount)):
        return None
    yrs = int(years) if (finite and years) else 10

    def pv(g):
        return (finite_pv(base_ps, g, term_g, discount, yrs, residual) if finite
                else two_stage_pv(base_ps, g, term_g, discount, years=10))

    lo, hi = -0.30, 0.60
    plo, phi = pv(lo), pv(hi)
    if not (_fnum(plo) and _fnum(phi)):
        return None
    if price <= plo:
        return ("below", lo)
    if price >= phi:
        return ("above", hi)
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        (lo, hi) = (mid, hi) if pv(mid) < price else (lo, mid)
    return ("solved", 0.5 * (lo + hi))


def scenario_values(base_ps, g1, term_g, discount, finite=False, years=10, residual=0.0):
    """Bear / base / bull fair values per share: the base case is your assumptions;
    bear cuts growth and demands a higher return; bull does the opposite. Makes the
    uncertainty explicit instead of hiding it inside a single number."""
    if not (_fnum(base_ps) and base_ps > 0):
        return {}

    def val(g, d):
        d = max(d, term_g + 0.02)
        return (finite_pv(base_ps, g, term_g, d, years, residual) if finite
                else two_stage_pv(base_ps, g, term_g, d, years=10))

    return {
        "bear": val(max(term_g, g1 - 0.05), discount + 0.02),
        "base": val(g1, discount),
        "bull": val(g1 + 0.03, discount - 0.02),
        "assumptions": {
            "bear": (max(term_g, g1 - 0.05), discount + 0.02),
            "base": (g1, discount),
            "bull": (g1 + 0.03, discount - 0.02),
        },
    }


def growth_reasonableness(g_impl, p):
    """Judge the price-implied growth against what the business has delivered.
    Returns (kind, delivered_rate, sentence)."""
    hist = [g for g in (p.get("niCagr"), p.get("revCagr")) if _fnum(g)]
    d = max(hist) if hist else None
    if not _fnum(g_impl):
        return ("neutral", d, "")
    if d is not None:
        if g_impl <= max(d, 0.0) - 0.005:
            return ("good", d, "That is LESS than the business has actually delivered, "
                    "so the price is not demanding heroics - it leaves room to be "
                    "positively surprised.")
        if g_impl <= d + 0.03 and g_impl <= 0.15:
            return ("good", d, "That is roughly in line with what it has delivered - a "
                    "reasonable bar to clear.")
        if g_impl <= 0.20:
            return ("caution", d, "That is above its track record - achievable only if "
                    "growth accelerates from here.")
        return ("bad", d, "That is faster than the business has ever sustained - the "
                "price is leaning on optimistic expectations.")
    if g_impl <= 0.08:
        return ("good", None, "a modest bar in absolute terms.")
    if g_impl <= 0.15:
        return ("neutral", None, "a moderate bar; there is limited history to judge it.")
    return ("caution", None, "a demanding bar, with little history to support it.")


# Default recovery rates for a liquidation/salvage estimate - the fraction of
# each asset's reported book value a seller could realistically recover, net of
# ALL liabilities at 100%. Roughly Graham/Buffett conventions; user-adjustable.
DEFAULT_RECOVERY = {"invest": 0.90, "recv": 0.80, "inv": 0.60, "ppe": 0.40, "other": 0.20}


def asset_backing(balance, p, shares, recovery=None):
    """Balance-sheet downside: what the assets alone are worth per share, net of
    ALL liabilities, at conservative recovery rates. These are FLOORS, not fair
    values. Returns per-share figures (JMD) or {} if the data is insufficient."""
    if balance is None or not shares:
        return {}
    assets = p.get("assets")
    equity = p.get("equity")
    if not (_fnum(assets) and _fnum(equity)):
        return {}
    rr = dict(DEFAULT_RECOVERY)
    if recovery:
        rr.update(recovery)

    cash   = p.get("cash") or 0.0
    debt   = p.get("debt") or 0.0
    ppe    = p.get("ppe") or 0.0
    cura   = p.get("curAssets") or 0.0
    invest = _last(_pick(balance, "Total Investments", "Long-Term Investments")) or 0.0
    recv   = _last(_pick(balance, "Receivables", "Accounts Receivable",
                         "Net Receivables", "Total Receivables")) or 0.0
    invy   = _last(_pick(balance, "Inventory", "Inventories", "Total Inventory")) or 0.0
    total_liab = assets - equity

    known = cash + invest + recv + invy + ppe
    other = max(assets - known, 0.0)          # everything not separately haircut

    liq_assets = (cash + invest * rr["invest"] + recv * rr["recv"]
                  + invy * rr["inv"] + ppe * rr["ppe"] + other * rr["other"])
    liquidation = liq_assets - total_liab
    nnwc = cash + 0.75 * recv + 0.50 * invy - total_liab      # Graham net-net WC
    ncav = cura - total_liab                                  # Graham NCAV
    net_cash = cash - debt

    def ps(x):
        return x / shares
    return {
        "netCashPs": ps(net_cash),
        "nnwcPs": ps(nnwc),
        "ncavPs": ps(ncav),
        "liquidationPs": ps(liquidation),
        "bookPs": ps(equity),
        "debtFree": debt <= 0.02 * assets,
        "hasDebt": debt > 0.02 * assets,
        "recovery": rr,
        "parts": {"cash": cash, "invest": invest, "recv": recv, "inv": invy,
                  "ppe": ppe, "other": other, "liab": total_liab},
    }


def per_share_xray(income, balance, cashflow, p, shares, price, ctype):
    """Every balance-sheet and income line divided by shares, alongside the price,
    plus a RIGOROUS set of undervaluation signals. Rigour matters: a gross asset
    (or revenue) above the price is NOT cheap on its own - it is usually funded by
    debt, or is low-margin. Only figures net of ALL liabilities, or sales backed by
    real margins, are treated as signals. Everything else is shown for context only."""
    if not (shares and shares > 0):
        return {}

    def ps(v):
        return (v / shares) if _fnum(v) else None

    cash = p.get("cash") or 0.0
    debt = p.get("debt") or 0.0
    assets = p.get("assets")
    equity = p.get("equity")
    curassets = p.get("curAssets")
    invest = _last(_pick(balance, "Total Investments", "Long-Term Investments"))
    recv = _last(_pick(balance, "Receivables", "Accounts Receivable", "Net Receivables"))
    invy = _last(_pick(balance, "Inventory", "Inventories"))
    ppe = p.get("ppe")
    total_liab = (assets - equity) if (_fnum(assets) and _fnum(equity)) else None

    asset_items = [("Cash & equivalents", cash), ("Investments", invest),
                   ("Receivables", recv), ("Inventory", invy),
                   ("Property, plant & equipment", ppe)]
    known = sum(v for _, v in asset_items if _fnum(v))
    if _fnum(assets):
        asset_items.append(("Other assets", max(assets - known, 0.0)))
    assets_ps = [(lbl, ps(v)) for lbl, v in asset_items if _fnum(v) and abs(v) > 0]

    liab_ps = [(lbl, ps(v)) for lbl, v in
               [("Total debt", debt), ("Current liabilities", p.get("curLiab")),
                ("All liabilities", total_liab)] if _fnum(v) and abs(v) > 0]

    income_ps = [(lbl, ps(v)) for lbl, v in
                 [("Revenue", p.get("rev")),
                  ("Gross profit", _last(_pick(income, "Gross Profit"))),
                  ("Operating income (EBIT)", p.get("ebitUsed")),
                  ("Net income (EPS)", p.get("ni")),
                  ("Free cash flow", p.get("fcf"))] if _fnum(v)]

    # --- Vetted signal checks ----------------------------------------------
    # Each consequential per-share figure vs the price, with an explicit verdict
    # and the reasoning - so a figure that merely sits above/below the price is
    # explained rather than silently (mis)read as cheap.
    net_cash_ps = ps(cash - debt)
    ncav_ps = ps(curassets - total_liab) if (_fnum(curassets) and _fnum(total_liab)) else None
    book_ps = ps(equity)
    rev_ps = ps(p.get("rev"))
    recv_ps = ps(recv)
    liab_ps_val = ps(total_liab)
    nm = p.get("netMargin")
    fin = ctype in ("reit", "bank", "insurer", "holding")
    # A deposit-taking bank / insurer: net-net, 'cash less all liabilities', NCAV and
    # P/S are meaningless (its liabilities are deposits/reserves; its 'revenue' is
    # interest/premium income). For those, only book value is a valid per-share check.
    deposit_fin = ctype in ("bank", "insurer")
    above = (lambda v: _fnum(v) and _fnum(price) and price > 0 and v >= price)

    checks = []

    def chk(label, val, status, why):
        if _fnum(val):
            checks.append({"label": label, "val": val, "status": status, "why": why})

    if not deposit_fin and above(net_cash_ps):
        chk("Net cash / share", net_cash_ps, "strong",
            "Cash minus ALL debt already exceeds the price - you'd be getting the "
            "operating business for free (or be paid to take it). The strongest "
            "asset-based signal there is.")
    elif not deposit_fin:
        chk("Net cash / share", net_cash_ps, "none",
            "Below the price, which is normal - a healthy business is worth more than "
            "its spare cash. Only a signal when it rises above the price.")

    if not deposit_fin and above(ncav_ps):
        chk("Net current assets (net-net) / share", ncav_ps, "strong",
            "Current assets net of EVERY liability still exceed the price - Graham's "
            "net-net, a hard liquidation floor. This is the rigorous version of "
            "'assets vs price': it already subtracts all debt.")
    elif not deposit_fin and _fnum(ncav_ps):
        chk("Net current assets (net-net) / share", ncav_ps, "none",
            "Below the price. This already nets off all liabilities, so it - not raw "
            "receivables or inventory - is the honest 'assets vs price' test.")

    if above(book_ps):
        if fin:
            chk("Book value (~ NAV) / share", book_ps, "moderate",
                "For a bank, insurer or property company book value is close to net "
                "asset value, so trading below it is a genuine discount to NAV.")
        else:
            chk("Book value / share", book_ps, "weak",
                "P/B below 1, but book is historical cost - a below-book industrial "
                "can be cheap OR a value trap. Confirm the assets actually earn.")
    elif _fnum(book_ps):
        chk("Book value / share", book_ps, "none",
            "Below the price (P/B > 1) - no discount to book here.")

    if not deposit_fin and above(rev_ps):
        if _fnum(nm) and nm >= 0.08:
            chk("Revenue / share", rev_ps, "moderate",
                f"Price is under a year's sales (P/S {price/rev_ps:.2f}) AND the "
                f"business earns a real {nm*100:.0f}% net margin - a low sales multiple "
                "on genuine profit, which is worth a look.")
        else:
            chk("Revenue / share", rev_ps, "none",
                "Price is under a year's sales (P/S < 1), BUT the net margin is thin - "
                "low-margin businesses routinely trade below sales, so on its own this "
                "is not a signal.")
    elif not deposit_fin and _fnum(rev_ps):
        chk("Revenue / share", rev_ps, "none",
            "Price is ABOVE a year's sales (P/S > 1). That is the expensive direction - "
            "revenue sitting below the price is not a cheapness signal.")

    if not deposit_fin and _fnum(recv_ps):
        chk("Receivables / share", recv_ps, "context",
            "A single asset line. Whether it's above or below the price says nothing on "
            "its own - it only counts net of all liabilities, which the net-current-"
            "assets check above already does.")
    if not deposit_fin and _fnum(liab_ps_val):
        chk("All liabilities / share", liab_ps_val, "context",
            "What the company OWES, shown for scale - not value it holds. Liabilities "
            "below the price is not a signal; what matters is assets NET of these.")

    return {
        "assets": assets_ps, "assets_total": ps(assets),
        "liab": liab_ps, "book_ps": book_ps,
        "income": income_ps, "checks": checks, "price": price,
    }


def _cagr_of(d):
    ys = sorted(d)
    if len(ys) >= 2 and _fnum(d[ys[0]]) and d[ys[0]] > 0:
        try:
            return (d[ys[-1]] / d[ys[0]]) ** (1 / (len(ys) - 1)) - 1
        except Exception:
            return None
    return None


def liquidation_test(income, balance, cashflow, p, shares, price, currency, recovery=None):
    """The rigorous version of 'price below receivables/assets'. Computes a
    Conservative Liquidation Value per share - cash + haircut receivables +
    haircut inventory, LESS every liability - and a coverage ratio vs the price,
    THEN gates it with validation checks (receivables collectibility, cash
    conversion, receivables vs payables, cash burn, dilution, a catalyst). A raw
    'price < receivables' that fails the net-of-liabilities test is reported as a
    FALSE signal, with the reason. Returns {} if the balance sheet is too thin."""
    if not (shares and shares > 0):
        return {}
    assets, equity = p.get("assets"), p.get("equity")
    if not (_fnum(assets) and _fnum(equity)):
        return {}
    rr = dict(DEFAULT_RECOVERY)
    if recovery:
        rr.update(recovery)
    cur = currency or "JMD"
    recv_rate, inv_rate = rr["recv"], rr["inv"]

    cash = p.get("cash") or 0.0
    total_liab = assets - equity
    curassets = p.get("curAssets")
    recv = _last(_pick(balance, "Receivables", "Accounts Receivable", "Net Receivables")) or 0.0
    inv = _last(_pick(balance, "Inventory", "Inventories")) or 0.0
    payables = _last(_pick(balance, "Accounts Payable", "Payables",
                           "Accounts Payable & Accrued")) or 0.0
    st_debt = p.get("currentDebtDue") or 0.0

    def ps(v):
        return (v / shares) if _fnum(v) else None

    def m(v):
        return "-" if not _fnum(v) else f"{cur} {v:,.2f}"

    def m0(v):
        return f"{cur} {v:,.0f}"

    crlv_ps = ps(cash + recv * recv_rate + inv * inv_rate - total_liab)
    net_cash_ps = ps(cash - total_liab)
    ncav_ps = ps(curassets - total_liab) if _fnum(curassets) else None
    coverage = (crlv_ps / price) if (_fnum(crlv_ps) and _fnum(price) and price > 0) else None

    checks = []

    def add(status, name, text):
        checks.append({"status": status, "name": name, "text": text})

    add("info", "All liabilities netted, not just debt",
        f"Every liability on the balance sheet ({m0(total_liab)}) is subtracted - "
        "payables, borrowings, leases, tax and provisions, not only bank debt. "
        "Off-balance-sheet items (guarantees, uncapitalised leases, disputes) aren't "
        "visible here and would need the audited notes.")

    rg = _cagr_of(_row_by_year(balance, "Receivables", "Accounts Receivable", "Net Receivables"))
    sg = _cagr_of(_row_by_year(income, "Total Revenue", "Revenue"))
    if _fnum(rg) and _fnum(sg):
        if rg - sg > 0.15:
            add("fail", "Receivables outgrowing sales",
                f"Receivables have grown ~{rg*100:.0f}%/yr against sales ~{sg*100:.0f}%/yr. "
                "When receivables balloon faster than revenue the company may be booking "
                f"sales it isn't collecting - the {recv_rate*100:.0f}% recovery assumption "
                "is likely optimistic, and the discount may be illusory.")
        elif rg - sg > 0.05:
            add("warn", "Receivables growing faster than sales",
                f"Receivables ~{rg*100:.0f}%/yr vs sales ~{sg*100:.0f}%/yr - a yellow flag "
                "on collectibility; watch the next cash-flow statement.")
        else:
            add("pass", "Receivables in line with sales",
                f"Receivables (~{rg*100:.0f}%/yr) aren't outrunning sales (~{sg*100:.0f}%/yr), "
                "consistent with real, collectible balances.")
    add("info", "Ageing not in this data",
        "The feed has no receivables ageing. Government or large, solvent counterparties "
        "can justify the haircut; old, disputed or related-party balances would need a "
        "deeper cut - confirm in the audited notes.")

    ocf_y = _row_by_year(cashflow, "Operating Cash Flow")
    ni_y = _row_by_year(income, "Net Income to Common", "Net Income")
    common = sorted(set(ocf_y) & set(ni_y))
    if common:
        cum_ocf = sum(ocf_y[y] for y in common)
        cum_ni = sum(ni_y[y] for y in common)
        if cum_ni > 0:
            cc = cum_ocf / cum_ni
            if cc < 0.5:
                add("fail", "Profits not backed by cash",
                    f"Over {len(common)} years operating cash flow was only {cc*100:.0f}% of "
                    "reported profit - earnings, and the receivables behind them, aren't "
                    "converting to cash.")
            elif cc < 0.8:
                add("warn", "Cash conversion soft",
                    f"Operating cash flow is {cc*100:.0f}% of profit over {len(common)} years - "
                    "adequate but worth watching.")
            else:
                add("pass", "Earnings backed by cash",
                    f"Operating cash flow is {cc*100:.0f}% of cumulative profit - the "
                    "receivables have historically turned into cash.")

    net_recv = recv - payables - st_debt
    if recv > 0:
        if net_recv > 0:
            add("pass", "Receivables exceed near-term claims",
                f"After netting payables and current debt, net receivables are {m0(net_recv)} "
                f"({m(ps(net_recv))}/sh) - they aren't merely offsetting what's owed to "
                "suppliers and lenders.")
        else:
            add("warn", "Receivables offset by payables/current debt",
                "Payables and current debt roughly match or exceed receivables, so much of "
                "the receivable is already spoken for by near-term claims.")

    fcf = p.get("fcf")
    if p.get("structurallyWeak") or (p.get("lossYears") or 0) >= max(2, (p.get("totalYears") or 0) - 1):
        add("fail", "Loss-making - the discount can erode",
            "The business loses money in most years, so cash burn can consume the asset "
            "discount before shareholders ever see it. A liquidation floor only helps if "
            "realisation is near or the losses stop.")
    elif _fnum(fcf) and fcf < 0:
        burn_ps = ps(abs(fcf))
        adj = (crlv_ps - 2 * burn_ps) if (_fnum(crlv_ps) and _fnum(burn_ps)) else None
        add("warn", "Burning cash",
            f"Free cash flow is negative (~{m(burn_ps)}/sh a year). If liquidation isn't "
            f"imminent, subtract a couple of years of burn: adjusted value ~ {m(adj)}/sh.")
    else:
        add("pass", "Profitable / cash-generative",
            "Not burning the discount away - profits or positive cash flow protect the "
            "asset value while you wait.")

    sh_g, iss = p.get("shareGrowth"), p.get("netIssuance")
    if _fnum(sh_g) and sh_g > 0.05:
        add("warn", "Diluting shareholders",
            f"Share count has grown ~{sh_g*100:.0f}% - issuing stock waters down the very "
            "discount you're buying.")
    elif _fnum(iss) and iss < 0:
        add("pass", "Not diluting / returning capital",
            "Net buybacks or no issuance - management isn't watering down the discount.")

    if p.get("paysDividend"):
        add("pass", "A route to value exists",
            "The company pays dividends, so collected value has a channel back to holders.")
    else:
        add("warn", "No visible catalyst",
            "No dividend or buyback in the data - without a liquidation, sale or payout, "
            "value can stay trapped for years while management sits on it.")

    fails = sum(1 for c in checks if c["status"] == "fail")
    order = ["thin", "qualified", "strong", "exceptional"]
    if not _fnum(coverage):
        level, summary = "na", "Not enough balance-sheet data for a liquidation test."
    elif coverage < 1.0:
        level = "rejected"
        summary = (f"**Not a real signal.** Once EVERY liability is netted off, conservative "
                   f"liquidation value is {m(crlv_ps)}/share - below the price of {m(price)} "
                   f"(coverage {coverage:.2f}x). A raw 'price under receivables' here is an "
                   "optical bargain: the receivables are already claimed by creditors.")
    else:
        base = ("exceptional" if coverage >= 2.0 else "strong" if coverage >= 1.5
                else "qualified" if coverage >= 1.25 else "thin")
        if fails >= 2:
            level = "weak"
        elif fails == 1:
            level = order[min(order.index(base), 1)]      # cap at 'qualified'
        else:
            level = base
        head = {"exceptional": "Potentially exceptional", "strong": "Strong asset backing",
                "qualified": "Some asset backing (caveated)", "thin": "Thin coverage",
                "weak": "Coverage on paper only"}.get(level, level)
        summary = (f"**{head}.** Conservative liquidation value {m(crlv_ps)}/share vs price "
                   f"{m(price)} = **{coverage:.2f}x coverage**. "
                   + ("The validation checks hold up." if fails == 0
                      else f"But {fails} check(s) failed - the coverage exists on paper, but "
                           "collectibility/quality concerns undercut it, so do not treat it "
                           "as a clean signal."))
    return {
        "crlv_ps": crlv_ps, "net_cash_ps": net_cash_ps, "ncav_ps": ncav_ps,
        "coverage": coverage, "price": price, "recv_rate": recv_rate,
        "inv_rate": inv_rate, "checks": checks, "level": level, "summary": summary,
        "recv_material": recv > 0.05 * (assets or 1),
    }


def _growth_estimate(p, term_g):
    """A single, deliberately sober stage-1 growth rate for the DCF: the lower of
    earnings and revenue CAGR (so a one-off earnings spike can't run away with
    it), clamped to a sane band and never below the long-term rate."""
    cands = [g for g in (p.get("niCagr"), p.get("revCagr")) if _fnum(g)]
    if not cands:
        g = p.get("ebitCagr") if _fnum(p.get("ebitCagr")) else term_g
    else:
        g = min(cands)
    g = _clamp(g, -0.05, 0.12)
    return max(g, term_g)


def _sustainable_growth(p, term_g, discount):
    """Near-term reinvestment-funded growth, g = ROE x (1 - payout), used as the
    *stage-one* rate in the two-stage models. Capped to a sane band and kept below
    the discount rate."""
    roe = p.get("roe")
    if not _fnum(roe) or roe <= 0:
        return term_g
    payout = p.get("divToNi")
    payout = _clamp(payout, 0.0, 1.0) if _fnum(payout) else 0.5
    g = roe * (1 - payout)
    return _clamp(g, 0.0, min(0.12, discount - 0.005))


def _perpetual_growth(g_sust, term_g, discount):
    """Growth safe to assume *forever* in a single-stage perpetuity (Gordon /
    justified price-to-book). High reinvestment growth cannot persist, so it is
    pulled down to a long-run rate and always kept a comfortable 3 points below
    the discount rate, which stops these formulas from exploding as g -> r."""
    gp = min(g_sust, term_g + 0.02)
    return _clamp(gp, 0.0, max(0.0, discount - 0.03))


def intrinsic_valuation(income, balance, cashflow, p, ratios, ctype,
                        discount, term_g, currency, ticker, mos=0.25, quote=None,
                        asset_life=0, residual_pct=0.0, recovery=None):
    """Build a fair value from several long-standing methods, blend the ones that
    suit this kind of business into a central estimate with a range, and compare
    it to the live market price. All per-share figures are returned in JMD.

    Returns a dict:
      price, shares, eps, bvps, dps, oeps        -> the per-share building blocks
      methods  -> list of {name, value, core, applies, basis, why, note}
      central, low, high, buy_below              -> the blended read + MoS line
      upside, verdict, band                      -> price vs central
    """
    fx = (_FX_CONTEXT.get(ticker, {}) or {}).get("rate") or 1.0

    # Share count. Statements were multiplied by fx to reach JMD, and the share
    # row rides along with them, so divide it back out. Every per-share number is
    # then (JMD absolute) / (true shares) = a genuine JMD-per-share figure.
    shares_scaled = None
    for src in (balance, income):
        if src is None:
            continue
        for nm in ("Total Common Shares Outstanding", "Shares Outstanding (Basic)",
                   "Basic Shares Outstanding", "Shares Outstanding",
                   "Filing Date Shares Outstanding"):
            if nm in src.index:
                s = clean_series(src, nm)
                if len(s) and _fnum(s.iloc[-1]) and s.iloc[-1] > 0:
                    shares_scaled = float(s.iloc[-1])
                    break
        if shares_scaled:
            break
    if shares_scaled is None and _fnum(p.get("sharesLast")) and p["sharesLast"] > 0:
        shares_scaled = float(p["sharesLast"])
    shares = shares_scaled / fx if shares_scaled else None

    def per_share(total):
        return (total / shares) if (shares and _fnum(total)) else None

    eps  = per_share(p.get("ni"))
    bvps = per_share(p.get("equity"))
    dps  = per_share(p.get("divPaid"))
    oe, oe_detail = owner_earnings_base(p)
    oeps = per_share(oe)
    fcfps = per_share(p.get("fcf"))

    # Current price. Market data (price, 52-week range, market cap) may be quoted
    # in JMD or in the USD *reporting* currency depending on the listing - a company
    # can report in USD yet trade in JMD (e.g. TJH), so the reporting currency does
    # NOT tell us the quote currency. Rather than assume, pick the interpretation
    # (as-is vs x FX) whose implied price sits at a sane multiple of book value per
    # share. The intrinsic per-share figures are already genuine JMD, so book value
    # per share is a reliable JMD-scale anchor.
    mcap = (ratios or {}).get("marketcap")
    q_price = (quote or {}).get("price")
    anchor = bvps if (_fnum(bvps) and bvps > 0) else (
        eps * 12.0 if (_fnum(eps) and eps > 0) else None)

    def _mkt_mult(raw_ps):
        """1.0 or fx, whichever lands raw closest (in log-distance) to the JMD
        book-value anchor. 1.0 for JMD reporters (fx==1) or when no anchor."""
        if abs(fx - 1.0) < 1e-9 or not (_fnum(raw_ps) and raw_ps > 0
                                        and _fnum(anchor) and anchor > 0):
            return 1.0
        asis = abs(math.log(raw_ps / anchor))
        withfx = abs(math.log(raw_ps * fx / anchor))
        return fx if withfx < asis else 1.0

    test_ps = q_price if (_fnum(q_price) and q_price > 0) else (
        (mcap / shares) if (shares and _fnum(mcap)) else None)
    mkt_mult = _mkt_mult(test_ps)

    mcap_jmd = mcap * mkt_mult if _fnum(mcap) else None
    derived_price = (mcap_jmd / shares) if (shares and _fnum(mcap_jmd)) else None
    q_jmd = q_price * mkt_mult if _fnum(q_price) else None

    price = derived_price
    price_source = "market cap / shares" if _fnum(derived_price) else None
    price_date = None
    quote_ok = False
    # The live quote already passed the book-value currency anchor above, so trust
    # it whenever present; market-cap/shares is only a fallback (its 'in millions'
    # scale is ambiguous and must never override a validated quote).
    if _fnum(q_jmd):
        price = q_jmd
        price_source = "latest close (price history)"
        price_date = (quote or {}).get("date")
        quote_ok = True

    # Only trust the 52-week range if the quote itself passed the sanity check.
    low52 = (quote or {}).get("low52") if quote_ok else None
    high52 = (quote or {}).get("high52") if quote_ok else None
    low52_jmd = low52 * mkt_mult if _fnum(low52) else None
    high52_jmd = high52 * mkt_mult if _fnum(high52) else None

    roe = p.get("roe")
    g1  = _growth_estimate(p, term_g)
    g_sust = _sustainable_growth(p, term_g, discount)
    g_perp = _perpetual_growth(g_sust, term_g, discount)

    fin = ctype in ("bank", "insurer", "holding")
    methods = []

    def add(name, value, core, applies, basis, why, note=""):
        methods.append({
            "name": name,
            "value": value if (_fnum(value) and value > 0) else None,
            "core": core and applies and _fnum(value) and value > 0,
            "applies": applies,
            "basis": basis,
            "why": why,
            "note": note,
        })

    # --- Owner-earnings DCF (Buffett) -- operating businesses only -----------
    # If a finite asset life is set (e.g. a concession that expires and reverts),
    # the cash flows stop at that horizon - no perpetual terminal value.
    finite = _fnum(asset_life) and asset_life and asset_life > 0
    resid_ps = (residual_pct or 0.0) * (bvps or 0.0)
    oe_dcf = None
    if not fin and _fnum(oeps) and oeps > 0:
        oe_dcf = (finite_pv(oeps, g1, term_g, discount, asset_life, resid_ps)
                  if finite else two_stage_pv(oeps, g1, term_g, discount, years=10))
    _dcf_name = (f"Owner-earnings DCF ({int(asset_life)}-yr life)" if finite
                 else "Owner-earnings DCF (Buffett)")
    add(_dcf_name, oe_dcf, core=True, applies=not fin,
        basis=(f"OE/sh {currency} {oeps:,.2f} over {int(asset_life)} yrs then the "
               f"asset expires" + (f" (residual {currency} {resid_ps:,.2f})" if resid_ps else "")
               if finite else
               f"OE/sh {currency} {oeps:,.2f} - grow {g1*100:.1f}% fading to "
               f"{term_g*100:.1f}%, discount {discount*100:.1f}%") if _fnum(oeps) else
              "owner earnings not positive/available",
        why="A business is worth the cash an owner can pull from it over its life, "
            "discounted to today. Owner earnings = net income + non-cash "
            "depreciation - the capex needed just to hold position."
            + (" With a finite asset life the stream stops at expiry rather than "
               "compounding forever - the right treatment for a concession." if finite else ""),
        note="" if oe_dcf else "Needs positive owner earnings; skipped.")

    # --- Free-cash-flow DCF (cross-check for operating businesses) -----------
    fcf_dcf = None
    if not fin and _fnum(fcfps) and fcfps > 0:
        fcf_dcf = (finite_pv(fcfps, g1, term_g, discount, asset_life, resid_ps)
                   if finite else two_stage_pv(fcfps, g1, term_g, discount, years=10))
    add("Free-cash-flow DCF", fcf_dcf, core=False, applies=not fin,
        basis=f"FCF/sh {currency} {fcfps:,.2f}, same horizon/discount" if _fnum(fcfps)
              else "free cash flow not positive/available",
        why="The same discounting logic run on reported free cash flow, as an "
            "independent check on the owner-earnings number.")

    # --- Earnings Power Value (Greenwald) -- no-growth capitalised earnings --
    epv = None
    if not (ctype in ("bank", "insurer")):
        nopat = p.get("nopat")
        if _fnum(nopat) and nopat > 0 and discount:
            epv_equity = nopat / discount + (p.get("cash") or 0.0) - (p.get("debt") or 0.0)
            epv = per_share(epv_equity)
    add("Earnings Power Value", epv, core=True,
        applies=ctype in ("industrial", "reit", "holding"),
        basis="NOPAT capitalised at the discount rate, no growth, plus net cash",
        why="Values only today's proven earning power with zero growth assumed - "
            "a conservative floor on a profitable, durable business.")

    # --- Justified price-to-book, (ROE - g)/(r - g) -- financials ------------
    jpb = None
    if _fnum(roe) and roe > 0 and _fnum(bvps) and bvps > 0 and discount > g_perp:
        pb_fair = (roe - g_perp) / (discount - g_perp)
        if pb_fair > 0:
            jpb = pb_fair * bvps
    add("Justified price-to-book", jpb, core=fin,
        applies=fin or ctype == "reit",
        basis=f"fair P/B = (ROE {(_pct(roe))} - g {g_perp*100:.1f}%) / "
              f"(r {discount*100:.1f}% - g); x book value/sh {currency} "
              f"{bvps:,.2f}" if _fnum(bvps) else "book value/share unavailable",
        why="For banks and insurers value is driven by equity capital and the "
            "return earned on it. A fair multiple of book falls straight out of "
            "ROE, growth and the required return.")

    # --- Dividend discount (two-stage) -- any reliable dividend payer --------
    ddm = None
    pays = _fnum(dps) and dps > 0
    if pays:
        ddm = two_stage_pv(dps, g_sust, term_g, discount, years=10)
    income_led = fin or ctype == "reit"      # types where payouts drive value
    add("Dividend discount (two-stage)", ddm, core=(pays and income_led), applies=pays,
        basis=f"D/sh {currency} {dps:,.2f} grown {g_sust*100:.1f}% fading to "
              f"{term_g*100:.1f}%, discount {discount*100:.1f}%" if _fnum(dps)
              else "no dividend paid",
        why="The oldest rigorous method (Williams, 1938): a share is worth the "
            "growing stream of dividends it pays, discounted back. Two stages - a "
            "faster near-term rate settling to a perpetual one - avoid the classic "
            "blow-up when growth nears the discount rate.",
        note="" if pays else "Company pays no dividend; not applicable.")

    # --- Graham Number, sqrt(22.5 x EPS x BVPS) -----------------------------
    graham_n = None
    if _fnum(eps) and eps > 0 and _fnum(bvps) and bvps > 0:
        graham_n = (22.5 * eps * bvps) ** 0.5
    add("Graham Number", graham_n, core=(not fin), applies=True,
        basis=f"sqrt(22.5 x EPS {currency} {eps:,.2f} x BVPS {currency} {bvps:,.2f})"
              if (_fnum(eps) and _fnum(bvps)) else "needs positive EPS and book value",
        why="Graham's defensive ceiling: never pay more than 15x earnings and "
            "1.5x book (15 x 1.5 = 22.5). A durable upper sanity bound.")

    # --- Fair P/E reversion -------------------------------------------------
    pe_val = None
    if _fnum(eps) and eps > 0:
        pe_fair = 8.0 + 100 * _clamp(g1, 0.0, 0.12)          # 8x .. 20x
        if _fnum(roe) and roe > 0.18:
            pe_fair += 1.5
        pe_ceiling = 15.0 if fin else 20.0    # banks rarely sustain a high multiple
        pe_fair = _clamp(pe_fair, 7.0, pe_ceiling)
        pe_val = eps * pe_fair
    add("Fair P/E reversion", pe_val, core=True, applies=_fnum(eps) and eps > 0,
        basis=f"EPS {currency} {eps:,.2f} x a growth/quality-scaled multiple"
              if _fnum(eps) else "needs positive EPS",
        why="Multiples mean-revert over the long run. Anchor sober, normalised "
            "earnings to a multiple that leans conservative and only edges up "
            "for genuine growth and high returns on equity.")

    # --- P/FFO for REITs ----------------------------------------------------
    if ctype == "reit":
        ffops = per_share(p.get("ffo"))
        ffo_val = ffops * 13.0 if (_fnum(ffops) and ffops > 0) else None
        add("Price / FFO", ffo_val, core=True, applies=True,
            basis=f"FFO/sh {currency} {ffops:,.2f} x 13" if _fnum(ffops)
                  else "funds from operations unavailable",
            why="Property companies are valued on funds from operations (earnings "
                "plus property depreciation), the cash a rent roll throws off.")

    # --- Graham's revised formula (cross-check, off to the side) ------------
    graham_rev = None
    if _fnum(eps) and eps > 0:
        gpct = 100 * _clamp(g1, 0.0, 0.10)
        yld = max(discount * 100, 4.4)
        graham_rev = eps * (8.5 + 2 * gpct) * (4.4 / yld)
    add("Graham revised formula", graham_rev, core=False,
        applies=_fnum(eps) and eps > 0,
        basis="EPS x (8.5 + 2g) x 4.4/required-return",
        why="Graham's later growth-and-interest formula. Kept as a cross-check "
            "only - it is the most sensitive to the growth rate you feed it.")

    # --- Net current asset value (liquidation FLOOR, never a target) --------
    ncav = None
    if _fnum(p.get("curAssets")) and _fnum(p.get("assets")) and _fnum(p.get("equity")):
        total_liab = p["assets"] - p["equity"]
        ncav_total = p["curAssets"] - total_liab
        ncav = per_share(ncav_total) if ncav_total > 0 else None
    add("Net current asset value (floor)", ncav, core=False, applies=not fin,
        basis="current assets - ALL liabilities, per share",
        why="Graham's net-net: a liquidation floor. Rarely reached, but when the "
            "price is near it you are getting the operating business for free.")

    # --- Blend the core, applicable methods into a central read -------------
    core_vals = [m["value"] for m in methods if m["core"]]
    central = _median(core_vals)
    low  = min(core_vals) if core_vals else None
    high = max(core_vals) if core_vals else None
    buy_below = central * (1 - mos) if _fnum(central) else None

    upside = (central / price - 1) if (_fnum(central) and _fnum(price) and price > 0) else None
    verdict, band = _valuation_verdict(price, central, buy_below)

    backing = asset_backing(balance, p, shares, recovery)
    dividend = _dividend_safety(dps, price, p)
    expret = _expected_return(price, central, dividend.get("yield"))

    return {
        "fx": fx, "shares": shares, "currency": currency,
        "price": price, "price_source": price_source, "price_date": price_date,
        "low52": low52_jmd, "high52": high52_jmd,
        "eps": eps, "bvps": bvps, "dps": dps,
        "oeps": oeps, "fcfps": fcfps, "oe_detail": oe_detail,
        "g1": g1, "g_sust": g_sust, "discount": discount, "term_g": term_g,
        "asset_life": int(asset_life) if finite else 0,
        "methods": methods, "n_core": len(core_vals),
        "central": central, "low": low, "high": high, "buy_below": buy_below,
        "mos": mos, "upside": upside, "verdict": verdict, "band": band,
        "ncav": ncav, "graham_n": graham_n, "backing": backing, "ctype": ctype,
        "dividend": dividend, "expret": expret, "dqFlags": _data_flags(p),
    }


def _dividend_safety(dps, price, p):
    """Yield, payout ratios (of earnings AND of free cash flow) and a plain-language
    coverage verdict. Empty dict when the company pays no dividend."""
    if not (_fnum(dps) and dps > 0):
        return {}
    d = {"dps": dps}
    d["yield"] = (dps / price) if (_fnum(price) and price > 0) else None
    d["payoutNi"] = p.get("divToNi")
    d["payoutFcf"] = p.get("divToFcf")
    ni, fcf = d["payoutNi"], d["payoutFcf"]
    if _fnum(ni) and ni > 1.0:
        d["safety"] = ("bad", "The dividend is bigger than net income - it is being "
                       "funded from reserves or borrowing, not current profit.")
    elif _fnum(fcf) and fcf > 1.0:
        d["safety"] = ("caution", "Covered by earnings but not by free cash flow - "
                       "vulnerable to a trim if cash gets tight.")
    elif _fnum(ni) and ni <= 0.85 and (not _fnum(fcf) or fcf <= 0.9):
        d["safety"] = ("good", "Comfortably covered by both earnings and free cash flow.")
    else:
        d["safety"] = ("caution", "Coverage is adequate but not generous - a downturn "
                       "would squeeze it.")
    return d


def _expected_return(price, central, div_yield):
    """A rough expected annualised return if the price converges to the central
    fair value over five years, plus the dividend collected along the way."""
    if not (_fnum(price) and price > 0 and _fnum(central) and central > 0):
        return {}
    rerate = (central / price) ** (1 / 5.0) - 1.0
    dy = div_yield if _fnum(div_yield) else 0.0
    return {"rerate": rerate, "divYield": dy, "annual5y": rerate + dy}


def _data_flags(p):
    """Reasons to trust the valuation less, for a plain-language reliability note."""
    flags = list(p.get("dqFlags") or [])
    if p.get("ipoDistorted") or (p.get("totalYears") or 0) < 4:
        flags.append("short or IPO-distorted history (less than ~4 clean years)")
    if p.get("recentCollapse"):
        flags.append("earnings swung from profit to loss in the latest year")
    if p.get("structurallyWeak"):
        flags.append("losses in most years on record")
    return flags


def _row_by_year(df, *names):
    """One statement line as {calendar_year:int -> value}, dropping non-numeric."""
    s = _pick(df, *names)
    if s is None:
        return {}
    out = {}
    for col, val in s.items():
        if not _fnum(val):
            continue
        try:
            y = int(str(col)[:4])
        except Exception:
            continue
        out[y] = float(val)
    return out


def _price_in_year(series, year, mult=1.0):
    """Last close within `year` (or the most recent close before it) from an
    ascending (date, close) series, scaled by `mult`. None if dates aren't year-
    parseable or nothing is on/before that year."""
    best = None
    for d, c in series:
        try:
            y = int(str(d)[:4])
        except Exception:
            return None
        if y <= year and _fnum(c):
            best = c * mult
        elif y > year:
            break
    return best


def _pctile(cur, vals):
    """Fraction of history at or below `cur` (0=cheapest end, 1=dearest)."""
    xs = [v for v in vals if _fnum(v)]
    if not xs or not _fnum(cur):
        return None
    return sum(1 for v in xs if v <= cur) / len(xs)


def valuation_history(rhist, income, balance, cashflow, price_series, fx,
                      cur_price, cur_eps, cur_bvps, cur_dps):
    """Per-year P/E, P/B and dividend yield versus today, so a stock can be judged
    against its OWN valuation history. Prefers the ratios feed's own history arrays
    (`rhist`); falls back to reconstructing them from year-end prices. Returns {}."""
    pe = dict(((rhist or {}).get("pe") or {}).get("byYear") or {})
    pb = dict(((rhist or {}).get("pb") or {}).get("byYear") or {})
    dy = dict(((rhist or {}).get("yield") or {}).get("byYear") or {})
    pe_vals = list(((rhist or {}).get("pe") or {}).get("vals") or [])
    pb_vals = list(((rhist or {}).get("pb") or {}).get("vals") or [])
    dy_vals = list(((rhist or {}).get("yield") or {}).get("vals") or [])

    # Fallback: reconstruct from prices only if the ratios history is thin.
    if len(pe_vals) < 3 and price_series and len(price_series) >= 40:
        raw_last = price_series[-1][1]
        mult = fx if (_fnum(cur_price) and _fnum(raw_last) and raw_last > 0
                      and cur_price / raw_last > 10) else 1.0
        ni  = _row_by_year(income, "Net Income to Common", "Net Income")
        eq  = _row_by_year(balance, "Total Common Equity", "Shareholders' Equity")
        dvd = _row_by_year(cashflow, "Common Dividends Paid")
        shy = (_row_by_year(income, "Shares Outstanding (Basic)", "Basic Shares Outstanding")
               or _row_by_year(balance, "Total Common Shares Outstanding"))
        for y in sorted(y for y in ni if y in shy and shy[y] > 0):
            px = _price_in_year(price_series, y, mult)
            if not _fnum(px) or px <= 0:
                continue
            sh = shy[y]
            if ni[y] / sh > 0:
                pe[y] = px / (ni[y] / sh)
            if y in eq and eq[y] > 0:
                pb[y] = px / (eq[y] / sh)
            if y in dvd and dvd[y]:
                dy[y] = (abs(dvd[y]) / sh) / px
        pe_vals, pb_vals, dy_vals = list(pe.values()), list(pb.values()), list(dy.values())

    def stat(vals, by_year, cur, higher_is_cheaper=False):
        if len(vals) < 3 or not _fnum(cur):
            return None
        p = _pctile(cur, vals)
        if higher_is_cheaper and p is not None:
            p = 1 - p
        return {"current": cur, "median": _median(vals), "low": min(vals),
                "high": max(vals), "cheap_pctile": p, "series": by_year, "n": len(vals)}

    cur_yield = _safe_div(cur_dps, cur_price) if (_fnum(cur_dps) and _fnum(cur_price)
                                                  and cur_price > 0) else None
    return {
        "pe": stat(pe_vals, pe, _safe_div(cur_price, cur_eps) if (_fnum(cur_eps) and cur_eps > 0) else None),
        "pb": stat(pb_vals, pb, _safe_div(cur_price, cur_bvps) if (_fnum(cur_bvps) and cur_bvps > 0) else None),
        "yield": stat(dy_vals, dy, cur_yield, higher_is_cheaper=True),
    }


def dividend_history(cashflow, income):
    """Multi-year dividend-per-share record: streak of rises, cuts, growth and how
    many of the last years paid. Returns {} if no dividend data."""
    dvd = _row_by_year(cashflow, "Common Dividends Paid")
    shy = (_row_by_year(income, "Shares Outstanding (Basic)", "Basic Shares Outstanding")
           or {})
    if not dvd:
        return {}
    dps = {}
    for y, v in dvd.items():
        sh = shy.get(y)
        if _fnum(sh) and sh > 0:
            dps[y] = abs(v) / sh
    years = sorted(dps)
    if len(years) < 2:
        return {}
    vals = [dps[y] for y in years]

    paid = sum(1 for v in vals if v > 0)
    # current streak of consecutive year-on-year rises (from the latest year back)
    streak = 0
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] > vals[i - 1] * 1.001:
            streak += 1
        else:
            break
    cuts = [years[i] for i in range(1, len(vals))
            if vals[i] < vals[i - 1] * 0.999 and vals[i - 1] > 0]
    pos = [v for v in vals if v > 0]
    cagr = ((pos[-1] / pos[0]) ** (1 / (len(pos) - 1)) - 1) if len(pos) >= 2 and pos[0] > 0 else None

    if paid == len(years) and not cuts and streak >= 3:
        verdict = ("good", f"Paid every year on record and raised for {streak} years "
                   "running - a dependable grower.")
    elif cuts:
        verdict = ("caution", f"Paid in {paid} of {len(years)} years, but cut in "
                   + ", ".join(str(c) for c in cuts[-2:]) + " - not a steady grower.")
    elif paid == len(years):
        verdict = ("good", f"Paid in every one of the last {len(years)} years.")
    else:
        verdict = ("caution", f"Paid in {paid} of the last {len(years)} years - "
                   "irregular.")
    return {"series": {y: dps[y] for y in years}, "streak": streak, "cuts": cuts,
            "cagr": cagr, "paid": paid, "years": len(years), "verdict": verdict}


def earnings_quality(income, balance, cashflow, p):
    """Forensic red-flags: is reported profit backed by cash, are working-capital
    items ballooning, is the balance sheet stretched? Returns flags + a verdict."""
    rev = _row_by_year(income, "Total Revenue", "Revenue")
    ni  = _row_by_year(income, "Net Income to Common", "Net Income")
    ocf = _row_by_year(cashflow, "Operating Cash Flow")
    assets = _row_by_year(balance, "Total Assets")
    recv = _row_by_year(balance, "Receivables", "Accounts Receivable", "Net Receivables")
    inv  = _row_by_year(balance, "Inventory", "Inventories")

    def last_two(d):
        ys = sorted(d)
        return (d[ys[-2]], d[ys[-1]]) if len(ys) >= 2 else (None, None)

    flags = []
    ys = sorted(set(ni) & set(ocf))
    ocf_ni = None
    if ys:
        y = ys[-1]
        if ni[y] > 0:
            ocf_ni = ocf[y] / ni[y]
    if _fnum(ocf_ni) and ocf_ni < 0.7:
        flags.append(("high" if ocf_ni < 0.4 else "medium",
                      f"Only {ocf_ni*100:.0f}% of last year's reported profit showed up "
                      "as operating cash - earnings aren't fully backed by cash."))
    if ys and assets:
        y = ys[-1]
        ay = assets.get(y)
        if _fnum(ay) and ay > 0:
            accr = (ni[y] - ocf[y]) / ay
            if accr > 0.10:
                flags.append(("medium", f"High accruals: net income ran {accr*100:.0f}% "
                              "of assets ahead of operating cash - profit leans on "
                              "non-cash items."))
    r0, r1 = last_two(recv)
    s0, s1 = last_two(rev)
    rg = sg = None
    if all(_fnum(x) and x > 0 for x in (r0, r1, s0, s1)):
        rg, sg = r1 / r0 - 1, s1 / s0 - 1
        if rg - sg > 0.15:
            flags.append(("medium", f"Receivables grew {rg*100:.0f}% while sales grew "
                          f"{sg*100:.0f}% - customers are paying slower, or revenue is "
                          "booked ahead of cash."))
    i0, i1 = last_two(inv)
    if all(_fnum(x) and x > 0 for x in (i0, i1)) and _fnum(sg):
        ig = i1 / i0 - 1
        if ig - sg > 0.20:
            flags.append(("medium", f"Inventory grew {ig*100:.0f}% vs sales "
                          f"{sg*100:.0f}% - stock is piling up faster than it sells."))
    ic = p.get("intCover")
    if _fnum(ic) and ic < 3:
        flags.append(("high" if ic < 1.5 else "medium", f"Interest cover of {ic:.1f}x "
                      "is thin - little cushion if earnings dip or rates rise."))
    shg = p.get("shareGrowth")
    if _fnum(shg) and shg > 0.10:
        flags.append(("medium", f"Share count grew ~{shg*100:.0f}% over the period - "
                      "existing holders are being diluted."))
    if _fnum(p.get("equity")) and p["equity"] < 0:
        flags.append(("high", "Negative book equity - liabilities exceed assets."))

    highs = sum(1 for s, _ in flags if s == "high")
    meds = sum(1 for s, _ in flags if s == "medium")
    if highs >= 1 or meds >= 3:
        overall = ("bad", "Several earnings-quality concerns - read the notes before "
                   "trusting the headline profit.")
    elif meds >= 1:
        overall = ("caution", "A few things to watch, but nothing alarming.")
    else:
        overall = ("good", "Earnings look clean - profit is backed by cash and the "
                   "balance sheet isn't sending warning signs.")
    return {"flags": flags, "overall": overall, "ocfNi": ocf_ni,
            "cashConv": p.get("cashConversion")}


def liquidity_read(series):
    """A tradability proxy from the daily close series: on thin JSE names the close
    is unchanged for long stretches (no trades). Exact $-turnover would need volume,
    which this feed doesn't carry, so this is labelled a proxy."""
    if not series or len(series) < 20:
        return {}
    recent = series[-60:]
    closes = [c for _, c in recent if _fnum(c)]
    if len(closes) < 10:
        return {}
    unchanged = sum(1 for i in range(1, len(closes)) if closes[i] == closes[i - 1])
    frac = unchanged / (len(closes) - 1)
    moves = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))
             if closes[i - 1] > 0]
    avgmove = (sum(moves) / len(moves)) if moves else None
    if frac > 0.5:
        kind, label = "bad", "very thinly traded"
        note = ("The price is unchanged on most days - trades happen rarely. Building "
                "or exiting any real position will be slow and can move the price.")
    elif frac > 0.3:
        kind, label = "caution", "thinly traded"
        note = ("Many days see no price change. Use limit orders and expect to "
                "accumulate slowly.")
    elif frac > 0.15:
        kind, label = "neutral", "moderately traded"
        note = "Trades regularly, but large orders may still move the price."
    else:
        kind, label = "good", "reasonably liquid"
        note = "Trades on most days; a normal-sized position should be workable."
    return {"unchangedFrac": frac, "avgMove": avgmove, "kind": kind,
            "label": label, "note": note, "n": len(closes)}


def _pct(x):
    return "-" if not _fnum(x) else "{:.1f}%".format(x * 100)


def _valuation_verdict(price, central, buy_below):
    """Plain-language read of price vs the blended fair value."""
    if not (_fnum(price) and _fnum(central) and central > 0):
        return ("No live market price was available, so only the intrinsic "
                "estimates are shown.", "unknown")
    if _fnum(buy_below) and price <= buy_below:
        return ("Trading below fair value with a margin of safety - the kind of "
                "gap a patient buyer looks for.", "undervalued")
    if price <= central:
        return ("Trading below the central fair value, but inside the margin of "
                "safety cushion rather than clear of it.", "cheap")
    if price <= central * 1.2:
        return ("Priced at roughly fair value - close to what the methods say the "
                "business is worth.", "fair")
    return ("Priced above the central fair value - the market is assuming more "
            "than these time-tested methods support.", "expensive")


def dcf_sensitivity(base_ps, g1, term_g, currency):
    """A small grid of Buffett-DCF value/share across discount rate x terminal
    growth, so the reader can see how much the answer leans on the assumptions."""
    if not (_fnum(base_ps) and base_ps > 0):
        return None
    discounts = [0.10, 0.12, 0.14, 0.16]
    terms = [0.01, 0.02, 0.03, 0.04]
    rows = {}
    for d in discounts:
        rows[f"{d*100:.0f}% discount"] = {
            f"g={t*100:.0f}%": two_stage_pv(base_ps, g1, min(t, d - 0.01), d, years=10)
            for t in terms
        }
    return pd.DataFrame(rows).T


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
# 3.5  BUSINESS CLASSIFICATION ("Verdict")  \u2014 forensic, financials-driven
# ---------------------------------------------------------------------------
# Every stamp and score below is derived strictly from reported figures.
# Nothing here is opinion: each verdict cites the numbers that produced it.

def _last(series):
    try:
        s = series.dropna()
        return float(s.iloc[-1]) if len(s) else None
    except Exception:
        return None

def _first(series):
    try:
        s = series.dropna()
        return float(s.iloc[0]) if len(s) else None
    except Exception:
        return None

def _vals(series):
    try:
        return [float(x) for x in series.dropna().tolist()]
    except Exception:
        return []

def _series_cagr(series):
    v = _vals(series)
    if len(v) < 2 or v[0] is None or v[0] == 0:
        return None
    if v[0] <= 0 or v[-1] <= 0:
        return None
    yrs = len(v) - 1
    try:
        out = (v[-1] / v[0]) ** (1.0 / yrs) - 1.0
        if isinstance(out, complex):
            return None
        return out
    except Exception:
        return None

def _pick(df, *titles):
    """Return the series for the first matching row title (case/space tolerant)."""
    if df is None:
        return None
    idx = {str(i).strip().lower(): i for i in df.index}
    for t in titles:
        key = str(t).strip().lower()
        if key in idx:
            return df.loc[idx[key]]
    return None

def _safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b

def detect_company_type(income, balance):
    """Classify the business so like-for-like scoring is applied.
    Returns one of: bank, insurer, reit, holding, industrial."""
    prem = _pick(income, "Premiums & Annuity Revenue", "Net Premiums Earned")
    nii  = _pick(income, "Net Interest Income")
    rental = _pick(income, "Rental Revenue")
    inv  = _pick(balance, "Total Investments")
    insliab = _pick(balance, "Insurance & Annuity Liabilities", "Unpaid Claims", "Unearned Premiums")
    deposits = _pick(balance, "Total Deposits", "Total Deposit")
    assets = _last(_pick(balance, "Total Assets"))
    rev = _last(_pick(income, "Total Revenue", "Revenue"))

    if _last(prem) is not None or _last(insliab) is not None:
        return "insurer"
    if _last(nii) is not None or _last(deposits) is not None:
        return "bank"
    if _last(rental) is not None:
        return "reit"
    if assets and rev is not None and rev > 0 and assets / max(rev, 1) > 12:
        return "holding"
    if assets and rev is not None and rev > 0 and assets / max(rev, 1) > 8 and _last(inv) is not None:
        return "holding"
    if assets and (rev is None or rev <= 0) and _last(inv) is not None:
        return "holding"
    if assets and rev is not None and rev <= 0:
        return "holding"
    return "industrial"

def _last_complete_col(df, threshold=5):
    """Return the label of the last 'complete' fiscal column. Some feeds tack on
    an interim/partial most-recent period (e.g. a 9-month FY2026) whose rows are
    largely null; including it would corrupt 'latest year' metrics, so we detect
    a markedly sparser trailing column and step back to the last full one."""
    if df is None or df.shape[1] == 0:
        return None
    cols = list(df.columns)
    i = len(cols) - 1
    while i >= 1:
        last_nulls = int(df.iloc[:, i].isna().sum())
        prev_nulls = int(df.iloc[:, i - 1].isna().sum())
        if last_nulls >= prev_nulls + threshold:
            i -= 1
        else:
            break
    return cols[i]

def _align_to_complete_years(income, balance, cashflow):
    """Trim all three statements to the last fiscal year that is complete across
    the board, so 'latest' metrics use a true full-year figure."""
    refs = []
    for d in (balance, income):
        if d is not None and d.shape[1]:
            lcc = _last_complete_col(d)
            if lcc is not None:
                refs.append(str(lcc))
    if not refs:
        return income, balance, cashflow
    ref = min(refs)
    out = []
    for d in (income, balance, cashflow):
        if d is None:
            out.append(None)
            continue
        keep = [c for c in d.columns if str(c) <= ref]
        out.append(d[keep] if keep else d)
    return out[0], out[1], out[2]

def build_metric_panel(income, balance, cashflow, ctype):
    """One panel of financials-derived metrics shared by every scorer.
    Keys are stable; scorers read what they need for their business type."""
    I, B, C = income, balance, cashflow
    p = {}

    rev      = _pick(I, "Total Revenue", "Revenue")
    ebit     = _pick(I, "EBIT")
    oi       = _pick(I, "Operating Income")
    ebitda   = _pick(I, "EBITDA")
    gp       = _pick(I, "Gross Profit")
    ni       = _pick(I, "Net Income to Common", "Net Income")
    nii      = _pick(I, "Net Interest Income")
    prem     = _pick(I, "Premiums & Annuity Revenue", "Net Premiums Earned")
    rental   = _pick(I, "Rental Revenue")
    intex    = _pick(I, "Interest Expense")
    da       = _pick(I, "D&A For Ebitda", "D&A For EBITDA", "Depreciation & Amortization")
    shares   = _pick(I, "Shares Outstanding (Basic)", "Basic Shares Outstanding",
                     "Total Common Shares Outstanding")
    if intex is not None:
        intex = intex.abs()

    assets   = _pick(B, "Total Assets")
    equity   = _pick(B, "Total Common Equity", "Shareholders' Equity")
    debt     = _pick(B, "Total Debt")
    cash     = _pick(B, "Cash & Equivalents", "Cash & Cash Equivalents")
    cura     = _pick(B, "Total Current Assets")
    curl     = _pick(B, "Total Current Liabilities")
    inv      = _pick(B, "Total Investments")
    insliab  = _pick(B, "Insurance & Annuity Liabilities")
    ppe      = _pick(B, "Property, Plant & Equipment")
    retained = _pick(B, "Retained Earnings")

    ocf      = _pick(C, "Operating Cash Flow")
    fcf      = _pick(C, "Free Cash Flow")
    capex    = _pick(C, "Capital Expenditures")
    div      = _pick(C, "Common Dividends Paid")
    issuance = _pick(C, "Issuance of Common Stock")
    dbtissue = _pick(C, "Long-Term Debt Issued")
    dbtrepay = _pick(C, "Long-Term Debt Repaid")

    p["rev"]      = _last(rev)
    p["revFirst"] = _first(rev)
    p["ebit"]     = _last(ebit)
    p["oi"]       = _last(oi)
    p["ebitda"]   = _last(ebitda)
    p["ni"]       = _last(ni)
    p["niPrev"]   = _vals(ni)[-2] if len(_vals(ni)) >= 2 else None
    p["nii"]      = _last(nii)
    p["prem"]     = _last(prem)
    p["rental"]   = _last(rental)
    p["intExp"]   = _last(intex)
    p["da"]       = _last(da)
    p["assets"]   = _last(assets)
    p["equity"]   = _last(equity)
    p["equityFirst"] = _first(equity)
    p["debt"]     = _last(debt) or 0.0
    p["cash"]     = _last(cash) or 0.0
    p["curAssets"]= _last(cura)
    p["curLiab"]  = _last(curl)
    p["invBook"]  = _last(inv)
    p["insLiab"]  = _last(insliab)
    p["ppe"]      = _last(ppe)
    p["retained"] = _last(retained)
    p["ocf"]      = _last(ocf)
    p["fcf"]      = _last(fcf)
    p["capex"]    = _last(capex)
    p["div"]      = _last(div)
    p["sharesLast"] = _last(shares)
    p["sharesFirst"]= _first(shares)

    ebitV = p["ebit"] if p["ebit"] is not None else p["oi"]
    p["ebitUsed"] = ebitV

    rv = p["rev"]
    p["opMargin"]    = _safe_div(ebitV, rv) if rv else None
    p["grossMargin"] = _safe_div(_last(gp), rv) if rv else None
    p["netMargin"]   = _safe_div(p["ni"], rv) if rv else None
    p["fcfMargin"]   = _safe_div(p["fcf"], rv) if rv else None
    p["ebitdaMargin"]= _safe_div(p["ebitda"], rv) if rv else None

    eq = p["equity"]
    p["roe"] = _safe_div(p["ni"], eq) if eq and eq > 0 else None
    p["roa"] = _safe_div(p["ni"], p["assets"]) if p["assets"] else None

    # --- after-tax ROIC (NOPAT / invested capital) --------------------------
    pretax = _pick(I, "Pretax Income", "Pre-Tax Income", "EBT", "Earnings Before Tax")
    taxexp = _pick(I, "Income Tax", "Income Tax Expense", "Provision for Income Taxes")
    _ptx, _tx = _last(pretax), _last(taxexp)
    eff = _safe_div(_tx, _ptx) if (_ptx and _ptx > 0) else None
    if eff is None or eff < 0 or eff > 0.40:
        eff = 0.25                                   # Jamaican statutory fallback
    p["effTaxRate"] = eff
    p["nopat"] = ebitV * (1 - eff) if ebitV is not None else None
    _ic = (p["debt"] or 0.0) + (eq or 0.0) - (p["cash"] or 0.0)
    p["investedCapital"] = _ic if _ic > 0 else None
    p["roic"] = _safe_div(p["nopat"], p["investedCapital"]) if p["investedCapital"] else None
    p["netDebtToEbit"] = _safe_div(p["debt"] - p["cash"], ebitV) if (ebitV and ebitV > 0) else None

    p["netDebt"] = p["debt"] - p["cash"]
    p["debtToEquity"]   = _safe_div(p["debt"], eq) if eq and eq > 0 else None
    p["netDebtToEbitda"]= _safe_div(p["netDebt"], p["ebitda"]) if p["ebitda"] and p["ebitda"] > 0 else None
    p["equityToAssets"] = _safe_div(eq, p["assets"]) if p["assets"] else None
    p["currentRatio"]   = _safe_div(p["curAssets"], p["curLiab"]) if p["curLiab"] else None

    if p["intExp"] and p["intExp"] > 0 and ebitV is not None:
        p["intCover"] = ebitV / p["intExp"]
    else:
        p["intCover"] = None

    p["fcfPositive"]   = (p["fcf"] is not None and p["fcf"] > 0)
    p["cashConversion"]= _safe_div(p["fcf"], p["ni"]) if (p["ni"] and p["ni"] > 0) else None

    nvg = _vals(ni)
    _share_restructured = (p["sharesFirst"] and p["sharesLast"]
                           and p["sharesFirst"] > 0 and p["sharesLast"] / p["sharesFirst"] > 3)
    p["ipoDistorted"] = ((len(nvg) >= 2 and nvg[0] != 0 and abs(nvg[-1] / nvg[0]) > 6)
                         or bool(_share_restructured))
    p["revCagr"]    = _series_cagr(rev)
    p["niCagr"]     = _series_cagr(ni)
    p["equityCagr"] = _series_cagr(equity)
    p["ebitCagr"]   = _series_cagr(ebit if ebit is not None else oi)

    nv = _vals(ni)
    p["totalYears"]   = len(nv)
    p["lossYears"]    = sum(1 for x in nv if x < 0)
    p["niLast"]       = nv[-1] if nv else None
    p["niDeteriorating"] = (len(nv) >= 3 and nv[-1] < nv[-2] < nv[-3])
    p["recentCollapse"]  = (len(nv) >= 2 and nv[-2] > 0 and nv[-1] < 0)
    omv = p_om_series(I, rv) if rv else []
    p["omSeries"]     = omv
    p["omNegYears"]   = sum(1 for x in omv if x < 0)
    p["opMarginStart"]= omv[0] if omv else None
    p["opMarginEnd"]  = omv[-1] if omv else None
    p["structurallyWeak"] = (p["totalYears"] >= 3 and p["lossYears"] >= max(2, p["totalYears"] - 1))

    p["divPaid"]  = abs(p["div"]) if p["div"] is not None else 0.0
    p["divToFcf"] = _safe_div(p["divPaid"], p["fcf"]) if (p["fcf"] and p["fcf"] > 0) else None
    p["divToNi"]  = _safe_div(p["divPaid"], p["ni"]) if (p["ni"] and p["ni"] > 0) else None
    p["paysDividend"] = p["divPaid"] > 0
    issv = _vals(issuance)
    p["netIssuance"]  = sum(issv) if issv else 0.0
    sf, sl = p["sharesFirst"], p["sharesLast"]
    sg = _safe_div(sl - sf, sf) if (sf and sf > 0 and sl is not None) else None
    if sg is not None and sg > 1.0:
        sg = None
    p["shareGrowth"] = sg

    p["capexToRev"]   = _safe_div(abs(p["capex"]), rv) if (p["capex"] is not None and rv) else None
    p["ppeToAssets"]  = _safe_div(p["ppe"], p["assets"]) if (p["ppe"] is not None and p["assets"]) else None

    p["floatToEquity"] = _safe_div(p["insLiab"], eq) if (p["insLiab"] is not None and eq and eq > 0) else None
    p["investToAssets"]= _safe_div(p["invBook"], p["assets"]) if (p["invBook"] is not None and p["assets"]) else None
    ffo_line = _last(_pick(I, "Funds From Operations (FFO)"))
    if ffo_line is not None:
        p["ffo"] = ffo_line
    elif p["ni"] is not None and p["da"] is not None:
        p["ffo"] = p["ni"] + p["da"]
    else:
        p["ffo"] = None
    p["ffoToDebt"]  = _safe_div(p["ffo"], p["debt"]) if (p["ffo"] is not None and p["debt"]) else None
    p["debtToAssets"]= _safe_div(p["debt"], p["assets"]) if p["assets"] else None

    stdebt  = _pick(B, "Short-Term Debt", "Short-Term Borrowings")
    cpltd   = _pick(B, "Current Portion of Long-Term Debt")
    clease  = _pick(B, "Current Portion of Leases")
    ltdebt  = _pick(B, "Long-Term Debt")
    ltlease = _pick(B, "Long-Term Leases")
    p["stDebt"]      = _last(stdebt) or 0.0
    p["curPortLTD"]  = _last(cpltd) or 0.0
    p["curLeases"]   = _last(clease) or 0.0
    p["ltDebt"]      = _last(ltdebt) or 0.0
    p["ltLeases"]    = _last(ltlease) or 0.0
    p["currentDebtDue"] = p["stDebt"] + p["curPortLTD"] + p["curLeases"]

    flags = []
    if rv is not None and rv < 0:
        flags.append("negative reported revenue")
    if p["totalYears"] < 2:
        flags.append("insufficient history")
    if p["equity"] is not None and p["equity"] < 0:
        flags.append("negative equity")
    p["dqFlags"] = flags
    p["dataConfidence"] = "low" if flags else "ok"
    return p

def p_om_series(I, rv_last):
    """Operating-margin series used for trend detection."""
    ebit = _pick(I, "EBIT")
    oi   = _pick(I, "Operating Income")
    rev  = _pick(I, "Total Revenue", "Revenue")
    base = ebit if ebit is not None else oi
    if base is None or rev is None:
        return []
    out = []
    try:
        for d in rev.index:
            r = rev.get(d)
            e = base.get(d) if d in base.index else None
            if r is not None and r != 0 and e is not None:
                try:
                    out.append(float(e) / float(r))
                except Exception:
                    pass
    except Exception:
        return []
    return out

def _refinancing_read(p, currency):
    """Near-term debt & refinancing pressure. We do NOT have a year-by-year
    maturity ladder (that lives in audited notes), so we read the current
    portion of debt against the liquidity available to cover it."""
    due = p["currentDebtDue"]
    if due <= 0:
        return ("none", "No debt falls due within twelve months on the latest balance sheet.")
    cover_sources = (p["cash"] or 0) + (p["fcf"] if (p["fcf"] or 0) > 0 else 0)
    ratio = cover_sources / due if due else None
    due_txt = fmt_money_compact(due, currency)
    cash_txt = fmt_money_compact(p["cash"] or 0, currency)
    if ratio is not None and ratio >= 1.5:
        return ("low", "About " + due_txt + " of debt matures within a year, comfortably covered by "
                + cash_txt + " of cash plus free cash flow.")
    if ratio is not None and ratio >= 1.0:
        return ("moderate", "About " + due_txt + " of debt is due within a year; cash and free cash flow "
                "roughly cover it but leave little slack, so terms on rollover matter.")
    return ("high", "About " + due_txt + " of debt matures within a year against only " + cash_txt
            + " of cash. The company depends on refinancing or fresh cash flow to meet it, which is a real risk if credit tightens.")

def _band(score):
    if score >= 85: return "High quality"
    if score >= 70: return "Solid"
    if score >= 55: return "Mixed / watch"
    if score >= 35: return "Weak"
    return "Avoid"

def _clip(x, lo=0, hi=100):
    return max(lo, min(hi, x))

def _score_from(value, lo, hi):
    """Linear 0-100 score: value<=lo -> 0, value>=hi -> 100."""
    if value is None:
        return None
    if hi == lo:
        return 50.0
    return _clip(100.0 * (value - lo) / (hi - lo))

def score_industrial(p):
    """Five pillars for an operating (industrial / consumer / services) business."""
    P = {}
    parts = []
    s = _score_from(p["opMargin"], 0.02, 0.22);  _ = parts.append(s) if s is not None else None
    s = _score_from(p["roe"], 0.05, 0.25);        _ = parts.append(s) if s is not None else None
    s = _score_from(p["netMargin"], 0.01, 0.15);  _ = parts.append(s) if s is not None else None
    P["Profitability"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["fcfMargin"], 0.0, 0.15);      _ = parts.append(s) if s is not None else None
    s = _score_from(p["cashConversion"], 0.4, 1.1);  _ = parts.append(s) if s is not None else None
    if p["fcfPositive"]:
        parts.append(80.0)
    else:
        parts.append(20.0)
    P["Cash generation"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    if p["netDebtToEbitda"] is not None:
        parts.append(_score_from(-p["netDebtToEbitda"], -4.0, 0.0))
    if p["intCover"] is not None:
        parts.append(_score_from(p["intCover"], 1.5, 8.0))
    elif p["debt"] == 0:
        parts.append(90.0)
    if p["currentRatio"] is not None:
        parts.append(_score_from(p["currentRatio"], 0.9, 2.0))
    if p["equityToAssets"] is not None:
        parts.append(_score_from(p["equityToAssets"], 0.2, 0.6))
    P["Balance sheet"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["revCagr"], 0.0, 0.15);  _ = parts.append(s) if s is not None else None
    s = _score_from(p["niCagr"], 0.0, 0.18);   _ = parts.append(s) if s is not None else None
    P["Growth"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["roe"], 0.10, 0.28);          _ = parts.append(s) if s is not None else None
    s = _score_from(p["grossMargin"], 0.15, 0.45);  _ = parts.append(s) if s is not None else None
    if p["opMarginStart"] is not None and p["opMarginEnd"] is not None:
        parts.append(75.0 if p["opMarginEnd"] >= p["opMarginStart"] else 40.0)
    P["Moat"] = round(sum(parts)/len(parts)) if parts else 50
    return P

def score_financial(p, ctype):
    """Pillars for a deposit-taking bank or diversified financial.
    Calibrated to banking norms (low ROA, high asset leverage are normal)."""
    P = {}
    parts = []
    re = _score_from(p["roe"], 0.06, 0.16)
    ra = _score_from(p["roa"], 0.005, 0.020)
    if re is not None: parts += [re, re]
    if ra is not None: parts.append(ra)
    P["Returns"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["equityToAssets"], 0.05, 0.12); _ = parts.append(s) if s is not None else None
    P["Capital strength"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["netMargin"], 0.10, 0.30); _ = parts.append(s) if s is not None else None
    if p["lossYears"] == 0 and p["totalYears"] >= 3:
        parts.append(80.0)
    elif p["lossYears"] > 0:
        parts.append(30.0)
    P["Quality"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["niCagr"], -0.05, 0.15);     _ = parts.append(s) if s is not None else None
    s = _score_from(p["equityCagr"], -0.02, 0.12); _ = parts.append(s) if s is not None else None
    P["Growth"] = round(sum(parts)/len(parts)) if parts else 50
    if p["lossYears"] == 0 and p["totalYears"] >= 3:
        P["Growth"] = max(P["Growth"], 40)
    return P

def score_insurer(p):
    """Pillars for an insurer: earnings power, capital adequacy, underwriting
    quality (stability), and growth."""
    P = {}
    parts = []
    s = _score_from(p["roe"], 0.08, 0.20);   _ = parts.append(s) if s is not None else None
    s = _score_from(p["netMargin"], 0.05, 0.20); _ = parts.append(s) if s is not None else None
    P["Earnings power"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["equityToAssets"], 0.10, 0.25); _ = parts.append(s) if s is not None else None
    if p["floatToEquity"] is not None:
        parts.append(_score_from(-p["floatToEquity"], -4.0, -0.5))
    P["Capital adequacy"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    if p["lossYears"] == 0 and p["totalYears"] >= 3:
        parts.append(82.0)
    elif p["lossYears"] > 0:
        parts.append(30.0)
    s = _score_from(p["roa"], 0.01, 0.035); _ = parts.append(s) if s is not None else None
    P["Underwriting quality"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["niCagr"], -0.05, 0.15);     _ = parts.append(s) if s is not None else None
    s = _score_from(p["equityCagr"], -0.02, 0.12); _ = parts.append(s) if s is not None else None
    P["Growth"] = round(sum(parts)/len(parts)) if parts else 50
    if p["lossYears"] == 0 and p["totalYears"] >= 3:
        P["Growth"] = max(P["Growth"], 45)
    return P

def score_reit(p):
    """Pillars for a property / REIT: rental earnings power, FFO-based
    leverage, balance-sheet conservatism, and growth."""
    P = {}
    parts = []
    s = _score_from(p["roe"], 0.04, 0.12);    _ = parts.append(s) if s is not None else None
    s = _score_from(p["netMargin"], 0.10, 0.40); _ = parts.append(s) if s is not None else None
    P["Rental earnings power"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    if p["ffoToDebt"] is not None:
        parts.append(_score_from(p["ffoToDebt"], 0.05, 0.25))
    if p["debtToAssets"] is not None:
        parts.append(_score_from(-p["debtToAssets"], -0.55, -0.15))
    if p["intCover"] is not None:
        parts.append(_score_from(p["intCover"], 1.5, 5.0))
    P["Leverage & coverage"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["equityToAssets"], 0.30, 0.65); _ = parts.append(s) if s is not None else None
    if p["lossYears"] == 0 and p["totalYears"] >= 3:
        parts.append(78.0)
    elif p["lossYears"] > 0:
        parts.append(30.0)
    P["Balance sheet"] = round(sum(parts)/len(parts)) if parts else 50

    parts = []
    s = _score_from(p["revCagr"], 0.0, 0.12); _ = parts.append(s) if s is not None else None
    s = _score_from(p["niCagr"], 0.0, 0.12);  _ = parts.append(s) if s is not None else None
    P["Growth"] = round(sum(parts)/len(parts)) if parts else 50
    return P

def detect_risks(p, ctype):
    """Concrete, financials-grounded risks to watch. Each entry is (severity, text)."""
    risks = []
    if p["currentDebtDue"] > 0:
        cover = (p["cash"] or 0) + (p["fcf"] if (p["fcf"] or 0) > 0 else 0)
        if cover < p["currentDebtDue"]:
            risks.append(("high", "Near-term debt due exceeds cash plus free cash flow, so the company "
                          "leans on refinancing to meet maturities."))
    if p["netDebtToEbitda"] is not None and p["netDebtToEbitda"] > 4 and ctype == "industrial":
        risks.append(("high", "Net debt is over four times EBITDA - a heavy load that limits flexibility."))
    elif p["netDebtToEbitda"] is not None and p["netDebtToEbitda"] > 3 and ctype == "industrial":
        risks.append(("medium", "Net debt is more than three times EBITDA; leverage is on the high side."))
    if p["intCover"] is not None and p["intCover"] < 1.5:
        risks.append(("high", "Operating profit barely covers interest (coverage below 1.5x); a bad year "
                      "could mean missed payments."))
    elif p["intCover"] is not None and p["intCover"] < 3:
        risks.append(("medium", "Interest coverage under 3x leaves limited cushion for a downturn."))
    if p["currentRatio"] is not None and p["currentRatio"] < 1 and ctype in ("industrial", "reit"):
        risks.append(("medium", "Current liabilities exceed current assets, so day-to-day liquidity is tight."))
    if p["niDeteriorating"]:
        risks.append(("medium", "Net income has fallen for three straight years - momentum is negative."))
    if p["recentCollapse"]:
        risks.append(("high", "The business swung to a loss in the latest year after being profitable; find "
                      "out whether this is one-off or the start of a trend."))
    if p["opMarginStart"] is not None and p["opMarginEnd"] is not None and p["opMarginEnd"] < p["opMarginStart"] - 0.03:
        risks.append(("medium", "Operating margin has compressed meaningfully over the record - pricing power "
                      "or cost control may be slipping."))
    if p["shareGrowth"] is not None and p["shareGrowth"] > 0.10:
        risks.append(("medium", "Shares outstanding have grown notably, diluting existing owners."))
    if ctype in ("industrial", "reit") and not p["fcfPositive"] and p["paysDividend"]:
        risks.append(("medium", "The dividend is being paid while free cash flow is negative, which is not "
                      "sustainable without borrowing or asset sales."))
    if ctype == "insurer" and p["floatToEquity"] is not None and p["floatToEquity"] > 3:
        risks.append(("medium", "Insurance liabilities are large relative to equity, so reserve or claims "
                      "shocks would hit the capital base hard."))
    if ctype == "reit" and p["debtToAssets"] is not None and p["debtToAssets"] > 0.5:
        risks.append(("high", "Debt funds over half the property book; rising rates or falling valuations "
                      "would squeeze equity."))
    if not risks:
        risks.append(("low", "No major red flags in the reported figures. Keep watching the usual drivers of "
                      "this type of business."))
    return risks

def _add(stamps, name, desc, kind="neutral"):
    stamps.append({"name": name, "desc": desc, "kind": kind})

def classify_stamps(p, pillars, overall, ctype):
    """Easy-to-understand stamps, each justified by the numbers.
    kind: good / caution / bad / neutral. Stamps may nest (several can apply)."""
    stamps = []
    fcf_meaningful = ctype in ("industrial", "reit")
    structural  = p["structurallyWeak"]
    collapsing  = p["recentCollapse"] or p["niDeteriorating"]
    resources   = (p["cash"] or 0) + (p["fcf"] if (p["fcf"] or 0) > 0 else 0)
    profitable  = (p["ni"] is not None and p["ni"] > 0)

    if p["dataConfidence"] == "low":
        _add(stamps, "Data check needed",
             "The reported figures look unusual for this company (" + "; ".join(p["dqFlags"])
             + "), so a confident stamp would be misleading. Read the statements directly.", "neutral")
        return stamps

    cant_service = (p["intCover"] is not None and p["intCover"] < 1 and not profitable
                    and p["currentDebtDue"] > 0 and resources < p["currentDebtDue"])

    chronic_losses = (p["lossYears"] is not None and p["totalYears"] >= 3
                      and p["lossYears"] > p["totalYears"] / 2)
    grenade = ((structural or (cant_service and not collapsing) or chronic_losses)
               and not profitable)
    if grenade:
        _add(stamps, "Grenade",
             "Chronically unprofitable and/or unable to service its obligations from its own cash. "
             "The kind of business to stay away from - it tends to destroy capital over time.", "bad")

    if collapsing and not grenade:
        _add(stamps, "Falling knife",
             "The numbers are deteriorating fast - profit is sliding or has turned to a loss. It may "
             "stabilise, but the trend is down and the cause needs to be understood before touching it.", "caution")

    bal_key = "Balance sheet" if "Balance sheet" in pillars else (
              "Capital strength" if "Capital strength" in pillars else (
              "Capital adequacy" if "Capital adequacy" in pillars else
              "Leverage & coverage"))
    bal = pillars.get(bal_key, 50)
    if bal >= 82 and (p["netDebtToEbitda"] is None or p["netDebtToEbitda"] <= 1) and not collapsing:
        _add(stamps, "Fortress",
             "Very little net debt and strong coverage - the balance sheet can absorb shocks and fund "
             "opportunities without strain.", "good")

    if (fcf_meaningful and p["fcfMargin"] is not None and p["fcfMargin"] >= 0.10 and p["fcfPositive"]
            and (p["cashConversion"] is None or p["cashConversion"] >= 0.6) and not collapsing):
        _add(stamps, "Cash machine",
             "Turns a high share of revenue into real free cash flow year after year - the hallmark of a "
             "business that funds itself and rewards owners.", "good")

    moat = pillars.get("Moat", 0)
    if moat >= 70 and (p["roe"] or 0) >= 0.18 and not collapsing:
        _add(stamps, "Wide moat",
             "Sustained high returns on equity with sturdy margins point to a durable competitive edge that "
             "lets it earn well above its cost of capital.", "good")
    elif moat >= 58 and (p["roe"] or 0) >= 0.13 and not collapsing:
        _add(stamps, "Narrow moat",
             "Returns and margins are comfortably above average, suggesting some real competitive protection, "
             "though not an impregnable one.", "good")

    if ((p["revCagr"] or 0) >= 0.08 and (p["niCagr"] or 0) >= 0.08
            and (p["roe"] or 0) >= 0.14 and not collapsing
            and not p.get("ipoDistorted") and p["totalYears"] >= 4):
        _add(stamps, "Compounder",
             "Grows the top and bottom line at double digits while earning high returns - left alone, it "
             "compounds owners' capital steadily.", "good")

    if (fcf_meaningful and p["capexToRev"] is not None and p["capexToRev"] <= 0.05
            and (p["roe"] or 0) >= 0.15 and p["fcfPositive"] and not collapsing):
        _add(stamps, "Capital-light",
             "Earns strong returns without heavy reinvestment in plant and equipment, so growth drops "
             "through to cash rather than being eaten by capex.", "good")

    if p["paysDividend"]:
        dfcf, dni = p["divToFcf"], p["divToNi"]
        if fcf_meaningful:
            covered = ((dfcf is not None and 0 < dfcf <= 0.85 and p["fcfPositive"])
                       or (dni is not None and 0 < dni <= 0.70 and p["fcfPositive"]))
            stretched = ((not p["fcfPositive"])
                         or (dfcf is not None and dfcf > 1.2 and (dni is None or dni > 1.0)))
        else:
            covered = (dni is not None and 0 < dni <= 0.85 and (p["ni"] or 0) > 0)
            stretched = (dni is not None and dni > 1.0)
        if covered and not collapsing:
            _add(stamps, "Dividend payer",
                 "Pays a dividend that is comfortably covered by earnings, so the payout looks sustainable "
                 "rather than borrowed.", "good")
        elif stretched:
            _add(stamps, "Stretched dividend",
                 "The dividend is not covered by the company's own profits or cash flow - it is effectively "
                 "being funded by the balance sheet, which cannot continue indefinitely.", "caution")

    if p["shareGrowth"] is not None and p["shareGrowth"] > 0.15:
        _add(stamps, "Serial diluter",
             "The share count keeps climbing, so each existing share owns a smaller slice over time - returns "
             "per share lag the headline growth.", "caution")

    if (fcf_meaningful
            and (p["revCagr"] is not None and abs(p["revCagr"]) < 0.04)
            and (p["niCagr"] is not None and abs(p["niCagr"]) < 0.04)
            and p["fcfPositive"] and p["paysDividend"]
            and (p["intCover"] is None or p["intCover"] >= 3) and not collapsing):
        _add(stamps, "Bond proxy",
             "Barely grows but throws off steady, well-covered cash and dividends - it behaves more like a "
             "fixed-income holding than a growth stock.", "neutral")

    if (not collapsing and not grenade and p["lossYears"] == 0 and p["totalYears"] >= 3
            and 55 <= overall < 82 and not any(s["name"] in ("Cash machine","Compounder","Wide moat") for s in stamps)):
        _add(stamps, "Steady operator",
             "Consistently profitable with no obvious red flags - a dependable if unspectacular business.", "good")

    if (p["opMarginStart"] is not None and p["opMarginEnd"] is not None
            and p["opMarginEnd"] < p["opMarginStart"] - 0.03 and not grenade):
        _add(stamps, "Margins under pressure",
             "Operating margin has narrowed over the record - costs are outrunning pricing, which erodes "
             "profitability if it continues.", "caution")

    if (p["netDebtToEbitda"] is not None and p["netDebtToEbitda"] > 4 and ctype == "industrial"):
        _add(stamps, "Leveraged",
             "Carries heavy net debt relative to earnings, which amplifies both returns and risk and leaves "
             "little room for error.", "caution")

    if ctype == "insurer" and p["floatToEquity"] is not None and 0.5 <= p["floatToEquity"] <= 3 and p["lossYears"] == 0:
        _add(stamps, "Float engine",
             "Holds a large, well-managed pool of policyholder funds that it invests for its own account - a "
             "powerful model when underwriting stays disciplined.", "good")

    if not stamps:
        _add(stamps, "Unremarkable",
             "Nothing in the figures stands out either way - an average business on the current numbers.", "neutral")
    return stamps

_PILLAR_WEIGHTS = {
    "industrial": {"Profitability": 0.24, "Cash generation": 0.22, "Balance sheet": 0.22,
                   "Growth": 0.14, "Moat": 0.18},
    "bank":       {"Returns": 0.30, "Capital strength": 0.26, "Quality": 0.24, "Growth": 0.20},
    "holding":    {"Returns": 0.30, "Capital strength": 0.26, "Quality": 0.24, "Growth": 0.20},
    "insurer":    {"Earnings power": 0.28, "Capital adequacy": 0.28, "Underwriting quality": 0.24,
                   "Growth": 0.20},
    "reit":       {"Rental earnings power": 0.26, "Leverage & coverage": 0.30, "Balance sheet": 0.24,
                   "Growth": 0.20},
}

_TYPE_LABEL = {
    "industrial": "Operating business",
    "bank": "Bank / diversified financial",
    "holding": "Holding / investment company",
    "insurer": "Insurer",
    "reit": "Property / REIT",
}

def assess_business(income, balance, cashflow):
    """Top-level assessment: detect type, score the right way, stamp and flag."""
    income, balance, cashflow = _align_to_complete_years(income, balance, cashflow)
    ctype = detect_company_type(income, balance)
    p = build_metric_panel(income, balance, cashflow, ctype)

    if ctype == "industrial":
        pillars = score_industrial(p)
    elif ctype in ("bank", "holding"):
        pillars = score_financial(p, ctype)
    elif ctype == "insurer":
        pillars = score_insurer(p)
    elif ctype == "reit":
        pillars = score_reit(p)
    else:
        pillars = score_industrial(p)

    if "Growth" in pillars and (p.get("ipoDistorted") or p["totalYears"] < 4):
        pillars["Growth"] = min(pillars["Growth"], 65)

    weights = _PILLAR_WEIGHTS.get(ctype, _PILLAR_WEIGHTS["industrial"])
    tw = sum(weights.get(k, 0) for k in pillars)
    if tw <= 0:
        overall = round(sum(pillars.values()) / max(len(pillars), 1))
    else:
        overall = round(sum(pillars.get(k, 0) * weights.get(k, 0) for k in pillars) / tw)

    if p.get("ipoDistorted") or p["totalYears"] < 4:
        overall = min(overall, 85)

    if (p["recentCollapse"] or p["structurallyWeak"]) and overall > 55:
        overall = 55
    if p["structurallyWeak"]:
        overall = min(overall, 30)

    stamps = classify_stamps(p, pillars, overall, ctype)
    risks  = detect_risks(p, ctype)
    return {
        "ctype": ctype,
        "typeLabel": _TYPE_LABEL.get(ctype, "Business"),
        "panel": p,
        "pillars": pillars,
        "overall": overall,
        "band": _band(overall),
        "stamps": stamps,
        "risks": risks,
    }

_KIND_COLOR = {"good": "#1a7f37", "caution": "#9a6700", "bad": "#cf222e", "neutral": "#57606a"}
_KIND_BG    = {"good": "#dafbe1", "caution": "#fff8c5", "bad": "#ffebe9", "neutral": "#eaeef2"}

def _stamp_html(s):
    c = _KIND_COLOR.get(s["kind"], "#57606a")
    bg = _KIND_BG.get(s["kind"], "#eaeef2")
    return ("<span style='display:inline-block;padding:4px 12px;margin:3px 6px 3px 0;"
            "border-radius:14px;background:" + bg + ";color:" + c + ";border:1px solid " + c
            + "33;font-weight:600;font-size:0.86rem'>" + s["name"] + "</span>")

def render_verdict(income, balance, cashflow, currency, company_name):
    """The Verdict tab: one clear, financials-justified read on the business."""
    a = assess_business(income, balance, cashflow)
    p = a["panel"]

    if p["dataConfidence"] == "low":
        st.warning("Limited or unusual data for this company (" + "; ".join(p["dqFlags"])
                   + "). The read below may be unreliable - treat it as indicative only.")

    band = a["band"]
    band_color = {"High quality": "#1a7f37", "Solid": "#1a7f37", "Mixed / watch": "#9a6700",
                  "Weak": "#cf222e", "Avoid": "#cf222e"}.get(band, "#57606a")
    st.markdown("<div style='padding:6px 0'>"
                "<span style='font-size:1.5rem;font-weight:700'>" + company_name + "</span>"
                "&nbsp;&nbsp;<span style='color:#57606a'>" + a["typeLabel"] + "</span></div>",
                unsafe_allow_html=True)
    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown("<div style='font-size:3rem;font-weight:800;line-height:1;color:" + band_color
                    + "'>" + str(a["overall"]) + "<span style='font-size:1rem;color:#57606a'>/100</span></div>"
                    "<div style='font-size:1.1rem;font-weight:700;color:" + band_color + "'>" + band + "</div>",
                    unsafe_allow_html=True)
    with c2:
        st.markdown("<div style='margin-bottom:4px;font-weight:600'>Stamps</div>"
                    + "".join(_stamp_html(s) for s in a["stamps"]), unsafe_allow_html=True)

    st.caption("Every score and stamp below is derived only from this company's reported figures. "
               "This is a disciplined reading of the numbers, not investment advice.")
    st.divider()

    st.markdown("#### Scorecard")
    pc = st.columns(len(a["pillars"]))
    for (name, val), col in zip(a["pillars"].items(), pc):
        _ = col.metric(name, str(val))
    st.caption("Pillars are weighted for a " + a["typeLabel"].lower() + " to reach the headline score.")
    st.divider()

    st.markdown("#### What the numbers say")
    def fmt_pct(x):
        return "-" if x is None else "{:.1f}%".format(x * 100)
    def fmt_x(x):
        return "-" if x is None else "{:.1f}x".format(x)
    m = st.columns(4)
    m[0].metric("Return on equity", fmt_pct(p["roe"]))
    m[1].metric("Operating margin", fmt_pct(p["opMargin"]))
    m[2].metric("Net margin", fmt_pct(p["netMargin"]))
    m[3].metric("FCF margin", fmt_pct(p["fcfMargin"]))
    m = st.columns(4)
    m[0].metric("Revenue CAGR", fmt_pct(p["revCagr"]))
    m[1].metric("Earnings CAGR", fmt_pct(p["niCagr"]))
    m[2].metric("Net debt / EBITDA", fmt_x(p["netDebtToEbitda"]))
    m[3].metric("Interest cover", fmt_x(p["intCover"]))
    st.divider()

    st.markdown("#### Why these stamps")
    for s in a["stamps"]:
        c = _KIND_COLOR.get(s["kind"], "#57606a")
        st.markdown("<div style='margin:6px 0'><span style='font-weight:700;color:" + c + "'>"
                    + s["name"] + ".</span> " + s["desc"] + "</div>", unsafe_allow_html=True)
    st.divider()

    st.markdown("#### Debt & refinancing")
    level, text = _refinancing_read(p, currency)
    icon = {"none": "OK", "low": "OK", "moderate": "WATCH", "high": "RISK"}.get(level, "")
    st.markdown("**" + icon + "** - " + text)
    if p["debt"] and p["debt"] > 0:
        d = st.columns(4)
        d[0].metric("Total debt", fmt_money_compact(p["debt"], currency))
        d[1].metric("Cash", fmt_money_compact(p["cash"], currency))
        d[2].metric("Net debt", fmt_money_compact(p["netDebt"], currency))
        d[3].metric("Due within 1yr", fmt_money_compact(p["currentDebtDue"], currency))
    st.caption("A full year-by-year maturity ladder lives in the audited notes, not this data feed. "
               "The read above compares debt coming due against cash and free cash flow on hand.")
    st.divider()

    st.markdown("#### Risks to watch")
    sev_order = {"high": 0, "medium": 1, "low": 2}
    for sev, text in sorted(a["risks"], key=lambda r: sev_order.get(r[0], 3)):
        tag = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}.get(sev, "")
        col = {"high": "#cf222e", "medium": "#9a6700", "low": "#1a7f37"}.get(sev, "#57606a")
        st.markdown("<div style='margin:5px 0'><span style='font-weight:700;color:" + col
                    + "'>[" + tag + "]</span> " + text + "</div>", unsafe_allow_html=True)


_VAL_BAND_COLOR = {
    "undervalued": "#1a7f37", "cheap": "#2da44e", "fair": "#9a6700",
    "expensive": "#cf222e", "unknown": "#57606a",
}
_VAL_BAND_BG = {
    "undervalued": "#dafbe1", "cheap": "#dafbe1", "fair": "#fff8c5",
    "expensive": "#ffebe9", "unknown": "#eaeef2",
}


def _qseries(df, *names):
    for nm in names:
        if df is not None and nm in df.index:
            s = clean_series(df, nm)
            if len(s):
                return s
    return pd.Series(dtype=float)


def quarterly_momentum(qinc, qbal, qcf, fx=1.0):
    """Trailing-twelve-month figures and momentum from quarterly data. Compares the
    latest TTM to the year-before TTM and the latest quarter to the year-ago
    quarter, so a business that is quietly rolling over gets caught between annual
    reports. Returns {} if there are too few clean quarters."""
    rev = _qseries(qinc, "Total Revenue", "Revenue")
    ni  = _qseries(qinc, "Net Income to Common", "Net Income")
    oi  = _qseries(qinc, "Operating Income", "EBIT")
    fcf = _qseries(qcf, "Free Cash Flow")
    if fcf.empty:
        ocf, capex = _qseries(qcf, "Operating Cash Flow"), _qseries(qcf, "Capital Expenditures")
        if len(ocf) and len(capex):
            common = ocf.index.intersection(capex.index)
            fcf = (ocf[common] + capex[common]).dropna()
    if len(rev) < 4:
        return {}

    def ttm(s, back=0):
        if len(s) < 4 + back:
            return None
        end = len(s) - back
        return float(s.iloc[end - 4:end].sum())

    def yoy_q(s):
        if len(s) >= 5 and _fnum(s.iloc[-5]) and s.iloc[-5] != 0:
            return s.iloc[-1] / abs(s.iloc[-5]) - 1 if s.iloc[-5] > 0 else None
        return None

    def g(a, b):
        return (a / b - 1) if (_fnum(a) and _fnum(b) and b > 0) else None

    sh = _qseries(qinc, "Shares Outstanding (Basic)", "Basic Shares Outstanding",
                  "Total Common Shares Outstanding")
    shares_true = (sh.iloc[-1] / fx) if (len(sh) and _fnum(sh.iloc[-1]) and sh.iloc[-1] > 0) else None

    out = {
        "n_quarters": len(rev),
        "latestQ": str(rev.index[-1]),
        "ttmRev": ttm(rev), "ttmRevPrev": ttm(rev, 4),
        "ttmNi": ttm(ni),  "ttmNiPrev": ttm(ni, 4),
        "ttmOi": ttm(oi),  "ttmFcf": ttm(fcf),
        "qRevYoY": yoy_q(rev), "qNiYoY": yoy_q(ni),
        "revSeries": rev.iloc[-8:], "niSeries": ni.iloc[-8:],
    }
    out["ttmRevYoY"] = g(out["ttmRev"], out["ttmRevPrev"])
    out["ttmNiYoY"] = g(out["ttmNi"], out["ttmNiPrev"])
    out["ttmOpMargin"] = _safe_div(out["ttmOi"], out["ttmRev"])
    out["ttmNetMargin"] = _safe_div(out["ttmNi"], out["ttmRev"])
    out["ttmFcfMargin"] = _safe_div(out["ttmFcf"], out["ttmRev"])
    if shares_true and _fnum(out["ttmNi"]):
        out["ttmEps"] = out["ttmNi"] / shares_true
    out["momentum"] = _momentum_read(out)
    return out


def _momentum_read(m):
    qr, qn = m.get("qRevYoY"), m.get("qNiYoY")
    tr, tn = m.get("ttmRevYoY"), m.get("ttmNiYoY")
    if _fnum(qr) and qr < -0.01 and _fnum(tr) and tr > 0:
        return ("caution", "The latest quarter's revenue is DOWN year-on-year even "
                "though the trailing year is still up - momentum may be rolling over.")
    if _fnum(qn) and qn < -0.02 and _fnum(tn) and tn >= 0:
        return ("caution", "Latest-quarter earnings fell year-on-year while the "
                "trailing year held up - watch the next print.")
    if _fnum(qr) and _fnum(tr) and qr > tr + 0.03 and tr > 0:
        return ("good", "Growth is accelerating - the latest quarter is running ahead "
                "of the trailing-year pace.")
    if _fnum(tr) and tr > 0.02:
        return ("good", "Steady growth - the trailing twelve months are ahead of the "
                "year before.")
    if _fnum(tr) and tr < -0.02:
        return ("bad", "The trailing twelve months are below the year before - on "
                "current data the business is shrinking.")
    return ("neutral", "Roughly flat, or too few clean quarters to call it.")


def render_momentum(ticker, currency):
    """Momentum tab: quarterly trend and trailing-twelve-month figures, so the read
    isn't months out of date between annual reports."""
    st.subheader("Momentum - latest quarter & trailing twelve months")
    st.caption("Annual statements can be six months stale. This uses the quarterly "
               "feed to show the trailing twelve months (TTM) and whether the trend "
               "is accelerating or rolling over.")
    qinc, _, _ = get_quarterly(ticker, "Income Statement")
    qbal, _, _ = get_quarterly(ticker, "Balance Sheet")
    qcf, _, _ = get_quarterly(ticker, "Cash Flow")
    fx = (_FX_CONTEXT.get(ticker, {}) or {}).get("rate") or 1.0
    m = quarterly_momentum(qinc, qbal, qcf, fx)
    if not m:
        st.info("Quarterly data isn't available for this company from the source, so "
                "a momentum read can't be built. The annual view still applies.")
        return

    cur = currency or "JMD"

    def pct(x):
        return "-" if not _fnum(x) else f"{x*100:+.0f}%"

    def pctm(x):
        return "-" if not _fnum(x) else f"{x*100:.1f}%"

    def money(x):
        return "-" if not _fnum(x) else fmt_money_compact(x, cur)

    kind, txt = m["momentum"]
    col = {"good": "#1a7f37", "caution": "#9a6700", "bad": "#cf222e",
           "neutral": "#57606a"}.get(kind, "#57606a")
    bg = {"good": "#dafbe1", "caution": "#fff8c5", "bad": "#ffebe9",
          "neutral": "#eaeef2"}.get(kind, "#eaeef2")
    st.markdown("<div style='margin:8px 0;padding:12px 16px;border-radius:10px;"
                "background:" + bg + ";border:1px solid " + col + "44'>"
                "<b style='color:" + col + "'>Momentum: " + txt + "</b></div>",
                unsafe_allow_html=True)
    st.caption(f"Most recent quarter on file: {m['latestQ']} "
               f"({m['n_quarters']} quarters available).")

    c = st.columns(4)
    c[0].metric("TTM revenue", money(m.get("ttmRev")), pct(m.get("ttmRevYoY")),
                help="Trailing twelve months vs the twelve months before.")
    c[1].metric("TTM net income", money(m.get("ttmNi")), pct(m.get("ttmNiYoY")))
    c[2].metric("Latest-Q revenue YoY", pct(m.get("qRevYoY")))
    c[3].metric("Latest-Q earnings YoY", pct(m.get("qNiYoY")))
    c = st.columns(4)
    c[0].metric("TTM operating margin", pctm(m.get("ttmOpMargin")))
    c[1].metric("TTM net margin", pctm(m.get("ttmNetMargin")))
    c[2].metric("TTM FCF margin", pctm(m.get("ttmFcfMargin")))
    if _fnum(m.get("ttmEps")):
        c[3].metric("TTM EPS", f"{cur} {m['ttmEps']:,.2f}")

    st.divider()
    rs, ns = m.get("revSeries"), m.get("niSeries")
    cc = st.columns(2)
    if rs is not None and len(rs):
        with cc[0]:
            st.plotly_chart(bar_chart(rs, "Quarterly revenue", cur),
                            use_container_width=True)
    if ns is not None and len(ns):
        with cc[1]:
            st.plotly_chart(bar_chart(ns, "Quarterly net income", cur),
                            use_container_width=True)
    st.caption("Quarterly figures are as-reported and can be seasonal; the YoY "
               "comparisons above control for seasonality by using the same quarter "
               "a year earlier and the full trailing year.")


def _band_metric(col, label, stat, fmtfn, cheap_when_below):
    """Render one 'now vs its own history' metric; return a (label, kind, read)
    summary tuple for the caption line, or None."""
    if not stat or not _fnum(stat.get("current")):
        col.metric(label, "-")
        return None
    cur, med = stat["current"], stat["median"]
    delta = None
    if _fnum(cur) and _fnum(med) and med > 0:
        delta = f"{(cur/med - 1)*100:+.0f}% vs median"
    col.metric(label, fmtfn(cur), delta, delta_color="off",
               help=f"Median {fmtfn(med)}, range {fmtfn(stat['low'])}-"
                    f"{fmtfn(stat['high'])} over {stat['n']} years.")
    if not (_fnum(cur) and _fnum(med) and med > 0):
        return None
    ratio = cur / med
    cheap = (ratio < 0.9) if cheap_when_below else (ratio > 1.1)
    rich = (ratio > 1.1) if cheap_when_below else (ratio < 0.9)
    kind = "good" if cheap else "caution" if rich else "neutral"
    word = "cheaper than usual" if cheap else "dearer than usual" if rich else "about typical"
    return (label, kind, f"{label} is {word}")


def render_history(income, balance, cashflow, currency, ticker):
    """History tab: valuation versus the company's own past, and its dividend record."""
    st.subheader("Valuation history & dividend record")
    st.caption("A stock is cheap or dear relative to its OWN past, not just in the "
               "abstract. This maps today's multiples onto their history and shows "
               "the multi-year dividend record.")
    inc_a, bal_a, cf_a = _align_to_complete_years(income, balance, cashflow)
    ctype = detect_company_type(inc_a, bal_a)
    p = build_metric_panel(inc_a, bal_a, cf_a, ctype)
    r = intrinsic_valuation(inc_a, bal_a, cf_a, p, get_ratios(ticker), ctype,
                            0.12, 0.02, currency, ticker, 0.25, quote=get_price(ticker))
    fx = (_FX_CONTEXT.get(ticker, {}) or {}).get("rate") or 1.0
    rhist = get_ratios_history(ticker)
    series = get_price_history(ticker)
    vh = valuation_history(rhist, inc_a, bal_a, cf_a, series, fx, r.get("price"),
                           r.get("eps"), r.get("bvps"), r.get("dps"))
    cur = currency or "JMD"

    def fx_(x):
        return "-" if not _fnum(x) else f"{x:.1f}x"

    def fy_(x):
        return "-" if not _fnum(x) else f"{x*100:.1f}%"

    st.markdown("#### Valuation vs its own history")
    if not vh or not any(vh.get(k) for k in ("pe", "pb", "yield")):
        st.info("Not enough overlapping price history and annual data to build "
                "valuation bands for this company (needs a few years of both).")
    else:
        cols = st.columns(3)
        reads = [
            _band_metric(cols[0], "P/E", vh.get("pe"), fx_, True),
            _band_metric(cols[1], "P/B", vh.get("pb"), fx_, True),
            _band_metric(cols[2], "Dividend yield", vh.get("yield"), fy_, False),
        ]
        good = [rd for rd in reads if rd and rd[1] == "good"]
        rich = [rd for rd in reads if rd and rd[1] == "caution"]
        if good:
            st.markdown("<span style='color:#1a7f37'>Versus its own history, "
                        + ", ".join(rd[0] for rd in good) + " look cheap.</span>",
                        unsafe_allow_html=True)
        if rich:
            st.markdown("<span style='color:#9a6700'>" + ", ".join(rd[0] for rd in rich)
                        + " sit above their historical norm.</span>",
                        unsafe_allow_html=True)
        pe = vh.get("pe")
        if pe and len(pe.get("series", {})) >= 3:
            s = pd.Series(pe["series"])
            s.index = [str(y) for y in s.index]
            st.plotly_chart(line_chart(s, "P/E by year", "P/E"),
                            use_container_width=True)
        st.caption(f"Historical multiples come from the data source's own ratios "
                   f"history ({vh.get('pe', {}).get('n', '?')} years); today's figure "
                   "uses the live price against the latest reported per-share values.")

    st.divider()
    st.markdown("#### Dividend track record")
    dh = dividend_history(cf_a, inc_a)
    if not dh:
        st.info("No multi-year dividend-per-share record is available for this company.")
    else:
        kind, txt = dh["verdict"]
        col = {"good": "#1a7f37", "caution": "#9a6700"}.get(kind, "#57606a")
        bg = {"good": "#dafbe1", "caution": "#fff8c5"}.get(kind, "#eaeef2")
        st.markdown("<div style='margin:6px 0;padding:12px 16px;border-radius:10px;"
                    "background:" + bg + ";border:1px solid " + col + "44'>"
                    "<b style='color:" + col + "'>" + txt + "</b></div>",
                    unsafe_allow_html=True)
        dc = st.columns(4)
        dc[0].metric("Years paid", f"{dh['paid']} / {dh['years']}")
        dc[1].metric("Rising streak", f"{dh['streak']} yrs")
        dc[2].metric("Cuts on record", str(len(dh["cuts"])))
        dc[3].metric("Dividend CAGR", fy_(dh["cagr"]))
        s = pd.Series(dh["series"])
        s.index = [str(y) for y in s.index]
        st.plotly_chart(bar_chart(s, "Dividend per share by year", cur),
                        use_container_width=True)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_report_html(ticker, companies, income, balance, cashflow, currency):
    """A self-contained one-page HTML summary the user can open and print to PDF."""
    inc_a, bal_a, cf_a = _align_to_complete_years(income, balance, cashflow)
    a = assess_business(inc_a, bal_a, cf_a)
    p = a["panel"]
    r = intrinsic_valuation(inc_a, bal_a, cf_a, p, get_ratios(ticker), a["ctype"],
                            0.12, 0.02, currency, ticker, 0.25, quote=get_price(ticker))
    eq = earnings_quality(inc_a, bal_a, cf_a, p)
    dh = dividend_history(cf_a, inc_a)
    cur = currency or "JMD"
    name = companies.get(ticker, ticker)

    def money(x):
        return "-" if not _fnum(x) else f"{cur} {x:,.2f}"

    def pctv(x):
        return "-" if not _fnum(x) else f"{x*100:.1f}%"

    def row(label, val):
        return f"<tr><td>{_esc(label)}</td><td style='text-align:right'>{val}</td></tr>"

    up = r.get("upside")
    up_txt = f"{up*100:+.0f}%" if _fnum(up) else "-"
    stamps = " · ".join(_esc(s.get("name", "")) for s in a.get("stamps", []))
    dvd = r.get("dividend") or {}
    dsafe = (dvd.get("safety") or ("", ""))[1]
    flags_html = "".join(f"<li><b>[{_esc(s.upper())}]</b> {_esc(t)}</li>"
                         for s, t in eq.get("flags", [])) or "<li>No forensic flags.</li>"
    div_line = (f"{pctv(dvd.get('yield'))} yield · {_esc(dsafe)}" if dvd.get("yield")
                else "No dividend")
    streak = (f"{dh['streak']}-yr rising streak, {dh['paid']}/{dh['years']} yrs paid"
              if dh else "n/a")

    return f"""<!doctype html><html><head><meta charset='utf-8'>
<title>{_esc(ticker)} — one-page report</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
   color:#1b1f24;max-width:820px;margin:24px auto;padding:0 18px;line-height:1.45}}
 h1{{margin:0 0 2px}} .sub{{color:#57606a}} h2{{border-bottom:2px solid #eaeef2;
   padding-bottom:4px;margin-top:22px;font-size:1.05rem}}
 table{{width:100%;border-collapse:collapse;font-size:0.92rem}}
 td{{padding:3px 6px;border-bottom:1px solid #f0f2f4}}
 .big{{font-size:2rem;font-weight:800}} .muted{{color:#57606a;font-size:0.85rem}}
 ul{{margin:6px 0;padding-left:18px}} .cols{{display:flex;gap:24px;flex-wrap:wrap}}
 .col{{flex:1;min-width:240px}}
 @media print{{body{{margin:0}}}}
</style></head><body>
<h1>{_esc(name)}</h1>
<div class='sub'>{_esc(ticker)} · {_esc(a['typeLabel'])} · figures in {_esc(cur)}</div>
<div class='cols'><div class='col'>
 <div class='big' style='color:#1a7f37'>{a['overall']}/100</div>
 <div><b>{_esc(a['band'])}</b></div>
 <div class='muted'>{stamps}</div>
</div><div class='col'>
 <table>
  {row('Central fair value', money(r.get('central')))}
  {row('Current price', money(r.get('price')))}
  {row('Upside to fair value', up_txt)}
  {row('Buy below (25% MoS)', money(r.get('buy_below')))}
 </table>
</div></div>

<h2>What the numbers say</h2>
<div class='cols'><div class='col'><table>
 {row('Return on equity', pctv(p.get('roe')))}
 {row('Operating margin', pctv(p.get('opMargin')))}
 {row('Net margin', pctv(p.get('netMargin')))}
 {row('FCF margin', pctv(p.get('fcfMargin')))}
</table></div><div class='col'><table>
 {row('Revenue CAGR', pctv(p.get('revCagr')))}
 {row('Earnings CAGR', pctv(p.get('niCagr')))}
 {row('Net debt / EBITDA', ('-' if not _fnum(p.get('netDebtToEbitda')) else f"{p['netDebtToEbitda']:.1f}x"))}
 {row('Interest cover', ('-' if not _fnum(p.get('intCover')) else f"{p['intCover']:.1f}x"))}
</table></div></div>

<h2>Dividend</h2>
<div>{div_line} · {_esc(streak)}</div>

<h2>Earnings quality — {_esc(eq['overall'][1])}</h2>
<ul>{flags_html}</ul>

<p class='muted'>Generated by the JSE Financial Analyzer at fixed screen assumptions
(12% discount, 2% terminal growth). A disciplined reading of reported figures,
not investment advice. Data: stockanalysis.com.</p>
</body></html>"""


def render_quality(income, balance, cashflow, currency, ticker):
    """Quality tab: earnings-quality forensic flags and a tradability read."""
    st.subheader("Earnings quality & tradability")
    st.caption("Two ways a good-looking headline can still hurt you: profit that "
               "isn't backed by cash, and a stock too thin to actually trade.")
    inc_a, bal_a, cf_a = _align_to_complete_years(income, balance, cashflow)
    ctype = detect_company_type(inc_a, bal_a)
    p = build_metric_panel(inc_a, bal_a, cf_a, ctype)
    eq = earnings_quality(inc_a, bal_a, cf_a, p)

    st.markdown("#### Earnings quality")
    kind, txt = eq["overall"]
    col = {"good": "#1a7f37", "caution": "#9a6700", "bad": "#cf222e"}.get(kind, "#57606a")
    bg = {"good": "#dafbe1", "caution": "#fff8c5", "bad": "#ffebe9"}.get(kind, "#eaeef2")
    st.markdown("<div style='margin:6px 0;padding:12px 16px;border-radius:10px;"
                "background:" + bg + ";border:1px solid " + col + "44'>"
                "<b style='color:" + col + "'>" + txt + "</b></div>",
                unsafe_allow_html=True)
    if _fnum(eq.get("ocfNi")):
        mc = st.columns(2)
        mc[0].metric("Operating cash / profit", f"{eq['ocfNi']*100:.0f}%",
                     help="How much of reported net income turned into operating "
                          "cash last year. Below ~70% is a yellow flag.")
        if _fnum(eq.get("cashConv")):
            mc[1].metric("Free cash / profit", f"{eq['cashConv']*100:.0f}%")
    sev = {"high": ("#cf222e", "HIGH"), "medium": ("#9a6700", "WATCH")}
    if eq["flags"]:
        for s, text in sorted(eq["flags"], key=lambda f: 0 if f[0] == "high" else 1):
            c, tag = sev.get(s, ("#57606a", ""))
            st.markdown("<div style='margin:5px 0'><span style='font-weight:700;color:"
                        + c + "'>[" + tag + "]</span> " + text + "</div>",
                        unsafe_allow_html=True)
    else:
        st.caption("No forensic flags tripped on the reported figures.")

    st.divider()
    st.markdown("#### Tradability (liquidity)")
    liq = liquidity_read(get_price_history(ticker))
    if not liq:
        st.info("Not enough price history to gauge how thinly this trades.")
    else:
        lc = {"good": "#1a7f37", "caution": "#9a6700", "bad": "#cf222e",
              "neutral": "#57606a"}.get(liq["kind"], "#57606a")
        st.markdown("<div style='margin:4px 0'><b style='color:" + lc + "'>"
                    + liq["label"].capitalize() + ".</b> " + liq["note"] + "</div>",
                    unsafe_allow_html=True)
        qc = st.columns(2)
        qc[0].metric("Days with no price change",
                     f"{liq['unchangedFrac']*100:.0f}%",
                     help="Over the last ~60 trading days on file. High means trades "
                          "are rare - a thinly-traded stock.")
        if _fnum(liq.get("avgMove")):
            qc[1].metric("Average daily move", f"{liq['avgMove']*100:.1f}%")
        st.caption("A proxy from daily closes - this data feed doesn't carry share "
                   "volume, so exact J$ turnover isn't shown. Unchanged-price days are "
                   "the tell for a stock that rarely trades.")


def render_valuation(income, balance, cashflow, currency, ticker,
                     discount, term_g, mos, asset_life=0, residual_pct=0.0,
                     recovery=None):
    """The Valuation tab: a fair value built from several long-standing methods,
    blended to a central estimate with a range and a margin-of-safety line, then
    compared to the live market price."""
    st.subheader("Valuation - what the business is worth")
    st.caption(
        "A fair value assembled from methods that have held up for a very long "
        "time - a Buffett owner-earnings DCF, dividend discounting, earnings "
        "power, book-value returns and Graham's bounds - each applied only where "
        "it fits this kind of business, then blended into one central estimate "
        "with a range. A tool for judgement, not a target price."
    )

    inc_a, bal_a, cf_a = _align_to_complete_years(income, balance, cashflow)
    ctype = detect_company_type(inc_a, bal_a)
    p = build_metric_panel(inc_a, bal_a, cf_a, ctype)
    ratios = get_ratios(ticker)
    quote = get_price(ticker)
    r = intrinsic_valuation(inc_a, bal_a, cf_a, p, ratios, ctype,
                            discount, term_g, currency, ticker, mos, quote,
                            asset_life=asset_life, residual_pct=residual_pct,
                            recovery=recovery)

    if p["dataConfidence"] == "low":
        st.warning("Limited or unusual data for this company (" + "; ".join(p["dqFlags"])
                   + "). Treat the valuation as indicative only.")

    type_label = _TYPE_LABEL.get(ctype, "Business")
    st.markdown("<span style='color:#57606a'>Valued as a <b>" + type_label.lower()
                + "</b> - the methods below are the ones that suit that.</span>",
                unsafe_allow_html=True)
    if r.get("asset_life"):
        st.info(
            f"**Finite-life mode: {r['asset_life']} years.** The DCF stops at that "
            "horizon instead of assuming cash flows last forever - the right "
            "treatment for a concession, mine or single lease that expires. Set "
            "this back to 0 in the sidebar for an ordinary going concern."
        )

    if not _fnum(r["central"]):
        st.info(
            "Not enough of the right data to build a fair value for this company "
            "(it may be a fund, a very new listing, or missing key statement "
            "lines). The individual methods that could be computed are listed below."
        )

    cur = currency or "JMD"

    def money(x):
        return "-" if not _fnum(x) else f"{cur} {x:,.2f}"

    # ---- Headline: central fair value, price, upside, buy-below ------------
    if _fnum(r["central"]):
        cols = st.columns(4)
        cols[0].metric("Central fair value", money(r["central"]),
                       help=f"Median of {r['n_core']} methods that fit a "
                            f"{type_label.lower()}.")
        _psrc = r.get("price_source") or "unavailable"
        _pdate = f" as of {r['price_date']}" if r.get("price_date") else ""
        cols[1].metric("Current price",
                       money(r["price"]) if _fnum(r["price"]) else "n/a",
                       help=f"Source: {_psrc}{_pdate} (live, cached).")
        if _fnum(r["upside"]):
            cols[2].metric("Upside to fair value", f"{r['upside']*100:+.0f}%",
                           delta=f"{r['upside']*100:+.0f}%", delta_color="normal")
        else:
            cols[2].metric("Upside to fair value", "n/a")
        cols[3].metric(f"Buy below (-{mos*100:.0f}% MoS)", money(r["buy_below"]),
                       help="Fair value less your margin of safety - the price at "
                            "which the odds are tilted your way.")

        # verdict banner
        bcol = _VAL_BAND_COLOR.get(r["band"], "#57606a")
        bbg = _VAL_BAND_BG.get(r["band"], "#eaeef2")
        st.markdown(
            "<div style='margin:10px 0;padding:12px 16px;border-radius:10px;"
            "background:" + bbg + ";border:1px solid " + bcol + "44'>"
            "<span style='font-weight:700;color:" + bcol + "'>" + r["verdict"]
            + "</span></div>", unsafe_allow_html=True)

        if _fnum(r["low"]) and _fnum(r["high"]):
            st.caption(
                f"Methods span {money(r['low'])} to {money(r['high'])} per share. "
                "A wide spread means the answer depends heavily on which lens you "
                "trust; a tight one means the methods agree."
            )

        if _fnum(r.get("low52")) and _fnum(r.get("high52")):
            pos = ""
            if _fnum(r["price"]) and r["high52"] > r["low52"]:
                frac = (r["price"] - r["low52"]) / (r["high52"] - r["low52"])
                pos = f" - trading {frac*100:.0f}% of the way up its 12-month range"
            st.caption(
                f"52-week price range: {money(r['low52'])} to {money(r['high52'])}"
                f"{pos}."
            )

    # ---- Expected return + dividend safety --------------------------------
    ex = r.get("expret") or {}
    dv = r.get("dividend") or {}
    if ex or dv:
        ec = st.columns(3)
        if ex.get("annual5y") is not None:
            ec[0].metric("Expected return (~5 yr)", f"{ex['annual5y']*100:+.0f}%/yr",
                         help="Rough annualised return if the price drifts to the "
                              "central fair value over five years, plus the dividend "
                              "yield collected along the way. Not a forecast.")
        if dv.get("yield") is not None:
            ec[1].metric("Dividend yield", f"{dv['yield']*100:.1f}%")
        if _fnum(dv.get("payoutNi")):
            ec[2].metric("Payout of earnings", f"{dv['payoutNi']*100:.0f}%",
                         help="Dividends as a share of net income. Under ~70-80% is "
                              "usually comfortable.")
        if dv.get("safety"):
            kind, txt = dv["safety"]
            col = {"good": "#1a7f37", "caution": "#9a6700",
                   "bad": "#cf222e"}.get(kind, "#57606a")
            fcf_txt = (f" Payout of free cash flow: {dv['payoutFcf']*100:.0f}%."
                       if _fnum(dv.get("payoutFcf")) else "")
            st.markdown("<div style='margin:2px 0;color:" + col + "'><b>Dividend "
                        "safety:</b> " + txt + fcf_txt + "</div>",
                        unsafe_allow_html=True)

    # ---- Reliability / data-trust note ------------------------------------
    _flags = r.get("dqFlags") or []
    if _flags:
        st.warning("**Read this valuation with extra caution** - " + "; ".join(_flags)
                   + ". The methods still run, but a fair value built on unusual or "
                   "short data is less dependable.")

    st.divider()

    # ---- How each method sees it ------------------------------------------
    st.markdown("#### How each method values it")
    rows = []
    for m in r["methods"]:
        if not m["applies"]:
            continue
        vs = "-"
        if _fnum(m["value"]) and _fnum(r["price"]) and r["price"] > 0:
            vs = f"{(m['value']/r['price']-1)*100:+.0f}%"
        rows.append({
            "Method": ("* " if m["core"] else "") + m["name"],
            "Fair value / share": money(m["value"]) if _fnum(m["value"]) else "n/a",
            "vs price": vs,
            "In blend": "yes" if m["core"] else "-",
            "What it assumes": m["basis"],
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("Rows marked * are blended into the central fair value. Others "
                   "are cross-checks, floors or ceilings shown for context.")

    # ---- Buffett owner-earnings DCF, in detail ----------------------------
    oe_method = next((m for m in r["methods"]
                      if m["name"].startswith("Owner-earnings")), None)
    if oe_method and _fnum(oe_method["value"]):
        with st.expander("Buffett owner-earnings DCF - the assumptions, and how "
                         "sensitive it is", expanded=True):
            d = r["oe_detail"]
            st.markdown(
                f"**Owner earnings** = net income {money(d.get('ni'))} "
                f"+ depreciation & amortisation {money(d.get('da'))} "
                f"- maintenance capex {money(d.get('maint_capex'))} "
                f"= **{money((d.get('ni') or 0)+(d.get('da') or 0)-(d.get('maint_capex') or 0))}**, "
                f"or {money(r['oeps'])} per share."
            )
            st.markdown(
                f"Grown at **{r['g1']*100:.1f}%** in year one (the lower of earnings "
                f"and revenue CAGR, capped), fading to **{term_g*100:.1f}%** by year "
                f"ten, then held there forever. Discounted at **{discount*100:.1f}%**."
            )
            grid = dcf_sensitivity(r["oeps"], r["g1"], term_g, cur)
            if grid is not None:
                st.markdown("**Value per share across assumptions** (discount rate "
                            "down the side, terminal growth across the top):")
                disp = grid.copy()
                for _c in disp.columns:
                    disp[_c] = disp[_c].map(lambda x: money(x) if _fnum(x) else "-")
                st.dataframe(disp, use_container_width=True)
                st.caption("If the value swings wildly across this grid, the DCF is "
                           "leaning hard on assumptions - lean more on the "
                           "book-value, dividend and Graham methods instead.")

    # ---- What the price is assuming (reverse DCF) -------------------------
    base_ps = None
    base_label = ""
    if _fnum(r.get("oeps")) and r["oeps"] > 0:
        base_ps, base_label = r["oeps"], "owner earnings"
    elif _fnum(r.get("dps")) and r["dps"] > 0:
        base_ps, base_label = r["dps"], "dividends"
    elif _fnum(r.get("fcfps")) and r["fcfps"] > 0:
        base_ps, base_label = r["fcfps"], "free cash flow"
    if _fnum(r.get("price")) and _fnum(base_ps):
        finite = bool(r.get("asset_life"))
        yrs = r.get("asset_life") or 10
        ig = implied_growth(base_ps, r["price"], term_g, discount,
                            finite=finite, years=yrs)
        st.markdown("#### What today's price is assuming")
        st.caption(
            "A reverse DCF: instead of guessing growth and getting a value, it "
            "holds today's price fixed and solves for the growth the market must "
            "be counting on - then checks that against what the business has done."
        )
        if ig is None:
            st.caption("Not enough data to reverse-engineer the implied growth.")
        elif ig[0] == "below":
            st.markdown(
                "<div style='color:#1a7f37'>The price is covered even if "
                + base_label + " <b>shrink</b> from here - the market is pricing in "
                "decline, so merely holding steady would be upside.</div>",
                unsafe_allow_html=True)
        elif ig[0] == "above":
            st.markdown(
                "<div style='color:#cf222e'>Even ~60%/yr growth would not justify "
                "the price under these assumptions - either expectations are extreme, "
                "or a cash-flow DCF is the wrong lens for this one (lean on the asset "
                "and book-value methods).</div>", unsafe_allow_html=True)
        else:
            g_impl = ig[1]
            kind, delivered, sentence = growth_reasonableness(g_impl, p)
            col = {"good": "#1a7f37", "caution": "#9a6700",
                   "bad": "#cf222e", "neutral": "#57606a"}.get(kind, "#57606a")
            horizon = f"{int(yrs)} years" if finite else "the next decade"
            st.markdown(
                f"To justify today's price of **{money(r['price'])}**, {base_label} "
                f"must grow about **{g_impl*100:.0f}% a year** for {horizon} "
                f"(then {term_g*100:.0f}% thereafter)."
            )
            deliv_txt = (f"For comparison, it has delivered "
                         f"{_pct(p.get('niCagr'))} earnings and "
                         f"{_pct(p.get('revCagr'))} revenue growth (annualised).")
            st.markdown(deliv_txt)
            st.markdown("<div style='margin-top:4px;font-weight:600;color:" + col
                        + "'>" + sentence + "</div>", unsafe_allow_html=True)
        st.caption(f"Assumes discount {discount*100:.0f}%, terminal growth "
                   f"{term_g*100:.0f}%" + (f", {int(yrs)}-yr life" if finite else "")
                   + f", on {base_label} of {money(base_ps)}/share.")

    # ---- Bear / base / bull scenarios -------------------------------------
    if _fnum(base_ps) and base_ps > 0:
        sc = scenario_values(base_ps, r["g1"], term_g, discount,
                             finite=bool(r.get("asset_life")),
                             years=r.get("asset_life") or 10)
        if sc and _fnum(sc.get("base")):
            st.markdown("#### Scenarios")
            st.caption("One number hides the risk. Here the base case is your "
                       "assumptions; the bear case cuts growth 5 points and demands "
                       "2% more return; the bull case does the reverse.")
            a = sc["assumptions"]
            scols = st.columns(3)
            for i, (nm, key) in enumerate([("Bear", "bear"), ("Base", "base"),
                                           ("Bull", "bull")]):
                v = sc.get(key)
                g_, d_ = a[key]
                up = (v / r["price"] - 1) if (_fnum(v) and _fnum(r.get("price"))
                                              and r["price"] > 0) else None
                scols[i].metric(f"{nm} fair value", money(v),
                                (f"{up*100:+.0f}% vs price" if _fnum(up) else None),
                                delta_color="off",
                                help=f"growth {g_*100:.0f}%, discount {d_*100:.0f}%")
            if _fnum(sc.get("bear")) and _fnum(r.get("price")):
                if r["price"] <= sc["bear"]:
                    st.markdown("<span style='color:#1a7f37'>Price is below even the "
                                "bear case - a genuine margin of safety.</span>",
                                unsafe_allow_html=True)
                elif _fnum(sc.get("bull")) and r["price"] > sc["bull"]:
                    st.markdown("<span style='color:#cf222e'>Price is above even the "
                                "bull case - a lot has to go right.</span>",
                                unsafe_allow_html=True)

    # ---- Comparables vs sector (peer-relative cross-check) ----------------
    _sm = st.session_state.get("sector_medians", {}).get(type_label)
    if _sm:
        peer_bits = []
        if _fnum(_sm.get("P/E")) and _fnum(r.get("eps")) and r["eps"] > 0:
            peer_bits.append(("on its sector's median P/E of "
                              f"{_sm['P/E']:.1f}x", _sm["P/E"] * r["eps"]))
        if _fnum(_sm.get("P/B")) and _fnum(r.get("bvps")) and r["bvps"] > 0:
            peer_bits.append(("on its sector's median P/B of "
                              f"{_sm['P/B']:.1f}x", _sm["P/B"] * r["bvps"]))
        if peer_bits:
            st.markdown("#### Comparables (vs sector)")
            for basis, val in peer_bits:
                vs = ""
                if _fnum(r.get("price")) and r["price"] > 0:
                    vs = f" - that's {(val/r['price']-1)*100:+.0f}% vs today's price"
                st.markdown(f"- Valued {basis}: **{money(val)}**{vs}.")
            st.caption("Peer medians come from the Compare page's scored universe. A "
                       "name can be cheap on its own yet dear versus its industry.")
    elif not st.session_state.get("sector_medians"):
        st.caption("Tip: open the **Compare companies** page once to load sector "
                   "medians, then this tab adds a peer-relative cross-check.")

    # ---- Asset backing & downside -----------------------------------------
    b = r.get("backing") or {}
    deposit_fin = ctype in ("bank", "insurer")
    if b and deposit_fin:
        # A bank/insurer's liabilities are customer deposits / policy reserves, not
        # trade creditors - so net-net, liquidation and 'cash less all liabilities'
        # are meaningless (they'd always be hugely negative). Book value is the anchor.
        st.markdown("#### Asset backing (book value)")
        price = r.get("price")
        bk = b.get("bookPs")
        c1, c2 = st.columns(2)
        c1.metric("Book value (~ NAV) / share", money(bk))
        if _fnum(bk) and _fnum(price) and bk > 0:
            c2.metric("Price / book", f"{price/bk:.2f}x")
        st.info(
            "For a deposit-taking bank or insurer, net-net and liquidation measures "
            "do **not** apply - its liabilities are customer deposits / policy "
            "reserves, not trade creditors, so 'cash minus all liabilities' is "
            "always deeply negative and says nothing about value. The right asset "
            "anchor is **book value (equity) per share**, and such businesses are "
            "valued on price-to-book against their return on equity (see the "
            "methods above)."
        )
        if _fnum(bk) and _fnum(price) and price <= bk:
            st.markdown("<div style='margin:4px 0;color:#1a7f37'>• Trading **below "
                        "book value** - a genuine discount to net asset value for a "
                        "financial, provided the loan/investment book is sound.</div>",
                        unsafe_allow_html=True)
    elif b:
        st.markdown("#### Asset backing & downside")
        st.caption(
            "What the assets alone are worth per share, net of ALL debt - the "
            "floor under the price. If the price sits near or below these, you are "
            "getting the operating business cheaply or for free."
        )
        ac = st.columns(4)
        ac[0].metric("Net cash / share", money(b.get("netCashPs")),
                     help="Cash minus all debt, per share. Positive means the "
                          "company could clear its debt and still hand cash back.")
        ac[1].metric("Net-net (NNWC)", money(b.get("nnwcPs")),
                     help="Graham's strictest floor: cash + 75% of receivables + "
                          "50% of inventory, less ALL liabilities.")
        ac[2].metric("Liquidation value", money(b.get("liquidationPs")),
                     help="Assets recovered at the sidebar's haircut rates, less all "
                          "liabilities. A wind-down estimate.")
        nav_label = "Book / NAV / share" if r.get("ctype") in ("reit",) else "Book value / share"
        ac[3].metric(nav_label, money(b.get("bookPs")))

        if r.get("ctype") == "reit":
            st.caption(
                "This is a property company, so its investment property is carried "
                "near fair value - book value here is effectively **net asset value "
                "(NAV)**, and price-to-book is price-to-NAV."
            )

        # Plain-language confidence flags, strongest first.
        price = r.get("price")
        flags = []
        if _fnum(price):
            df_note = " (and it is effectively debt-free)" if b.get("debtFree") else ""
            if _fnum(b.get("netCashPs")) and price <= b["netCashPs"]:
                flags.append(("good", "Price is **at or below net cash** - the market "
                              "is handing you the whole operating business for free" + df_note + "."))
            if _fnum(b.get("nnwcPs")) and price <= b["nnwcPs"]:
                flags.append(("good", "Price is **below Graham net-net working capital** - "
                              "a classic deep-value floor rarely seen in a sound business."))
            elif _fnum(b.get("liquidationPs")) and price <= b["liquidationPs"]:
                flags.append(("good", "Price is **below estimated liquidation value** - "
                              "the assets alone, sold off and debts paid, look worth more "
                              "than the market cap."))
            elif _fnum(b.get("bookPs")) and price <= b["bookPs"]:
                flags.append(("caution", "Price is **below book value** (P/B < 1). Cheap on "
                              "assets, but confirm the assets are real and earning."))
        for kind, txt in flags:
            col = "#1a7f37" if kind == "good" else "#9a6700"
            st.markdown("<div style='margin:4px 0;color:" + col + "'>• " + txt + "</div>",
                        unsafe_allow_html=True)

    if _fnum(r["graham_n"]):
        st.caption(f"Graham ceiling (upper sanity bound): {money(r['graham_n'])} - the "
                   "most a defensive buyer should pay on earnings and book together.")

    # ---- Per-share X-ray --------------------------------------------------
    xr = per_share_xray(inc_a, bal_a, cf_a, p, r.get("shares"), r.get("price"), ctype)
    if xr:
        st.divider()
        st.markdown("#### Per-share X-ray")
        price = xr.get("price")
        st.caption(
            "Every line divided by shares, next to the price"
            + (f" of {money(price)}" if _fnum(price) else "")
            + ". **A single figure above the price is not 'cheap' on its own** - "
            "gross assets are usually funded by debt, and low sales multiples are "
            "normal for thin-margin businesses. Only the signals at the bottom, "
            "which net off all liabilities or require real margins, are treated as "
            "genuine indications."
        )

        def _tbl(title, items, total=None):
            rows = []
            for lbl, v in items:
                pctp = (f"{v/price*100:,.0f}%" if (_fnum(v) and _fnum(price) and price > 0) else "-")
                rows.append({"Line": lbl, "Per share": money(v), "% of price": pctp,
                             "_v": v if _fnum(v) else None})
            if total is not None and _fnum(total):
                rows.append({"Line": "Total", "Per share": money(total),
                             "% of price": (f"{total/price*100:,.0f}%"
                                            if _fnum(price) and price > 0 else "-"),
                             "_v": total})
            if not rows:
                return
            df = pd.DataFrame(rows)

            def _hl(row):
                over = (_fnum(price) and price > 0 and _fnum(row["_v"])
                        and row["_v"] >= price)
                return ["background-color:#fff3cd" if over else ""] * len(row)
            st.markdown(f"**{title}**")
            st.dataframe(df.style.apply(_hl, axis=1).hide(axis="columns", subset=["_v"]),
                         hide_index=True, use_container_width=True)

        xc = st.columns(2)
        with xc[0]:
            _tbl("Assets / share", xr["assets"], xr.get("assets_total"))
            _tbl("Liabilities / share", xr["liab"])
        with xc[1]:
            _tbl("Income / share (per year)", xr["income"])
            if _fnum(xr.get("book_ps")):
                _tbl("Equity / share", [("Book value", xr["book_ps"])])
        st.caption("Amber-highlighted rows sit at or above the price. That alone is "
                   "**not** cheapness - the vetted check below says which comparisons "
                   "actually count, and why.")

        # ---- Signal check: what counts, and why --------------------------
        checks = xr.get("checks") or []
        if checks:
            st.markdown("**Signal check - what counts, and why**")
            _bg = {"strong": "#dafbe1", "moderate": "#dafbe1", "weak": "#fff8c5",
                   "none": "#f6f8fa", "context": "#eef1f4"}
            _lab = {"strong": "STRONG signal", "moderate": "Signal",
                    "weak": "Weak / caveated", "none": "Not a signal",
                    "context": "Context only"}
            crows = [{
                "Comparison": c["label"],
                "Per share": money(c["val"]),
                "vs price": ("above" if (_fnum(price) and price > 0 and c["val"] >= price)
                             else "below"),
                "Verdict": _lab.get(c["status"], c["status"]),
                "Why": c["why"],
                "_s": c["status"],
            } for c in checks]
            cdf = pd.DataFrame(crows)

            def _hlc(row):
                return [f"background-color:{_bg.get(row['_s'], '')}"] * len(row)
            st.dataframe(cdf.style.apply(_hlc, axis=1).hide(axis="columns", subset=["_s"]),
                         hide_index=True, use_container_width=True)
            st.caption("Green = a genuine undervaluation signal; yellow = real but "
                       "caveated; grey = considered and explicitly not a signal (with "
                       "the reason). This is why revenue, receivables or liabilities "
                       "sitting either side of the price don't move the needle on "
                       "their own.")

        # ---- Conservative liquidation test (the rigorous asset check) ----
        # Only for non-deposit businesses: on a bank/insurer the liabilities are
        # deposits/reserves, so a liquidation/net-net view is meaningless.
        lt = (liquidation_test(inc_a, bal_a, cf_a, p, r.get("shares"), r.get("price"),
                               currency, recovery) if not deposit_fin else None)
        if not deposit_fin and lt and lt.get("level") != "na":
            st.markdown("**Conservative liquidation test** - the rigorous version of "
                        "“price below its assets”")
            st.caption(
                "Cash + haircut receivables + haircut inventory, LESS every liability, "
                "per share - then coverage vs the price, gated by validation checks. A "
                "raw 'price below receivables' only counts if it survives all of this."
            )
            lc = st.columns(3)
            lc[0].metric("Liquidation value / share", money(lt.get("crlv_ps")),
                         help=f"Cash 100% + receivables {lt['recv_rate']*100:.0f}% + "
                              f"inventory {lt['inv_rate']*100:.0f}% - ALL liabilities. "
                              "Set the haircuts in the sidebar (e.g. 60% receivables).")
            lc[1].metric("Net cash / share (all liab.)", money(lt.get("net_cash_ps")))
            _cov = lt.get("coverage")
            lc[2].metric("Coverage vs price",
                         f"{_cov:.2f}x" if _fnum(_cov) else "n/a",
                         help="Liquidation value / price. Below 1x = the price is NOT "
                              "covered. 1.5-2x strong, >2x exceptional (but verify).")
            _lvbg = {"exceptional": "#dafbe1", "strong": "#dafbe1", "qualified": "#fff8c5",
                     "thin": "#fff8c5", "weak": "#ffebe9", "rejected": "#ffebe9"}
            _lvfg = {"exceptional": "#1a7f37", "strong": "#1a7f37", "qualified": "#9a6700",
                     "thin": "#9a6700", "weak": "#cf222e", "rejected": "#cf222e"}
            lv = lt.get("level")
            st.markdown("<div style='margin:6px 0;padding:12px 16px;border-radius:10px;"
                        "background:" + _lvbg.get(lv, "#eaeef2") + ";border:1px solid "
                        + _lvfg.get(lv, "#57606a") + "44'><span style='color:"
                        + _lvfg.get(lv, "#57606a") + "'>" + lt.get("summary", "")
                        + "</span></div>", unsafe_allow_html=True)
            _icon = {"pass": ("✓", "#1a7f37"), "warn": ("⚠", "#9a6700"),
                     "fail": ("✗", "#cf222e"), "info": ("•", "#57606a")}
            for c in lt.get("checks", []):
                ic, col = _icon.get(c["status"], ("•", "#57606a"))
                st.markdown("<div style='margin:3px 0'><span style='color:" + col
                            + ";font-weight:700'>" + ic + " " + _esc(c["name"])
                            + ".</span> <span style='color:#3a3f45'>" + _esc(c["text"])
                            + "</span></div>", unsafe_allow_html=True)
            st.caption("Coverage ladder: <1.0x not covered · 1.0-1.25x thin · "
                       "1.25-1.5x interesting · 1.5-2x strong · >2x exceptional. A "
                       "failed check (✗) caps the verdict regardless of the ratio.")

    # ---- Why these methods -------------------------------------------------
    with st.expander("Why these methods - and why they have lasted"):
        for m in r["methods"]:
            if not m["applies"]:
                continue
            st.markdown("**" + m["name"] + ".** " + m["why"]
                        + (("  \n_" + m["note"] + "_") if m["note"] else ""))

    # ---- Assumptions & FX footnote ----------------------------------------
    st.caption(
        f"Assumptions (all editable in the sidebar): discount / required return "
        f"{discount*100:.1f}%, long-term growth {term_g*100:.1f}%, margin of safety "
        f"{mos*100:.0f}%. Stage-one growth used: {r['g1']*100:.1f}%."
    )
    _fxc = _FX_CONTEXT.get(ticker, {})
    if _fxc.get("reported") == "USD":
        _psrc2 = r.get("price_source") or ""
        st.caption(
            f"Financials reported in USD, converted to JMD at {_fxc.get('rate', 0):,.2f} "
            "(live rate, 6-hour cache). The market price is taken in its own quote "
            "currency (this listing may trade in JMD even though it reports in USD), "
            "so for a JMD-traded USD reporter the fair-value comparison does depend "
            "on the exchange rate."
        )
    st.caption(
        "Intrinsic values are derived from this company's reported statements; the "
        "current price is the latest close from the site's price history (falling "
        "back to market cap / shares if that feed is unavailable). A disciplined "
        "reading of the numbers, not investment advice."
    )


# 4. USER INTERFACE
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60 * 60, show_spinner="Scoring the JSE universe...")
def build_universe():
    """Score and value every JSE company once, returning a tidy comparison table
    ordered strongest-first. Reuses assess_business() and intrinsic_valuation() so
    the numbers match the single-company view. Price comes from the same price-
    history feed the single view uses (the ratios feed is unreliable in bulk)."""
    rows = []
    panel_for = {}
    companies = get_companies()
    for tkr, name in companies.items():
        try:
            income, _, _ = get_statement(tkr, "Income Statement")
            balance, _, _ = get_statement(tkr, "Balance Sheet")
            cash, _, _ = get_statement(tkr, "Cash Flow")
            if income is None or balance is None:
                continue
            a = assess_business(income, balance, cash)
            p = a["panel"]
            panel_for[tkr] = p
            fxc = _FX_CONTEXT.get(tkr, {})

            def pct(x):
                return round(x * 100, 1) if isinstance(x, (int, float)) else None

            def num(x, nd=2):
                return round(x, nd) if isinstance(x, (int, float)) else None

            # Skip the (bulk-unreliable) ratios feed; price comes from the history
            # feed via quote, and the currency anchor is book value per share.
            rv = intrinsic_valuation(income, balance, cash, p, {},
                                     a["ctype"], 0.12, 0.02, "JMD", tkr, 0.25,
                                     quote=get_price(tkr))
            price, eps, bvps = rv.get("price"), rv.get("eps"), rv.get("bvps")
            up = rv.get("upside")
            value_lbl = ("Undervalued" if (isinstance(up, (int, float)) and up >= 0.25)
                         else "Overvalued" if (isinstance(up, (int, float)) and up <= -0.20)
                         else "Around fair" if isinstance(up, (int, float)) else "—")
            pe = (price / eps) if (_fnum(price) and _fnum(eps) and eps > 0) else None
            pb = (price / bvps) if (_fnum(price) and _fnum(bvps) and bvps > 0) else None
            dy = (rv.get("dividend") or {}).get("yield")
            rows.append({
                "Ticker": tkr,
                "Company": name,
                "Type": a["typeLabel"],
                "Score": a["overall"],
                "Band": a["band"],
                "Value": value_lbl,
                "Upside %": pct(up),
                "Price": num(price),
                "Fair value": num(rv.get("central")),
                "P/E": num(pe, 1),
                "P/B": num(pb, 1),
                "Div yield %": pct(dy),
                "ROE %": pct(p.get("roe")),
                "Op margin %": pct(p.get("opMargin")),
                "Net margin %": pct(p.get("netMargin")),
                "Rev CAGR %": pct(p.get("revCagr")),
                "Net debt/EBIT": num(p.get("netDebtToEbit"), 1),
                "Reported ccy": fxc.get("reported", "JMD"),
                "Stamps": ", ".join(s.get("name", "") for s in a.get("stamps", [])),
            })
        except Exception:
            continue
    st.session_state["panel_for"] = panel_for
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Score", ascending=False).reset_index(drop=True)
        # Stash sector medians so the single-company Valuation tab can add a
        # peer-relative comparables cross-check.
        try:
            med = (df.groupby("Type")[["P/E", "P/B", "Div yield %"]]
                   .median(numeric_only=True))
            st.session_state["sector_medians"] = med.to_dict("index")
        except Exception:
            pass
    return df


# ---------------------------------------------------------------------------
# Watchlist - a small set of flagged tickers with a one-line thesis each. The
# list lives in the page URL (?watch=CAR,TJH,...) so it survives a refresh and can
# be bookmarked or shared, with no server-side storage.
# ---------------------------------------------------------------------------

def get_watchlist():
    raw = st.query_params.get("watch", "")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return [t.strip().upper() for t in str(raw).split(",") if t.strip()]


def set_watchlist(tickers):
    seen, clean = set(), []
    for t in tickers:
        u = t.upper()
        if u and u not in seen:
            seen.add(u)
            clean.append(u)
    if clean:
        st.query_params["watch"] = ",".join(clean)
    else:
        try:
            del st.query_params["watch"]
        except Exception:
            st.query_params["watch"] = ""


def toggle_watch(ticker):
    wl = get_watchlist()
    u = ticker.upper()
    if u in wl:
        wl.remove(u)
    else:
        wl.append(u)
    set_watchlist(wl)


def watchlist_thesis(ticker, companies):
    """A compact one-line read for a watched ticker: quality, value vs price, the
    growth the price implies, and dividend safety. Fixed screen assumptions."""
    income, _, cur = get_statement(ticker, "Income Statement")
    balance, _, _ = get_statement(ticker, "Balance Sheet")
    cash, _, _ = get_statement(ticker, "Cash Flow")
    name = companies.get(ticker, ticker)
    if income is None and balance is None:
        return {"Ticker": ticker, "Company": name, "Score": None, "Band": "no data",
                "Value": "—", "Upside %": None, "Price": None, "Fair value": None,
                "Priced-in growth": "-", "Dividend": "-"}
    a = assess_business(income, balance, cash)
    p = a["panel"]
    rv = intrinsic_valuation(income, balance, cash, p, {}, a["ctype"],
                             0.12, 0.02, cur or "JMD", ticker, 0.25,
                             quote=get_price(ticker))
    up = rv.get("upside")
    value_lbl = ("Undervalued" if (isinstance(up, (int, float)) and up >= 0.25)
                 else "Overvalued" if (isinstance(up, (int, float)) and up <= -0.20)
                 else "Around fair" if isinstance(up, (int, float)) else "—")
    base = (rv.get("oeps") if (_fnum(rv.get("oeps")) and rv["oeps"] > 0)
            else rv.get("dps") if (_fnum(rv.get("dps")) and rv["dps"] > 0) else None)
    gtxt = "-"
    if base and _fnum(rv.get("price")):
        ig = implied_growth(base, rv["price"], 0.02, 0.12,
                            finite=bool(rv.get("asset_life")),
                            years=rv.get("asset_life") or 10)
        if ig:
            gtxt = ({"below": "decline priced in", "above": ">60%/yr"}
                    .get(ig[0], f"{ig[1]*100:.0f}%/yr"))
    dv = rv.get("dividend") or {}
    if dv.get("yield") is not None:
        safe = (dv.get("safety") or ("", ""))[0]
        dtxt = f"{dv['yield']*100:.1f}% ({safe})" if safe else f"{dv['yield']*100:.1f}%"
    else:
        dtxt = "none"
    return {
        "Ticker": ticker, "Company": name,
        "Score": a["overall"], "Band": a["band"], "Value": value_lbl,
        "Upside %": round(up * 100, 1) if isinstance(up, (int, float)) else None,
        "Price": round(rv["price"], 2) if _fnum(rv.get("price")) else None,
        "Fair value": round(rv["central"], 2) if _fnum(rv.get("central")) else None,
        "Priced-in growth": gtxt, "Dividend": dtxt,
    }


def render_watchlist(companies):
    """The Watchlist mode: a one-line thesis for each flagged ticker."""
    st.title("Watchlist")
    wl = get_watchlist()
    if not wl:
        st.info("Your watchlist is empty. Open a company under **Analyze one "
                "company** and use **☆ Add to watchlist** in the sidebar. The list "
                "is saved in this page's URL, so you can bookmark or share it.")
        return
    st.caption("One line per name: quality score, value vs price, the growth the "
               "price is implying, and dividend safety. Fixed screen assumptions "
               "(12% discount, 2% terminal). Saved in the page URL.")

    rows = [watchlist_thesis(t, companies) for t in wl]
    df = pd.DataFrame(rows)
    _vcolor = {"Undervalued": "#dafbe1", "Overvalued": "#ffebe9",
               "Around fair": "#fff8c5"}
    fmt = {"Upside %": "{:+.0f}%", "Price": "{:,.2f}", "Fair value": "{:,.2f}"}

    def _style(d):
        return (d.style.format(fmt, na_rep="-")
                .map(lambda v: f"background-color:{_vcolor.get(v, '')}",
                     subset=["Value"] if "Value" in d.columns else []))
    st.dataframe(_style(df), use_container_width=True, hide_index=True)

    c1, c2 = st.columns([3, 1])
    drop = c1.multiselect("Remove from watchlist", wl)
    if c2.button("Remove", use_container_width=True) and drop:
        set_watchlist([t for t in wl if t not in drop])
        st.rerun()
    st.download_button("Download watchlist as CSV", df.to_csv(index=False),
                       "jse_watchlist.csv", "text/csv")


# ---------------------------------------------------------------------------
# Portfolio - holdings with amounts, aggregated to a blended quality/value read,
# expected return, dividend income and sector concentration. Stored in the URL
# (?port=CAR:100000,TJH:50000) so it survives refresh and can be shared.
# ---------------------------------------------------------------------------

def get_portfolio():
    raw = st.query_params.get("port", "")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    out = {}
    for part in str(raw).split(","):
        if ":" in part:
            t, a = part.split(":", 1)
            t = t.strip().upper()
            try:
                amt = float(a)
            except Exception:
                continue
            if t and amt > 0:
                out[t] = out.get(t, 0.0) + amt
    return out


def set_portfolio(holdings):
    items = [f"{t}:{amt:g}" for t, amt in holdings.items() if amt > 0]
    if items:
        st.query_params["port"] = ",".join(items)
    else:
        try:
            del st.query_params["port"]
        except Exception:
            st.query_params["port"] = ""


def _holding_metrics(ticker, companies):
    income, _, cur = get_statement(ticker, "Income Statement")
    balance, _, _ = get_statement(ticker, "Balance Sheet")
    cash, _, _ = get_statement(ticker, "Cash Flow")
    if income is None and balance is None:
        return None
    a = assess_business(income, balance, cash)
    rv = intrinsic_valuation(income, balance, cash, a["panel"], {}, a["ctype"],
                             0.12, 0.02, cur or "JMD", ticker, 0.25,
                             quote=get_price(ticker))
    up = rv.get("upside")
    return {
        "name": companies.get(ticker, ticker), "type": a["typeLabel"],
        "score": a["overall"], "upside": up if isinstance(up, (int, float)) else None,
        "yield": (rv.get("dividend") or {}).get("yield"),
        "value": ("Undervalued" if (isinstance(up, (int, float)) and up >= 0.25)
                  else "Overvalued" if (isinstance(up, (int, float)) and up <= -0.20)
                  else "Around fair" if isinstance(up, (int, float)) else "—"),
    }


def render_portfolio(companies):
    """Portfolio mode: holdings with amounts, blended to a quality/value read,
    expected return, dividend income and sector concentration."""
    st.title("Portfolio")
    st.caption("Enter what you hold (in J$) to see a blended quality score, "
               "value read, expected return, dividend income and sector "
               "concentration. Saved in the page URL, so you can bookmark or share.")
    holdings = get_portfolio()

    labels = [f"{t} — {n}" for t, n in companies.items()]
    sym = {f"{t} — {n}": t for t, n in companies.items()}
    with st.form("add_holding", clear_on_submit=True):
        cc = st.columns([3, 2, 1])
        pick = cc[0].selectbox("Add a holding", labels)
        amt = cc[1].number_input("Amount (J$)", min_value=0.0, step=10000.0, value=0.0)
        if cc[2].form_submit_button("Add") and amt > 0:
            holdings[sym[pick]] = holdings.get(sym[pick], 0.0) + amt
            set_portfolio(holdings)
            st.rerun()

    if not holdings:
        st.info("No holdings yet. Add one above. Everything below updates live and "
                "is stored in the page URL.")
        return

    total = sum(holdings.values())
    rows, wscore, wups, income_jmd = [], 0.0, 0.0, 0.0
    sector = {}
    for t, amt in holdings.items():
        m = _holding_metrics(t, companies)
        w = amt / total if total else 0
        if m:
            if _fnum(m["score"]):
                wscore += w * m["score"]
            if _fnum(m["upside"]):
                wups += w * m["upside"]
            if _fnum(m["yield"]):
                income_jmd += amt * m["yield"]
            sector[m["type"]] = sector.get(m["type"], 0.0) + amt
            rows.append({"Ticker": t, "Company": m["name"], "Type": m["type"],
                         "Amount (J$)": round(amt), "Weight %": round(w * 100, 1),
                         "Score": m["score"], "Value": m["value"],
                         "Upside %": round(m["upside"] * 100, 1) if _fnum(m["upside"]) else None,
                         "Div yield %": round(m["yield"] * 100, 1) if _fnum(m["yield"]) else None})
        else:
            sector["Unknown"] = sector.get("Unknown", 0.0) + amt
            rows.append({"Ticker": t, "Company": companies.get(t, t), "Type": "—",
                         "Amount (J$)": round(amt), "Weight %": round(w * 100, 1),
                         "Score": None, "Value": "no data", "Upside %": None,
                         "Div yield %": None})

    mc = st.columns(4)
    mc[0].metric("Total invested", f"J$ {total:,.0f}")
    mc[1].metric("Blended quality", f"{wscore:.0f}/100")
    mc[2].metric("Expected upside", f"{wups*100:+.0f}%",
                 help="Weighted upside to each holding's central fair value.")
    mc[3].metric("Dividend income", f"J$ {income_jmd:,.0f}",
                 help=f"~{(income_jmd/total*100 if total else 0):.1f}% portfolio yield.")

    df = pd.DataFrame(rows).sort_values("Amount (J$)", ascending=False)
    _vcolor = {"Undervalued": "#dafbe1", "Overvalued": "#ffebe9",
               "Around fair": "#fff8c5"}
    fmt = {"Amount (J$)": "{:,.0f}", "Weight %": "{:.1f}%", "Upside %": "{:+.0f}%",
           "Div yield %": "{:.1f}%"}
    st.dataframe(df.style.format(fmt, na_rep="-")
                 .map(lambda v: f"background-color:{_vcolor.get(v, '')}",
                      subset=["Value"]),
                 use_container_width=True, hide_index=True)

    # Concentration checks
    top_w = max((h / total for h in holdings.values()), default=0)
    top_sec, top_sec_amt = (max(sector.items(), key=lambda kv: kv[1])
                            if sector else ("-", 0))
    warns = []
    if top_w > 0.25:
        warns.append(f"Your largest single position is {top_w*100:.0f}% of the "
                     "portfolio - concentrated in one name.")
    if total and top_sec_amt / total > 0.40:
        warns.append(f"{top_sec_amt/total*100:.0f}% sits in **{top_sec}** - heavy on "
                     "one sector.")
    for w in warns:
        st.warning(w)

    if sector:
        ss = pd.Series({k: round(v / total * 100, 1) for k, v in sector.items()})
        st.plotly_chart(bar_chart(ss, "Sector weights (%)", "%"),
                        use_container_width=True)

    c1, c2 = st.columns([3, 1])
    drop = c1.multiselect("Remove a holding", list(holdings.keys()))
    if c2.button("Remove", use_container_width=True) and drop:
        set_portfolio({t: a for t, a in holdings.items() if t not in drop})
        st.rerun()
    st.download_button("Download portfolio as CSV", df.to_csv(index=False),
                       "jse_portfolio.csv", "text/csv")


def render_compare():
    """Compare/screen the whole exchange: by industry, quality (investible
    universe), various metrics, and arbitrary head-to-head."""
    st.title("Compare companies")
    st.caption("Every score reuses the same disciplined, financials-only engine "
               "as the single-company view. Not investment advice.")
    uni = build_universe()
    if uni.empty:
        st.warning("Could not score any companies right now. Try again shortly.")
        return

    tab_screen, tab_h2h = st.tabs(["Screen the universe", "Head-to-head"])

    with tab_screen:
        c1, c2, c3 = st.columns(3)
        investible = c1.checkbox("Investible universe only (score > 70)", value=True)
        types = c2.multiselect("Industry / type", sorted(uni["Type"].unique()))
        sort_by = c3.selectbox("Rank by", [
            "Quality (strongest first)", "Upside to fair value", "ROE %",
            "Op margin %", "Rev CAGR %", "Div yield %",
            "Net debt/EBIT (low to high)",
        ])

        view = uni.copy()
        if investible:
            view = view[view["Score"] > 70]
        if types:
            view = view[view["Type"].isin(types)]

        _sortmap = {"Quality (strongest first)": "Score",
                    "Upside to fair value": "Upside %"}
        ascending = "low to high" in sort_by
        key = _sortmap.get(sort_by, sort_by.split(" (")[0].strip())
        view = view.sort_values(key, ascending=ascending, na_position="last")

        st.caption(f"{len(view)} companies, strongest first by default. **Value** "
                   "compares price to a blended fair value at fixed screen "
                   "assumptions (12% discount, 2% terminal growth) - a first pass, "
                   "not a substitute for the per-company Valuation tab.")

        # Clean, per-column number formatting (no ragged decimals / stray zeros).
        _pcols = ["Upside %", "Div yield %", "ROE %", "Op margin %", "Net margin %",
                  "Rev CAGR %"]
        fmt = {c: "{:.1f}%" for c in _pcols if c in view.columns}
        fmt["Upside %"] = "{:+.0f}%"
        for c in ("Price", "Fair value"):
            if c in view.columns:
                fmt[c] = "{:,.2f}"
        for c in ("P/E", "P/B", "Net debt/EBIT"):
            if c in view.columns:
                fmt[c] = "{:.1f}"
        _vcolor = {"Undervalued": "#dafbe1", "Overvalued": "#ffebe9",
                   "Around fair": "#fff8c5"}

        def _style_screen(df):
            return (df.style
                    .format(fmt, na_rep="-")
                    .map(lambda v: f"background-color:{_vcolor.get(v, '')}",
                         subset=["Value"] if "Value" in df.columns else []))
        st.dataframe(_style_screen(view), use_container_width=True, hide_index=True)
        st.download_button("Download as CSV", view.to_csv(index=False),
                           "jse_screen.csv", "text/csv")

        # ---- Peer-relative: how each valuation multiple sits vs its sector ----
        with st.expander("Sector medians (peer-relative valuation)"):
            st.caption("A company can look cheap on its own yet be dear versus its "
                       "peers, or vice-versa. These are the median multiples by "
                       "industry across the scored universe.")
            med = (uni.groupby("Type")[["P/E", "P/B", "Div yield %", "ROE %"]]
                   .median(numeric_only=True).round(1))
            med["# cos"] = uni.groupby("Type").size()
            st.dataframe(med.style.format({"P/E": "{:.1f}", "P/B": "{:.1f}",
                                           "Div yield %": "{:.1f}%",
                                           "ROE %": "{:.1f}%"}, na_rep="-"),
                         use_container_width=True)

    with tab_h2h:
        options = [f"{r.Ticker} \u2014 {r.Company}" for r in uni.itertuples()]
        picks = st.multiselect("Pick any 2 or more companies to compare", options)
        if len(picks) < 2:
            st.info("Choose at least two companies to see them side by side.")
            return
        chosen = [pk.split(" \u2014 ")[0] for pk in picks]
        sub = uni[uni["Ticker"].isin(chosen)].set_index("Ticker")
        panel_for = st.session_state.get("panel_for", {})

        types = set(sub["Type"])
        is_fin  = bool(types & {"Bank / diversified financial", "Insurer",
                                "Holding / investment company"})
        is_reit = "Property / REIT" in types
        OPERATING = [("ROIC %", False), ("ROE %", False), ("Op margin %", False),
                     ("Net margin %", False), ("FCF margin %", False), ("Rev CAGR %", False),
                     ("Net debt/EBIT", True), ("Interest cover", False)]
        FINANCIAL = [("ROE %", False), ("ROA %", False), ("Net margin %", False),
                     ("Rev CAGR %", False), ("Equity/assets %", False), ("Interest cover", False)]
        REITY     = [("ROA %", False), ("Op margin %", False), ("FCF margin %", False),
                     ("Rev CAGR %", False), ("Net debt/EBIT", True), ("Equity/assets %", False)]
        metric_set = REITY if (is_reit and not is_fin) else (FINANCIAL if is_fin else OPERATING)

        if len(types) > 1:
            st.warning("You're comparing different business types "
                       f"({', '.join(sorted(types))}). Margins, ROIC and leverage aren't "
                       "directly comparable across them — read each metric in context.")

        # valuation profiles
        vprof = {t: value_profile(panel_for.get(t, {}), get_ratios(t)) for t in chosen}
        VAL = [("EV/EBIT", "evEbit", True), ("P/B", "pb", True),
               ("FCF yield %", "fcfYield", False), ("Earnings yield %", "earningsYield", False),
               ("Yrs of net income", "priceToNi", True), ("Price/NCAV", "priceToNcav", True),
               ("Dividend yield %", "dividendYield", False)]
        for label, key, _ in VAL:
            for t in chosen:
                val = vprof[t].get(key)
                if "yield" in label.lower() and val is not None:
                    val = round(val * 100, 1)
                sub.loc[t, label] = round(val, 2) if isinstance(val, (int, float)) else None

        quality_cols = metric_set
        val_cols = [(lbl, lb) for (lbl, _k, lb) in VAL]
        show_cols = ["Company", "Score"] + [m for m, _ in quality_cols] + [m for m, _ in val_cols]
        table = sub[show_cols].copy()

        def _style(df):
            sty = df.style.format(precision=1, na_rep="–")
            for col, lower_better in [("Score", False)] + quality_cols + val_cols:
                if col not in df.columns:
                    continue
                vals = pd.to_numeric(df[col], errors="coerce")
                if vals.notna().sum() < 2:
                    continue
                sty = sty.background_gradient(
                    cmap="RdYlGn_r" if lower_better else "RdYlGn", subset=[col], axis=0)
                best = vals.idxmin() if lower_better else vals.idxmax()
                sty = sty.set_properties(subset=pd.IndexSlice[[best], [col]],
                                         **{"font-weight": "700", "border": "2px solid #1a7f37"})
            return sty
        st.dataframe(_style(table), use_container_width=True)
        if is_fin:
            st.caption("ROIC and EBIT-based leverage are omitted for financials — "
                       "they aren't meaningful on a bank/insurer balance sheet.")

        # normalized radar (quality metrics; skip lower-is-better leverage)
        radar_metrics = [m for m, lb in quality_cols if not lb]
        fig = go.Figure()
        for t in chosen:
            r = []
            for m in radar_metrics:
                col = pd.to_numeric(sub[m], errors="coerce")
                lo, hi = col.min(), col.max()
                x = pd.to_numeric(pd.Series([sub.loc[t, m]]), errors="coerce").iloc[0]
                r.append(0.0 if pd.isna(x) or hi == lo else (x - lo) / (hi - lo) * 100)
            fig.add_trace(go.Scatterpolar(r=r + r[:1], theta=radar_metrics + radar_metrics[:1],
                                          fill="toself", name=t))
        fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 100],
                          showticklabels=False)), showlegend=True, height=440,
                          margin=dict(l=40, r=40, t=20, b=20))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Radar is normalized per metric across only the selected companies "
                   "(100 = best of this group, not an absolute score).")

        st.markdown("#### What the price is telling you")
        for t in chosen:
            st.markdown(f"**{sub.loc[t, 'Company']}**")
            cl = vprof[t].get("clues", [])
            if cl:
                for kind, txt in cl:
                    st.markdown(f"{'🟢' if kind == 'good' else '🟠'} {txt}")
            else:
                st.caption("No standout value signals at the current price.")

        st.download_button("Download as CSV", table.to_csv(),
                           "jse_head_to_head.csv", "text/csv")


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
    _wl = get_watchlist()
    _pf = get_portfolio()
    mode = st.sidebar.radio(
        "Mode", ["Analyze one company", "Compare companies",
                 f"Watchlist ({len(_wl)})" if _wl else "Watchlist",
                 f"Portfolio ({len(_pf)})" if _pf else "Portfolio"])
    if mode == "Compare companies":
        render_compare()
        return
    if mode.startswith("Watchlist"):
        render_watchlist(companies)
        return
    if mode.startswith("Portfolio"):
        render_portfolio(companies)
        return
    labels = [f"{t} \u2014 {n}" for t, n in companies.items()]
    sym_by_label = {f"{t} \u2014 {n}": t for t, n in companies.items()}

    with st.sidebar:
        st.header("Company")
        default = next((l for l in labels if l.startswith("NCBFG")), labels[0])
        chosen = st.selectbox("Select a ticker", labels,
                              index=labels.index(default) if default in labels else 0)
        ticker = sym_by_label[chosen]

        _in_wl = ticker.upper() in get_watchlist()
        if st.button(("\u2605 Remove from watchlist" if _in_wl else "\u2606 Add to watchlist"),
                     use_container_width=True):
            toggle_watch(ticker)
            st.rerun()

        st.header("Valuation assumptions")
        discount = st.slider("Discount rate / required return (%)", 6.0, 20.0, 12.0, 0.5,
                             help="What you need to earn to bother owning it. JMD "
                                  "required returns are typically 12-16%.") / 100
        term_g = st.slider("Long-term growth (%)", 0.0, 5.0, 2.0, 0.5,
                           help="Perpetual growth after the explicit forecast. Keep "
                                "it at or below long-run GDP + inflation.") / 100
        mos = st.slider("Margin of safety (%)", 0.0, 50.0, 25.0, 5.0,
                        help="How far below fair value you insist on buying, to "
                             "protect against being wrong.") / 100
        asset_life = st.slider("Concession / asset life (years, 0 = forever)",
                               0, 40, 0, 1,
                               help="For businesses whose cash flows END on a date - "
                                    "a toll-road concession, a mine, a single lease. "
                                    "Set the years remaining and the DCF stops there "
                                    "instead of assuming the cash lasts forever. Leave "
                                    "at 0 for ordinary going concerns like TJH's "
                                    "concession.")
        residual_pct = 0.0
        recovery = dict(DEFAULT_RECOVERY)
        with st.expander("Liquidation recovery rates (advanced)"):
            if asset_life > 0:
                residual_pct = st.slider("Residual value at expiry (% of book)",
                                         0, 100, 0, 5,
                                         help="What equity holders recover when the "
                                              "asset expires/reverts. Usually 0 for a "
                                              "build-operate-transfer concession.") / 100
            st.caption("Fraction of each asset's book value recovered in a wind-down "
                       "(cash is always 100%, all debts paid in full).")
            recovery["recv"]  = st.slider("Receivables recovered (%)", 0, 100, 80, 5) / 100
            recovery["inv"]   = st.slider("Inventory recovered (%)", 0, 100, 60, 5) / 100
            recovery["ppe"]   = st.slider("Property, plant & equipment recovered (%)",
                                          0, 100, 40, 5) / 100
            recovery["other"] = st.slider("Other assets recovered (%)", 0, 100, 20, 5) / 100

        st.divider()
        st.caption(f"Build: {APP_BUILD}")

    # Load the three core statements once.
    income, inc_agg, cur_i = get_statement(ticker, "Income Statement")
    balance, bal_agg, cur_b = get_statement(ticker, "Balance Sheet")
    cashflow, cf_agg, cur_c = get_statement(ticker, "Cash Flow")
    # Take the currency from whichever statement loaded, so a single failed feed
    # never leaves it None (which showed up as "None 15.64" in the valuation).
    currency = cur_i or cur_b or cur_c or "JMD"

    if income is None and balance is None:
        st.error(
            f"No financial data could be loaded for {ticker}. Some JSE listings "
            "(funds, very new listings) are not covered by the data source. Try "
            "another ticker."
        )
        return

    missing = [nm for nm, df in (("income statement", income),
                                 ("balance sheet", balance),
                                 ("cash-flow statement", cashflow)) if df is None]
    if missing:
        st.warning(
            "Could not load the " + ", ".join(missing) + " for " + ticker
            + " right now - the data source sometimes throttles rapid requests. "
            "Figures that depend on it will be blank; reload the page in a moment "
            "to try again."
        )

    tabs = st.tabs(["Overview", "Verdict", "Income Statement", "Balance Sheet",
                    "Cash Flow", "Decomposition", "Ratios", "Valuation", "Momentum",
                    "History", "Quality"])

    # ---- Overview ---------------------------------------------------------
    with tabs[0]:
        oc1, oc2 = st.columns([3, 1])
        oc1.subheader(f"{companies.get(ticker, ticker)} \u2014 overview")
        try:
            _html = build_report_html(ticker, companies, income, balance,
                                      cashflow, currency)
            oc2.download_button("\u2b07 One-page report", _html,
                                f"{ticker}_report.html", "text/html",
                                use_container_width=True,
                                help="A printable summary - open it and print to PDF.")
        except Exception:
            pass
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
                               help=f"{r['name']} \u2014 {r.get('desc', '')}")

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
                _att = _STMT_ATTEMPTS.get((ticker, sname))
                if _att:
                    st.caption("Data source URLs tried (click to check directly):")
                    for _u, _outcome in _att:
                        st.caption(f"- [{_outcome}]({_u}) — {_outcome}")
                continue
            st.subheader(sname)
            item = st.selectbox("Choose a line item to analyse",
                                list(df.index), key=f"sel_{sname}")
            s = clean_series(df, item)

            c1, c2 = st.columns(2)
            c1.plotly_chart(bar_chart(s, f"{item}", currency), use_container_width=True)
            if len(s) > 1:
                c2.plotly_chart(growth_chart(s, f"{item} \u2014 yearly growth"),
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
            "and which components drove it \u2014 using only this company's reported data."
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
                        sign = "+" if drv["delta"] >= 0 else "\u2212"
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
                ylab = "%" if info["unit"] == "%" else "Ratio (\u00d7)"
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

    def _safe_tab(fn, *args):
        """Never let one company's data quirk crash the whole page."""
        try:
            fn(*args)
        except Exception as exc:
            st.error("This section hit an unexpected snag on this company's data "
                     f"and was skipped: {type(exc).__name__}: {exc}. The other tabs "
                     "still work - please let the developer know which ticker.")

    # ---- Valuation --------------------------------------------------------
    with tabs[7]:
        _safe_tab(render_valuation, income, balance, cashflow, currency, ticker,
                  discount, term_g, mos, asset_life, residual_pct, recovery)

    # ---- Momentum (quarterly / TTM) --------------------------------------
    with tabs[8]:
        _safe_tab(render_momentum, ticker, currency)

    # ---- History (valuation bands + dividend record) ---------------------
    with tabs[9]:
        _safe_tab(render_history, income, balance, cashflow, currency, ticker)

    # ---- Quality (earnings quality + liquidity) --------------------------
    with tabs[10]:
        _safe_tab(render_quality, income, balance, cashflow, currency, ticker)


if __name__ == "__main__":
    main()
