"""Turn an ensemble max-temp distribution into bucket probabilities that match
Polymarket's resolution rules.

Resolution rule (from market descriptions): the station reports the daily high
ROUNDED TO WHOLE DEGREES CELSIUS. So a market "25°C" wins iff round(max) == 25,
"27°C or higher" wins iff round(max) >= 27, "17°C or below" iff round(max) <= 17.

We model the predictive distribution of the *true* max as Normal(mu, sigma)
fitted to the ensemble members (with optional bias correction), then integrate
the standard-rounding interval for each bucket. Using the smooth Normal instead
of raw member counts avoids zero-probability buckets from a thin ensemble and
lets us widen sigma to stay humble about model error.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm

from .openmeteo import MaxTempForecast


def _mu_sigma(fc: MaxTempForecast) -> tuple[float, float]:
    # Humility floor on σ. MIN_PREDICTIVE_SIGMA is a LOW global safety (raising it
    # globally over-widens well-calibrated stations and degrades Brier — measured);
    # fc.sigma_min is the TARGETED per-station floor (config/calibration.yaml) that
    # widens only demonstrably overconfident/drifting stations like RCSS/Taipei,
    # which otherwise throw ruinous ~0.000-probability "No" bets in a heat regime.
    from ..config import MIN_PREDICTIVE_SIGMA
    floor = max(MIN_PREDICTIVE_SIGMA, fc.sigma_min)
    # EMOS/NGR (preferred when fitted): μ = a + b·mean, σ² = c + d·var.
    if fc.emos is not None:
        a, b, c, d = fc.emos
        mu = a + b * fc.mean
        sigma = float(np.sqrt(max(c + d * fc.std ** 2, 0.25)))
        return mu, max(sigma, floor)
    # Fallback: hand-tuned bias + sigma_floor (per-station, see calibrate.py).
    mu = fc.mean - fc.bias_c
    sigma = float(np.sqrt(fc.std ** 2 + fc.sigma_floor ** 2))
    return mu, max(sigma, floor)


def apply_calibration(fc: MaxTempForecast, calibration: dict) -> MaxTempForecast:
    """Attach learned per-station calibration (EMOS coeffs and/or bias) to a
    forecast. Used by edge / market-making / backtest so they stay consistent."""
    from .openmeteo import DEFAULT_SIGMA_FLOOR
    cal = calibration.get(fc.station_code, {})
    fc.bias_c = float(cal.get("bias", 0.0))
    fc.sigma_floor = float(cal.get("sigma", DEFAULT_SIGMA_FLOOR))
    fc.sigma_min = float(cal.get("sigma_min", 0.0))
    e = cal.get("emos")
    if e:
        fc.emos = (float(e["a"]), float(e["b"]), float(e["c"]), float(e["d"]))
    return fc


def prob_exact(fc: MaxTempForecast, degree: int) -> float:
    """P(round(max) == degree) = P(degree-0.5 <= max < degree+0.5)."""
    mu, sigma = _mu_sigma(fc)
    return float(norm.cdf(degree + 0.5, mu, sigma) - norm.cdf(degree - 0.5, mu, sigma))


def prob_gte(fc: MaxTempForecast, degree: int) -> float:
    """P(round(max) >= degree) = P(max >= degree-0.5)."""
    mu, sigma = _mu_sigma(fc)
    return float(1.0 - norm.cdf(degree - 0.5, mu, sigma))


def prob_lte(fc: MaxTempForecast, degree: int) -> float:
    """P(round(max) <= degree) = P(max < degree+0.5)."""
    mu, sigma = _mu_sigma(fc)
    return float(norm.cdf(degree + 0.5, mu, sigma))


def yes_probability(fc: MaxTempForecast, bucket_kind: str, degree: int) -> float:
    if bucket_kind == "exact":
        return prob_exact(fc, degree)
    if bucket_kind == "gte":
        return prob_gte(fc, degree)
    if bucket_kind == "lte":
        return prob_lte(fc, degree)
    raise ValueError(bucket_kind)
