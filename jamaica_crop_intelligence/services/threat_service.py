"""Threat, seasonality and alert service."""

from __future__ import annotations

import pandas as pd

from ..calculations import core
from .repository import Repository
from .production_service import ProductionService


class ThreatService:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.production = ProductionService(repo)

    def list_threats(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT id, name, threat_type, severity, description "
            "FROM threats ORDER BY severity DESC, name")

    def seasonality(self, threat_id: int) -> list[float]:
        rows = self.repo.query(
            "SELECT month, intensity FROM threat_seasonality "
            "WHERE threat_id=? ORDER BY month", (threat_id,))
        curve = [0.0] * 12
        for r in rows:
            curve[r["month"] - 1] = r["intensity"]
        return curve

    def seasonality_matrix(self) -> pd.DataFrame:
        """Threats x months intensity matrix for a heatmap."""
        threats = self.repo.query("SELECT id, name FROM threats ORDER BY name")
        data = {}
        for t in threats:
            data[t["name"]] = self.seasonality(t["id"])
        df = pd.DataFrame(data, index=[f"M{m}" for m in range(1, 13)]).T
        df.columns = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return df

    def crops_affected(self, threat_id: int) -> pd.DataFrame:
        return self.repo.df(
            "SELECT c.canonical_name AS crop, l.impact FROM crop_threat_links l "
            "JOIN crops c ON c.id=l.crop_id WHERE l.threat_id=? "
            "ORDER BY l.impact DESC", (threat_id,))

    def threats_for_crop(self, crop_id: int) -> pd.DataFrame:
        return self.repo.df(
            "SELECT t.id AS threat_id, t.name, t.threat_type, t.severity, l.impact "
            "FROM crop_threat_links l JOIN threats t ON t.id=l.threat_id "
            "WHERE l.crop_id=? ORDER BY l.impact DESC", (crop_id,))

    def exposure_calendar(self, crop_id: int) -> pd.DataFrame:
        """Per-month exposure score combining crop-threat impact, threat
        seasonal intensity, and the crop's modelled availability.
        """
        supply = self.production.supply_index(crop_id)
        threats = self.threats_for_crop(crop_id)
        rows = []
        for month in range(1, 13):
            total = 0.0
            drivers = []
            for _, t in threats.iterrows():
                intensity = self.seasonality(int(t["threat_id"]))[month - 1]
                score = core.threat_exposure_score(
                    t["impact"], intensity, supply[month - 1])
                total += score
                if score > 0:
                    drivers.append((t["name"], score))
            drivers.sort(key=lambda x: x[1], reverse=True)
            rows.append({
                "month": month,
                "exposure": round(total, 1),
                "top_threat": drivers[0][0] if drivers else "-",
            })
        return pd.DataFrame(rows)

    def active_threats_for_month(self, month: int, min_intensity: float = 0.5) -> pd.DataFrame:
        threats = self.repo.query("SELECT id, name, severity FROM threats")
        rows = []
        for t in threats:
            intensity = self.seasonality(t["id"])[month - 1]
            if intensity >= min_intensity:
                rows.append({"threat": t["name"], "severity": t["severity"],
                             "intensity": round(intensity, 2)})
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(["intensity", "severity"], ascending=False).reset_index(drop=True)
        return df

    def alerts(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT level, title, body, created_at FROM alerts "
            "ORDER BY created_at DESC")
