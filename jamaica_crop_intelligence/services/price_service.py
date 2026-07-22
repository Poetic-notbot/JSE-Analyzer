"""Price read/aggregation service."""

from __future__ import annotations

import pandas as pd

from ..calculations import core
from .repository import Repository


class PriceService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def prices(self, crop_id: int | None = None) -> pd.DataFrame:
        sql = ("SELECT c.canonical_name AS crop, p.year, p.quarter, "
               "p.price_jmd, p.price_per_kg_jmd, p.price_type, p.provenance, "
               "m.name AS market FROM price_records p "
               "JOIN crops c ON c.id=p.crop_id "
               "LEFT JOIN markets m ON m.id=p.market_id")
        params: list = []
        if crop_id is not None:
            sql += " WHERE p.crop_id=?"
            params.append(crop_id)
        sql += " ORDER BY p.year, p.quarter"
        return self.repo.df(sql, params)

    def price_series(self, crop_id: int) -> pd.DataFrame:
        df = self.prices(crop_id)
        if df.empty:
            return df
        df["period"] = df["year"].astype(str) + "-Q" + df["quarter"].astype(str)
        return df

    def statistics(self, crop_id: int) -> dict:
        df = self.prices(crop_id)
        vals = df["price_per_kg_jmd"].dropna().tolist() if not df.empty else []
        return core.price_statistics(vals)

    def latest_prices(self) -> pd.DataFrame:
        """Most recent quarter's price per crop (JMD/kg)."""
        return self.repo.df(
            "SELECT c.canonical_name AS crop, p.price_per_kg_jmd AS price_jmd_per_kg, "
            "p.year, p.quarter, p.provenance FROM price_records p "
            "JOIN crops c ON c.id=p.crop_id "
            "WHERE (p.year, p.quarter) = "
            "  (SELECT year, quarter FROM price_records "
            "   ORDER BY year DESC, quarter DESC LIMIT 1) "
            "ORDER BY p.price_per_kg_jmd DESC")

    def most_volatile(self, top: int = 8) -> pd.DataFrame:
        rows = []
        for c in self.repo.query("SELECT id, canonical_name FROM crops"):
            stats = self.statistics(c["id"])
            if stats["count"]:
                rows.append({"crop": c["canonical_name"],
                             "mean_jmd_per_kg": round(stats["mean"], 1),
                             "volatility_cv": round(stats["cv"], 3)})
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("volatility_cv", ascending=False).head(top).reset_index(drop=True)
        return df
