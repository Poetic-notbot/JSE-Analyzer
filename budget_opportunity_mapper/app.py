"""Budget Opportunity Mapper -- Streamlit dashboard.

Run from inside the budget_opportunity_mapper/ folder:
    streamlit run app.py

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
import os
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
                   page_icon="\U0001F1EF\U0001F1F2", layout="wide")

HERE = Path(__file__).parent
SOURCES_PATH = HERE / "config" / "sources.json"
SME_THRESHOLD = 15_000_000  # JMD '000 (illustrative SME ceiling)


# ----------------------------- state ---------------------------------
@st.cache_resource
def _seed_ledger() -> Ledger:
    return build_ledger()


if "ledger" not in st.session_state:
    # deep copy so edits in session don't mutate the cached seed
    st.session_state.ledger = Ledger.model_validate(_seed_ledger().model_dump())

ledger: Ledger = st.session_state.ledger


def load_sources() -> list[dict]:
    if SOURCES_PATH.exists():
        return json.loads(SOURCES_PATH.read_text()).get("sources", [])
    return []


# ----------------------------- helpers --------------------------------
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


def money_total(led: Ledger, **filters) -> float:
    total = 0.0
    for n in led.nodes:
        if n.node_type in (NodeType.ROOT, NodeType.PARENT, NodeType.CROSS_CUT):
            continue  # only leaves count, never cross-cut, never parents
        ok = all(getattr(n, k) == v for k, v in filters.items())
        if ok:
            total += n.amount
    return total


def to_excel_bytes(frames: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xl:
        for name, df in frames.items():
            df.to_excel(xl, sheet_name=name[:31], index=False)
    return buf.getvalue()


# ----------------------------- sidebar --------------------------------
st.sidebar.title("Budget Opportunity Mapper")
st.sidebar.caption("Jamaica Government \u00b7 web-first, PDF-fallback")

mode = st.sidebar.radio("Ingestion mode", ["Web-first (official PDFs)",
                                           "Upload PDF (fallback)"])
parent_options = [n.node_id for n in ledger.nodes
                  if n.node_type in (NodeType.ROOT, NodeType.PARENT)]

if mode.startswith("Web"):
    st.sidebar.subheader("Official sources")
    for s in load_sources():
        with st.sidebar.expander(s["title"]):
            st.write(s.get("description", ""))
            st.write(s["url"])
            if s.get("is_pdf"):
                tgt = st.selectbox("Attach extracted lines under",
                                   parent_options, key="p_" + s["id"])
                if st.button("Fetch & extract", key="f_" + s["id"]):
                    try:
                        items = ingest_web_pdf(ledger, s["url"], s["title"], tgt,
                                               max_pages=s.get("max_pages"))
                        st.success(f"Extracted {len(items)} lines to review queue")
                    except Exception as e:
                        st.error(f"Fetch failed: {e}")
else:
    up = st.sidebar.file_uploader("Upload budget PDF", type=["pdf"])
    tgt = st.sidebar.selectbox("Attach under", parent_options)
    if up and st.sidebar.button("Extract uploaded PDF"):
        items = ingest_uploaded_pdf(ledger, up.read(), up.name, tgt)
        st.sidebar.success(f"Extracted {len(items)} lines to review queue")


# ----------------------------- header KPIs ----------------------------
st.title("Jamaica Budget \u2192 Opportunity Map")

root = ledger.root()
central = ledger.by_id("central")
pb = ledger.by_id("pb_capex")

contractor_mkt = money_total(ledger, access_path=AccessPath.PROCUREMENT_CONTRACTOR)
supplier_mkt = money_total(ledger, access_path=AccessPath.PROCUREMENT_SUPPLIER)
consultant_mkt = money_total(ledger, access_path=AccessPath.PROCUREMENT_CONSULTANT)
welfare_mkt = money_total(ledger, access_path=AccessPath.CITIZEN_WELFARE)
pb_mkt = money_total(ledger, access_path=AccessPath.PUBLIC_BODY_INVESTMENT)
sme_mkt = sum(n.amount for n in ledger.nodes
              if n.node_type not in (NodeType.ROOT, NodeType.PARENT, NodeType.CROSS_CUT)
              and is_sme_accessible(n, SME_THRESHOLD))

def fmt(v):
    return f"J$ {v/1_000_000:,.1f}B" if v else "J$ 0"

c = st.columns(4)
c[0].metric("Master Opportunity Total", fmt(root.amount if root else 0))
c[1].metric("Central Government", fmt(central.amount if central else 0))
c[2].metric("Public Body Capex", fmt(pb.amount if pb else 0))
c[3].metric("Contractor-accessible", fmt(contractor_mkt))
c2 = st.columns(4)
c2[0].metric("Supplier market", fmt(supplier_mkt))
c2[1].metric("Consultant market", fmt(consultant_mkt))
c2[2].metric("SME-accessible", fmt(sme_mkt))
c2[3].metric("Citizen/Welfare", fmt(welfare_mkt))

issues = validate_all(ledger)
healthy = is_healthy(ledger)
st.markdown(
    f"### Reconciliation status: "
    + ("\u2705 HEALTHY" if healthy else "\u26A0\uFE0F ISSUES FOUND"))
if not healthy:
    for k, v in issues.items():
        for msg in v:
            st.warning(f"[{k}] {msg}")

st.divider()

# ----------------------------- charts ---------------------------------
df_all = ledger_to_df(ledger, include_cross_cut=False)  # money tree only
df_with_xc = ledger_to_df(ledger, include_cross_cut=True)

tab1, tab2, tab3, tab4 = st.tabs(
    ["Budget Tree", "Bridge & Reconciliation", "Opportunities", "Tables & Review"])

with tab1:
    a, b = st.columns(2)
    a.subheader("Sunburst")
    a.plotly_chart(visuals.sunburst_tree(df_all), use_container_width=True)
    b.subheader("Treemap")
    b.plotly_chart(visuals.treemap_tree(df_all), use_container_width=True)

with tab2:
    st.subheader("Waterfall: Central + Public Bodies \u2192 Master Total")
    st.plotly_chart(visuals.waterfall_to_master(
        central.amount if central else 0, pb.amount if pb else 0,
        root.amount if root else 0), use_container_width=True)
    st.subheader("Reconciliation: parent vs children")
    recon = reconcile(ledger)
    recon_df = pd.DataFrame([r.model_dump() for r in recon])
    st.plotly_chart(visuals.reconciliation_chart(recon_df), use_container_width=True)
    st.dataframe(recon_df, use_container_width=True)

with tab3:
    leaves = df_all[~df_all["node_type"].isin(["ROOT", "PARENT"])]
    a, b = st.columns(2)
    a.plotly_chart(visuals.opportunity_by_dimension(
        leaves, "access_path", "Opportunity by access path"), use_container_width=True)
    b.plotly_chart(visuals.opportunity_by_dimension(
        leaves.assign(ministry=leaves["ministry"].fillna("(unassigned)")),
        "ministry", "Opportunity by ministry/agency"), use_container_width=True)
    st.plotly_chart(visuals.opportunity_by_dimension(
        leaves, "funding_stream", "Opportunity by funding stream"),
        use_container_width=True)

with tab4:
    st.subheader("Search & drilldown")
    fc = st.columns(4)
    f_min = fc[0].text_input("Ministry contains")
    f_path = fc[1].selectbox("Access path", ["(all)"] + sorted(df_all["access_path"].unique()))
    f_src = fc[2].text_input("Source contains")
    f_text = fc[3].text_input("Name contains")
    view = df_all.copy()
    if f_min:
        view = view[view["ministry"].fillna("").str.contains(f_min, case=False)]
    if f_path != "(all)":
        view = view[view["access_path"] == f_path]
    if f_src:
        view = view[view["source_title"].fillna("").str.contains(f_src, case=False)]
    if f_text:
        view = view[view["name"].str.contains(f_text, case=False)]

    st.markdown("**Full ledger**")
    st.dataframe(view, use_container_width=True)

    st.markdown("**Source table**")
    st.dataframe(df_with_xc[["name", "source_title", "source_url", "source_page",
                             "method", "confidence", "quote"]],
                 use_container_width=True)

    st.markdown("**Reconciliation issues**")
    issue_rows = [{"check": k, "message": m} for k, v in issues.items() for m in v]
    st.dataframe(pd.DataFrame(issue_rows) if issue_rows
                 else pd.DataFrame([{"check": "all", "message": "No issues"}]),
                 use_container_width=True)

    st.markdown("**Extracted-but-unverified (review queue)**")
    pending = [r for r in ledger.review_queue if r.status == "pending"]
    if not pending:
        st.info("Review queue empty.")
    for r in pending:
        n = r.proposed_node
        cols = st.columns([4, 2, 2, 1, 1])
        cols[0].write(f"**{n.name}** \u2014 {n.amount:,.0f} (p.{n.source.page})")
        cols[1].write(r.reason)
        cols[2].write(n.source.title)
        if cols[3].button("Accept", key="acc_" + r.review_id):
            accept_review_item(ledger, r.review_id)
            st.rerun()
        if cols[4].button("Reject", key="rej_" + r.review_id):
            reject_review_item(ledger, r.review_id)
            st.rerun()

    st.divider()
    st.subheader("Export")
    frames = {"ledger": df_with_xc,
              "reconciliation": pd.DataFrame([x.model_dump() for x in reconcile(ledger)]),
              "issues": pd.DataFrame(issue_rows)}
    e1, e2 = st.columns(2)
    e1.download_button("Download Excel", to_excel_bytes(frames),
                       "budget_opportunity_map.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    e2.download_button("Download CSV (ledger)", df_with_xc.to_csv(index=False),
                       "ledger.csv", "text/csv")
"""Budget Opportunity Mapper -- Streamlit dashboard.

