"""What-if scenario service: shock supply/price/threat and read the effect."""

from __future__ import annotations

import json

import pandas as pd

from ..calculations import core
from .price_service import PriceService
from .production_service import ProductionService
from .repository import Repository


class ScenarioService:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.production = ProductionService(repo)
        self.prices = PriceService(repo)

    def supply_shock(self, crop_id: int, shock_pct: float) -> pd.DataFrame:
        """Apply a proportional supply shock to the modelled monthly curve.

        ``shock_pct`` of -0.3 means a 30% reduction in availability. The result
        keeps observed vs. modelled honesty: it only touches the modelled curve.
        """
        curve = self.production.supply_index(crop_id)
        shocked = [max(0.0, v * (1.0 + shock_pct)) for v in curve]
        return pd.DataFrame({
            "month": range(1, 13),
            "baseline_supply_index": curve,
            "scenario_supply_index": shocked,
        })

    def price_response(self, crop_id: int, supply_shock_pct: float,
                       elasticity: float = -0.6) -> dict:
        """Rough modelled price response to a supply shock via price elasticity.

        %ΔPrice = %ΔQuantity / elasticity. With elasticity -0.6, a -20% supply
        change implies roughly +33% price. Clearly a modelled relationship.
        """
        if elasticity >= 0:
            raise ValueError("price elasticity of supply should be negative")
        pct_price = supply_shock_pct / elasticity
        stats = self.prices.statistics(crop_id)
        base = stats.get("mean", 0.0)
        return {
            "supply_shock_pct": supply_shock_pct,
            "elasticity": elasticity,
            "price_change_pct": round(pct_price, 3),
            "baseline_price_jmd_per_kg": round(base, 1),
            "scenario_price_jmd_per_kg": round(base * (1.0 + pct_price), 1),
            "provenance": "modelled",
        }

    def procurement_stress(self, crop_id: int, demand_kg: float,
                           supply_shock_pct: float, buffer_pct: float = 0.10,
                           loss_pct: float = 0.15) -> dict:
        """How a supply shock changes required procurement vs. local capacity."""
        from .procurement_service import ProcurementService
        proc = ProcurementService(self.repo)
        required = core.required_procurement_kg(
            demand_kg, buffer_pct=buffer_pct, loss_pct=loss_pct)
        sup = proc.suppliers_for_crop(crop_id)
        base_capacity = float(sup["typical_kg_per_week"].fillna(0).sum()) if not sup.empty else 0.0
        shocked_capacity = base_capacity * (1.0 + supply_shock_pct)
        return {
            "required_kg": round(required, 1),
            "baseline_capacity_kg": round(base_capacity, 1),
            "scenario_capacity_kg": round(shocked_capacity, 1),
            "baseline_gap_kg": round(core.supply_gap_kg(required, base_capacity), 1),
            "scenario_gap_kg": round(core.supply_gap_kg(required, shocked_capacity), 1),
        }

    def save_scenario(self, name: str, crop_id: int, params: dict,
                      result: dict) -> int:
        return self.repo.insert("scenarios", {
            "name": name, "crop_id": crop_id,
            "params_json": json.dumps(params), "result_json": json.dumps(result),
        })

    def scenarios(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT s.id, s.name, c.canonical_name AS crop, s.created_at "
            "FROM scenarios s LEFT JOIN crops c ON c.id=s.crop_id "
            "ORDER BY s.created_at DESC")
