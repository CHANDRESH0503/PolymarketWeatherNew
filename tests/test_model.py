"""Sanity checks for the bucket-probability model."""
import numpy as np

from src.forecast.openmeteo import MaxTempForecast
from src.forecast.model import prob_exact, prob_gte, prob_lte, yes_probability
from src.strategy.sizing import kelly_fraction


def _fc(values):
    return MaxTempForecast("TEST", "2026-06-04", np.array(values, dtype=float))


def test_exact_buckets_sum_to_one():
    fc = _fc([24.0, 25.1, 25.4, 26.2, 24.8, 25.9, 26.1, 25.0])
    total = sum(prob_exact(fc, d) for d in range(15, 40))
    assert abs(total - 1.0) < 1e-6


def test_gte_lte_complement():
    fc = _fc([28, 29, 30, 31, 29.5, 30.5])
    # P(>=30) + P(<=29) == 1 since buckets are integer-partitioned
    assert abs(prob_gte(fc, 30) + prob_lte(fc, 29) - 1.0) < 1e-6


def test_tight_ensemble_concentrates_mass():
    # With the LOW global σ floor (0.5°, neutral for calibration), a tight
    # ensemble is allowed to concentrate — sharp forecasts must stay sharp.
    fc = _fc([30.0, 30.1, 29.9, 30.0, 30.05])
    assert prob_exact(fc, 30) > 0.3
    assert prob_exact(fc, 25) < 0.01


def test_per_station_sigma_min_widens_only_that_station():
    # The TARGETED humility: sigma_min hard-floors a single overconfident station
    # (e.g. RCSS/Taipei) so a far bucket can't collapse to ~0, WITHOUT touching
    # well-calibrated stations. Same tight ensemble, sigma_min=2.0 => much flatter.
    sharp = _fc([30.0, 30.1, 29.9, 30.0, 30.05])
    humble = _fc([30.0, 30.1, 29.9, 30.0, 30.05])
    humble.sigma_min = 2.0
    assert prob_exact(humble, 30) < prob_exact(sharp, 30)      # mode widened
    assert prob_exact(humble, 33) > 2 * prob_exact(sharp, 33)  # tail lifted a lot


def test_kelly_zero_without_edge():
    assert kelly_fraction(0.5, 0.6) == 0.0
    assert kelly_fraction(0.7, 0.5) > 0.0


def test_yes_probability_dispatch():
    fc = _fc([25, 26, 27])
    assert yes_probability(fc, "gte", 25) > yes_probability(fc, "gte", 27)
