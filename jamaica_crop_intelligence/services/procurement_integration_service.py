"""RFQ -> quotation -> contract -> delivery transaction lifecycle.

Also computes quotation comparison scores, contract fulfillment KPIs,
supplier-performance rollups and planned-vs-actual analytics from *actual*
recorded deliveries.
"""

from __future__ import annotations

import pandas as pd

from ..calculations import core
from ..config import settings
from .repository import Repository


class ProcurementIntegrationService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ------------------------------------------------------------------ RFQ
    def create_rfq(self, buyer_name: str, crop_id: int, quantity_kg: float,
                   needed_by: str | None = None, quality_spec: str | None = None) -> int:
        n = self.repo.table_count("rfqs") + 1
        reference = f"RFQ-{n:04d}"
        return self.repo.insert("rfqs", {
            "reference": reference, "buyer_name": buyer_name, "crop_id": crop_id,
            "quantity_kg": quantity_kg, "needed_by": needed_by,
            "quality_spec": quality_spec, "status": "open",
        })

    def rfqs(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT r.id, r.reference, r.buyer_name, c.canonical_name AS crop, "
            "r.quantity_kg, r.needed_by, r.status, r.created_at "
            "FROM rfqs r JOIN crops c ON c.id=r.crop_id ORDER BY r.id DESC")

    def rfq(self, rfq_id: int) -> dict | None:
        row = self.repo.query_one("SELECT * FROM rfqs WHERE id=?", (rfq_id,))
        return dict(row) if row else None

    # ----------------------------------------------------------- quotations
    def add_quotation(self, rfq_id: int, supplier_id: int, unit_price: float,
                      available_kg: float, lead_time_days: int = 7,
                      quality_rating: float = 3.0) -> int:
        return self.repo.insert("quotations", {
            "rfq_id": rfq_id, "supplier_id": supplier_id,
            "unit_price_jmd_per_kg": unit_price, "available_kg": available_kg,
            "lead_time_days": lead_time_days, "quality_rating": quality_rating,
            "status": "received",
        })

    def quotations_for_rfq(self, rfq_id: int) -> pd.DataFrame:
        return self.repo.df(
            "SELECT q.id, s.name AS supplier, s.reliability, "
            "q.unit_price_jmd_per_kg, q.available_kg, q.lead_time_days, "
            "q.quality_rating, q.score, q.status "
            "FROM quotations q JOIN suppliers s ON s.id=q.supplier_id "
            "WHERE q.rfq_id=? ORDER BY q.id", (rfq_id,))

    def score_quotations(self, rfq_id: int,
                         weights: dict | None = None) -> pd.DataFrame:
        """Score and persist comparison scores for all quotes on an RFQ."""
        rows = self.repo.query(
            "SELECT q.id, q.unit_price_jmd_per_kg, q.lead_time_days, "
            "q.quality_rating, s.reliability, s.name AS supplier "
            "FROM quotations q JOIN suppliers s ON s.id=q.supplier_id "
            "WHERE q.rfq_id=?", (rfq_id,))
        quotes = [{
            "id": r["id"], "supplier": r["supplier"],
            "price": r["unit_price_jmd_per_kg"], "lead_time": r["lead_time_days"],
            "quality": r["quality_rating"], "reliability": r["reliability"],
        } for r in rows]
        scored = core.score_quotations(
            quotes, weights=weights or settings.DEFAULT_QUOTE_WEIGHTS)
        for q in scored:
            status = "shortlisted" if q["rank"] == 1 else "received"
            self.repo.execute(
                "UPDATE quotations SET score=?, status=? WHERE id=?",
                (q["score"], status, q["id"]))
        return pd.DataFrame(scored)

    # ------------------------------------------------------------- contracts
    def award_contract(self, rfq_id: int, quotation_id: int,
                       contracted_kg: float | None = None,
                       start_date: str | None = None,
                       end_date: str | None = None) -> int:
        q = self.repo.query_one("SELECT * FROM quotations WHERE id=?", (quotation_id,))
        r = self.repo.query_one("SELECT * FROM rfqs WHERE id=?", (rfq_id,))
        if not q or not r:
            raise ValueError("RFQ or quotation not found")
        kg = contracted_kg if contracted_kg is not None else min(
            r["quantity_kg"], q["available_kg"])
        n = self.repo.table_count("contracts") + 1
        reference = f"CON-{n:04d}"
        cid = self.repo.insert("contracts", {
            "reference": reference, "rfq_id": rfq_id, "quotation_id": quotation_id,
            "supplier_id": q["supplier_id"], "crop_id": r["crop_id"],
            "contracted_kg": kg,
            "unit_price_jmd_per_kg": q["unit_price_jmd_per_kg"],
            "start_date": start_date, "end_date": end_date, "status": "active",
        })
        self.repo.execute("UPDATE rfqs SET status='awarded' WHERE id=?", (rfq_id,))
        self.repo.execute("UPDATE quotations SET status='accepted' WHERE id=?",
                          (quotation_id,))
        return cid

    def contracts(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT ct.id, ct.reference, s.name AS supplier, c.canonical_name AS crop, "
            "ct.contracted_kg, ct.unit_price_jmd_per_kg, ct.status, ct.start_date "
            "FROM contracts ct JOIN suppliers s ON s.id=ct.supplier_id "
            "JOIN crops c ON c.id=ct.crop_id ORDER BY ct.id DESC")

    def contract(self, contract_id: int) -> dict | None:
        row = self.repo.query_one("SELECT * FROM contracts WHERE id=?", (contract_id,))
        return dict(row) if row else None

    # ------------------------------------------------------------ deliveries
    def record_delivery(self, contract_id: int, delivery_date: str,
                        ordered_kg: float, delivered_kg: float,
                        rejected_kg: float = 0.0, on_time: bool = True,
                        quality_rating: float = 3.0, notes: str | None = None) -> int:
        did = self.repo.insert("deliveries", {
            "contract_id": contract_id, "delivery_date": delivery_date,
            "ordered_kg": ordered_kg, "delivered_kg": delivered_kg,
            "rejected_kg": rejected_kg, "on_time": 1 if on_time else 0,
            "quality_rating": quality_rating, "notes": notes,
        })
        self._refresh_contract_status(contract_id)
        return did

    def deliveries_for_contract(self, contract_id: int) -> pd.DataFrame:
        return self.repo.df(
            "SELECT id, delivery_date, ordered_kg, delivered_kg, rejected_kg, "
            "on_time, quality_rating, notes FROM deliveries "
            "WHERE contract_id=? ORDER BY delivery_date", (contract_id,))

    def _delivery_dicts(self, contract_id: int) -> list[dict]:
        return [dict(r) for r in self.repo.query(
            "SELECT * FROM deliveries WHERE contract_id=?", (contract_id,))]

    def _refresh_contract_status(self, contract_id: int) -> None:
        c = self.contract(contract_id)
        if not c:
            return
        kpi = core.contract_fulfillment(c["contracted_kg"],
                                        self._delivery_dicts(contract_id))
        status = "fulfilled" if kpi["fill_rate"] >= 0.99 else "active"
        self.repo.execute("UPDATE contracts SET status=? WHERE id=?",
                          (status, contract_id))

    # ------------------------------------------------------------------ KPIs
    def contract_kpis(self, contract_id: int) -> dict:
        c = self.contract(contract_id)
        if not c:
            return {}
        return core.contract_fulfillment(c["contracted_kg"],
                                         self._delivery_dicts(contract_id))

    def all_contract_kpis(self) -> pd.DataFrame:
        rows = []
        for c in self.repo.query("SELECT id, reference FROM contracts"):
            kpi = self.contract_kpis(c["id"])
            if kpi:
                kpi["contract"] = c["reference"]
                rows.append(kpi)
        return pd.DataFrame(rows)

    def supplier_performance(self) -> pd.DataFrame:
        """Rollup of actual deliveries by supplier (planned vs actual, quality)."""
        rows = []
        suppliers = self.repo.query("SELECT id, name FROM suppliers")
        for s in suppliers:
            deliveries = [dict(r) for r in self.repo.query(
                "SELECT d.* FROM deliveries d "
                "JOIN contracts ct ON ct.id=d.contract_id "
                "WHERE ct.supplier_id=?", (s["id"],))]
            if not deliveries:
                continue
            roll = core.supplier_rollup(deliveries)
            roll["supplier"] = s["name"]
            rows.append(roll)
            # persist rollup
            self.repo.upsert("supplier_performance", {
                "supplier_id": s["id"], "period": "rolling",
                "fill_rate": roll["fill_rate"], "on_time_rate": roll["on_time_rate"],
                "reject_rate": roll["reject_rate"], "avg_quality": roll["avg_quality"],
                "deliveries_count": roll["deliveries_count"],
            }, conflict_cols=["supplier_id", "period"],
               update_cols=["fill_rate", "on_time_rate", "reject_rate",
                            "avg_quality", "deliveries_count"])
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df[["supplier", "deliveries_count", "fill_rate", "on_time_rate",
                     "reject_rate", "avg_quality", "total_delivered_kg"]]
        return df

    def planned_vs_actual(self) -> pd.DataFrame:
        """Per contract: contracted (planned) vs accepted (actual) kg."""
        rows = []
        for c in self.repo.query(
                "SELECT ct.id, ct.reference, ct.contracted_kg FROM contracts ct"):
            kpi = self.contract_kpis(c["id"])
            pva = core.planned_vs_actual(c["contracted_kg"],
                                         kpi.get("accepted_kg", 0.0))
            pva["contract"] = c["reference"]
            rows.append(pva)
        return pd.DataFrame(rows)

    def shortages_and_rejections(self) -> pd.DataFrame:
        """Outstanding (shortfall) and rejected quantities per contract."""
        rows = []
        for c in self.repo.query(
                "SELECT ct.id, ct.reference, c.canonical_name AS crop "
                "FROM contracts ct JOIN crops c ON c.id=ct.crop_id"):
            kpi = self.contract_kpis(c["id"])
            if not kpi:
                continue
            rows.append({
                "contract": c["reference"], "crop": c["crop"],
                "outstanding_kg": round(kpi["outstanding_kg"], 1),
                "rejected_kg": round(kpi["rejected_kg"], 1),
                "reject_rate": round(kpi["reject_rate"], 3),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df[(df["outstanding_kg"] > 0) | (df["rejected_kg"] > 0)]
            df = df.reset_index(drop=True)
        return df
