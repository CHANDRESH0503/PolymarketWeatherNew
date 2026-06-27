"""Per-station drift kill-switch.

σ-widening (config/calibration.yaml `sigma_min:`) softens overconfidence but the
edge filter can still let a big bet through when a station's MEAN forecast breaks
down — e.g. RCSS/Taipei ran 3–4σ hot in a June heat regime and a ruinous "No"
bet survived the filter. This guard is the hard stop: when a station's recent
forecast error blows out, we SKIP it entirely until it recovers.

`drifting_stations` is the pure decision (testable, no I/O); `current_blocked`
is the convenience that reads the paper store's backfilled forecast errors.
"""
from __future__ import annotations


def drifting_stations(metrics: dict[str, tuple[float, int]],
                      max_mae: float, min_samples: int) -> set[str]:
    """Stations to bench: rolling MAE ≥ max_mae with at least min_samples resolved
    days behind it (n<min_samples = too little evidence to bench, e.g. the very
    first encounter of a new regime, which is inherently uncatchable from history)."""
    return {s for s, (mae, n) in metrics.items()
            if n >= min_samples and mae >= max_mae}


def current_blocked() -> set[str]:
    """Stations currently benched by the drift guard, from the paper store's
    forecast-vs-actual history. Returns empty set when the guard is off or the
    store/history isn't available (fail-open: never block on a read error)."""
    from ..config import (DRIFT_GUARD, DRIFT_LOOKBACK_DAYS, DRIFT_MAX_MAE,
                          DRIFT_MIN_SAMPLES)
    if not DRIFT_GUARD:
        return set()
    try:
        from ..paper import store
        con = store.connect()
        metrics = store.station_error_metrics(con, DRIFT_LOOKBACK_DAYS)
        return drifting_stations(metrics, DRIFT_MAX_MAE, DRIFT_MIN_SAMPLES)
    except Exception:  # noqa: BLE001 — a guard read must never break a scan
        return set()
