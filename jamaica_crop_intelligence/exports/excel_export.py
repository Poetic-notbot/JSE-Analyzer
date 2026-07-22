"""Excel workbook export for reports (uses xlsxwriter via pandas)."""

from __future__ import annotations

import io

import pandas as pd


def build_workbook(sheets: dict[str, pd.DataFrame],
                   title: str = "Jamaica Crop Intelligence Report") -> bytes:
    """Write a multi-sheet .xlsx to bytes.

    ``sheets`` maps sheet name -> DataFrame. A cover sheet with the title and
    an honesty note is written first. Sheet names are truncated to Excel's
    31-char limit and de-duplicated.
    """
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        book = writer.book
        cover = book.add_worksheet("About")
        bold = book.add_format({"bold": True, "font_size": 14})
        wrap = book.add_format({"text_wrap": True, "valign": "top"})
        cover.set_column(0, 0, 90)
        cover.write(0, 0, title, bold)
        cover.write(2, 0,
                    "Provenance note: values are labelled by provenance "
                    "(official observed, official registry, modelled, "
                    "illustrative). Illustrative seed values are placeholders "
                    "until official Ministry/RADA files are ingested. Quarterly "
                    "production is never monthly output; the monthly supply "
                    "index is a modelled availability shape, not a tonnage.",
                    wrap)

        used: set[str] = {"About"}
        for name, df in sheets.items():
            sheet = _safe_sheet_name(name, used)
            used.add(sheet)
            if df is None or df.empty:
                pd.DataFrame({"note": ["No data"]}).to_excel(
                    writer, sheet_name=sheet, index=False)
                continue
            df.to_excel(writer, sheet_name=sheet, index=False)
            ws = writer.sheets[sheet]
            for i, col in enumerate(df.columns):
                width = max(12, min(40, int(df[col].astype(str).map(len).max()) + 2
                                    if len(df) else 12, 40))
                ws.set_column(i, i, max(width, len(str(col)) + 2))
    buffer.seek(0)
    return buffer.getvalue()


def _safe_sheet_name(name: str, used: set[str]) -> str:
    base = "".join(c for c in str(name) if c not in "[]:*?/\\")[:31] or "Sheet"
    candidate = base
    i = 1
    while candidate in used:
        suffix = f"_{i}"
        candidate = base[:31 - len(suffix)] + suffix
        i += 1
    return candidate
