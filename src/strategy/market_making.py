"""Fair-value market making for Polymarket liquidity rewards (Tier-1 edge #2).

Why this is the live opportunity: the weather bucket books are *wide* (we
measured Σask≈1.04-1.15 vs Σbid≈0.85-1.02 — spreads of 6-25%). Polymarket pays
makers daily for two-sided quotes within `rewardsMaxSpread` of the midpoint
(docs.polymarket.com/market-makers/liquidity-rewards). So instead of betting
direction we can quote both sides and earn the spread + rewards.

The trick is to quote around OUR model's fair value, not blindly around the
mid — otherwise we get adversely selected (informed flow lifts the side we
mispriced). We center quotes on `fair`, clamp them into the reward band around
the mid, and flag buckets where our fair disagrees with the mid by more than the
band (there we'd lean one-sided or skip).

This module is an ADVISOR: it produces a quote sheet. Execution stays guarded in
clob.py (DRY_RUN by default).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import STATIONS, CALIBRATION
from ..forecast.openmeteo import fetch_max_temp_distribution, MaxTempForecast
from ..forecast.model import yes_probability, apply_calibration
from ..polymarket.clob import get_books, best_ask, best_bid
from ..polymarket.gamma import parse_event
from . import lp_rewards


TICK = 0.01


def reward_params(ev: dict) -> tuple[float, float, float]:
    """(band, min_size, daily_pool) for an event's reward program, read from the
    Gamma market payload. band = rewardsMaxSpread in PRICE units (cents/100)."""
    raw = ev.get("markets", [{}])[0]
    band = float(raw.get("rewardsMaxSpread", 3.0)) / 100.0
    min_size = float(raw.get("rewardsMinSize", 0) or 0)
    daily = float(raw.get("rewardsDailyRate", 0) or 0)
    return band, min_size, daily


@dataclass
class MMQuote:
    label: str
    fair: float                 # our model P(Yes)
    mid: float                  # current book midpoint
    cur_spread: float | None    # current best ask - best bid (None if one-sided)
    bid: float | None           # our suggested bid (None when ask-only lean)
    ask: float | None           # our suggested ask (None when bid-only lean)
    reward_eligible: bool
    lean: str                   # "" | "bid-only" | "ask-only"
    edge_vs_mid: float          # fair - mid
    size: float = 0.0           # shares quoted PER SIDE (balanced => max Q_min)
    reward_score: float = 0.0   # this bucket's Q_min for the quoted size
    yes_token: str = ""         # for execution: bid = BUY yes_token @ bid
    no_token: str = ""          #                 ask = BUY no_token @ (1-ask)

    def __str__(self) -> str:
        flag = "✓rwd" if self.reward_eligible else " ·  "
        lean = f" [{self.lean}]" if self.lean else ""
        q = (f"{self.bid:.2f}" if self.bid is not None else "  - ") + "/" + \
            (f"{self.ask:.2f}" if self.ask is not None else "  - ")
        spr = f"{self.cur_spread:.2f}" if self.cur_spread is not None else " n/a"
        sc = f"  score={self.reward_score:.3f}" if self.reward_eligible else ""
        return (f"  {self.label:>5}  {self.fair:5.2f}  {self.mid:5.2f}  "
                f"{spr:>5}  {q:>11}  {flag}{lean}{sc}")


def _forecast(ev_markets) -> MaxTempForecast | None:
    m = next((x for x in ev_markets if x.station_code in STATIONS), None)
    if not m:
        return None
    s = STATIONS[m.station_code]
    fc = fetch_max_temp_distribution(s["lat"], s["lon"], m.end_date[:10],
                                     s["tz"], m.station_code)
    return apply_calibration(fc, CALIBRATION)


def quote_event(ev: dict, half_spread: float = 0.02,
                size_per_side: float = 0.0) -> list[MMQuote]:
    """Suggest two-sided maker quotes for every bucket in an event.

    `size_per_side` (shares) is placed equally on bid and ask — the shape that
    maximises the reward Q_min for a fixed size (see lp_rewards). Each eligible
    quote gets its `reward_score` so the engine can rank/skip buckets."""
    markets = parse_event(ev)
    if not markets:
        return []
    fc = _forecast(markets)
    if fc is None:
        return []

    band, min_size, _daily = reward_params(ev)   # band in price units
    v_cents = band * 100.0                        # max_incentive_spread in cents
    qty = max(size_per_side, min_size)            # honour min_incentive_size

    books = get_books([m.yes_token_id for m in markets])
    out: list[MMQuote] = []
    for m in sorted(markets, key=lambda x: x.threshold_c):
        fair = yes_probability(fc, m.bucket_kind, m.threshold_c)
        bk = books.get(m.yes_token_id)
        ba, bb = best_ask(bk), best_bid(bk)
        two_sided = ba is not None and bb is not None
        # reference midpoint: book mid when two-sided, else our fair value
        mid = (ba[0] + bb[0]) / 2 if two_sided else fair
        cur_spread = (ba[0] - bb[0]) if two_sided else None

        edge = fair - mid
        lean = "bid-only" if edge > band else "ask-only" if edge < -band else ""

        # quotes centered on fair, clamped into the reward band and to valid ticks
        def clamp(p):
            return round(min(1 - TICK, max(TICK, p)), 2)
        bid = clamp(max(fair - half_spread, mid - band))
        ask = clamp(min(fair + half_spread, mid + band))
        if ask <= bid:                       # avoid crossed/locked quotes
            ask = clamp(bid + TICK)

        # when we strongly disagree with the book, quote only the safe side
        if lean == "bid-only":
            ask = None
        elif lean == "ask-only":
            bid = None

        eligible = (not lean and two_sided
                    and bid is not None and ask is not None
                    and bid >= mid - band - 1e-9 and ask <= mid + band + 1e-9)

        # Reward score for the quoted size: balanced sizes at each side's spread
        # from mid (cents). Only meaningful when eligible (two-sided, in band).
        score = 0.0
        if eligible:
            q_bid = lp_rewards.order_score(v_cents, (mid - bid) * 100.0) * qty
            q_ask = lp_rewards.order_score(v_cents, (ask - mid) * 100.0) * qty
            score = lp_rewards.q_min(q_bid, q_ask, mid)

        out.append(MMQuote(f"{m.threshold_c}°", fair, round(mid, 3),
                           round(cur_spread, 3) if cur_spread is not None else None,
                           bid, ask, eligible, lean, round(edge, 3),
                           round(qty, 2), round(score, 4),
                           m.yes_token_id, m.no_token_id))
    return out
