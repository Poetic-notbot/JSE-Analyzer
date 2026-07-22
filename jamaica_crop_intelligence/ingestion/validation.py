"""Row-level data-quality validation producing data_quality_flags entries.

Pure functions over a normalized DataFrame. Each returns a list of flag dicts
shaped for ImportService.log_flag: {severity, category, detail, raw_value}.
"""

from __future__ import annotations

import pandas as pd

from ..calculations import core


def validate_price_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Validate a normalized price preview. Returns (clean_df, flags).

    Rules:
      * negative or zero prices are dropped and flagged (error)
      * per-crop price_per_kg outliers are flagged (warning), not dropped
      * suspected unit mismatch (per-kg wildly above per-unit) flagged (warning)
      * missing year flagged (warning)
    """
    flags: list[dict] = []
    if df is None or df.empty:
        return df, flags

    df = df.copy()
    price_col = "price_per_kg_jmd" if "price_per_kg_jmd" in df.columns else "price_jmd"

    # Negative / zero prices -> drop + flag.
    bad_mask = df[price_col].isna() | (df[price_col] <= 0)
    for _, r in df[bad_mask].iterrows():
        flags.append({"severity": "error", "category": "range",
                      "detail": f"Non-positive price dropped for "
                                f"{r.get('crop_raw', r.get('crop_id'))}",
                      "raw_value": str(r.get(price_col))})
    df = df[~bad_mask].reset_index(drop=True)
    if df.empty:
        return df, flags

    # Per-crop outliers (IQR).
    for cid, grp in df.groupby("crop_id"):
        idxs = core.detect_outliers_iqr(grp[price_col].tolist())
        for local_i in idxs:
            row = grp.iloc[local_i]
            flags.append({"severity": "warning", "category": "outlier",
                          "detail": f"Price outlier for "
                                    f"{row.get('crop_raw', cid)}: {row[price_col]:.1f}",
                          "raw_value": str(row[price_col])})

    # Unit-mismatch heuristic: normalized /kg far above raw suggests the raw
    # unit (e.g. per dozen / per bunch) was not mass-convertible correctly.
    if "price_jmd" in df.columns and "price_per_kg_jmd" in df.columns:
        ratio = df["price_per_kg_jmd"] / df["price_jmd"].replace(0, pd.NA)
        suspect = ratio[(ratio > 5) | (ratio < 0.2)].dropna()
        for i in suspect.index:
            r = df.loc[i]
            flags.append({"severity": "warning", "category": "unit",
                          "detail": f"Possible unit mismatch for "
                                    f"{r.get('crop_raw')} (per-kg/raw ratio "
                                    f"{ratio[i]:.2f})",
                          "raw_value": str(r.get("price_jmd"))})

    if "year" in df.columns:
        miss = df["year"].isna().sum()
        if miss:
            flags.append({"severity": "warning", "category": "range",
                          "detail": f"{int(miss)} row(s) missing year",
                          "raw_value": ""})
    return df, flags


def validate_quantity_rows(df: pd.DataFrame, value_col: str) -> tuple[pd.DataFrame, list[dict]]:
    """Validate production/area rows: drop non-positive values with an error flag."""
    flags: list[dict] = []
    if df is None or df.empty or value_col not in df.columns:
        return df, flags
    df = df.copy()
    bad = df[value_col].isna() | (df[value_col] < 0)
    for _, r in df[bad].iterrows():
        flags.append({"severity": "error", "category": "range",
                      "detail": f"Negative/missing {value_col} dropped for "
                                f"{r.get('crop_raw', r.get('crop_id'))}",
                      "raw_value": str(r.get(value_col))})
    df = df[~bad].reset_index(drop=True)
    return df, flags
