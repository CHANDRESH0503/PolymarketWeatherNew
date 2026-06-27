"""Polymarket liquidity-rewards scoring (CLOB v2 maker rebates).

Implements the documented maker-reward score so the LP engine can (a) decide
which buckets are worth quoting, (b) size the two sides to MAXIMISE the score,
and (c) estimate the daily USDC rebate. Money rides on this being exact, so it
mirrors the published formula 1:1.  Source:
https://docs.polymarket.com/market-makers/liquidity-rewards

Per-order score (v, s in CENTS; b = in-game multiplier, 1.0 for weather):

    S(v, s) = ((v - s) / v) ** 2 * b      for 0 <= s <= v        (else 0)

A side's score is Q = Σ S(v, s_i) * size_i over its resting orders. With Q_one
and Q_two the two sides (bid-side vs ask-side liquidity across both outcomes),
the market's qualifying score is

    mid in [0.10, 0.90]:  Q_min = max(min(Q1,Q2), max(Q1,Q2)/3)   # 1-sided => /3
    else:                 Q_min = min(Q1, Q2)                      # must be 2-sided

Daily payout for a market = pool * Q_min_self / Σ_makers Q_min, sampled ~every
minute and summed over the day. We can't see competitors, so reward ESTIMATES
take their aggregate qualifying score as a parameter `competition`.

Two consequences the engine exploits:
  * Q_min is maximised, for a fixed total size, by BALANCING the two sides
    (min(Q1,Q2) peaks at Q1==Q2). At equal spread that means equal share size.
  * S is quadratic in closeness to mid, so tighter quotes score far more — quote
    as tight as the fair value safely allows, not at the band edge.
"""
from __future__ import annotations

C_ONE_SIDED = 3.0          # one-sided penalty divisor (docs: c = 3.0)
BAND_LO, BAND_HI = 0.10, 0.90   # midpoint band where one-sided still earns (1/c)


def order_score(v_cents: float, s_cents: float, b: float = 1.0) -> float:
    """Per-share score S(v,s) for one resting order. v = max_incentive_spread,
    s = the order's spread from the midpoint (both in cents). Zero outside band."""
    if v_cents <= 0 or s_cents < 0 or s_cents > v_cents:
        return 0.0
    return ((v_cents - s_cents) / v_cents) ** 2 * b


def side_score(orders: list[tuple[float, float]], v_cents: float,
               min_size: float = 0.0, b: float = 1.0) -> float:
    """Σ S(v, s_i) * size_i for one side. `orders` = [(spread_cents, size), ...].
    Orders below `min_size` score zero (Polymarket's min_incentive_size rule)."""
    return sum(order_score(v_cents, s, b) * size
               for s, size in orders if size >= min_size)


def q_min(q_one: float, q_two: float, mid: float) -> float:
    """Qualifying score from the two side scores, band-aware (see module docs)."""
    if BAND_LO <= mid <= BAND_HI:
        return max(min(q_one, q_two), max(q_one, q_two) / C_ONE_SIDED)
    return min(q_one, q_two)


def balanced_two_sided(size_per_side: float, spread_cents: float,
                       v_cents: float, b: float = 1.0) -> float:
    """Q_min for the engine's canonical quote: equal `size_per_side` on each side
    at the same `spread_cents` from mid — the score-maximising shape for a fixed
    total size. Returns the per-market qualifying score (in [0.10,0.90] band)."""
    s = order_score(v_cents, spread_cents, b) * size_per_side
    return q_min(s, s, mid=0.5)


def estimate_daily_reward(q_min_self: float, daily_pool: float,
                          competition: float) -> float:
    """USDC/day we'd earn: pool * our_share. `competition` is the summed Q_min of
    every OTHER maker in the market (unobservable live — pass an assumption)."""
    denom = q_min_self + max(competition, 0.0)
    if denom <= 0:
        return 0.0
    return daily_pool * q_min_self / denom
