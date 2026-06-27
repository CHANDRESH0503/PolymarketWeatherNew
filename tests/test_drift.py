"""Per-station drift kill-switch (capital protection — real money rides on it)."""
from src.strategy.drift import drifting_stations


def test_benches_only_sustained_blowout():
    metrics = {
        "RCSS": (3.8, 3),   # Taipei heat regime: big MAE, enough samples -> bench
        "RKSI": (0.9, 5),   # Seoul healthy -> keep
        "ZBAA": (2.6, 4),   # Beijing over threshold -> bench
    }
    assert drifting_stations(metrics, max_mae=2.5, min_samples=2) == {"RCSS", "ZBAA"}


def test_min_samples_guards_against_flukes():
    # A single bad day is not enough evidence to bench (n < min_samples).
    metrics = {"RCSS": (4.8, 1)}
    assert drifting_stations(metrics, max_mae=2.5, min_samples=2) == set()
    # ...but if you trust one sample, it trips.
    assert drifting_stations(metrics, max_mae=2.5, min_samples=1) == {"RCSS"}


def test_threshold_is_inclusive_and_healthy_stations_pass():
    metrics = {"A": (2.5, 3), "B": (2.49, 3)}
    assert drifting_stations(metrics, max_mae=2.5, min_samples=2) == {"A"}
