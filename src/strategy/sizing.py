"""Position sizing. Fractional Kelly for a binary contract priced at `price`
that pays 1 if it resolves Yes."""
from __future__ import annotations

import numpy as np

from ..config import (BANKROLL, CORR_KELLY_RHO, KELLY_FRACTION,
                      MAX_STAKE_PER_MARKET, MAX_STAKE_FRACTION)


def market_cap(bankroll: float = BANKROLL) -> float:
    """Per-market stake ceiling: the smaller of the absolute cap and a fraction
    of live equity. Compounds as the book grows and shrinks in a drawdown, so a
    single bet can't blow up the book regardless of bankroll size."""
    cap = MAX_STAKE_PER_MARKET
    if MAX_STAKE_FRACTION > 0:
        cap = min(cap, MAX_STAKE_FRACTION * bankroll)
    return cap


def kelly_fraction(p: float, price: float) -> float:
    """Optimal bankroll fraction for buying a Yes contract at `price`.

    Buying pays net odds b = (1-price)/price on a win prob p.
    Kelly f* = (b*p - (1-p)) / b = (p - price) / (1 - price).
    Returns 0 when there is no edge.
    """
    if price <= 0 or price >= 1:
        return 0.0
    f = (p - price) / (1.0 - price)
    return max(0.0, f)


def stake_usdc(p: float, price: float,
               bankroll: float = BANKROLL,
               fraction: float = KELLY_FRACTION,
               cap: float | None = None) -> float:
    if cap is None:
        cap = market_cap(bankroll)
    f = kelly_fraction(p, price) * fraction
    return round(min(f * bankroll, cap), 2)


# --- Tier 3: correlation-aware (simultaneous) Kelly -------------------------
# Independent single-bet Kelly over-bets when the outcomes are correlated — one
# heat wave pushes many cities the same way, so several "Yes/No" bets win or lose
# together, inflating variance. The continuous (log-normal) approximation of
# simultaneous Kelly maximises E[log wealth] with f* = Σ⁻¹ μ, where μ is the
# vector of expected per-dollar returns and Σ their covariance. Positive
# correlation in Σ shrinks the correlated legs — pure downside protection.

def correlation_kelly(probs, prices, corr=None,
                      rho: float = CORR_KELLY_RHO) -> np.ndarray:
    """Full-Kelly bankroll fractions for a set of simultaneous binary bets.

    `probs[i]`  = our win probability for buying leg i at `prices[i]`.
    `corr`      = NxN correlation matrix of the legs' outcomes; if None, an
                  equicorrelation matrix with off-diagonal `rho` is used.
    Returns f*[i] >= 0 (no shorting; negative/no-edge legs clamp to 0). Apply
    KELLY_FRACTION and per-market caps separately (see `correlated_stakes`)."""
    p = np.asarray(probs, dtype=float)
    q = np.asarray(prices, dtype=float)
    n = p.size
    if n == 0:
        return np.zeros(0)

    # Per-dollar return of buying leg i: pays (1/q - 1) on win, -1 on loss.
    mu = (p - q) / q                       # expected return = edge / price
    sigma = np.sqrt(np.clip(p * (1.0 - p), 0.0, None)) / q   # Bernoulli std, scaled

    if corr is None:
        corr = np.full((n, n), float(rho))
        np.fill_diagonal(corr, 1.0)
    else:
        corr = np.asarray(corr, dtype=float)

    cov = corr * np.outer(sigma, sigma)
    cov += np.eye(n) * 1e-9                 # ridge for invertibility
    try:
        f = np.linalg.solve(cov, mu)
    except np.linalg.LinAlgError:
        f = mu / np.diag(cov)              # fall back to independent Kelly
    return np.clip(f, 0.0, None)


def correlated_stakes(probs, prices, corr=None,
                      bankroll: float = BANKROLL,
                      fraction: float = KELLY_FRACTION,
                      cap: float | None = None,
                      rho: float = CORR_KELLY_RHO) -> list[float]:
    """USDC stakes for simultaneous correlated bets: fractional, covariance-shrunk
    Kelly, with a total-bankroll guard and per-market cap."""
    if cap is None:
        cap = market_cap(bankroll)
    f = correlation_kelly(probs, prices, corr, rho) * fraction
    total = f.sum()
    if total > 1.0:                        # never stake more than the bankroll
        f = f / total
    return [round(min(fi * bankroll, cap), 2) for fi in f]
