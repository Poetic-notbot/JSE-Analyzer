from __future__ import annotations

from pathlib import Path
import json
import re

import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from budget_mapper.ingest import download_pdf, extract_pdf_text, find_money_near_terms
from budget_mapper.profiles.jamaica_fy2026_27 import build_verified_seed_ledger
from budget_mapper.visuals import sunburst, treemap, reconciliation_bar

st.set_page_config(
    page_title="Jamaica Budget Opportunity Mapper",
    page_icon="🇯🇲",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------
# STYLE
# -----------------------------
st.markdown("""
<style>
.block-container {
    padding-top: 1.2rem;
    padding-bottom: 3rem;
}
[data-testid="stAppViewContainer"] {
    background:
        radial-gradient(circle at top left, rgba(0, 120, 70, 0.12), transparent 30%),
        radial-gradient(circle at top right, rgba(255, 190, 50, 0.16), transparent 28%),
        linear-gradient(180deg, #f8fafc 0%, #ffffff 50%, #f8fafc 100%);
}
.hero {
    padding: 1.4rem 1.6rem;
    border-radius: 26px;
    background: linear-gradient(135deg, #071b16 0%, #123c31 55%, #1f6f55 100%);
    color: white;
    box-shadow: 0 22px 45px rgba(15,23,42,0.22);
    margin-bottom: 1rem;
}
.hero h1 {
    font-size: 2.25rem;
    letter-spacing: -0.04em;
    margin-bottom: 0.25rem;
}
.hero p {
    color: rgba(255,255,255,0.84);
    font-size: 1rem;
    margin-bottom: 0;
}
.card {
    background: rgba(255,255,255,0.9);
    border: 1px solid rgba(15,23,42,0.08);
    border-radius: 22px;
    padding: 1.1rem 1.2rem;
    box-shadow: 0 16px 35px rgba(15,23,42,0.07);
}
.metric-title {
    color: #64748b;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
}
.metric-value {
    color: #0f172a;
    font-size: 1.85rem;
    font-weight: 850;
    letter-spacing: -0.04em;
}
.metric-note {
    color: #64748b;
    font-size: 0.88rem;
}
.badge {
    display: inline-block;
    padding: .28rem .62rem;
    border-radius: 999px;
    font-weight: 700;
    font-size: .78rem;
    margin-right: .3rem;
}
.ok { background: #dcfce7; color: #166534; }
.warn { background: #fef9c3; color: #854d0e; }
.err { background: #fee2e2; color: #991b1b; }
.info { background: #e0f2fe; color: #075985; }
.small {
    color: #64748b;
    font-size: .92rem;
}
</style>
""", unsafe_allow_html=True)

# -----------------------------
# HELPERS
# -----------------------------
def fmt_b(x):
    if pd.isna(x):
        return "—"
    return f"J${x:,.3f}B"

def pct(x):
    if pd.isna(x):
        return "—"
    return f"{x:.1%}"

def classify_access(row):
    text = " ".join([
        str(row.get("name", "")),
        str(row.get("branch", "")),
        str(row.get("opportunity_type", "")),
        str(row.get("user_access", "")),
        str(row.get("notes", "")),
    ]).lower()
    if any(w in text for w in ["road", "construction", "housing", "reconstruction", "infrastructure"]):
        return "Contractor"
    if any(w in text for w in ["supplier", "equipment", "materials", "goods"]):
        return "Supplier"
    if any(w in text for w in ["consult", "transformation", "it", "software", "rais"]):
        return "Consultant / IT"
    if any(w in text for w in ["student", "citizen", "path", "welfare"]):
        return "Citizen / Welfare"
    if "public" in text:
        return "Public Body"
    return "Unclassified"

def quality_label(row):
    if row["kind"] == "BALANCE":
        return "Computed balance"
    if row["kind"] == "CROSS_CUT":
        return "Cross-cut only"
    if row.get("confidence", 0) >= 0.9:
        return "High confidence"
    if row.get("confidence", 0) >= 0.7:
        return "Medium confidence"
    return "Needs review"

def build_root_waterfall(central, public_bodies, root):
    fig = go.Figure(go.Waterfall(
        name="Opportunity Stack",
        orientation="v",
        measure=["absolute", "relative", "total"],
        x=[
            "Central Government Opportunity Ceiling",
            "Separate Public Body Capital / Investment",
            "Total Opportunity Economy"
        ],
        y=[central, public_bodies, root],
        connector={"line": {"width": 1}},
    ))
    fig.update_layout(
        title="Central Budget + Separate Public Body Capex = Master Opportunity Stack",
        height=470,
        margin=dict(l=20, r=20, t=60, b=20),
    )
    return fig

def opportunity_bar(df):
    x = df[df["kind"] != "CROSS_CUT"].copy()
    x["access_group"] = x.apply(classify_access, axis=1)
    grouped = (
        x.groupby("access_group", as_index=False)["amount_jmd_b"]
        .sum()
        .sort_values("amount_jmd_b", ascending=False)
    )
    fig = px.bar(
        grouped,
        x="access_group",
        y="amount_jmd_b",
        text="amount_jmd_b",
        title="Opportunity by Access Path",
    )
    fig.update_traces(texttemplate="J$%{text:.1f}B", textposition="outside")
    fig.update_layout(height=470, margin=dict(l=20, r=20, t=60, b=20))
    return fig

def branch_bar(df):
    x = (
        df[df["kind"] != "CROSS_CUT"]
        .groupby("branch", as_index=False)["amount_jmd_b"]
        .sum()
        .sort_values("amount_jmd_b", ascending=False)
    )
    fig = px.bar(
        x,
        x="branch",
        y="amount_jmd_b",
        text="amount_jmd_b",
        title="Opportunity by Budget Branch",
    )
    fig.update_traces(texttemplate="J$%{text:.1f}B", textposition="outside")
    fig.update_layout(height=470, margin=dict(l=20, r=20, t=60, b=20))
    return fig

def source_confidence_chart(df):
    if "confidence" not in df.columns:
        return None
    x = df.copy()
    x["quality"] = x.apply(quality_label, axis=1)
    grouped = (
        x.groupby("quality", as_index=False)["amount_jmd_b"]
        .sum()
        .sort_values("amount_jmd_b", ascending=False)
    )
    fig = px.pie(
        grouped,
        names="quality",
        values="amount_jmd_b",
        title="Source / Verification Quality",
        hole=0.45,
    )
    fig.update_layout(height=470)
    return fig

# -----------------------------
# LOAD LEDGER
# -----------------------------
ledger = build_verified_seed_ledger()

with st.sidebar:
    st.title("🇯🇲 Budget Mapper")
    st.caption("No double-counting. Source-backed. Opportunity-focused.")
    mode = st.radio(
        "Data mode",
        ["Verified seed ledger", "Web PDF extraction", "Upload PDF fallback"],
    )
    tolerance = st.number_input(
        "Reconciliation tolerance, J$B",
        min_value=0.0,
        max_value=10.0,
        value=0.05,
        step=0.05,
    )
    st.divider()
    st.markdown("### Rules")
    st.markdown(
        """
<span class="badge ok">ROOT</span>
<span class="badge info">PARENT</span>
<span class="badge info">CHILD</span>
<span class="badge warn">BALANCE</span>
<span class="badge err">CROSS_CUT excluded</span>
""",
        unsafe_allow_html=True,
    )

# -----------------------------
# INGESTION MODES
# -----------------------------
if mode == "Web PDF extraction":
    st.info("This scans official PDFs for candidate figures. It does not overwrite the verified ledger automatically.")
    try:
        with open("config/sources.json", "r", encoding="utf-8") as f:
            sources = json.load(f)
        selected = st.selectbox(
            "Official source",
            list(sources["jamaica_fy2026_27"].keys()),
        )
        src = sources["jamaica_fy2026_27"][selected]
        terms = st.text_area(
            "Search terms",
            "Capital Expenditure, Recurrent Programmes, Compensation of Employees, Public Bodies, National Housing Trust, PATH, Students Loan Bureau, SPARK, Montego Bay Perimeter Road",
        )
        if st.button("Download and scan official PDF"):
            path = download_pdf(src["url"], Path("data/raw"))
            pages = extract_pdf_text(path, max_pages=120)
            term_list = [t.strip() for t in terms.split(",") if t.strip()]
            hits = find_money_near_terms(pages, term_list)
            st.success(f"Scanned {path.name}. Found {len(hits)} candidate snippets.")
            st.dataframe(pd.DataFrame(hits), use_container_width=True)
    except Exception as e:
        st.error(f"Web extraction failed: {e}")

elif mode == "Upload PDF fallback":
    uploaded = st.file_uploader("Upload budget PDF", type=["pdf"])
    if uploaded:
        out = Path("data/uploads") / uploaded.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(uploaded.getvalue())
        terms = st.text_area(
            "Search terms",
            "Capital Expenditure, Recurrent Programmes, Public Bodies, National Housing Trust, PATH, SPARK, procurement, road, housing",
        )
        pages = extract_pdf_text(out, max_pages=120)
        term_list = [t.strip() for t in terms.split(",") if t.strip()]
        hits = find_money_near_terms(pages, term_list)
        st.success(f"Scanned uploaded PDF. Found {len(hits)} candidate snippets.")
        st.dataframe(pd.DataFrame(hits), use_container_width=True)

# -----------------------------
# BUILD DATAFRAME
# -----------------------------
ledger.add_balance_nodes(tolerance_jmd_b=tolerance)
df = pd.DataFrame(ledger.to_dataframe_rows())
issues = ledger.validate_reconciliation(tolerance_jmd_b=tolerance)
issues_df = pd.DataFrame([i.model_dump() for i in issues])

df["access_group"] = df.apply(classify_access, axis=1)
df["verification_quality"] = df.apply(quality_label, axis=1)

root_amount = df.loc[df["name"].eq("Total Opportunity Economy"), "amount_jmd_b"].sum()
central_amount = df.loc[df["name"].eq("Central Government Opportunity Ceiling"), "amount_jmd_b"].sum()
public_body_amount = df.loc[df["name"].eq("Self-Financing Public Bodies Capital / Investment"), "amount_jmd_b"].sum()
contractor_amount = df.loc[df["access_group"].eq("Contractor"), "amount_jmd_b"].sum()
supplier_amount = df.loc[df["access_group"].eq("Supplier"), "amount_jmd_b"].sum()

error_count = int((issues_df["severity"] == "ERROR").sum()) if not issues_df.empty else 0
warning_count = int((issues_df["severity"] == "WARNING").sum()) if not issues_df.empty else 0

# -----------------------------
# HERO
# -----------------------------
st.markdown(
    """
<div class="hero">
    <h1>Jamaica Budget Opportunity Mapper</h1>
    <p>Maps the government budget into a clean opportunity stack: central budget, public body capex, contractors, suppliers, SMEs, citizens, and computed balances without double-counting.</p>
</div>
""",
    unsafe_allow_html=True,
)

# -----------------------------
# KPI CARDS
# -----------------------------
c1, c2, c3, c4, c5 = st.columns(5)
cards = [
    (c1, "Total Opportunity Economy", fmt_b(root_amount), "Central + public bodies"),
    (c2, "Central Government Ceiling", fmt_b(central_amount), "Budget-funded opportunity"),
    (c3, "Public Body Capex", fmt_b(public_body_amount), "Separate opportunity stack"),
    (c4, "Contractor Lens", fmt_b(contractor_amount), "Construction / infrastructure"),
    (c5, "Issues", f"{error_count} errors", f"{warning_count} warnings"),
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

# -----------------------------
# MAIN TABS
# -----------------------------
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Executive View",
    "Opportunity Tree",
    "Drilldown",
    "Reconciliation",
    "Sources & Review",
    "Export",
])