Run from inside the budget_opportunity_mapper/ folder:
    streamlit run app.py

Web-first: pull official Jamaica budget PDFs (config/sources.json) on demand;
PDF-fallback: upload your own budget PDF. All extracted figures go to a REVIEW
QUEUE first and never overwrite verified ledger values automatically.
"""
from __future__ import annotations

import io
import json
import os
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
                   page_icon="\U0001F1EF\U0001F1F2", layout="wide")

HERE = Path(__file__).parent
SOURCES_PATH = HERE / "config" / "sources.json"
SME_THRESHOLD = 15_000_000  # JMD '000 (illustrative SME ceiling)


# ----------------------------- state ---------------------------------
@st.cache_resource
def _seed_ledger() -> Ledger:
    return build_ledger()


if "ledger" not in st.session_state:
    # deep copy so edits in session don't mutate the cached seed
    st.session_state.ledger = Ledger.model_validate(_seed_ledger().model_dump())

ledger: Ledger = st.session_state.ledger


def load_sources() -> list[dict]:
    if SOURCES_PATH.exists():
        return json.loads(SOURCES_PATH.read_text()).get("sources", [])
    return []


# ----------------------------- helpers --------------------------------
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


def money_total(led: Ledger, **filters) -> float:
    total = 0.0
    for n in led.nodes:
        if n.node_type in (NodeType.ROOT, NodeType.PARENT, NodeType.CROSS_CUT):
            continue  # only leaves count, never cross-cut, never parents
        ok = all(getattr(n, k) == v for k, v in filters.items())
        if ok:
            total += n.amount
    return total


def to_excel_bytes(frames: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xl:
        for name, df in frames.items():
            df.to_excel(xl, sheet_name=name[:31], index=False)
    return buf.getvalue()


# ----------------------------- sidebar --------------------------------
st.sidebar.title("Budget Opportunity Mapper")
st.sidebar.caption("Jamaica Government \u00b7 web-first, PDF-fallback")

mode = st.sidebar.radio("Ingestion mode", ["Web-first (official PDFs)",
                                           "Upload PDF (fallback)"])
parent_options = [n.node_id for n in ledger.nodes
                  if n.node_type in (NodeType.ROOT, NodeType.PARENT)]

if mode.startswith("Web"):
    st.sidebar.subheader("Official sources")
    for s in load_sources():
        with st.sidebar.expander(s["title"]):
            st.write(s.get("description", ""))
            st.write(s["url"])
            if s.get("is_pdf"):
                tgt = st.selectbox("Attach extracted lines under",
                                   parent_options, key="p_" + s["id"])
                if st.button("Fetch & extract", key="f_" + s["id"]):
                    try:
                        items = ingest_web_pdf(ledger, s["url"], s["title"], tgt,
                                               max_pages=s.get("max_pages"))
                        st.success(f"Extracted {len(items)} lines to review queue")
                    except Exception as e:
                        st.error(f"Fetch failed: {e}")
else:
    up = st.sidebar.file_uploader("Upload budget PDF", type=["pdf"])
    tgt = st.sidebar.selectbox("Attach under", parent_options)
    if up and st.sidebar.button("Extract uploaded PDF"):
        items = ingest_uploaded_pdf(ledger, up.read(), up.name, tgt)
        st.sidebar.success(f"Extracted {len(items)} lines to review queue")


# ----------------------------- header KPIs ----------------------------
st.title("Jamaica Budget \u2192 Opportunity Map")

root = ledger.root()
central = ledger.by_id("central")
pb = ledger.by_id("pb_capex")

contractor_mkt = money_total(ledger, access_path=AccessPath.PROCUREMENT_CONTRACTOR)
supplier_mkt = money_total(ledger, access_path=AccessPath.PROCUREMENT_SUPPLIER)
consultant_mkt = money_total(ledger, access_path=AccessPath.PROCUREMENT_CONSULTANT)
welfare_mkt = money_total(ledger, access_path=AccessPath.CITIZEN_WELFARE)
pb_mkt = money_total(ledger, access_path=AccessPath.PUBLIC_BODY_INVESTMENT)
sme_mkt = sum(n.amount for n in ledger.nodes
              if n.node_type not in (NodeType.ROOT, NodeType.PARENT, NodeType.CROSS_CUT)
              and is_sme_accessible(n, SME_THRESHOLD))

def fmt(v):
    return f"J$ {v/1_000_000:,.1f}B" if v else "J$ 0"

c = st.columns(4)
c[0].metric("Master Opportunity Total", fmt(root.amount if root else 0))
c[1].metric("Central Government", fmt(central.amount if central else 0))
c[2].metric("Public Body Capex", fmt(pb.amount if pb else 0))
c[3].metric("Contractor-accessible", fmt(contractor_mkt))
c2 = st.columns(4)
c2[0].metric("Supplier market", fmt(supplier_mkt))
c2[1].metric("Consultant market", fmt(consultant_mkt))
c2[2].metric("SME-accessible", fmt(sme_mkt))
c2[3].metric("Citizen/Welfare", fmt(welfare_mkt))

issues = validate_all(ledger)
healthy = is_healthy(ledger)
st.markdown(
    f"### Reconciliation status: "
    + ("\u2705 HEALTHY" if healthy else "\u26A0\uFE0F ISSUES FOUND"))
if not healthy:
    for k, v in issues.items():
        for msg in v:
            st.warning(f"[{k}] {msg}")

st.divider()

# ----------------------------- charts ---------------------------------
df_all = ledger_to_df(ledger, include_cross_cut=False)  # money tree only
df_with_xc = ledger_to_df(ledger, include_cross_cut=True)

tab1, tab2, tab3, tab4 = st.tabs(
    ["Budget Tree", "Bridge & Reconciliation", "Opportunities", "Tables & Review"])

with tab1:
    a, b = st.columns(2)
    a.subheader("Sunburst")
    a.plotly_chart(visuals.sunburst_tree(df_all), use_container_width=True)
    b.subheader("Treemap")
    b.plotly_chart(visuals.treemap_tree(df_all), use_container_width=True)

with tab2:
    st.subheader("Waterfall: Central + Public Bodies \u2192 Master Total")
    st.plotly_chart(visuals.waterfall_to_master(
        central.amount if central else 0, pb.amount if pb else 0,
        root.amount if root else 0), use_container_width=True)
    st.subheader("Reconciliation: parent vs children")
    recon = reconcile(ledger)
    recon_df = pd.DataFrame([r.model_dump() for r in recon])
    st.plotly_chart(visuals.reconciliation_chart(recon_df), use_container_width=True)
    st.dataframe(recon_df, use_container_width=True)

with tab3:
    leaves = df_all[~df_all["node_type"].isin(["ROOT", "PARENT"])]
    a, b = st.columns(2)
    a.plotly_chart(visuals.opportunity_by_dimension(
        leaves, "access_path", "Opportunity by access path"), use_container_width=True)
    b.plotly_chart(visuals.opportunity_by_dimension(
        leaves.assign(ministry=leaves["ministry"].fillna("(unassigned)")),
        "ministry", "Opportunity by ministry/agency"), use_container_width=True)
    st.plotly_chart(visuals.opportunity_by_dimension(
        leaves, "funding_stream", "Opportunity by funding stream"),
        use_container_width=True)

with tab4:
    st.subheader("Search & drilldown")
    fc = st.columns(4)
    f_min = fc[0].text_input("Ministry contains")
    f_path = fc[1].selectbox("Access path", ["(all)"] + sorted(df_all["access_path"].unique()))
    f_src = fc[2].text_input("Source contains")
    f_text = fc[3].text_input("Name contains")
    view = df_all.copy()
    if f_min:
        view = view[view["ministry"].fillna("").str.contains(f_min, case=False)]
    if f_path != "(all)":
        view = view[view["access_path"] == f_path]
    if f_src:
        view = view[view["source_title"].fillna("").str.contains(f_src, case=False)]
    if f_text:
        view = view[view["name"].str.contains(f_text, case=False)]

    st.markdown("**Full ledger**")
    st.dataframe(view, use_container_width=True)

    st.markdown("**Source table**")
    st.dataframe(df_with_xc[["name", "source_title", "source_url", "source_page",
                             "method", "confidence", "quote"]],
                 use_container_width=True)

    st.markdown("**Reconciliation issues**")
    issue_rows = [{"check": k, "message": m} for k, v in issues.items() for m in v]
    st.dataframe(pd.DataFrame(issue_rows) if issue_rows
                 else pd.DataFrame([{"check": "all", "message": "No issues"}]),
                 use_container_width=True)

    st.markdown("**Extracted-but-unverified (review queue)**")
    pending = [r for r in ledger.review_queue if r.status == "pending"]
    if not pending:
        st.info("Review queue empty.")
    for r in pending:
        n = r.proposed_node
        cols = st.columns([4, 2, 2, 1, 1])
        cols[0].write(f"**{n.name}** \u2014 {n.amount:,.0f} (p.{n.source.page})")
        cols[1].write(r.reason)
        cols[2].write(n.source.title)
        if cols[3].button("Accept", key="acc_" + r.review_id):
            accept_review_item(ledger, r.review_id)
            st.rerun()
        if cols[4].button("Reject", key="rej_" + r.review_id):
            reject_review_item(ledger, r.review_id)
            st.rerun()

    st.divider()
    st.subheader("Export")
    frames = {"ledger": df_with_xc,
              "reconciliation": pd.DataFrame([x.model_dump() for x in reconcile(ledger)]),
              "issues": pd.DataFrame(issue_rows)}
    e1, e2 = st.columns(2)
    e1.download_button("Download Excel", to_excel_bytes(frames),
                       "budget_opportunity_map.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    e2.download_button("Download CSV (ledger)", df_with_xc.to_csv(index=False),
                       "ledger.csv", "text/csv")
