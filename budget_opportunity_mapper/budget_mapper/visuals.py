"""Plotly visuals for the Budget Opportunity Mapper.

Investor-grade styling: cohesive Jamaica-themed palette (green / gold / charcoal),
transparent backgrounds so charts sit cleanly on the dark dashboard theme,
J$ currency formatting, and clear hover/labels. Amounts are JMD '000.
"""
from __future__ import annotations

import plotly.graph_objects as go
import pandas as pd

GREEN = "#1B7A43"
GREEN_LT = "#3DBB6B"
GOLD = "#F4C430"
CHARCOAL = "#0E1B14"
INK = "#E8F0EA"
MUTED = "#9DB3A4"
GRID = "rgba(255,255,255,0.08)"

PALETTE = ["#1B7A43", "#F4C430", "#3DBB6B", "#2980B9", "#E67E22",
           "#8E44AD", "#16A085", "#C0392B", "#5D6D7E", "#D4AC0D"]

STATUS_COLORS = {"OK": GREEN_LT, "WARNING": GOLD, "ERROR": "#E74C3C"}

FONT = dict(family="Inter, Segoe UI, system-ui, sans-serif", color=INK, size=13)


def _base_layout(fig, height, title=""):
    fig.update_layout(
        height=height,
        title=dict(text=title, font=dict(size=16, color=INK)) if title else None,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=FONT,
        margin=dict(t=46 if title else 16, l=8, r=8, b=8),
        hoverlabel=dict(bgcolor=CHARCOAL, bordercolor=GREEN, font=dict(color=INK)),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=INK)),
    )
    return fig


def _money(v):
    return f"J${v/1_000_000:,.2f}B"


def sunburst_tree(df):
    fig = go.Figure(go.Sunburst(
        ids=df["node_id"], labels=df["name"], parents=df["parent_id"],
        values=df["amount"], branchvalues="total",
        insidetextorientation="radial",
        marker=dict(colors=df["amount"],
                    colorscale=[[0, "#13241A"], [0.5, GREEN], [1, GREEN_LT]],
                    line=dict(color=CHARCOAL, width=1.4)),
        hovertemplate="<b>%{label}</b><br>%{value:,.0f} JMD'000"
                      "<br>%{percentRoot:.1%} of master<extra></extra>",
    ))
    return _base_layout(fig, 560)


def treemap_tree(df):
    fig = go.Figure(go.Treemap(
        ids=df["node_id"], labels=df["name"], parents=df["parent_id"],
        values=df["amount"], branchvalues="total", tiling=dict(pad=3),
        marker=dict(colors=df["amount"],
                    colorscale=[[0, "#13241A"], [0.5, GREEN], [1, GOLD]],
                    line=dict(color=CHARCOAL, width=2)),
        textfont=dict(color=INK, size=13),
        hovertemplate="<b>%{label}</b><br>%{value:,.0f} JMD'000"
                      "<br>%{percentParent:.1%} of parent<extra></extra>",
    ))
    return _base_layout(fig, 520)


def waterfall_to_master(central, public_bodies, master):
    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=["relative", "relative", "total"],
        x=["Central Government", "Public Body Capex", "Master Opportunity Total"],
        y=[central, public_bodies, master],
        text=[_money(central), _money(public_bodies), _money(master)],
        textposition="outside", textfont=dict(color=INK, size=13),
        connector={"line": {"color": MUTED, "width": 1}},
        increasing={"marker": {"color": GREEN}},
        totals={"marker": {"color": GOLD}},
        hovertemplate="<b>%{x}</b><br>%{y:,.0f} JMD'000<extra></extra>",
    ))
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID, tickformat=",.0f")
    fig.update_xaxes(showgrid=False)
    return _base_layout(fig, 440, "How central + public bodies build the master total")


def reconciliation_chart(recon_df):
    if recon_df.empty:
        return _base_layout(go.Figure(), 440, "Reconciliation")
    fig = go.Figure()
    fig.add_bar(name="Parent amount", x=recon_df["parent_name"],
                y=recon_df["parent_amount"], marker_color=GREEN,
                hovertemplate="<b>%{x}</b><br>parent: %{y:,.0f}<extra></extra>")
    fig.add_bar(name="Children sum", x=recon_df["parent_name"],
                y=recon_df["children_sum"], marker_color=GREEN_LT,
                hovertemplate="<b>%{x}</b><br>children: %{y:,.0f}<extra></extra>")
    fig.add_trace(go.Scatter(
        name="Status", x=recon_df["parent_name"], y=recon_df["parent_amount"],
        mode="markers",
        marker=dict(size=14, line=dict(color=CHARCOAL, width=1),
                    color=[STATUS_COLORS.get(s, MUTED) for s in recon_df["status"]]),
        hovertemplate="<b>%{x}</b><br>status: %{text}<extra></extra>",
        text=recon_df["status"]))
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID, tickformat=",.0f")
    fig.update_xaxes(showgrid=False, tickangle=-25)
    fig.update_layout(barmode="group")
    return _base_layout(fig, 440, "Parent vs children reconciliation")


def opportunity_by_dimension(df, dim, title):
    if df.empty or dim not in df.columns:
        return _base_layout(go.Figure(), 460, title)
    grp = (df.groupby(dim, dropna=False)["amount"].sum()
             .sort_values(ascending=True).reset_index())
    grp[dim] = grp[dim].fillna("(unclassified)").astype(str)
    fig = go.Figure(go.Bar(
        x=grp["amount"], y=grp[dim], orientation="h",
        marker=dict(color=grp["amount"],
                    colorscale=[[0, GREEN], [1, GOLD]],
                    line=dict(color=CHARCOAL, width=0.5)),
        text=[_money(v) for v in grp["amount"]], textposition="auto",
        hovertemplate="<b>%{y}</b><br>%{x:,.0f} JMD'000<extra></extra>"))
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, tickformat=",.0f")
    fig.update_yaxes(showgrid=False)
    return _base_layout(fig, 460, title)
