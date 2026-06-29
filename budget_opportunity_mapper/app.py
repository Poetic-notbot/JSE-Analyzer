from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ============================================================
# CONFIG
# ============================================================
st.set_page_config(
    page_title="JSE Investment Intelligence",
    page_icon="\U0001F4C8",
    layout="wide",
    initial_sidebar_state="expanded",
)

JMD = "J$"

# ============================================================
# STYLE
# ============================================================
st.markdown(
    """
<style>
.block-container { padding-top: 1.2rem; padding-bottom: 3rem; }
[data-testid="stAppViewContainer"] {
    background:
        radial-gradient(circle at top left, rgba(0, 140, 90, 0.10), transparent 32%),
        radial-gradient(circle at top right, rgba(255, 183, 3, 0.12), transparent 28%),
        linear-gradient(180deg, #f8fafc 0%, #ffffff 45%, #f8fafc 100%);
}
h1, h2, h3 { letter-spacing: -0.03em; }
.hero {
    padding: 1.4rem 1.6rem; border-radius: 24px;
    background: linear-gradient(135deg, #071b16 0%, #123c31 55%, #1f6f55 100%);
    color: white; box-shadow: 0 20px 45px rgba(0,0,0,0.16); margin-bottom: 1rem;
}
.hero h1 { margin-bottom: 0.2rem; font-size: 2.2rem; }
.hero p { color: rgba(255,255,255,0.82); margin-bottom: 0; font-size: 1.02rem; }
.card {
    background: rgba(255,255,255,0.86); border: 1px solid rgba(15,23,42,0.08);
    border-radius: 22px; padding: 1.1rem 1.2rem; box-shadow: 0 16px 40px rgba(15,23,42,0.06);
}
.metric-title { color: #64748b; font-size: 0.82rem; text-transform: uppercase; letter-spacing: .06em; }
.metric-value { color: #0f172a; font-size: 1.8rem; font-weight: 800; letter-spacing: -0.04em; }
.metric-note { color: #64748b; font-size: 0.88rem; }
.badge { display: inline-block; padding: 0.28rem 0.58rem; border-radius: 999px; font-weight: 700; font-size: 0.78rem; margin-right: 0.35rem; }
.badge-excellent { background: #dcfce7; color: #166534; }
.badge-good { background: #e0f2fe; color: #075985; }
.badge-watch { background: #fef9c3; color: #854d0e; }
.badge-risky { background: #fee2e2; color: #991b1b; }
.badge-avoid { background: #f1f5f9; color: #334155; }
.section-note { color: #475569; font-size: 0.95rem; }
.stDataFrame { border-radius: 18px; overflow: hidden; }
</style>
""",
    unsafe_allow_html=True,
)

# ============================================================
# DATA
# ============================================================
REQUIRED_COLUMNS = [
    "ticker", "company", "sector", "year", "price", "shares_outstanding",
    "revenue", "operating_profit", "net_income", "operating_cash_flow",
    "capex", "free_cash_flow", "dividends", "cash", "receivables", "inventory",
    "total_assets", "ppe", "total_debt", "payables", "equity",
]


