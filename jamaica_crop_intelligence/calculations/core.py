"""Pure calculation functions for Jamaica Crop Intelligence.

Everything here is deterministic and free of I/O so it can be unit-tested in
isolation. Two ideas are kept strictly separate, in code as in the UI:

  * **Observed quarterly production** — a real measured quantity per quarter.
  * **Modelled monthly supply index** — a 0..1 relative-availability curve
    derived from crop calendar windows. It is a *shape*, never a tonnage.
"""

from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #
QUARTER_MONTHS = {1: (1, 2, 3), 2: (4, 5, 6), 3: (7, 8, 9), 4: (10, 11, 12)}


def quarter_of_month(month: int) -> int:
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range: {month}")
    return (month - 1) // 3 + 1


def months_of_quarter(quarter: int) -> tuple[int, int, int]:
    if quarter not in QUARTER_MONTHS:
        raise ValueError(f"quarter out of range: {quarter}")
    return QUARTER_MONTHS[quarter]


# --------------------------------------------------------------------------- #
# Seasonality / supply index (MODELLED)
# --------------------------------------------------------------------------- #
def build_supply_index(
    harvest_months: Sequence[int],
    *,
    shoulder: float = 0.4,
    baseline: float = 0.05,
) -> list[float]:
    """Return a 12-length modelled monthly availability curve in [0, 1].

    Peak (1.0) at each harvest month, ``shoulder`` weight in the adjacent
    months, and ``baseline`` elsewhere so nothing is ever exactly zero (crops
    trickle in outside their peak). This is a *relative shape*, not a volume.
    """
    if not 0.0 <= shoulder <= 1.0:
        raise ValueError("shoulder must be in [0, 1]")
    if not 0.0 <= baseline <= 1.0:
        raise ValueError("baseline must be in [0, 1]")
    curve = [baseline] * 12
    harvest = {m for m in harvest_months if 1 <= m <= 12}
    for m in range(1, 13):
        idx = m - 1
        if m in harvest:
            curve[idx] = 1.0
        else:
            prev_m = 12 if m == 1 else m - 1
            next_m = 1 if m == 12 else m + 1
            if prev_m in harvest or next_m in harvest:
                curve[idx] = max(curve[idx], shoulder)
    return curve


def normalize_curve(curve: Sequence[float]) -> list[float]:
    """Scale a curve so its values sum to 1.0 (share-of-year). Empty -> zeros."""
    total = float(sum(curve))
    if total <= 0:
        return [0.0] * len(curve)
    return [v / total for v in curve]


def peak_months(curve: Sequence[float], threshold: float = 0.999) -> list[int]:
    """1-indexed months at or above ``threshold`` of the curve's max."""
    if not curve:
        return []
    hi = max(curve)
    if hi <= 0:
        return []
    return [i + 1 for i, v in enumerate(curve) if v >= threshold * hi]


def seasonality_availability(curve: Sequence[float], month: int) -> float:
    """Availability weight for a given 1-indexed month."""
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range: {month}")
    if len(curve) != 12:
        raise ValueError("curve must have 12 entries")
    return float(curve[month - 1])


