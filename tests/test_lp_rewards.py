"""Sanity checks for the liquidity-reward scoring (real money rides on this)."""
from src.strategy import lp_rewards as R


def test_order_score_endpoints():
    # At the midpoint (s=0) score is the in-game multiplier; at the band edge it
    # is 0; quadratic in between; zero outside the band.
    assert R.order_score(3, 0) == 1.0
    assert R.order_score(3, 3) == 0.0
    assert R.order_score(3, 4) == 0.0           # past max spread -> 0
    assert R.order_score(3, 1.5) == 0.25        # ((3-1.5)/3)^2 = 0.25
    assert R.order_score(0, 0) == 0.0           # no band -> 0


def test_qmin_balanced_beats_one_sided_in_band():
    # In [0.10,0.90], one-sided is penalised by c=3; balanced earns the full min.
    assert R.q_min(1.0, 1.0, mid=0.5) == 1.0
    assert R.q_min(1.0, 0.0, mid=0.5) == 1.0 / 3.0
    # balanced (Q1=Q2=0.5) beats all-on-one-side (1.0,0.0) for the same total 1.0
    assert R.q_min(0.5, 0.5, mid=0.5) > R.q_min(1.0, 0.0, mid=0.5)


def test_qmin_outside_band_requires_two_sided():
    assert R.q_min(1.0, 0.0, mid=0.95) == 0.0   # one-sided near extreme earns nothing
    assert R.q_min(1.0, 1.0, mid=0.95) == 1.0


def test_balanced_sizing_maximises_qmin():
    # For a fixed total size, splitting evenly across the two sides maximises
    # Q_min vs any lopsided split (at equal spread, score ∝ size).
    v, s, total = 3.0, 1.0, 100.0
    even = R.q_min(R.order_score(v, s) * total / 2,
                   R.order_score(v, s) * total / 2, mid=0.5)
    lop = R.q_min(R.order_score(v, s) * total * 0.8,
                  R.order_score(v, s) * total * 0.2, mid=0.5)
    assert even >= lop
    assert R.balanced_two_sided(total / 2, s, v) == even


def test_estimate_daily_reward_share():
    # pool split by our share of total qualifying score.
    assert R.estimate_daily_reward(1.0, 100.0, competition=1.0) == 50.0
    assert R.estimate_daily_reward(1.0, 100.0, competition=0.0) == 100.0
    assert R.estimate_daily_reward(0.0, 100.0, competition=1.0) == 0.0