def sample_data() -> pd.DataFrame:
    """
    Replace this sample with data/jse_financials.csv.
    Units:
    - Financial statement values should be J$ millions.
    - price should be J$ per share.
    - shares_outstanding should be millions of shares.
    """
    rows = []
    companies = [
        {"ticker": "TJH", "company": "TransJamaican Highway", "sector": "Infrastructure",
         "price": 3.05, "shares_outstanding": 12501, "base_revenue": 6200, "quality": 1.15},
        {"ticker": "CAR", "company": "Carreras", "sector": "Consumer Defensive",
         "price": 9.10, "shares_outstanding": 4854, "base_revenue": 14500, "quality": 1.25},
        {"ticker": "LASM", "company": "Lasco Manufacturing", "sector": "Manufacturing",
         "price": 6.00, "shares_outstanding": 4090, "base_revenue": 11800, "quality": 1.05},
        {"ticker": "WISYNCO", "company": "Wisynco", "sector": "Distribution",
         "price": 21.50, "shares_outstanding": 3750, "base_revenue": 39500, "quality": 1.10},
        {"ticker": "NCBFG", "company": "NCB Financial Group", "sector": "Financials",
         "price": 68.00, "shares_outstanding": 2466, "base_revenue": 122000, "quality": 0.75},
        {"ticker": "GK", "company": "GraceKennedy", "sector": "Conglomerate",
         "price": 73.00, "shares_outstanding": 995, "base_revenue": 155000, "quality": 0.92},
    ]
    for c in companies:
        for i, year in enumerate(range(2020, 2025)):
            growth = 1 + (0.07 + (c["quality"] - 1) * 0.03)
            rev = c["base_revenue"] * (growth ** i)
            margin = 0.13 * c["quality"] + i * 0.002
            op = rev * margin
            ni = op * (0.72 + 0.03 * c["quality"])
            ocf = ni * (0.85 + 0.14 * c["quality"]) - (rev * 0.015 if c["ticker"] in ["NCBFG", "GK"] else 0)
            capex = rev * (0.035 + (1.15 - c["quality"]) * 0.01)
            fcf = ocf - capex
            dividends = max(fcf * (0.35 + 0.2 * c["quality"]), 0)
            assets = rev * (1.55 if c["sector"] != "Financials" else 4.8)
            debt = assets * (0.28 if c["sector"] != "Financials" else 0.55)
            equity = assets - debt
            rows.append({
                "ticker": c["ticker"], "company": c["company"], "sector": c["sector"],
                "year": year, "price": c["price"], "shares_outstanding": c["shares_outstanding"],
                "revenue": rev, "operating_profit": op, "net_income": ni,
                "operating_cash_flow": ocf, "capex": capex, "free_cash_flow": fcf,
                "dividends": dividends, "cash": assets * 0.10, "receivables": rev * 0.16,
                "inventory": rev * 0.12 if c["sector"] not in ["Financials", "Infrastructure"] else rev * 0.01,
                "total_assets": assets, "ppe": assets * 0.32, "total_debt": debt,
                "payables": rev * 0.11, "equity": equity,
            })
    return pd.DataFrame(rows)


@st.cache_data
def load_data() -> pd.DataFrame:
    try:
        df = pd.read_csv("data/jse_financials.csv")
    except Exception:
        df = sample_data()
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        st.error(f"Missing required columns in data/jse_financials.csv: {missing}")
        st.stop()
    df = df.copy()
    df["year"] = df["year"].astype(int)
    numeric_cols = [c for c in REQUIRED_COLUMNS if c not in ["ticker", "company", "sector"]]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

# ============================================================
# CALCULATIONS
# ============================================================
def safe_div(a, b):
    if b is None or b == 0 or pd.isna(b):
        return np.nan
    return a / b


def cagr(first, last, periods):
    if first is None or last is None or periods <= 0:
        return np.nan
    if first <= 0 or last <= 0:
        return np.nan
    return (last / first) ** (1 / periods) - 1


