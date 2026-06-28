"""Jamaica Budget Opportunity Mapper -- Streamlit dashboard.

Web-first: pull official Jamaica budget PDFs (config/sources.json) on demand;
PDF-fallback: upload your own budget PDF. All extracted figures go to a REVIEW
QUEUE first and never overwrite verified ledger values automatically.
"""
from __future__ import annotations

# --- Streamlit Cloud path shim: make the local package importable ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from budget_mapper.classification import is_sme_accessible
from budget_mapper.ingest import (accept_review_item, ingest_uploaded_pdf,
                                   ingest_web_pdf, reject_review_item)
from budget_mapper.models import AccessPath, Ledger, NodeType
from budget_mapper.profiles import build_ledger
from budget_mapper.reconciliation import reconcile, validate_all, is_healthy
from budget_mapper import visuals

st.set_page_config(page_title="Jamaica Budget Opportunity Mapper",
                   page_icon="\U0001F1EF\U0001F1F2", layout="wide",
                   initial_sidebar_state="expanded")

HERE = Path(__file__).parent
SOURCES_PATH = HERE / "config" / "sources.json"
SME_THRESHOLD = 15_000_000  # JMD '000 (illustrative SME ceiling)

# ----------------------------- theme / CSS ----------------------------
st.markdown(
    """
    <style>
      .stApp { background: radial-gradient(1200px 600px at 20% -10%, #16331F 0%, #0E1B14 55%, #0A140E 100%); }
      #MainMenu, footer {visibility: hidden;}
      .block-container {padding-top: 1.2rem; max-width: 1300px;}
      .hero {
        border-radius: 18px; padding: 26px 30px; margin-bottom: 14px;
        background: linear-gradient(110deg, rgba(27,122,67,0.35), rgba(244,196,48,0.10));
        border: 1px solid rgba(244,196,48,0.30);
        box-shadow: 0 10px 30px rgba(0,0,0,0.35);
      }
      .hero h1 {font-size: 2.0rem; margin: 0; color: #FFFFFF; letter-spacing: -0.5px;}
      .hero p {margin: 6px 0 0; color: #BFE3CB; font-size: 0.98rem;}
      .pill {display:inline-block; padding:3px 12px; border-radius:999px;
        font-size:0.74rem; font-weight:600; letter-spacing:.4px; margin-right:8px;}
      .pill-green {background:rgba(61,187,107,0.18); color:#7BE3A0; border:1px solid rgba(61,187,107,0.4);}
      .pill-gold {background:rgba(244,196,48,0.16); color:#F4C430; border:1px solid rgba(244,196,48,0.4);}
      div[data-testid="stMetric"] {
        background: linear-gradient(160deg, rgba(19,36,26,0.95), rgba(14,27,20,0.95));
        border: 1px solid rgba(255,255,255,0.07); border-radius: 14px;
        padding: 16px 18px; box-shadow: 0 6px 18px rgba(0,0,0,0.25);
      }
      div[data-testid="stMetric"]:hover {border-color: rgba(244,196,48,0.45);}
      div[data-testid="stMetricLabel"] p {color:#9DB3A4 !important; font-size:0.80rem;
        text-transform:uppercase; letter-spacing:.5px;}
      div[data-testid="stMetricValue"] {color:#FFFFFF !important; font-weight:700;}
      .status-banner {border-radius:14px; padding:14px 20px; margin:6px 0 4px;
        font-weight:600; font-size:1.05rem;}
      .status-ok {background:rgba(61,187,107,0.14); color:#7BE3A0; border:1px solid rgba(61,187,107,0.45);}
      .status-bad {background:rgba(231,76,60,0.14); color:#FF9B8E; border:1px solid rgba(231,76,60,0.45);}
      .stTabs [data-baseweb="tab-list"] {gap: 6px;}
      .stTabs [data-baseweb="tab"] {background:rgba(255,255,255,0.04); border-radius:10px 10px 0 0;
        padding:8px 16px; color:#BFE3CB;}
      .stTabs [aria-selected="true"] {background:rgba(27,122,67,0.35); color:#FFFFFF;}
      section[data-testid="stSidebar"] {background:#0B1610; border-right:1px solid rgba(255,255,255,0.06);}
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------- state ----------------------------------
@st.cache_resource
def _seed_ledger() -> Ledger:
    return build_ledger()


if "ledger" not in st.session_state:
    st.session_state.ledger = Ledger.model_validate(_seed_ledger().model_dump())

ledger: Ledger = st.session_state.ledger


@st.cache_data
def load_sources() -> list[dict]:
    if not SOURCES_PATH.exists():
        return []
    data = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("sources", [])
    return [s for s in data if isinstance(s, dict)]


def money_total(led: Ledger, access_path: AccessPath) -> float:
    return sum(n.amount for n in led.nodes
               if n.node_type not in (NodeType.ROOT, NodeType.PARENT, NodeType.CROSS_CUT)
               and n.access_path == access_path)


def ledger_to_df(led: Ledger, include_cross_cut: bool = True) -> pd.DataFrame:
    rows = []
    for n in led.nodes:
        if not include_cross_cut and n.node_type == NodeType.CROSS_CUT:
            continue
        rows.append(dict(
            node_id=n.node_id, name=n.name, node_type=n.node_type.value,
            amount=n.amount, parent_id=n.parent_id or "",
            funding_stream=n.funding_stream.value, access_path=n.access_path.value,
            ministry=n.ministry, agency=n.agency, programme=n.programme,
            project=n.project, public_body=n.public_body, object_class=n.object_class,
            verified=n.verified, is_balance=n.is_computed_balance,
            source_title=n.source.title, source_url=n.source.url,
            source_page=n.source.page, confidence=n.source.confidence,
            method=n.source.extraction_method.value, quote=n.source.quote,
        ))
    return pd.DataFrame(rows)


def to_excel_bytes(frames: dict) -> bytes:
    buf = io.BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="xlsxwriter") as xl:
            for name, frame in frames.items():
                frame.to_excel(xl, sheet_name=name[:31], index=False)
    except Exception:
        with pd.ExcelWriter(buf) as xl:
            for name, frame in frames.items():
                frame.to_excel(xl, sheet_name=name[:31], index=False)
    return buf.getvalue()


def fmt(v) -> str:
    return f"J$ {v/1_000_000:,.1f}B" if v else "J$ 0"

# ----------------------------- sidebar --------------------------------
st.sidebar.markdown("### \U0001F1EF\U0001F1F2 Budget Opportunity Mapper")
st.sidebar.caption("Jamaica Government \u00b7 web-first, PDF-fallback")
st.sidebar.divider()

mode = st.sidebar.radio("Ingestion mode",
                        ["Web-first (official PDFs)", "Upload PDF (fallback)"])
parent_options = [n.node_id for n in ledger.nodes
                  if n.node_type in (NodeType.ROOT, NodeType.PARENT)]

if mode.startswith("Web"):
    st.sidebar.subheader("Official sources")
    for s in load_sources():
        with st.sidebar.expander(s.get("title", "source")):
            st.write(s.get("description", ""))
            if s.get("url"):
                st.write(s["url"])
            if s.get("is_pdf"):
                tgt = st.selectbox("Attach extracted lines under",
                                   parent_options, key="p_" + str(s.get("id", "")))
                if st.button("Fetch & queue", key="f_" + str(s.get("id", ""))):
                    try:
                        items = ingest_web_pdf(ledger, s["url"], s.get("title") or s["url"], tgt)
                        st.success(f"Queued {len(items)} extracted line(s) for review.")
                    except Exception as exc:
                        st.error(f"Fetch failed: {exc}")
else:
    st.sidebar.subheader("Upload a budget PDF")
    up = st.sidebar.file_uploader("PDF file", type=["pdf"])
    tgt = st.sidebar.selectbox("Attach under", parent_options)
    if up is not None and st.sidebar.button("Extract & queue"):
        try:
            items = ingest_uploaded_pdf(ledger, up.read(), up.name, tgt)
            st.sidebar.success(f"Queued {len(items)} line(s) for review.")
        except Exception as exc:
            st.sidebar.error(f"Extraction failed: {exc}")

st.sidebar.divider()
st.sidebar.metric("Pending review items", len(ledger.review_queue))
st.sidebar.caption("Extracted figures never overwrite verified values automatically.")

# ----------------------------- header ---------------------------------
root = next((n for n in ledger.nodes if n.node_type == NodeType.ROOT), None)
def _find(*ids):
    for nid in ids:
        for n in ledger.nodes:
            if n.node_id == nid:
                return n
    return None

central = _find("central")
pb = _find("pb_capex", "public_bodies", "pb")

st.markdown(
    """
    <div class="hero">
      <span class="pill pill-green">WEB-FIRST</span>
      <span class="pill pill-gold">RECONCILED LEDGER</span>
      <h1>Jamaica Budget &rarr; Opportunity Map</h1>
      <p>Every dollar mapped to a single reconciled tree &mdash; no double counting &mdash;
         showing where contractors, suppliers, consultants, SMEs and citizens access value.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ----------------------------- KPI markets ----------------------------
contractor_mkt = money_total(ledger, AccessPath.PROCUREMENT_CONTRACTOR)
supplier_mkt = money_total(ledger, AccessPath.PROCUREMENT_SUPPLIER)
consultant_mkt = money_total(ledger, AccessPath.PROCUREMENT_CONSULTANT)
welfare_mkt = money_total(ledger, AccessPath.CITIZEN_WELFARE)
pb_mkt = money_total(ledger, AccessPath.PUBLIC_BODY_INVESTMENT)
sme_mkt = sum(n.amount for n in ledger.nodes
              if n.node_type not in (NodeType.ROOT, NodeType.PARENT, NodeType.CROSS_CUT)
              and is_sme_accessible(n, SME_THRESHOLD))

c = st.columns(4)
c[0].metric("Master Opportunity Total", fmt(root.amount if root else 0))
c[1].metric("Central Government", fmt(central.amount if central else 0))
c[2].metric("Public Body Capex", fmt(pb.amount if pb else 0))
c[3].metric("Contractor-accessible", fmt(contractor_mkt))
c2 = st.columns(4)
c2[0].metric("Supplier market", fmt(supplier_mkt))
c2[1].metric("Consultant market", fmt(consultant_mkt))
c2[2].metric("SME-accessible", fmt(sme_mkt))
c2[3].metric("Citizen / Welfare", fmt(welfare_mkt))

issues = validate_all(ledger)
healthy = is_healthy(ledger)
if healthy:
    st.markdown('<div class="status-banner status-ok">\u2705 Reconciliation status: HEALTHY \u2014 all parents reconcile to children, single root, no double counting.</div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="status-banner status-bad">\u26A0\uFE0F Reconciliation status: ISSUES FOUND</div>', unsafe_allow_html=True)
    for k, v in issues.items():
        for msg in v:
            st.warning(f"[{k}] {msg}")

st.write("")

# ----------------------------- charts ---------------------------------
df_all = ledger_to_df(ledger, include_cross_cut=False)
recon = reconcile(ledger)
recon_df = pd.DataFrame([r.model_dump() for r in recon]) if recon else pd.DataFrame()

tab1, tab2, tab3, tab4 = st.tabs(
    ["\U0001F333 Budget Tree", "\U0001F501 Bridge & Reconciliation",
     "\U0001F4BC Opportunities", "\U0001F4CB Tables & Review"])

with tab1:
    st.caption("The full reconciled money tree. Sunburst shows share of the master "
               "total; treemap shows relative weight by parent.")
    a, b = st.columns(2)
    a.subheader("Sunburst")
    a.plotly_chart(visuals.sunburst_tree(df_all), use_container_width=True)
    b.subheader("Treemap")
    b.plotly_chart(visuals.treemap_tree(df_all), use_container_width=True)

with tab2:
    st.plotly_chart(visuals.waterfall_to_master(
        central.amount if central else 0, pb.amount if pb else 0,
        root.amount if root else 0), use_container_width=True)
    st.plotly_chart(visuals.reconciliation_chart(recon_df), use_container_width=True)
    if not recon_df.empty:
        st.dataframe(recon_df, use_container_width=True, hide_index=True)

with tab3:
    leaves = df_all[~df_all["node_type"].isin(["ROOT", "PARENT"])]
    a, b = st.columns(2)
    a.plotly_chart(visuals.opportunity_by_dimension(
        leaves, "access_path", "Opportunity by access path"), use_container_width=True)
    b.plotly_chart(visuals.opportunity_by_dimension(
        leaves, "ministry", "Opportunity by ministry"), use_container_width=True)
    st.plotly_chart(visuals.opportunity_by_dimension(
        leaves, "funding_stream", "Opportunity by funding stream"), use_container_width=True)

with tab4:
    st.subheader("Full ledger")
    q = st.text_input("Search (name / ministry / project / source)", "")
    view = df_all
    if q:
        mask = df_all.apply(lambda r: q.lower() in " ".join(
            str(x).lower() for x in r.values), axis=1)
        view = df_all[mask]
    st.dataframe(view, use_container_width=True, hide_index=True)

    cda, cdb = st.columns(2)
    cda.download_button("Download ledger (CSV)", view.to_csv(index=False).encode("utf-8"),
                        "ledger.csv", "text/csv", use_container_width=True)
    frames = {"ledger": df_all, "reconciliation": recon_df}
    cdb.download_button("Download workbook (Excel)", to_excel_bytes(frames),
                        "budget_opportunity_mapper.xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True)

    st.divider()
    st.subheader(f"Review queue ({len(ledger.review_queue)} pending)")
    if not ledger.review_queue:
        st.info("No extracted figures awaiting review. Use the sidebar to fetch or upload a PDF.")
    for item in list(ledger.review_queue):
        if getattr(item, "status", "pending") != "pending":
            continue
        pn = item.proposed_node
        with st.expander(f"{pn.name} \u2014 {fmt(pn.amount)} ({item.reason})"):
            st.write(f"Source: {pn.source.title}")
            if pn.source.url:
                st.write(pn.source.url)
            if pn.source.page is not None:
                st.write(f"Page {pn.source.page}")
            if pn.source.quote:
                st.caption(pn.source.quote)
            ca, cb = st.columns(2)
            if ca.button("Accept", key="acc_" + item.review_id):
                accept_review_item(ledger, item.review_id)
                st.rerun()
            if cb.button("Reject", key="rej_" + item.review_id):
                reject_review_item(ledger, item.review_id)
                st.rerun()

st.divider()
st.caption("Seed figures are clearly-labelled ILLUSTRATIVE placeholders. Replace them "
           "with verified figures via the review queue. Amounts are JMD \u2019000 "
           "unless shown as J$B.")
