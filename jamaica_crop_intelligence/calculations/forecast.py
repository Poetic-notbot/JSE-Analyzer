"""Lightweight, dependency-light price forecasting (numpy/pandas only).

A seasonal + linear-trend model with an empirical confidence band. Chosen over
statsmodels/Prophet so the app deploys cleanly on Streamlit Community Cloud.

Everything here is a **modelled estimate**, never observed data. The service and
UI label it as such and it is stored with provenance='modelled'.

Method
------
1. Fit an OLS linear trend to the ordered series (value ~ a + b*t).
2. Additive seasonal factors: average the detrended residual by season slot
   (t mod season_length). This captures the recurring within-year shape.
3. Forecast_h = trend(t+h) + seasonal_factor[(t+h) mod season_length].
4. Band: point ± z * stdev(in-sample residuals). Default z = 1.645 (~90%).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ForecastPoint:
    step: int          # 1-based steps ahead
    point: float
    lower: float
    upper: float


def _ols_trend(y: np.ndarray) -> tuple[float, float]:
    """Return (intercept, slope) of a least-squares line over t = 0..n-1."""
    n = len(y)
    t = np.arange(n, dtype=float)
    t_mean = t.mean()
    y_mean = y.mean()
    denom = ((t - t_mean) ** 2).sum()
    slope = 0.0 if denom == 0 else ((t - t_mean) * (y - y_mean)).sum() / denom
    intercept = y_mean - slope * t_mean
    return intercept, slope


def _seasonal_factors(resid: np.ndarray, season_length: int) -> np.ndarray:
    """Average detrended residual per season slot (additive, mean-centred)."""
    factors = np.zeros(season_length)
    for s in range(season_length):
        slot = resid[s::season_length]
        factors[s] = slot.mean() if len(slot) else 0.0
    return factors - factors.mean()  # keep seasonal component zero-sum


def seasonal_trend_forecast(
    values: list[float] | np.ndarray,
    *,
    season_length: int = 4,
    horizon: int = 4,
    z: float = 1.645,
    non_negative: bool = True,
) -> list[ForecastPoint]:
    """Forecast ``horizon`` steps ahead. Degrades gracefully on short series."""
    y = np.asarray([v for v in values if v is not None], dtype=float)
    n = len(y)
    if n == 0:
        return []
    if n < 3:
        # Not enough to trend: naive (repeat last value), no band.
        last = float(y[-1])
        return [ForecastPoint(h, last, last, last) for h in range(1, horizon + 1)]

    intercept, slope = _ols_trend(y)
    t = np.arange(n, dtype=float)
    trend_fit = intercept + slope * t

    use_season = n >= 2 * season_length
    if use_season:
        resid = y - trend_fit
        factors = _seasonal_factors(resid, season_length)
        in_sample = trend_fit + factors[(t.astype(int)) % season_length]
    else:
        factors = np.zeros(season_length)
        in_sample = trend_fit

    residuals = y - in_sample
    sigma = float(residuals.std(ddof=1)) if n > 2 else 0.0

    out: list[ForecastPoint] = []
    for h in range(1, horizon + 1):
        tt = n - 1 + h
        point = intercept + slope * tt
        if use_season:
            point += factors[tt % season_length]
        if non_negative:
            point = max(0.0, point)
        lo = max(0.0, point - z * sigma) if non_negative else point - z * sigma
        hi = point + z * sigma
        out.append(ForecastPoint(h, round(point, 2), round(lo, 2), round(hi, 2)))
    return out


def backtest_mape(values: list[float] | np.ndarray, *, season_length: int = 4,
                  holdout: int = 4) -> float | None:
    """Simple backtest: fit on all but the last ``holdout`` points, forecast
    them, and return MAPE (%). None if the series is too short.
    """
    y = np.asarray([v for v in values if v is not None], dtype=float)
    if len(y) < holdout + 3:
        return None
    train, test = y[:-holdout], y[-holdout:]
    preds = seasonal_trend_forecast(train, season_length=season_length,
                                    horizon=holdout)
    errs = []
    for p, actual in zip(preds, test):
        if actual != 0:
            errs.append(abs(p.point - actual) / abs(actual))
    if not errs:
        return None
    return round(float(np.mean(errs)) * 100.0, 1)