def latest_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    latest_year = df["year"].max()
    latest = df[df["year"] == latest_year].copy()
    rows = []
    for ticker, g in df.groupby("ticker"):
        g = g.sort_values("year")
        first = g.iloc[0]
        last = g.iloc[-1]
        periods = int(last["year"] - first["year"])
        market_cap = last["price"] * last["shares_outstanding"]
        enterprise_value = market_cap + last["total_debt"] - last["cash"]
        invested_capital = last["total_debt"] + last["equity"] - last["cash"]
        nopat = last["operating_profit"] * 0.75
        row = {
            "ticker": ticker, "company": last["company"], "sector": last["sector"],
            "year": int(last["year"]), "price": last["price"], "market_cap": market_cap,
            "enterprise_value": enterprise_value, "revenue": last["revenue"],
            "operating_profit": last["operating_profit"], "net_income": last["net_income"],
            "free_cash_flow": last["free_cash_flow"], "dividends": last["dividends"],
            "total_debt": last["total_debt"], "cash": last["cash"], "equity": last["equity"],
            "invested_capital": invested_capital,
            "revenue_cagr": cagr(first["revenue"], last["revenue"], periods),
            "profit_cagr": cagr(first["operating_profit"], last["operating_profit"], periods),
            "fcf_cagr": cagr(first["free_cash_flow"], last["free_cash_flow"], periods),
            "roic": safe_div(nopat, invested_capital),
            "fcf_yield": safe_div(last["free_cash_flow"], market_cap),
            "earnings_yield": safe_div(last["net_income"], market_cap),
            "dividend_yield": safe_div(last["dividends"], market_cap),
            "fcf_conversion": safe_div(last["free_cash_flow"], last["net_income"]),
            "debt_to_equity": safe_div(last["total_debt"], last["equity"]),
            "debt_to_fcf": safe_div(last["total_debt"], last["free_cash_flow"]),
            "pe": safe_div(market_cap, last["net_income"]),
            "ev_ebit": safe_div(enterprise_value, last["operating_profit"]),
            "pb": safe_div(market_cap, last["equity"]),
            "payout_ratio": safe_div(last["dividends"], last["free_cash_flow"]),
            "expected_return": safe_div(last["free_cash_flow"], market_cap) + cagr(first["free_cash_flow"], last["free_cash_flow"], periods),
        }
        rows.append(row)
    out = pd.DataFrame(rows)
    return add_scores(out)


def score_series(s: pd.Series, higher_is_better=True, cap_low=None, cap_high=None) -> pd.Series:
    x = s.copy().astype(float)
    if cap_low is not None:
        x = x.clip(lower=cap_low)
    if cap_high is not None:
        x = x.clip(upper=cap_high)
    if x.nunique(dropna=True) <= 1:
        return pd.Series(50, index=s.index)
    pct = x.rank(pct=True)
    if not higher_is_better:
        pct = 1 - pct
    return (pct * 100).fillna(0)

