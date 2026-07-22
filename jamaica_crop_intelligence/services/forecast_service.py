"""Price-forecasting service. Produces MODELLED estimates only."""

from __future__ import annotations

import pandas as pd

from ..calculations import forecast as fc
from .price_service import PriceService
from .repository import Repository


class ForecastService:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.prices = PriceService(repo)

    def _ordered_series(self, crop_id: int, price_type: str | None) -> pd.DataFrame:
        df = self.prices.price_frame(crop_id, price_type=price_type)
        if df.empty:
            return df
        s = (df.groupby(["sort_key", "period_label"], as_index=False)
             ["price_per_kg_jmd"].mean())
        return s.sort_values("sort_key").reset_index(drop=True)

    def forecast_price(self, crop_id: int, *, horizon: int = 4,
                       price_type: str | None = None,
                       season_length: int = 4) -> dict:
        """Return {history, forecast, mape, provenance}. Empty history if no data."""
        series = self._ordered_series(crop_id, price_type)
        if series.empty:
            return {"history": pd.DataFrame(), "forecast": pd.DataFrame(),
                    "mape": None, "provenance": "modelled"}
        values = series["price_per_kg_jmd"].tolist()
        points = fc.seasonal_trend_forecast(
            values, season_length=season_length, horizon=horizon)
        mape = fc.backtest_mape(values, season_length=season_length,
                                holdout=min(season_length, max(2, len(values) // 3)))
        last_label = series["period_label"].iloc[-1]
        forecast_rows = [{
            "step": p.step, "period_label": f"+{p.step}",
            "point": p.point, "lower": p.lower, "upper": p.upper,
        } for p in points]
        return {
            "history": series.rename(columns={"price_per_kg_jmd": "price"}),
            "forecast": pd.DataFrame(forecast_rows),
            "mape": mape,
            "last_period": last_label,
            "provenance": "modelled",
        }
