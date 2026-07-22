"""Shared Streamlit UI helpers (badges, headers, honesty banners, KPI cards)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ..config import settings


def provenance_badge(provenance: str) -> str:
    """Return an inline HTML pill for a provenance tag."""
    label = settings.PROVENANCE.get(provenance, provenance)
    color = settings.PROVENANCE_COLOR.get(provenance, "#555")
    return (
        f"<span style='background:{color};color:#fff;padding:2px 8px;"
        f"border-radius:10px;font-size:0.72rem;white-space:nowrap;'>{label}</span>"
    )


def provenance_legend() -> None:
    pills = " ".join(provenance_badge(p) for p in settings.PROVENANCE)
    st.markdown(pills, unsafe_allow_html=True)


def section(title: str, subtitle: str | None = None) -> None:
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)


def honesty_note(text: str) -> None:
    st.info(text, icon="ℹ️")


def observed_vs_modelled_note() -> None:
    st.warning(
        "**Observed vs modelled.** Production figures are *quarterly observed* "
        "totals. The monthly curve is a *modelled supply index* (a relative "
        "availability shape derived from crop-calendar windows) - it is not a "
        "monthly tonnage. Registry footprint is not live harvest availability.",
        icon="⚖️",
    )


def kpi_row(items: list[tuple[str, str, str | None]]) -> None:
    """Render a row of metrics. Each item = (label, value, delta|None)."""
    cols = st.columns(len(items))
    for col, (label, value, delta) in zip(cols, items):
        col.metric(label, value, delta)


def provenance_table(df: pd.DataFrame, provenance_col: str = "provenance") -> None:
    """Show a dataframe, mapping provenance codes to friendly labels."""
    if df is None or df.empty:
        st.caption("No data available.")
        return
    show = df.copy()
    if provenance_col in show.columns:
        show[provenance_col] = show[provenance_col].map(
            lambda p: settings.PROVENANCE.get(p, p))
    st.dataframe(show, use_container_width=True, hide_index=True)


def month_name(m: int) -> str:
    return settings.MONTHS[m - 1] if 1 <= m <= 12 else str(m)


def download_excel_button(data: bytes, filename: str, label: str = "Download Excel") -> None:
    st.download_button(
        label, data=data, file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