def add_scores(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["roic_score"] = score_series(df["roic"], True, -0.05, 0.40)
    df["fcf_conversion_score"] = score_series(df["fcf_conversion"], True, -0.5, 1.5)
    df["growth_score"] = (
        score_series(df["revenue_cagr"], True, -0.10, 0.25) * 0.4
        + score_series(df["profit_cagr"], True, -0.10, 0.30) * 0.35
        + score_series(df["fcf_cagr"], True, -0.20, 0.35) * 0.25
    )
    df["balance_score"] = (
        score_series(df["debt_to_equity"], False, 0, 4) * 0.55
        + score_series(df["debt_to_fcf"], False, 0, 12) * 0.45
    )
    df["valuation_score"] = (
        score_series(df["fcf_yield"], True, -0.05, 0.25) * 0.45
        + score_series(df["earnings_yield"], True, -0.05, 0.25) * 0.25
        + score_series(df["pe"], False, 1, 40) * 0.15
        + score_series(df["ev_ebit"], False, 1, 35) * 0.15
    )
    df["dividend_score"] = (
        score_series(df["dividend_yield"], True, 0, 0.15) * 0.5
        + score_series((1 - (df["payout_ratio"] - 0.55).abs()), True) * 0.5
    )
    df["quality_score"] = (
        df["roic_score"] * 0.22
        + df["fcf_conversion_score"] * 0.18
        + df["growth_score"] * 0.18
        + df["balance_score"] * 0.17
        + df["valuation_score"] * 0.15
        + df["dividend_score"] * 0.10
    ).round(1)
    df["risk_score"] = (100 - df["balance_score"] * 0.55 - df["fcf_conversion_score"] * 0.25 - df["roic_score"] * 0.20).clip(0, 100).round(1)
    df["verdict"] = df["quality_score"].apply(verdict)
    return df


def verdict(score):
    if score >= 80:
        return "Excellent"
    if score >= 65:
        return "Good"
    if score >= 50:
        return "Watch"
    if score >= 35:
        return "Risky"
    return "Avoid for now"


def badge_html(v):
    klass = {
        "Excellent": "badge-excellent",
        "Good": "badge-good",
        "Watch": "badge-watch",
        "Risky": "badge-risky",
        "Avoid for now": "badge-avoid",
    }.get(v, "badge-watch")
    return f"<span class='badge {klass}'>{v}</span>"


def pct(x):
    if pd.isna(x):
        return "\u2014"
    return f"{x:.1%}"


def money(x):
    if pd.isna(x):
        return "\u2014"
    return f"{JMD}{x:,.0f}M"


def multiple(x):
    if pd.isna(x):
        return "\u2014"
    return f"{x:.1f}x"


def red_flags(company_df: pd.DataFrame) -> List[Tuple[str, str]]:
    g = company_df.sort_values("year")
    if len(g) < 2:
        return []
    last = g.iloc[-1]
    prev = g.iloc[-2]
    flags = []
    rev_growth = safe_div(last["revenue"] - prev["revenue"], prev["revenue"])
    profit_growth = safe_div(last["operating_profit"] - prev["operating_profit"], prev["operating_profit"])
    fcf_growth = safe_div(last["free_cash_flow"] - prev["free_cash_flow"], abs(prev["free_cash_flow"]))
    debt_growth = safe_div(last["total_debt"] - prev["total_debt"], prev["total_debt"])
    receivables_growth = safe_div(last["receivables"] - prev["receivables"], prev["receivables"])
    inventory_growth = safe_div(last["inventory"] - prev["inventory"], prev["inventory"])
    margin_last = safe_div(last["operating_profit"], last["revenue"])
    margin_prev = safe_div(prev["operating_profit"], prev["revenue"])
    if profit_growth > 0 and fcf_growth < 0:
        flags.append(("Profit up but FCF down", "Accounting profit improved, but cash generation weakened. Check working capital, capex, and one-off gains."))
    if rev_growth > 0 and margin_last < margin_prev:
        flags.append(("Revenue up but margin down", "The company is selling more but keeping less profit from each dollar of revenue."))
    if debt_growth > profit_growth and debt_growth > 0.10:
        flags.append(("Debt rising faster than profit", "Leverage may be funding growth instead of internally generated cash."))
    if receivables_growth > rev_growth + 0.08:
        flags.append(("Receivables rising faster than revenue", "Cash collection may be weakening or credit terms may be loosening."))
    if inventory_growth > rev_growth + 0.08:
        flags.append(("Inventory rising faster than revenue", "Inventory build may point to demand weakness or poor working-capital control."))
    if last["dividends"] > last["free_cash_flow"]:
        flags.append(("Dividends exceed FCF", "Dividend may be funded by cash reserves, debt, or balance sheet strain."))
    if last["operating_cash_flow"] < 0:
        flags.append(("Negative operating cash flow", "Core operations consumed cash."))
    return flags


# ============================================================
# VISUALS
# ============================================================
def trend_chart(g, cols, title):
    long = g.melt(id_vars=["year"], value_vars=cols, var_name="Metric", value_name="Value")
    fig = px.line(long, x="year", y="Value", color="Metric", markers=True, title=title)
    fig.update_layout(height=430, margin=dict(l=20, r=20, t=55, b=20))
    return fig


def ratio_trend(g, title):
    x = g.copy()
    x["ROIC"] = (x["operating_profit"] * 0.75) / (x["total_debt"] + x["equity"] - x["cash"])
    x["FCF conversion"] = x["free_cash_flow"] / x["net_income"]
    long = x.melt(id_vars=["year"], value_vars=["ROIC", "FCF conversion"], var_name="Metric", value_name="Value")
    fig = px.line(long, x="year", y="Value", color="Metric", markers=True, title=title)
    fig.update_yaxes(tickformat=".0%")
    fig.update_layout(height=430, margin=dict(l=20, r=20, t=55, b=20))
    return fig


def capital_waterfall(last):
    labels = ["Operating profit", "Tax estimate", "NOPAT", "Operating cash flow", "Capex", "Free cash flow", "Dividends", "Retained FCF"]
    op = last["operating_profit"]
    tax = -op * 0.25
    nopat = op * 0.75
    ocf_delta = last["operating_cash_flow"] - nopat
    capex = -last["capex"]
    fcf = last["free_cash_flow"]
    div = -last["dividends"]
    retained = fcf - last["dividends"]
    fig = go.Figure(
        go.Waterfall(
            x=labels,
            y=[op, tax, 0, ocf_delta, capex, 0, div, 0],
            measure=["relative", "relative", "total", "relative", "relative", "total", "relative", "total"],
        )
    )
    fig.update_layout(title="Profit \u2192 Cash \u2192 Dividends Waterfall", height=440, margin=dict(l=20, r=20, t=55, b=20))
    return fig


def asset_breakdown(last):
    labels = ["Cash", "Receivables", "Inventory", "PPE", "Other assets"]
    other = last["total_assets"] - last["cash"] - last["receivables"] - last["inventory"] - last["ppe"]
    values = [last["cash"], last["receivables"], last["inventory"], last["ppe"], max(other, 0)]
    fig = px.pie(names=labels, values=values, title="Asset Breakdown")
    fig.update_layout(height=430, margin=dict(l=20, r=20, t=55, b=20))
    return fig


# ============================================================
# APP
# ============================================================
df = load_data()
snap = latest_snapshot(df)

with st.sidebar:
    st.title("\U0001F4C8 JSE Intelligence")
    st.caption("Quality. Cash flow. Valuation. Risk.")
    sectors = sorted(snap["sector"].dropna().unique())
    selected_sectors = st.multiselect("Sector", sectors, default=sectors)
    min_score = st.slider("Minimum quality score", 0, 100, 0)
    st.divider()
    st.caption("Data source: data/jse_financials.csv if present; otherwise sample demo data.")

snap_filtered = snap[(snap["sector"].isin(selected_sectors)) & (snap["quality_score"] >= min_score)].copy()

st.markdown(
    """
<div class="hero">
    <h1>JSE Investment Intelligence Dashboard</h1>
    <p>Rank Jamaican stocks by capital efficiency, cash conversion, growth quality, valuation, dividends, and balance-sheet risk.</p>
</div>
""",
    unsafe_allow_html=True,
)

# ============================================================
# TOP CARDS
# ============================================================
if snap_filtered.empty:
    st.warning("No companies match the current filters. Widen the sector or lower the minimum quality score.")
    st.stop()

best = snap_filtered.sort_values("quality_score", ascending=False).head(1).iloc[0]
highest_roic = snap_filtered.sort_values("roic", ascending=False).head(1).iloc[0]
highest_fcfy = snap_filtered.sort_values("fcf_yield", ascending=False).head(1).iloc[0]
riskiest = snap_filtered.sort_values("risk_score", ascending=False).head(1).iloc[0]

c1, c2, c3, c4 = st.columns(4)
cards = [
    (c1, "Best overall candidate", f"{best['ticker']}", f"{best['company']} \u00b7 Score {best['quality_score']:.1f}"),
    (c2, "Highest ROIC", f"{highest_roic['ticker']}", f"{pct(highest_roic['roic'])} ROIC"),
    (c3, "Highest FCF yield", f"{highest_fcfy['ticker']}", f"{pct(highest_fcfy['fcf_yield'])} FCF yield"),
    (c4, "Highest risk", f"{riskiest['ticker']}", f"Risk score {riskiest['risk_score']:.1f}"),
]
for col, title, value, note in cards:
    with col:
        st.markdown(
            f"""
<div class="card">
    <div class="metric-title">{title}</div>
    <div class="metric-value">{value}</div>
    <div class="metric-note">{note}</div>
</div>
""",
            unsafe_allow_html=True,
        )

st.divider()

# ============================================================
# TABS
# ============================================================
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["\U0001F3E0 Executive View", "\U0001F50E Screener", "\U0001F3E2 Company Deep Dive", "\U0001F6A9 Red Flags", "\U0001F4E4 Export"]
)

