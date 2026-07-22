"""Production, seasonality and crop-explorer read services."""

from __future__ import annotations

import pandas as pd

from ..calculations import core
from .repository import Repository


class ProductionService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # -- crops ---------------------------------------------------------------
    def list_crops(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT id, canonical_name, category, scientific_name, is_seasonal "
            "FROM crops ORDER BY canonical_name"
        )

    def crop_names(self) -> list[str]:
        return [r["canonical_name"] for r in
                self.repo.query("SELECT canonical_name FROM crops ORDER BY canonical_name")]

    def crop_by_name(self, name: str) -> dict | None:
        row = self.repo.query_one(
            "SELECT * FROM crops WHERE canonical_name = ?", (name,))
        return dict(row) if row else None

    def aliases(self, crop_id: int) -> list[str]:
        return [r["alias"] for r in self.repo.query(
            "SELECT alias FROM crop_aliases WHERE crop_id=? ORDER BY alias", (crop_id,))]

    # -- modelled supply index ----------------------------------------------
    def supply_index(self, crop_id: int) -> list[float]:
        """Return the 12-month MODELLED availability curve for a crop."""
        rows = self.repo.query(
            "SELECT month FROM crop_calendar_events "
            "WHERE crop_id=? AND event_type='peak'", (crop_id,))
        harvest = [r["month"] for r in rows]
        return core.build_supply_index(harvest)

    def supply_index_df(self, crop_id: int) -> pd.DataFrame:
        curve = self.supply_index(crop_id)
        return pd.DataFrame({
            "month": range(1, 13),
            "supply_index": curve,
            "kind": "modelled monthly supply index",
        })

    def peak_months(self, crop_id: int) -> list[int]:
        return core.peak_months(self.supply_index(crop_id))

    # -- observed quarterly production --------------------------------------
    def production(self, crop_id: int) -> pd.DataFrame:
        """Observed/illustrative quarterly production for a crop.

        Once official rows exist for a crop, illustrative seeds for that crop are
        suppressed so real data supersedes placeholders (no double counting).
        """
        return self.repo.df(
            "SELECT year, quarter, tonnes, provenance FROM production_records p "
            "WHERE crop_id=? AND parish IS NULL "
            "AND NOT (provenance='illustrative' AND EXISTS ("
            "  SELECT 1 FROM production_records o WHERE o.crop_id=p.crop_id "
            "  AND o.provenance LIKE 'official%')) "
            "ORDER BY year, quarter",
            (crop_id,))

    def latest_year(self) -> int | None:
        return self.repo.scalar("SELECT MAX(year) FROM production_records")

    def total_production(self, year: int | None = None) -> pd.DataFrame:
        sql = ("SELECT c.canonical_name AS crop, SUM(p.tonnes) AS tonnes, "
               "MIN(p.provenance) AS provenance FROM production_records p "
               "JOIN crops c ON c.id=p.crop_id WHERE p.parish IS NULL "
               "AND NOT (p.provenance='illustrative' AND EXISTS ("
               "  SELECT 1 FROM production_records o WHERE o.crop_id=p.crop_id "
               "  AND o.provenance LIKE 'official%'))")
        params: list = []
        if year is not None:
            sql += " AND p.year=?"
            params.append(year)
        sql += " GROUP BY c.canonical_name ORDER BY tonnes DESC"
        return self.repo.df(sql, params)

    # -- parish sourcing -----------------------------------------------------
    def parish_registry(self, crop_id: int | None = None) -> pd.DataFrame:
        sql = ("SELECT r.parish, c.canonical_name AS crop, r.farmer_count, "
               "r.registered_hectares, r.provenance FROM parish_crop_registry r "
               "JOIN crops c ON c.id=r.crop_id")
        params: list = []
        if crop_id is not None:
            sql += " WHERE r.crop_id=?"
            params.append(crop_id)
        sql += " ORDER BY r.registered_hectares DESC"
        return self.repo.df(sql, params)

    def parish_totals(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT parish, COUNT(DISTINCT crop_id) AS crops, "
            "SUM(farmer_count) AS farmers, SUM(registered_hectares) AS hectares "
            "FROM parish_crop_registry GROUP BY parish ORDER BY hectares DESC")

    def crops_in_season(self, month: int, threshold: float = 0.5) -> pd.DataFrame:
        """Crops whose modelled availability in ``month`` is >= threshold."""
        rows = []
        for c in self.repo.query("SELECT id, canonical_name FROM crops"):
            curve = self.supply_index(c["id"])
            avail = core.seasonality_availability(curve, month)
            if avail >= threshold:
                rows.append({"crop": c["canonical_name"],
                             "availability": round(avail, 3)})
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("availability", ascending=False).reset_index(drop=True)
        return df
