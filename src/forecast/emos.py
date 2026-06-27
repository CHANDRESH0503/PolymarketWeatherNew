"""EMOS / Non-homogeneous Gaussian Regression (NGR) post-processing.

Fits a calibrated predictive Gaussian to ensemble output:

    y | ens  ~  N( a + b*mean,  c + d*var )

by minimising the mean Continuous Ranked Probability Score (CRPS) — the proper
scoring rule that rewards both sharpness and calibration. This replaces the
hand-tuned (bias, sigma_floor) and directly cures the over-dispersion the
backtest found (the raw cross-model spread is wider than the realised error).

Refs: Gneiting et al. 2005 (EMOS + min-CRPS); Wikipedia "Nonhomogeneous
Gaussian regression".
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm


def crps_gaussian(mu, sigma, y) -> np.ndarray:
    """Closed-form CRPS of N(mu, sigma) evaluated at observation y (vectorised).

    CRPS = sigma * ( w*(2Φ(w)-1) + 2φ(w) - 1/√π ),  w = (y-mu)/sigma
    """
    sigma = np.maximum(sigma, 1e-6)
    w = (np.asarray(y) - mu) / sigma
    return sigma * (w * (2 * norm.cdf(w) - 1) + 2 * norm.pdf(w) - 1 / np.sqrt(np.pi))


def fit_emos(means: np.ndarray, variances: np.ndarray, actuals: np.ndarray):
    """Return (a, b, c, d) minimising mean CRPS for N(a+b*mean, c+d*var).

    Constraints: b>=0 (don't invert the forecast), c>0, d>=0 (variance positive).
    """
    m = np.asarray(means, float)
    v = np.asarray(variances, float)
    y = np.asarray(actuals, float)

    def obj(p):
        a, b, c, d = p
        mu = a + b * m
        sig = np.sqrt(np.maximum(c + d * v, 1e-6))
        return crps_gaussian(mu, sig, y).mean()

    x0 = [float(np.mean(y - m)), 1.0, float(np.var(y - m)) or 1.0, 0.5]
    bounds = [(-10, 10), (0.0, 3.0), (1e-4, 50.0), (0.0, 50.0)]
    res = minimize(obj, x0, method="L-BFGS-B", bounds=bounds)
    a, b, c, d = res.x
    return float(a), float(b), float(c), float(d), float(res.fun)


def baseline_crps(means, variances, actuals, sigma_floor: float) -> float:
    """Mean CRPS of the current (uncalibrated) model, for comparison."""
    m = np.asarray(means, float)
    v = np.asarray(variances, float)
    y = np.asarray(actuals, float)
    sig = np.sqrt(v + sigma_floor ** 2)
    return float(crps_gaussian(m, sig, y).mean())
