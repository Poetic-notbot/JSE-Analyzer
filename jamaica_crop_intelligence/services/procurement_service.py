"""Procurement planning service (demand -> required quantity -> supplier match)."""

from __future__ import annotations

import pandas as pd

from ..calculations import core
from ..config import settings
from .repository import Repository


class ProcurementService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # -- suppliers -----------------------------------------------------------
    def suppliers(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT id, name, parish, supplier_type, reliability "
            "FROM suppliers ORDER BY name")

    def suppliers_for_crop(self, crop_id: int) -> pd.DataFrame:
        return self.repo.df(
            "SELECT s.id AS supplier_id, s.name, s.parish, s.reliability, "
            "cap.typical_kg_per_week, cap.lead_time_days "
            "FROM supplier_crop_capabilities cap "
            "JOIN suppliers s ON s.id=cap.supplier_id "
            "WHERE cap.crop_id=? ORDER BY cap.typical_kg_per_week DESC",
            (crop_id,))

    # -- planning ------------------------------------------------------------
    def plan_requirement(self, demand_kg: float, buffer_pct: float | None = None,
                         loss_pct: float | None = None) -> dict:
        buffer_pct = settings.DEFAULT_BUFFER_PCT if buffer_pct is None else buffer_pct
        loss_pct = settings.DEFAULT_POST_HARVEST_LOSS if loss_pct is None else loss_pct
        required = core.required_procurement_kg(
            demand_kg, buffer_pct=buffer_pct, loss_pct=loss_pct)
        return {
            "demand_kg": demand_kg,
            "buffer_pct": buffer_pct,
            "loss_pct": loss_pct,
            "required_kg": round(required, 1),
        }

    def supplier_coverage(self, crop_id: int, required_kg: float,
                          weeks: int = 1) -> pd.DataFrame:
        """How much of ``required_kg`` local suppliers could cover over ``weeks``."""
        sup = self.suppliers_for_crop(crop_id)
        if sup.empty:
            return sup
        sup = sup.copy()
        sup["capacity_kg"] = sup["typical_kg_per_week"].fillna(0) * weeks
        sup = sup.sort_values("capacity_kg", ascending=False).reset_index(drop=True)
        sup["cumulative_kg"] = sup["capacity_kg"].cumsum()
        sup["covers_requirement"] = sup["cumulative_kg"] >= required_kg
        return sup

    def coverage_summary(self, crop_id: int, required_kg: float,
                         weeks: int = 1) -> dict:
        sup = self.supplier_coverage(crop_id, required_kg, weeks)
        total_capacity = float(sup["capacity_kg"].sum()) if not sup.empty else 0.0
        gap = core.supply_gap_kg(required_kg, total_capacity)
        return {
            "required_kg": round(required_kg, 1),
            "local_capacity_kg": round(total_capacity, 1),
            "gap_kg": round(gap, 1),
            "coverage_ratio": round(core.coverage_ratio(total_capacity, required_kg), 3),
            "suppliers_needed": int((sup["covers_requirement"].idxmax() + 1)
                                    if (not sup.empty and sup["covers_requirement"].any())
                                    else len(sup)),
        }

    def save_plan(self, name: str, crop_id: int, target_month: int,
                  demand_kg: float, buffer_pct: float, loss_pct: float) -> int:
        req = core.required_procurement_kg(demand_kg, buffer_pct=buffer_pct,
                                           loss_pct=loss_pct)
        return self.repo.insert("procurement_plans", {
            "name": name, "crop_id": crop_id, "target_month": target_month,
            "demand_kg": demand_kg, "buffer_pct": buffer_pct, "loss_pct": loss_pct,
            "required_kg": round(req, 1), "status": "draft",
        })

    def plans(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT p.id, p.name, c.canonical_name AS crop, p.target_month, "
            "p.demand_kg, p.required_kg, p.status, p.created_at "
            "FROM procurement_plans p JOIN crops c ON c.id=p.crop_id "
            "ORDER BY p.created_at DESC")
