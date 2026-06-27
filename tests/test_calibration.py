"""Tests for the calibration layer."""
import numpy as np

from src.forecast.openmeteo import MaxTempForecast, DEFAULT_SIGMA_FLOOR
from src.forecast.model import yes_probability


def test_bias_shifts_probability():
    """A positive bias (model runs hot) should lower P(very high temp)."""
    members = np.array([30.0, 31.0, 32.0])
    cold = MaxTempForecast("X", "d", members, bias_c=0.0)
    hot_corrected = MaxTempForecast("X", "d", members, bias_c=2.0)  # subtract 2°C
    p_cold = yes_probability(cold, "gte", 32)
    p_corr = yes_probability(hot_corrected, "gte", 32)
    assert p_corr < p_cold


def test_sigma_floor_widens():
    members = np.array([25.0, 25.1, 24.9])
    tight = MaxTempForecast("X", "d", members, sigma_floor=0.5)
    wide = MaxTempForecast("X", "d", members, sigma_floor=3.0)
    # wider sigma pulls the exact-bucket probability down (mass spreads out)
    assert yes_probability(wide, "exact", 25) < yes_probability(tight, "exact", 25)


def test_default_sigma_floor_applied():
    fc = MaxTempForecast("X", "d", np.array([20.0, 21.0]))
    assert fc.sigma_floor == DEFAULT_SIGMA_FLOOR


def test_uniform_brier_baseline():
    from scripts.calibrate import uniform_brier
    b = uniform_brier(span=6)
    assert 0.8 < b < 1.0  # uniform over 13 buckets


def test_crps_perfect_forecast_is_small():
    from src.forecast.emos import crps_gaussian
    # tight forecast centred on the observation -> tiny CRPS
    tight = crps_gaussian(20.0, 0.3, 20.0)
    wide = crps_gaussian(20.0, 5.0, 20.0)
    assert tight < wide


def test_emos_fit_reduces_crps():
    import numpy as np
    from src.forecast.emos import fit_emos, baseline_crps
    rng = np.random.default_rng(0)
    actual = rng.normal(25, 3, 200)
    means = actual + 1.5 + rng.normal(0, 0.5, 200)   # biased, over-dispersed ens
    varis = np.full(200, 9.0)                          # ens var too large
    a, b, c, d, crps_star = fit_emos(means, varis, actual)
    crps0 = baseline_crps(means, varis, actual, 1.2)
    assert crps_star < crps0          # calibration must help
    assert 0 <= b <= 3


def test_emos_applied_overrides_bias():
    from src.forecast.openmeteo import MaxTempForecast
    from src.forecast.model import apply_calibration, yes_probability
    import numpy as np
    fc = MaxTempForecast("RKSI", "d", np.array([24.0, 25.0, 26.0]))
    cal = {"RKSI": {"emos": {"a": 1.0, "b": 1.0, "c": 1.0, "d": 0.0}}}
    apply_calibration(fc, cal)
    assert fc.emos == (1.0, 1.0, 1.0, 0.0)
    # μ = 1 + 1*25 = 26 ; should be a valid probability
    p = yes_probability(fc, "exact", 26)
    assert 0 < p < 1