with tab1:
    left, right = st.columns([1.25, 1])
    with left:
        st.subheader("Top Ranked Stocks")
        ranking = snap_filtered.sort_values("quality_score", ascending=False).copy()
        show = ranking[[
            "ticker", "company", "sector", "quality_score", "verdict", "roic",
            "fcf_yield", "fcf_conversion", "revenue_cagr", "expected_return", "pe", "debt_to_equity",
        ]].copy()
        for col in ["roic", "fcf_yield", "fcf_conversion", "revenue_cagr", "expected_return"]:
            show[col] = show[col].map(pct)
        show["pe"] = show["pe"].map(multiple)
        show["debt_to_equity"] = show["debt_to_equity"].map(multiple)
        st.dataframe(show, use_container_width=True, hide_index=True)
    with right:
        fig = px.scatter(
            snap_filtered, x="fcf_yield", y="roic", size="market_cap", color="quality_score",
            hover_name="ticker", hover_data=["company", "sector", "pe", "debt_to_equity"],
            title="Quality Map: ROIC vs FCF Yield",
        )
        fig.update_xaxes(tickformat=".0%")
        fig.update_yaxes(tickformat=".0%")
        fig.update_layout(height=520)
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Score Breakdown")
    score_cols = ["roic_score", "fcf_conversion_score", "growth_score", "balance_score", "valuation_score", "dividend_score"]
    heat = snap_filtered.set_index("ticker")[score_cols].sort_values("roic_score", ascending=False)
    fig = px.imshow(
        heat, text_auto=".0f", aspect="auto", title="Component Scores",
        color_continuous_scale="RdYlGn", zmin=0, zmax=100,
    )
    fig.update_layout(height=520)
    st.plotly_chart(fig, use_container_width=True)

