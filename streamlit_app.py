"""Jamaica Crop Intelligence - Streamlit entry point.

Run locally:      streamlit run streamlit_app.py
Deploy (Cloud):   main file path = streamlit_app.py

Connects Jamaican crop production, seasonal availability, parish sourcing,
prices, threats, procurement planning and the full RFQ -> quotation ->
contract -> delivery lifecycle, over an official-data ingestion and
stewardship layer. Data provenance is labelled everywhere: observed vs
modelled vs illustrative vs registry footprint.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from jamaica_crop_intelligence import __version__
from jamaica_crop_intelligence.components import ui
from jamaica_crop_intelligence.config import settings
from jamaica_crop_intelligence.exports import excel_export
from jamaica_crop_intelligence.services.import_service import ImportService
from jamaica_crop_intelligence.services.price_service import PriceService
from jamaica_crop_intelligence.services.procurement_integration_service import (
    ProcurementIntegrationService,
)
from jamaica_crop_intelligence.services.procurement_service import ProcurementService
from jamaica_crop_intelligence.services.production_service import ProductionService
from jamaica_crop_intelligence.services.repository import Repository
from jamaica_crop_intelligence.services.scenario_service import ScenarioService
from jamaica_crop_intelligence.services.threat_service import ThreatService

st.set_page_config(
    page_title=settings.APP_TITLE,
    page_icon=settings.APP_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)


class Context:
    """Bundle of services sharing one repository/connection."""

    def __init__(self, repo: Repository):
        self.repo = repo
        self.production = ProductionService(repo)
        self.prices = PriceService(repo)
        self.threats = ThreatService(repo)
        self.procurement = ProcurementService(repo)
        self.integration = ProcurementIntegrationService(repo)
        self.scenario = ScenarioService(repo)
        self.imports = ImportService(repo)


@st.cache_resource(show_spinner="Initialising crop database...")
def get_context() -> Context:
    repo = Repository.create(settings.DB_PATH, seed=True)
    return Context(repo)


def crop_selector(ctx: Context, key: str, label: str = "Crop") -> tuple[int, str]:
    names = ctx.production.crop_names()
    choice = st.selectbox(label, names, key=key)
    return ctx.repo.crop_id_by_name(choice), choice


# ======================================================================= PAGES
def page_dashboard(ctx: Context) -> None:
    st.title(f"{settings.APP_ICON} {settings.APP_TITLE}")
    st.caption(settings.APP_TAGLINE)

    year = ctx.production.latest_year()
    crops = ctx.repo.table_count("crops")
    suppliers = ctx.repo.table_count("suppliers")
    threats = ctx.repo.table_count("threats")
    contracts = ctx.repo.table_count("contracts")

    ui.kpi_row([
        ("Crops tracked", str(crops), None),
        ("Suppliers", str(suppliers), None),
        ("Threats modelled", str(threats), None),
        ("Active contracts", str(ctx.repo.scalar(
            "SELECT COUNT(*) FROM contracts WHERE status='active'") or 0), None),
    ])

    alerts = ctx.threats.alerts()
    if not alerts.empty:
        top = alerts.iloc[0]
        st.info(f"**{top['title']}** - {top['body']}", icon="📣")

    left, right = st.columns(2)
    with left:
        ui.section("Top crops by observed production",
                   f"All-island quarterly totals, {year}")
        prod = ctx.production.total_production(year).head(12)
        if not prod.empty:
            fig = px.bar(prod, x="tonnes", y="crop", orientation="h",
                         color="tonnes", color_continuous_scale="Greens")
            fig.update_layout(height=420, yaxis={"categoryorder": "total ascending"},
                              coloraxis_showscale=False, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)
            st.caption("Provenance: illustrative until official files ingested.")
    with right:
        ui.section("Most price-volatile crops",
                   "Coefficient of variation of farmgate JMD/kg")
        vol = ctx.prices.most_volatile()
        if not vol.empty:
            fig = px.bar(vol, x="volatility_cv", y="crop", orientation="h",
                         color="volatility_cv", color_continuous_scale="Oranges")
            fig.update_layout(height=420, yaxis={"categoryorder": "total ascending"},
                              coloraxis_showscale=False, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)

    ui.section("This month at a glance")
    month = pd.Timestamp.today().month
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**In season now ({ui.month_name(month)})** - modelled")
        inseason = ctx.production.crops_in_season(month)
        st.dataframe(inseason, use_container_width=True, hide_index=True, height=260)
    with c2:
        st.markdown(f"**Active threats now ({ui.month_name(month)})**")
        active = ctx.threats.active_threats_for_month(month)
        st.dataframe(active, use_container_width=True, hide_index=True, height=260)

    st.divider()
    ui.provenance_legend()


def page_calendar(ctx: Context) -> None:
    ui.section("Seasonal Calendar",
               "Observed quarterly production vs modelled monthly supply index")
    ui.observed_vs_modelled_note()
    crop_id, crop_name = crop_selector(ctx, "cal_crop")

    supply = ctx.production.supply_index_df(crop_id)
    prod = ctx.production.production(crop_id)

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Modelled monthly supply index** (0-1 availability)")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=[ui.month_name(m) for m in supply["month"]],
            y=supply["supply_index"], fill="tozeroy", mode="lines+markers",
            line=dict(color="#2166ac")))
        fig.update_layout(height=340, yaxis_range=[0, 1.05],
                          margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
        st.markdown(ui.provenance_badge("modelled"), unsafe_allow_html=True)
    with col2:
        st.markdown("**Observed quarterly production** (tonnes)")
        if not prod.empty:
            prod = prod.copy()
            prod["period"] = prod["year"].astype(str) + "-Q" + prod["quarter"].astype(str)
            fig = px.bar(prod, x="period", y="tonnes", color="tonnes",
                         color_continuous_scale="Greens")
            fig.update_layout(height=340, coloraxis_showscale=False,
                              margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)
            prov = prod["provenance"].iloc[0]
            st.markdown(ui.provenance_badge(prov), unsafe_allow_html=True)
        else:
            st.caption("No production records for this crop.")

    peaks = ctx.production.peak_months(crop_id)
    st.success(f"**{crop_name}** modelled harvest peak months: "
               + ", ".join(ui.month_name(m) for m in peaks))


def page_crop_explorer(ctx: Context) -> None:
    ui.section("Crop Explorer", "Profile, aliases, seasonality, sourcing, threats")
    crop_id, crop_name = crop_selector(ctx, "ce_crop")
    crop = ctx.production.crop_by_name(crop_name)

    c1, c2, c3 = st.columns(3)
    c1.metric("Category", crop["category"])
    c2.metric("Seasonal crop", "Yes" if crop["is_seasonal"] else "No")
    c3.metric("Scientific name", crop["scientific_name"] or "-")
    aliases = ctx.production.aliases(crop_id)
    st.caption("Aliases recognised by ingestion: " + ", ".join(aliases))

    tabs = st.tabs(["Seasonality", "Prices", "Sourcing", "Threats"])
    with tabs[0]:
        supply = ctx.production.supply_index_df(crop_id)
        fig = px.line(supply, x="month", y="supply_index", markers=True)
        fig.update_layout(height=300, yaxis_range=[0, 1.05])
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Modelled monthly supply index (relative availability shape).")
    with tabs[1]:
        series = ctx.prices.price_series(crop_id)
        if not series.empty:
            fig = px.line(series, x="period", y="price_per_kg_jmd", markers=True)
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)
        stats = ctx.prices.statistics(crop_id)
        ui.kpi_row([
            ("Mean JMD/kg", f"{stats['mean']:.0f}", None),
            ("Min", f"{stats['min']:.0f}", None),
            ("Max", f"{stats['max']:.0f}", None),
            ("Volatility (CV)", f"{stats['cv']:.2f}", None),
        ])
    with tabs[2]:
        reg = ctx.production.parish_registry(crop_id)
        ui.provenance_table(reg)
        st.caption("Registry footprint = where farmers are registered; "
                   "not a live harvest volume.")
    with tabs[3]:
        thr = ctx.threats.threats_for_crop(crop_id)
        st.dataframe(thr, use_container_width=True, hide_index=True)
        exp = ctx.threats.exposure_calendar(crop_id)
        fig = px.bar(exp, x="month", y="exposure", hover_data=["top_threat"],
                     color="exposure", color_continuous_scale="Reds")
        fig.update_layout(height=280, coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Exposure = impact x seasonal intensity x in-season availability.")


def page_parish(ctx: Context) -> None:
    ui.section("Parish Sourcing", "Where crops are registered across the 14 parishes")
    st.markdown(ui.provenance_badge("official_registry"), unsafe_allow_html=True)
    st.caption("Registry footprint is not live harvest availability.")

    totals = ctx.production.parish_totals()
    fig = px.bar(totals, x="parish", y="hectares", color="crops",
                 color_continuous_scale="Greens",
                 labels={"hectares": "Registered hectares", "crops": "Distinct crops"})
    fig.update_layout(height=380, xaxis_tickangle=-40, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)

    c1, c2 = st.columns([1, 2])
    with c1:
        parish = st.selectbox("Parish", settings.PARISHES, key="par_sel")
        county = settings.PARISH_COUNTY.get(parish, "-")
        st.metric("County", county)
    with c2:
        df = ctx.repo.df(
            "SELECT c.canonical_name AS crop, r.farmer_count, r.registered_hectares "
            "FROM parish_crop_registry r JOIN crops c ON c.id=r.crop_id "
            "WHERE r.parish=? ORDER BY r.registered_hectares DESC", (parish,))
        st.dataframe(df, use_container_width=True, hide_index=True, height=320)


def page_prices(ctx: Context) -> None:
    ui.section("Prices", "Farmgate JMD/kg - illustrative until official files ingested")
    latest = ctx.prices.latest_prices()
    if not latest.empty:
        st.markdown("**Latest quarter, JMD/kg**")
        fig = px.bar(latest.head(20), x="price_jmd_per_kg", y="crop", orientation="h",
                     color="price_jmd_per_kg", color_continuous_scale="Blues")
        fig.update_layout(height=520, yaxis={"categoryorder": "total ascending"},
                          coloraxis_showscale=False, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
        prov = latest["provenance"].iloc[0]
        st.markdown(ui.provenance_badge(prov), unsafe_allow_html=True)

    st.divider()
    crop_id, crop_name = crop_selector(ctx, "price_crop")
    series = ctx.prices.price_series(crop_id)
    if not series.empty:
        fig = px.line(series, x="period", y="price_per_kg_jmd", markers=True,
                      title=f"{crop_name} farmgate price trend (JMD/kg)")
        fig.update_layout(height=320)
        st.plotly_chart(fig, use_container_width=True)
    ui.provenance_table(series[["crop", "year", "quarter", "price_per_kg_jmd",
                                "price_type", "provenance"]] if not series.empty else series)


def page_threats(ctx: Context) -> None:
    ui.section("Threats", "Climate, pest, disease and security risks by season")
    matrix = ctx.threats.seasonality_matrix()
    fig = px.imshow(matrix, aspect="auto", color_continuous_scale="Reds",
                    labels=dict(color="Intensity"))
    fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Seasonal intensity (0-1). Praedial larceny peaks toward the "
               "Christmas harvest - a real Jamaican procurement risk.")

    st.divider()
    tdf = ctx.threats.list_threats()
    tname = st.selectbox("Threat", tdf["name"].tolist(), key="threat_sel")
    trow = tdf[tdf["name"] == tname].iloc[0]
    c1, c2 = st.columns([1, 2])
    with c1:
        st.metric("Type", trow["threat_type"])
        st.metric("Severity (1-5)", int(trow["severity"]))
        st.write(trow["description"])
    with c2:
        st.markdown("**Crops affected**")
        st.dataframe(ctx.threats.crops_affected(int(trow["id"])),
                     use_container_width=True, hide_index=True, height=260)


def page_procurement_planner(ctx: Context) -> None:
    ui.section("Procurement Planner", "Demand -> required quantity -> local coverage")
    crop_id, crop_name = crop_selector(ctx, "plan_crop")
    c1, c2, c3, c4 = st.columns(4)
    demand = c1.number_input("Weekly demand (kg)", min_value=0.0, value=1000.0, step=100.0)
    buffer = c2.slider("Safety buffer %", 0, 50, int(settings.DEFAULT_BUFFER_PCT * 100)) / 100
    loss = c3.slider("Post-harvest loss %", 0, 50,
                     int(settings.DEFAULT_POST_HARVEST_LOSS * 100)) / 100
    weeks = c4.number_input("Weeks of coverage", min_value=1, value=1, step=1)

    plan = ctx.procurement.plan_requirement(demand, buffer, loss)
    cov = ctx.procurement.coverage_summary(crop_id, plan["required_kg"], int(weeks))
    ui.kpi_row([
        ("Required (kg)", f"{plan['required_kg']:.0f}", None),
        ("Local capacity (kg)", f"{cov['local_capacity_kg']:.0f}", None),
        ("Gap (kg)", f"{cov['gap_kg']:.0f}",
         "shortfall" if cov["gap_kg"] > 0 else "covered"),
        ("Coverage", f"{cov['coverage_ratio'] * 100:.0f}%", None),
    ])

    sup = ctx.procurement.supplier_coverage(crop_id, plan["required_kg"], int(weeks))
    if not sup.empty:
        st.markdown("**Suppliers ranked by capacity**")
        st.dataframe(sup[["name", "parish", "capacity_kg", "cumulative_kg",
                          "lead_time_days", "reliability", "covers_requirement"]],
                     use_container_width=True, hide_index=True)
    else:
        st.caption("No registered suppliers for this crop.")

    with st.expander("What-if: supply shock (scenario)"):
        shock = st.slider("Supply change %", -80, 50, -30, key="shock") / 100
        stress = ctx.scenario.procurement_stress(crop_id, demand, shock, buffer, loss)
        pr = ctx.scenario.price_response(crop_id, shock)
        ui.kpi_row([
            ("Baseline gap", f"{stress['baseline_gap_kg']:.0f} kg", None),
            ("Scenario gap", f"{stress['scenario_gap_kg']:.0f} kg", None),
            ("Modelled price", f"{pr['scenario_price_jmd_per_kg']:.0f} JMD/kg",
             f"{pr['price_change_pct'] * 100:+.0f}%"),
        ])
        st.caption("Price response is a modelled elasticity relationship.")

    if st.button("Save plan"):
        month = pd.Timestamp.today().month
        ctx.procurement.save_plan(f"{crop_name} plan", crop_id, month,
                                  demand, buffer, loss)
        st.success("Plan saved.")


def page_procurement_integration(ctx: Context) -> None:
    ui.section("Procurement Integration",
               "RFQ -> quotation -> contract -> delivery, with KPIs")
    tabs = st.tabs(["RFQs & Quotes", "Contracts & Deliveries", "Performance & KPIs"])

    # ---- RFQs & quotations
    with tabs[0]:
        with st.expander("Create RFQ", expanded=False):
            with st.form("rfq_form"):
                buyer = st.text_input("Buyer name", "Kingston Hotel Group")
                crop_id, crop_name = crop_selector(ctx, "rfq_crop")
                qty = st.number_input("Quantity (kg)", min_value=1.0, value=500.0)
                needed = st.date_input("Needed by")
                spec = st.text_input("Quality spec", "Grade A")
                if st.form_submit_button("Create RFQ"):
                    ctx.integration.create_rfq(buyer, crop_id, qty,
                                               str(needed), spec)
                    st.success("RFQ created.")

        rfqs = ctx.integration.rfqs()
        st.dataframe(rfqs, use_container_width=True, hide_index=True)
        if rfqs.empty:
            st.caption("Create an RFQ to begin.")
            return

        rfq_ref = st.selectbox("Select RFQ", rfqs["reference"].tolist())
        rfq_id = int(rfqs[rfqs["reference"] == rfq_ref]["id"].iloc[0])
        rfq = ctx.integration.rfq(rfq_id)

        with st.expander("Add quotation"):
            sup = ctx.procurement.suppliers()
            with st.form("quote_form"):
                sname = st.selectbox("Supplier", sup["name"].tolist())
                sid = int(sup[sup["name"] == sname]["id"].iloc[0])
                price = st.number_input("Unit price (JMD/kg)", min_value=1.0, value=250.0)
                avail = st.number_input("Available (kg)", min_value=1.0,
                                        value=float(rfq["quantity_kg"]))
                lead = st.number_input("Lead time (days)", min_value=1, value=7)
                quality = st.slider("Quality (1-5)", 1.0, 5.0, 3.5)
                if st.form_submit_button("Add quotation"):
                    ctx.integration.add_quotation(rfq_id, sid, price, avail,
                                                  int(lead), quality)
                    st.success("Quotation added.")

        quotes = ctx.integration.quotations_for_rfq(rfq_id)
        if not quotes.empty:
            st.markdown("**Quotation comparison** (weighted score, best first)")
            scored = ctx.integration.score_quotations(rfq_id)
            st.dataframe(
                scored[["rank", "supplier", "price", "lead_time", "quality",
                        "reliability", "score"]],
                use_container_width=True, hide_index=True)
            best = scored.iloc[0]
            st.caption(f"Weighting: price 45%, lead time 20%, quality 20%, "
                       f"reliability 15%. Top: **{best['supplier']}** "
                       f"(score {best['score']}).")
            if rfq["status"] == "open":
                qopts = {f"{r['supplier']} (score {r['score']})": int(r["id"])
                         for _, r in scored.iterrows()}
                pick = st.selectbox("Award contract to", list(qopts))
                award_kg = st.number_input("Contracted kg", min_value=1.0,
                                           value=float(rfq["quantity_kg"]))
                if st.button("Award contract"):
                    ctx.integration.award_contract(rfq_id, qopts[pick], award_kg)
                    st.success("Contract awarded.")
                    st.rerun()

    # ---- Contracts & deliveries
    with tabs[1]:
        contracts = ctx.integration.contracts()
        st.dataframe(contracts, use_container_width=True, hide_index=True)
        if contracts.empty:
            st.caption("Award a contract from an RFQ first.")
            return
        cref = st.selectbox("Select contract", contracts["reference"].tolist())
        cid = int(contracts[contracts["reference"] == cref]["id"].iloc[0])
        contract = ctx.integration.contract(cid)

        with st.expander("Record delivery", expanded=True):
            with st.form("delivery_form"):
                d1, d2, d3 = st.columns(3)
                ddate = d1.date_input("Delivery date")
                ordered = d2.number_input("Ordered (kg)", min_value=0.0, value=100.0)
                delivered = d3.number_input("Delivered (kg)", min_value=0.0, value=95.0)
                d4, d5, d6 = st.columns(3)
                rejected = d4.number_input("Rejected (kg)", min_value=0.0, value=5.0)
                ontime = d5.checkbox("On time", value=True)
                quality = d6.slider("Quality (1-5)", 1.0, 5.0, 4.0)
                if st.form_submit_button("Record delivery"):
                    ctx.integration.record_delivery(
                        cid, str(ddate), ordered, delivered, rejected,
                        ontime, quality)
                    st.success("Delivery recorded.")
                    st.rerun()

        st.markdown("**Deliveries**")
        st.dataframe(ctx.integration.deliveries_for_contract(cid),
                     use_container_width=True, hide_index=True)
        kpi = ctx.integration.contract_kpis(cid)
        if kpi:
            ui.kpi_row([
                ("Fill rate", f"{kpi['fill_rate'] * 100:.0f}%", None),
                ("On-time", f"{kpi['on_time_rate'] * 100:.0f}%", None),
                ("Reject rate", f"{kpi['reject_rate'] * 100:.0f}%", None),
                ("Outstanding", f"{kpi['outstanding_kg']:.0f} kg", None),
            ])

    # ---- Performance & KPIs
    with tabs[2]:
        st.markdown("**Supplier performance** (from actual deliveries)")
        perf = ctx.integration.supplier_performance()
        if perf.empty:
            st.caption("Record deliveries to populate supplier performance.")
        else:
            show = perf.copy()
            for c in ["fill_rate", "on_time_rate", "reject_rate"]:
                show[c] = (show[c] * 100).round(0).astype(int).astype(str) + "%"
            st.dataframe(show, use_container_width=True, hide_index=True)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Planned vs actual**")
            pva = ctx.integration.planned_vs_actual()
            if not pva.empty:
                fig = px.bar(pva, x="contract", y=["planned_kg", "actual_kg"],
                             barmode="group")
                fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig, use_container_width=True)
        with c2:
            st.markdown("**Shortages & rejections**")
            sr = ctx.integration.shortages_and_rejections()
            st.dataframe(sr, use_container_width=True, hide_index=True, height=260)


def page_reports(ctx: Context) -> None:
    ui.section("Reports", "Export an Excel workbook of the current data")
    st.write("Choose the sheets to include, then download.")
    options = {
        "Production (by crop)": lambda: ctx.production.total_production(),
        "Latest prices": lambda: ctx.prices.latest_prices(),
        "Parish registry": lambda: ctx.production.parish_registry(),
        "Threats": lambda: ctx.threats.list_threats(),
        "Contracts": lambda: ctx.integration.contracts(),
        "Contract KPIs": lambda: ctx.integration.all_contract_kpis(),
        "Supplier performance": lambda: ctx.integration.supplier_performance(),
    }
    chosen = st.multiselect("Sheets", list(options), default=list(options)[:4])
    if st.button("Build workbook") and chosen:
        sheets = {name: options[name]() for name in chosen}
        data = excel_export.build_workbook(sheets)
        ui.download_excel_button(data, "jamaica_crop_intelligence_report.xlsx",
                                 "Download report (.xlsx)")
        st.success(f"Workbook ready with {len(chosen)} sheet(s).")


def page_methodology(ctx: Context) -> None:
    ui.section("Data & Methodology", "Sources, provenance and honest limitations")
    st.markdown("#### Provenance legend")
    ui.provenance_legend()

    st.markdown("#### Honesty principles")
    st.markdown(
        "- Quarterly production is **never** presented as observed monthly output.\n"
        "- The monthly **supply index** is a *modelled* availability shape, not a tonnage.\n"
        "- Registry footprint is **not** live harvest availability.\n"
        "- Prices are **not fabricated** when official files fail - seeds are "
        "clearly labelled *illustrative*.\n"
        "- Official observed data is distinguished from modelled relationships "
        "and research guidance."
    )

    st.markdown("#### Verified official sources")
    src = pd.DataFrame(settings.OFFICIAL_SOURCES)
    src["provenance"] = src["provenance"].map(lambda p: settings.PROVENANCE.get(p, p))
    st.dataframe(src, use_container_width=True, hide_index=True)
    st.caption("Some hosts may be unreachable under a restricted network policy; "
               "the same files can be uploaded manually on the Administration page.")

    st.markdown("#### Modelled relationships")
    st.markdown(
        "- **Supply index**: peak=1.0 at harvest months, shoulder weight in "
        "adjacent months, small baseline elsewhere.\n"
        "- **Price response**: %ΔPrice = %ΔQuantity / elasticity (default -0.6).\n"
        "- **Threat exposure**: impact x seasonal intensity x in-season availability.\n"
        "- **Required procurement**: demand x (1+buffer) / (1-loss)."
    )
    st.caption(f"App version {__version__}. Legacy JSE stock analyzer remains at app.py.")


def page_admin(ctx: Context) -> None:
    ui.section("Administration", "Official-file ingestion & data stewardship")
    ui.honesty_note(
        "Upload an official Ministry/RADA file. The parser normalizes crop "
        "names and units, flags data-quality issues, and lets you approve or "
        "reject before anything is written. Commits are idempotent (deduped by "
        "record key).")

    kind = st.selectbox("File kind",
                        ["prices", "production", "area_reaped", "registry"])
    uploaded = st.file_uploader("Upload CSV / Excel / PDF",
                                type=["csv", "xlsx", "xls", "pdf", "txt"])

    if uploaded is not None:
        content = uploaded.getvalue()
        sfid, is_new = ctx.imports.register_source_file(
            uploaded.name, content, kind)
        if not is_new:
            st.warning("This exact file was already registered (hash match). "
                       "Re-committing is safe and idempotent.")
        preview = ctx.imports.preview(content, uploaded.name, kind)
        summary = preview["summary"]

        cols = st.columns(5)
        cols[0].metric("Rows in file", summary.get("rows_in_file", 0))
        cols[1].metric("Normalized", summary.get("rows_normalized", 0))
        cols[2].metric("Unmatched crops", summary.get("unmatched_crops", 0))
        cols[3].metric("Unknown units", summary.get("unknown_units", 0))
        cols[4].metric("Quality flags", summary.get("quality_issues", 0))

        st.markdown("**Detected columns**")
        st.json({k: v for k, v in preview["columns_detected"].items() if v})

        if preview["crop_review"]:
            st.markdown("**Crop-name review** (unmatched -> suggestion)")
            st.dataframe(pd.DataFrame(preview["crop_review"]),
                         use_container_width=True, hide_index=True)
        if preview["unit_review"]:
            st.markdown("**Unit review** (unknown units)")
            st.write(preview["unit_review"])

        if not preview["normalized"].empty:
            st.markdown("**Normalized preview**")
            st.dataframe(preview["normalized"].head(30),
                         use_container_width=True, hide_index=True)

        if preview["quality_issues"]:
            with st.expander(f"Data-quality flags ({len(preview['quality_issues'])})"):
                st.dataframe(pd.DataFrame(preview["quality_issues"]),
                             use_container_width=True, hide_index=True)

        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Approve & commit", type="primary",
                         disabled=preview["normalized"].empty):
                res = ctx.imports.commit_preview(
                    sfid, kind, preview["normalized"], preview["quality_issues"])
                st.success(f"Committed {res['rows_committed']} rows "
                           f"({res['rows_skipped']} duplicates skipped).")
        with c2:
            reason = st.text_input("Reject reason", "layout not recognised")
            if st.button("❌ Reject"):
                ctx.imports.reject_preview(sfid, kind,
                                           summary.get("rows_normalized", 0), reason)
                st.info("Import rejected. Nothing was written to fact tables.")

    st.divider()
    st.markdown("#### Source files")
    st.dataframe(ctx.imports.source_files(), use_container_width=True, hide_index=True)
    st.markdown("#### Import runs")
    st.dataframe(ctx.imports.import_runs(), use_container_width=True, hide_index=True)


# ==================================================================== ROUTER
PAGES = {
    "Executive Dashboard": page_dashboard,
    "Seasonal Calendar": page_calendar,
    "Crop Explorer": page_crop_explorer,
    "Parish Sourcing": page_parish,
    "Prices": page_prices,
    "Threats": page_threats,
    "Procurement Planner": page_procurement_planner,
    "Procurement Integration": page_procurement_integration,
    "Reports": page_reports,
    "Data & Methodology": page_methodology,
    "Administration": page_admin,
}


def main() -> None:
    ctx = get_context()
    st.sidebar.title(f"{settings.APP_ICON} {settings.APP_TITLE}")
    st.sidebar.caption("Jamaican crop production, sourcing, prices, threats "
                       "and procurement.")
    choice = st.sidebar.radio("Navigate", list(PAGES), label_visibility="collapsed")
    st.sidebar.divider()
    st.sidebar.caption(f"v{__version__} - data provenance labelled throughout.")
    st.sidebar.caption("Legacy JSE stock analyzer: run `streamlit run app.py`.")
    PAGES[choice](ctx)


if __name__ == "__main__":
    main()
