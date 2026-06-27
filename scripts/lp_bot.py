"""Live LP quoting daemon (Tier-1 edge #2 execution).

Each cycle: cancel stale orders, recompute fair-value quotes for the target
events, and post two-sided maker orders to earn the liquidity-reward spread.

Two-sided quoting on a YES bucket is done with two BUY orders:
    bid  ->  BUY  YES  @ bid
    ask  ->  BUY  NO   @ (1 - ask)
If both fill, you hold 1 YES + 1 NO at total cost (bid + 1 - ask); exactly one
pays $1, so you net (ask - bid) — the captured spread — plus LP rewards on the
resting orders. Leaned buckets post only the safe side.

Buckets are ranked by their reward SCORE (lp_rewards.q_min) and deployed
greedily under LP_MAX_CAPITAL — so the limited bankroll funds the quotes that
earn the most rebate per dollar, not whatever bucket comes first. Each cycle
prints the estimated daily reward (assumption-driven; see LP_COMPETITION).

SAFETY: orders route through clob (DRY_RUN-guarded) AND require LP_EXECUTE=1.
Default = simulation (logs the quotes it would post). To go live you need
DRY_RUN=0 + funded PK + LP_EXECUTE=1.

    python scripts/lp_bot.py --loop 120
    LP_EVENTS=highest-temperature-in-busan-on-june-5-2026 python scripts/lp_bot.py
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import (LP_EXECUTE, LP_SIZE, LP_EVENTS, LP_MAX_CAPITAL,
                        LP_COMPETITION, DRY_RUN)
from src.polymarket.gamma import fetch_open_temperature_events
from src.polymarket.clob import place_maker, cancel_all
from src.strategy.market_making import quote_event, reward_params
from src.strategy.lp_rewards import estimate_daily_reward


def _target_events(events: list[dict]) -> list[dict]:
    if LP_EVENTS.strip():
        wanted = {s.strip() for s in LP_EVENTS.split(",")}
        return [e for e in events if e["slug"] in wanted]
    # default: events whose city we model (the same ones the bot trades)
    from src.config import STATIONS
    out = []
    for e in events:
        from src.polymarket.gamma import parse_event
        if any(m.station_code in STATIONS for m in parse_event(e)):
            out.append(e)
    return out[:4]


def _capital(q) -> float:
    """USDC tied up if this quote's resting orders both fill (both are BUYs):
    bid = buy YES @ bid, ask = buy NO @ (1-ask)."""
    c = 0.0
    if q.bid is not None:
        c += q.bid * q.size
    if q.ask is not None:
        c += (1.0 - q.ask) * q.size
    return c


def cycle(execute: bool) -> None:
    stamp = time.strftime("%H:%M:%S")
    events = _target_events(fetch_open_temperature_events())
    if execute:
        cancel_all()

    # Collect every quotable bucket with its event's reward pool, then rank by
    # reward score so the capital cap funds the highest-earning quotes first.
    cands: list[tuple] = []
    for ev in events:
        _band, _min, daily_pool = reward_params(ev)
        for q in quote_event(ev, size_per_side=LP_SIZE):
            if q.reward_eligible or q.lean:
                cands.append((q, ev["slug"], daily_pool))
    cands.sort(key=lambda c: (c[0].reward_eligible, c[0].reward_score), reverse=True)

    deployed = posted = 0.0
    est_reward = 0.0
    skipped_cap = 0
    for q, slug, daily_pool in cands:
        cap = _capital(q)
        if deployed + cap > LP_MAX_CAPITAL + 1e-9:   # respect the bankroll cap
            skipped_cap += 1
            continue
        if q.bid is not None:                        # BUY YES @ bid
            if execute:
                place_maker(q.yes_token, q.bid, q.size)
            posted += 1
        if q.ask is not None:                        # BUY NO @ (1 - ask)
            if execute:
                place_maker(q.no_token, round(1 - q.ask, 3), q.size)
            posted += 1
        deployed += cap
        if q.reward_eligible and daily_pool > 0:
            est_reward += estimate_daily_reward(q.reward_score, daily_pool,
                                                LP_COMPETITION)
        print(f"  {slug[:38]:38} {q}")

    print(f"[{stamp}] {'posted' if execute else 'would post'} {int(posted)} maker "
          f"orders | capital ${deployed:.2f}/{LP_MAX_CAPITAL:.0f} "
          f"| est reward ${est_reward:.2f}/day"
          + (f" | {skipped_cap} skipped (cap)" if skipped_cap else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0)
    args = ap.parse_args()
    execute = LP_EXECUTE
    mode = ("EXECUTE " + ("(DRY_RUN sim)" if DRY_RUN else "LIVE ORDERS")
            if execute else "quote-only (set LP_EXECUTE=1 to post)")
    print(f"LP quoting daemon — {mode} — size {LP_SIZE}/quote\n")
    if args.loop <= 0:
        cycle(execute)
        return
    while True:
        try:
            cycle(execute)
        except Exception as e:  # noqa: BLE001
            print(f"cycle error: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