with tab1:
    left, right = st.columns([1.15, 1])
    with left:
        st.subheader("Budget Opportunity Stack")
        st.plotly_chart(
            build_root_waterfall(central_amount, public_body_amount, root_amount),
            use_container_width=True,
        )
    with right:
        st.subheader("What this means")
        st.markdown(
            f"""
- The current mapped opportunity economy is {fmt_b(root_amount)}.
- The central government branch is {fmt_b(central_amount)}.
- The public body capital / investment branch is {fmt_b(public_body_amount)}.
- Public body capex is shown separately so it does not get double-counted inside the central budget.
- Contractor-accessible items are currently tagged at approximately {fmt_b(contractor_amount)}.
- Computed balances are not new opportunities; they are remainders used to reconcile parents.
"""
        )
        if error_count == 0:
            st.success("No reconciliation errors detected.")
        else:
            st.error(f"{error_count} reconciliation errors detected.")

    a, b = st.columns(2)
    with a:
        st.plotly_chart(opportunity_bar(df), use_container_width=True)
    with b:
        chart = source_confidence_chart(df)
        if chart:
            st.plotly_chart(chart, use_container_width=True)

    st.subheader("Highest-Value Opportunity Nodes")
    top_nodes = (
        df[df["kind"] != "CROSS_CUT"]
        .sort_values("amount_jmd_b", ascending=False)
        .head(15)
    )
    st.dataframe(
        top_nodes[
            [
                "name",
                "amount_jmd_b",
                "kind",
                "branch",
                "access_group",
                "opportunity_type",
                "access_path",
                "verification_quality",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

with tab2:
    st.subheader("Parent-Child Opportunity Tree")
    st.caption("Cross-cut views are excluded from budget totals.")
    st.plotly_chart(sunburst(df), use_container_width=True)
    st.subheader("Treemap")
    st.plotly_chart(treemap(df), use_container_width=True)

with tab3:
    st.subheader("Opportunity Drilldown")
    col1, col2, col3 = st.columns(3)
    with col1:
        branches = sorted(df["branch"].dropna().unique())
        selected_branch = st.multiselect("Branch", branches, default=branches)
    with col2:
        access_groups = sorted(df["access_group"].dropna().unique())
        selected_access = st.multiselect("Access group", access_groups, default=access_groups)
    with col3:
        kinds = sorted(df["kind"].dropna().unique())
        selected_kinds = st.multiselect("Node kind", kinds, default=kinds)

    search = st.text_input("Search node / notes / source")

    filtered = df[
        df["branch"].isin(selected_branch)
        & df["access_group"].isin(selected_access)
        & df["kind"].isin(selected_kinds)
    ].copy()

    if search:
        s = search.lower()
        filtered = filtered[
            filtered.astype(str).apply(
                lambda row: row.str.lower().str.contains(s, regex=False).any(),
                axis=1,
            )
        ]

    st.metric("Filtered opportunity amount", fmt_b(filtered["amount_jmd_b"].sum()))
    st.dataframe(
        filtered[
            [
                "name",
                "amount_jmd_b",
                "kind",
                "branch",
                "parent",
                "access_group",
                "opportunity_type",
                "access_path",
                "source_title",
                "confidence",
                "notes",
            ]
        ].sort_values("amount_jmd_b", ascending=False),
        use_container_width=True,
        hide_index=True,
    )
    st.plotly_chart(branch_bar(filtered), use_container_width=True)

with tab4:
    st.subheader("Reconciliation Engine")
    if issues_df.empty:
        st.warning("No reconciliation checks found.")
    else:
        st.dataframe(issues_df, use_container_width=True, hide_index=True)
        st.plotly_chart(reconciliation_bar(issues_df), use_container_width=True)
    st.markdown(
        """
### Rules enforced
- ROOT must reconcile to direct children.
- PARENT nodes must reconcile to children.
- CHILD nodes belong to exactly one parent.
- BALANCE nodes are computed remainders.
- CROSS_CUT nodes are analytical views only and are excluded from totals.
"""
    )

with tab5:
    st.subheader("Sources & Review Queue")
    st.caption("This is where extracted figures should be reviewed before entering the verified ledger.")
    source_cols = [
        "name",
        "amount_jmd_b",
        "kind",
        "source_title",
        "source_url",
        "source_page",
        "quote",
        "confidence",
        "verification_quality",
    ]
    existing_cols = [c for c in source_cols if c in df.columns]
    st.dataframe(
        df[existing_cols].sort_values("confidence", ascending=True),
        use_container_width=True,
        hide_index=True,
    )
    low_conf = df[df["confidence"].fillna(0) < 0.9] if "confidence" in df.columns else pd.DataFrame()
    if not low_conf.empty:
        st.warning(f"{len(low_conf)} ledger items have confidence below 0.90 and should be reviewed.")

with tab6:
    st.subheader("Export")
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download full ledger CSV",
        data=csv,
        file_name="jamaica_budget_opportunity_ledger.csv",
        mime="text/csv",
    )
    if not issues_df.empty:
        issues_csv = issues_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download reconciliation report CSV",
            data=issues_csv,
            file_name="budget_reconciliation_report.csv",
            mime="text/csv",
        )
    st.markdown(
        """
### Next build step
Add a persistent review queue:
data/review_queue.csv
Fields:
- candidate_name
- candidate_amount_jmd_b
- source_title
- source_url
- source_page
- quote
- proposed_parent
- proposed_kind
- reviewer_status
- reviewer_notes
"""
    )