with tab2:
    st.subheader("Company Screener")
    screener = snap_filtered.copy()
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        min_roic = st.slider("Min ROIC", -10, 50, -10) / 100
    with c2:
        min_fcf_yield = st.slider("Min FCF yield", -10, 30, -10) / 100
    with c3:
        max_pe = st.slider("Max P/E", 1, 80, 80)
    with c4:
        max_debt_equity = st.slider("Max debt/equity", 0.0, 5.0, 5.0)
    screener = screener[
        (screener["roic"].fillna(-999) >= min_roic)
        & (screener["fcf_yield"].fillna(-999) >= min_fcf_yield)
        & (screener["pe"].fillna(999) <= max_pe)
        & (screener["debt_to_equity"].fillna(999) <= max_debt_equity)
    ]
    display = screener.sort_values("quality_score", ascending=False)[[
        "ticker", "company", "sector", "price", "market_cap", "revenue_cagr", "profit_cagr",
        "fcf_cagr", "roic", "fcf_yield", "dividend_yield", "fcf_conversion", "pe", "ev_ebit",
        "debt_to_equity", "quality_score", "risk_score", "verdict",
    ]].copy()
    for col in ["revenue_cagr", "profit_cagr", "fcf_cagr", "roic", "fcf_yield", "dividend_yield", "fcf_conversion"]:
        display[col] = display[col].map(pct)
    for col in ["pe", "ev_ebit", "debt_to_equity"]:
        display[col] = display[col].map(multiple)
    display["market_cap"] = display["market_cap"].map(money)
    st.dataframe(display, use_container_width=True, hide_index=True)