# --------------------------------------------------------------------------- #
# Price statistics
# --------------------------------------------------------------------------- #
def price_statistics(prices: Sequence[float]) -> dict[str, float]:
    """Mean, median, min, max, stdev and coefficient of variation (volatility)."""
    vals = [float(p) for p in prices if p is not None]
    if not vals:
        return {"count": 0, "mean": 0.0, "median": 0.0, "min": 0.0,
                "max": 0.0, "stdev": 0.0, "cv": 0.0}
    n = len(vals)
    s = sorted(vals)
    mean = sum(vals) / n
    if n % 2:
        median = s[n // 2]
    else:
        median = (s[n // 2 - 1] + s[n // 2]) / 2
    variance = sum((v - mean) ** 2 for v in vals) / n
    stdev = math.sqrt(variance)
    cv = stdev / mean if mean else 0.0
    return {"count": n, "mean": mean, "median": median, "min": min(vals),
            "max": max(vals), "stdev": stdev, "cv": cv}


def to_price_per_kg(price: float, kg_equivalent: float | None) -> float | None:
    """Normalize a per-unit price to per-kg when the unit is mass-convertible."""
    if kg_equivalent is None or kg_equivalent <= 0:
        return None
    return float(price) / float(kg_equivalent)


# --------------------------------------------------------------------------- #
# Procurement planning quantities
# --------------------------------------------------------------------------- #
def required_procurement_kg(
    demand_kg: float,
    *,
    buffer_pct: float = 0.10,
    loss_pct: float = 0.15,
) -> float:
    """Gross quantity to procure to satisfy ``demand_kg`` after buffer + loss.

    demand is grossed up by a safety buffer, then divided by (1 - loss) so that
    post-harvest losses still leave the buffered demand intact.
    """
    if demand_kg < 0:
        raise ValueError("demand cannot be negative")
    if not 0 <= buffer_pct < 5:
        raise ValueError("buffer_pct out of sane range")
    if not 0 <= loss_pct < 1:
        raise ValueError("loss_pct must be in [0, 1)")
    buffered = demand_kg * (1.0 + buffer_pct)
    return buffered / (1.0 - loss_pct)


def supply_gap_kg(required_kg: float, available_kg: float) -> float:
    """Positive number = shortage; 0 = met or surplus."""
    return max(0.0, required_kg - available_kg)


def coverage_ratio(available_kg: float, required_kg: float) -> float:
    """available / required, capped implicitly by caller; 0 required -> 1.0."""
    if required_kg <= 0:
        return 1.0
    return available_kg / required_kg


# --------------------------------------------------------------------------- #
# Quotation comparison scoring
# --------------------------------------------------------------------------- #
def _minmax_norm(value: float, lo: float, hi: float, *, invert: bool = False) -> float:
    """Normalize into [0, 1]. invert=True means lower raw value is better."""
    if hi == lo:
        return 1.0
    frac = (value - lo) / (hi - lo)
    frac = max(0.0, min(1.0, frac))
    return 1.0 - frac if invert else frac


def score_quotations(
    quotes: Sequence[Mapping[str, float]],
    *,
    weights: Mapping[str, float] | None = None,
) -> list[dict]:
    """Score and rank quotations for a single RFQ.

    Each quote dict needs: ``price`` (per kg), ``lead_time`` (days),
    ``quality`` (1..5), ``reliability`` (0..1), and any id fields (passed
    through). Returns copies with ``score`` (0..100) and ``rank`` added,
    sorted best-first. Cheaper price and shorter lead time score higher.
    """
    weights = dict(weights or {
        "price": 0.45, "lead_time": 0.20, "quality": 0.20, "reliability": 0.15,
    })
    wsum = sum(weights.values())
    if wsum <= 0:
        raise ValueError("weights must sum to > 0")
    weights = {k: v / wsum for k, v in weights.items()}

    if not quotes:
        return []

    prices = [q["price"] for q in quotes]
    leads = [q["lead_time"] for q in quotes]
    p_lo, p_hi = min(prices), max(prices)
    l_lo, l_hi = min(leads), max(leads)

    scored = []
    for q in quotes:
        price_s = _minmax_norm(q["price"], p_lo, p_hi, invert=True)
        lead_s = _minmax_norm(q["lead_time"], l_lo, l_hi, invert=True)
        quality_s = max(0.0, min(1.0, (q.get("quality", 3.0) - 1.0) / 4.0))
        reliability_s = max(0.0, min(1.0, q.get("reliability", 0.8)))
        composite = (
            weights["price"] * price_s
            + weights["lead_time"] * lead_s
            + weights["quality"] * quality_s
            + weights["reliability"] * reliability_s
        )
        out = dict(q)
        out["score"] = round(composite * 100.0, 2)
        scored.append(out)

    scored.sort(key=lambda x: x["score"], reverse=True)
    for i, q in enumerate(scored, start=1):
        q["rank"] = i
    return scored


# --------------------------------------------------------------------------- #
# Contract fulfillment & supplier performance
# --------------------------------------------------------------------------- #
def contract_fulfillment(
    contracted_kg: float,
    deliveries: Sequence[Mapping[str, float]],
) -> dict[str, float]:
    """Roll deliveries up into fulfillment KPIs for a single contract.

    Each delivery dict may carry: ``delivered_kg``, ``rejected_kg``,
    ``on_time`` (0/1), ``quality_rating`` (1..5).
    """
    delivered = sum(float(d.get("delivered_kg", 0) or 0) for d in deliveries)
    rejected = sum(float(d.get("rejected_kg", 0) or 0) for d in deliveries)
    accepted = max(0.0, delivered - rejected)
    n = len(deliveries)
    on_time = sum(1 for d in deliveries if d.get("on_time", 1)) if n else 0
    qualities = [float(d["quality_rating"]) for d in deliveries
                 if d.get("quality_rating") is not None]

    fill_rate = accepted / contracted_kg if contracted_kg > 0 else 0.0
    reject_rate = rejected / delivered if delivered > 0 else 0.0
    on_time_rate = on_time / n if n else 0.0
    avg_quality = sum(qualities) / len(qualities) if qualities else 0.0

    return {
        "contracted_kg": float(contracted_kg),
        "delivered_kg": delivered,
        "accepted_kg": accepted,
        "rejected_kg": rejected,
        "outstanding_kg": max(0.0, contracted_kg - accepted),
        "fill_rate": min(fill_rate, 1.0 if contracted_kg else 0.0) if contracted_kg else fill_rate,
        "reject_rate": reject_rate,
        "on_time_rate": on_time_rate,
        "avg_quality": avg_quality,
        "deliveries_count": n,
    }


def supplier_rollup(deliveries: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """Aggregate a supplier's deliveries (across contracts) into performance."""
    delivered = sum(float(d.get("delivered_kg", 0) or 0) for d in deliveries)
    rejected = sum(float(d.get("rejected_kg", 0) or 0) for d in deliveries)
    ordered = sum(float(d.get("ordered_kg", d.get("delivered_kg", 0)) or 0)
                  for d in deliveries)
    n = len(deliveries)
    on_time = sum(1 for d in deliveries if d.get("on_time", 1)) if n else 0
    qualities = [float(d["quality_rating"]) for d in deliveries
                 if d.get("quality_rating") is not None]
    accepted = max(0.0, delivered - rejected)
    return {
        "deliveries_count": n,
        "fill_rate": (accepted / ordered) if ordered > 0 else 0.0,
        "on_time_rate": (on_time / n) if n else 0.0,
        "reject_rate": (rejected / delivered) if delivered > 0 else 0.0,
        "avg_quality": (sum(qualities) / len(qualities)) if qualities else 0.0,
        "total_delivered_kg": delivered,
    }


def planned_vs_actual(planned_kg: float, actual_kg: float) -> dict[str, float]:
    """Variance of actual procurement against plan."""
    variance = actual_kg - planned_kg
    pct = (variance / planned_kg) if planned_kg else 0.0
    return {
        "planned_kg": float(planned_kg),
        "actual_kg": float(actual_kg),
        "variance_kg": variance,
        "variance_pct": pct,
        "status": "surplus" if variance > 0 else ("shortfall" if variance < 0 else "on_plan"),
    }


# --------------------------------------------------------------------------- #
# Threat exposure
# --------------------------------------------------------------------------- #
def threat_exposure_score(
    impact: float,
    intensity: float,
    availability: float,
) -> float:
    """Combine crop-threat impact (1..5), seasonal intensity (0..1) and the
    modelled availability (0..1) into a 0..100 exposure score. Exposure is
    higher when a high-impact threat peaks while the crop is in season.
    """
    impact_n = max(0.0, min(1.0, (impact - 1.0) / 4.0))
    intensity = max(0.0, min(1.0, intensity))
    availability = max(0.0, min(1.0, availability))
    return round(impact_n * intensity * (0.5 + 0.5 * availability) * 100.0, 1)
