"""Price read/aggregation service."""

from __future__ import annotations

import pandas as pd

from ..calculations import core
from .repository import Repository


class PriceService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # Suppress illustrative seeds for any crop that has official prices, so real
    # ingested data supersedes placeholders instead of mixing with them.
    _SUPERSEDE = (" AND NOT (p.provenance='illustrative' AND EXISTS ("
                  "  SELECT 1 FROM price_records o WHERE o.crop_id=p.crop_id "
                  "  AND o.provenance LIKE 'official%'))")

    def prices(self, crop_id: int | None = None) -> pd.DataFrame:
        sql = ("SELECT c.canonical_name AS crop, p.year, p.quarter, "
               "p.price_jmd, p.price_per_kg_jmd, p.price_type, p.provenance, "
               "m.name AS market FROM price_records p "
               "JOIN crops c ON c.id=p.crop_id "
               "LEFT JOIN markets m ON m.id=p.market_id WHERE 1=1" + self._SUPERSEDE)
        params: list = []
        if crop_id is not None:
            sql += " AND p.crop_id=?"
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
            "   ORDER BY year DESC, quarter DESC LIMIT 1)" + self._SUPERSEDE +
            " ORDER BY p.price_per_kg_jmd DESC")

    # -- filters / dynamic dimensions ---------------------------------------
    def distinct_price_types(self, crop_id: int | None = None) -> list[str]:
        sql = "SELECT DISTINCT price_type FROM price_records WHERE price_type IS NOT NULL"
        params: list = []
        if crop_id is not None:
            sql += " AND crop_id=?"
            params.append(crop_id)
        return [r["price_type"] for r in self.repo.query(sql + " ORDER BY price_type", params)]

    def distinct_markets(self, crop_id: int | None = None) -> list[str]:
        sql = ("SELECT DISTINCT market_location FROM price_records "
               "WHERE market_location IS NOT NULL AND market_location != ''")
        params: list = []
        if crop_id is not None:
            sql += " AND crop_id=?"
            params.append(crop_id)
        return [r["market_location"] for r in self.repo.query(sql + " ORDER BY market_location", params)]

    def price_frame(self, crop_id: int, price_type: str | None = None,
                    market: str | None = None) -> pd.DataFrame:
        """Normalized price rows with a sortable period, robust to seed
        (quarterly, no date) and ingested weekly (dated) data alike.
        """
        sql = ("SELECT year, quarter, month, price_date, week_ending, "
               "price_type, market_location, price_per_kg_jmd, price_jmd, "
               "provenance FROM price_records p WHERE p.crop_id=?" + self._SUPERSEDE)
        params: list = [crop_id]
        if price_type:
            sql += " AND price_type=?"
            params.append(price_type)
        if market:
            sql += " AND market_location=?"
            params.append(market)
        df = self.repo.df(sql, params)
        if df.empty:
            return df
        # sort key: prefer explicit date, else year+quarter
        df["sort_key"] = df.apply(
            lambda r: (r["price_date"] or f"{int(r['year']):04d}-Q{int(r['quarter'] or 0)}"),
            axis=1)
        df["period_label"] = df.apply(
            lambda r: (r["price_date"] or r["week_ending"]
                       or f"{int(r['year'])}-Q{int(r['quarter'] or 0)}"),
            axis=1)
        return df.sort_values("sort_key").reset_index(drop=True)

    def spread_frame(self, crop_id: int) -> pd.DataFrame:
        """Period x price_type mean price, with farmgate->retail spread."""
        df = self.price_frame(crop_id)
        if df.empty:
            return df
        pivot = df.pivot_table(index="period_label", columns="price_type",
                               values="price_per_kg_jmd", aggfunc="mean")
        pivot = pivot.reset_index()
        rank = {"farmgate": 0, "wholesale": 1, "urban_retail": 2,
                "rural_retail": 2, "retail": 3}
        cheapest = min((c for c in pivot.columns if c in rank),
                       key=lambda c: rank[c], default=None)
        dearest = max((c for c in pivot.columns if c in rank),
                      key=lambda c: rank[c], default=None)
        if cheapest and dearest and cheapest != dearest:
            pivot["spread_jmd_per_kg"] = pivot[dearest] - pivot[cheapest]
            with_base = pivot[cheapest].replace(0, pd.NA)
            pivot["spread_pct"] = (pivot["spread_jmd_per_kg"] / with_base * 100).round(1)
        return pivot

    def change_metrics(self, crop_id: int, price_type: str | None = None) -> dict:
        """Latest price plus period-on-period change (week/quarter-on-prior)."""
        df = self.price_frame(crop_id, price_type=price_type)
        if df.empty or len(df) < 1:
            return {}
        series = df.groupby("sort_key")["price_per_kg_jmd"].mean().reset_index()
        latest = float(series.iloc[-1]["price_per_kg_jmd"])
        out = {"latest_jmd_per_kg": round(latest, 1),
               "latest_period": df.iloc[-1]["period_label"]}
        if len(series) >= 2:
            prev = float(series.iloc[-2]["price_per_kg_jmd"])
            out["prev_jmd_per_kg"] = round(prev, 1)
            out["change_pct"] = round((latest - prev) / prev * 100, 1) if prev else 0.0
        return out

    def last_refresh(self) -> dict:
        """Most recent price observation + its source label and provenance."""
        row = self.repo.query_one(
            "SELECT p.price_date, p.year, p.quarter, p.provenance, "
            "sf.source_name, sf.origin_url, p.created_at "
            "FROM price_records p LEFT JOIN source_files sf ON sf.id=p.source_file_id "
            "ORDER BY COALESCE(p.price_date, '') DESC, p.year DESC, p.quarter DESC, "
            "p.created_at DESC LIMIT 1")
        if not row:
            return {}
        d = dict(row)
        d["as_of"] = d.get("price_date") or f"{d.get('year')}-Q{d.get('quarter')}"
        return d

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