with tab3:
    st.subheader("Company Deep Dive")
    selected = st.selectbox(
        "Choose company",
        snap_filtered.sort_values("quality_score", ascending=False)["ticker"].tolist(),
    )
    s = snap[snap["ticker"] == selected].iloc[0]
    g = df[df["ticker"] == selected].sort_values("year").copy()
    last = g.iloc[-1]
    st.markdown(
        f"""
### {s['company']} {s['ticker']}
{badge_html(s['verdict'])}

This company turns every J$1 of invested capital into approximately {pct(s['roic'])} operating return after tax estimate.
It converts {pct(s['fcf_conversion'])} of profit into free cash flow and trades at an estimated {pct(s['fcf_yield'])} FCF yield.
Expected return using FCF yield + FCF growth is approximately {pct(s['expected_return'])}, before judgment adjustments.
""",
        unsafe_allow_html=True,
    )
    a, b, c, d, e = st.columns(5)
    metrics = [
        (a, "ROIC", pct(s["roic"]), "Capital efficiency"),
        (b, "FCF Yield", pct(s["fcf_yield"]), "Cash return on price"),
        (c, "FCF Conversion", pct(s["fcf_conversion"]), "Profit \u2192 cash"),
        (d, "P/E", multiple(s["pe"]), "Valuation"),
        (e, "Debt/Equity", multiple(s["debt_to_equity"]), "Balance sheet"),
    ]
    for col, title, value, note in metrics:
        with col:
            st.markdown(
                f"""
<div class="card">
    <div class="metric-title">{title}</div>
    <div class="metric-value">{value}</div>
    <div class="metric-note">{note}</div>
</div>
""",
                unsafe_allow_html=True,
            )
    st.divider()
    x1, x2 = st.columns(2)
    with x1:
        st.plotly_chart(
            trend_chart(g, ["revenue", "operating_profit", "net_income", "free_cash_flow"], "Revenue, Profit and FCF"),
            use_container_width=True,
        )
    with x2:
        st.plotly_chart(ratio_trend(g, "ROIC and FCF Conversion"), use_container_width=True)
    x3, x4 = st.columns(2)
    with x3:
        st.plotly_chart(capital_waterfall(last), use_container_width=True)
    with x4:
        st.plotly_chart(asset_breakdown(last), use_container_width=True)

    st.subheader("Capital \u2192 Assets \u2192 Revenue \u2192 Profit \u2192 FCF \u2192 Dividends")
    driver = pd.DataFrame(
        [
            ["Capital", last["total_debt"] + last["equity"], "Debt + equity funding base"],
            ["Assets", last["total_assets"], "Where capital is deployed"],
            ["Revenue", last["revenue"], "Sales generated from assets"],
            ["Operating profit", last["operating_profit"], "Profit before financing/tax noise"],
            ["Free cash flow", last["free_cash_flow"], "Cash left after capex"],
            ["Dividends", last["dividends"], "Cash returned to shareholders"],
        ],
        columns=["Stage", "Amount", "Meaning"],
    )
    driver["Amount"] = driver["Amount"].map(money)
    st.dataframe(driver, use_container_width=True, hide_index=True)

    st.subheader("Where did the money go?")
    if len(g) >= 2:
        prev = g.iloc[-2]
        movement = pd.DataFrame(
            {
                "Item": ["Cash", "Receivables", "Inventory", "PPE", "Debt", "Payables", "Equity", "Capex", "Dividends"],
                "Prior": [
                    prev["cash"], prev["receivables"], prev["inventory"], prev["ppe"],
                    prev["total_debt"], prev["payables"], prev["equity"], prev["capex"], prev["dividends"],
                ],
                "Latest": [
                    last["cash"], last["receivables"], last["inventory"], last["ppe"],
                    last["total_debt"], last["payables"], last["equity"], last["capex"], last["dividends"],
                ],
            }
        )
        movement["Change"] = movement["Latest"] - movement["Prior"]
        movement_display = movement.copy()
        for col in ["Prior", "Latest", "Change"]:
            movement_display[col] = movement_display[col].map(money)
        st.dataframe(movement_display, use_container_width=True, hide_index=True)

with tab4:
    st.subheader("Red Flag Engine")
    all_flags = []
    for ticker, g in df.groupby("ticker"):
        company_name = g["company"].iloc[-1]
        for flag, explanation in red_flags(g):
            all_flags.append(
                {
                    "ticker": ticker,
                    "company": company_name,
                    "red_flag": flag,
                    "why_it_matters": explanation,
                }
            )
    if not all_flags:
        st.success("No major red flags detected using current rules.")
    else:
        flags_df = pd.DataFrame(all_flags)
        st.dataframe(flags_df, use_container_width=True, hide_index=True)

with tab5:
    st.subheader("Export Reports")
    export = snap_filtered.sort_values("quality_score", ascending=False).copy()
    csv = export.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download full ranking CSV",
        data=csv,
        file_name="jse_investment_ranking.csv",
        mime="text/csv",
    )
    st.caption("For production, connect this to StockAnalysis/JSE filings and save normalized financials into data/jse_financials.csv.")
