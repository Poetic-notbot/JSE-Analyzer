"""Plotly visualisations for the opportunity dashboard.

All chart builders take a pandas DataFrame derived from the Ledger and return
a plotly.graph_objects.Figure. Cross-cut nodes are excluded from money charts
upstream (in app.py) so totals never inflate.
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

PALETTE = ["#0A3D62", "#1B7A43", "#E1B12C", "#C0392B", "#2980B9",
           "#8E44AD", "#16A085", "#D35400"]


def sunburst_tree(df: pd.DataFrame) -> go.Figure:
    """Hierarchical sunburst of the budget tree (id/parent/value)."""
    fig = go.Figure(go.Sunburst(
        ids=df["node_id"], labels=df["name"], parents=df["parent_id"].fillna(""),
        values=df["amount"], branchvalues="total",
        hovertemplate="<b>%{label}</b><br>%{value:,.0f} JMD'000<extra></extra>",
    ))
    fig.update_layout(margin=dict(t=20, l=0, r=0, b=0), height=560)
    return fig


def treemap_tree(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure(go.Treemap(
        ids=df["node_id"], labels=df["name"], parents=df["parent_id"].fillna(""),
        values=df["amount"], branchvalues="total",
        hovertemplate="<b>%{label}</b><br>%{value:,.0f} JMD'000<extra></extra>",
    ))
    fig.update_layout(margin=dict(t=20, l=0, r=0, b=0), height=520)
    return fig


def waterfall_to_master(central: float, public_bodies: float, master: float) -> go.Figure:
    """Central government + public-body capex bridge to the master total."""
    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=["absolute", "relative", "total"],
        x=["Central Government", "Public Body Capex", "Master Opportunity Total"],
        y=[central, public_bodies, master],
        text=[f"{v:,.0f}" for v in [central, public_bodies, master]],
        connector={"line": {"color": "#888"}},
    ))
    fig.update_layout(margin=dict(t=30, l=10, r=10, b=10), height=420,
                      yaxis_title="JMD '000")
    return fig


def reconciliation_chart(recon_df: pd.DataFrame) -> go.Figure:
    """Bar of parent vs children sum, coloured by status."""
    colours = {"OK": "#1B7A43", "WARNING": "#E1B12C", "ERROR": "#C0392B"}
    fig = go.Figure()
    fig.add_bar(name="Parent amount", x=recon_df["parent_name"], y=recon_df["parent_amount"],
                marker_color="#0A3D62")
    fig.add_bar(name="Children sum", x=recon_df["parent_name"], y=recon_df["children_sum"],
                marker_color="#2980B9")
    fig.add_scatter(name="Status", x=recon_df["parent_name"], y=recon_df["difference"],
                    mode="markers",
                    marker=dict(size=12, color=[colours.get(s, "#888") for s in recon_df["status"]]),
                    yaxis="y2")
    fig.update_layout(barmode="group", height=420, margin=dict(t=30, l=10, r=10, b=80),
                      yaxis_title="JMD '000",
                      yaxis2=dict(title="Difference", overlaying="y", side="right"),
                      xaxis_tickangle=-30)
    return fig


def opportunity_by_dimension(df: pd.DataFrame, dim: str, title: str) -> go.Figure:
    grp = df.groupby(dim, dropna=False)["amount"].sum().reset_index().sort_values("amount")
    fig = px.bar(grp, x="amount", y=dim, orientation="h", title=title,
                 color_discrete_sequence=PALETTE)
    fig.update_layout(height=460, margin=dict(t=40, l=10, r=10, b=10),
                      xaxis_title="JMD '000", yaxis_title="")
    return fig
