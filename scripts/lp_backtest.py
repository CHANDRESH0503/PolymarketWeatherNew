"""Backtest the liquidity-provision (reward market-making) engine on the trades
we already have — the paper-run book in data/paper.db (or any --db copy).

It does NOT need historical order books. For every distinct market we touched
(known model fair value + known Polymarket resolution), it simulates the engine
posting BALANCED two-sided maker quotes at ±half_spread around our fair value,
inside the reward band, and reports:

  * how many market-days are reward-ELIGIBLE (fair in the [0.10,0.90] band),
  * the qualifying reward SCORE and the capital those resting quotes tie up,
  * the maker INVENTORY P&L band from the real resolutions — best case (both
    sides fill: capture the spread) to worst case (only the losing side fills:
    full adverse selection),
  * an estimated $ reward IF you supply the per-market daily pool (--daily-pool),
    with a competition sensitivity (you can't see other makers live).

Honest by construction: the spread/adverse P&L is exact given resolutions; the
$ reward is assumption-driven and labelled as such (the LIVE daemon prints real
$ from each market's rewardsDailyRate).

    python scripts/lp_backtest.py --db ~/Downloads/paper.db
    python scripts/lp_backtest.py --half-spread 0.015 --size 50 --daily-pool 5
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.strategy.lp_rewards import balanced_two_sided, estimate_daily_reward, BAND_LO, BAND_HI


def load_markets(db: str) -> list[dict]:
    """One row per distinct settled market: its model fair P(Yes) and whether
    YES won. Buckets touched by multiple fills are collapsed to one quote."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT condition_id,
                  AVG(model_prob)        AS fair,
                  MAX(resolved_yes)      AS resolved_yes,
                  substr(MAX(end_date),1,10) AS day,
                  MAX(city)              AS city
             FROM fills
            WHERE status='settled' AND resolved_yes IS NOT NULL
              AND model_prob IS NOT NULL
            GROUP BY condition_id""").fetchall()
    con.close()
    return [dict(r) for r in rows]


def simulate(markets: list[dict], half_spread: float, band: float,
             size: float) -> dict:
    """Aggregate the engine's reward score, capital, and inventory-P&L band."""
    hs, v_cents = half_spread, band * 100.0
    spread_cents = hs * 100.0
    agg = {"n": 0, "eligible": 0, "score": 0.0, "capital": 0.0,
           "both_fill_pnl": 0.0, "adverse_pnl": 0.0, "days": set(),
           "per_market_score": 0.0}
    for m in markets:
        fair = m["fair"]
        agg["n"] += 1
        agg["days"].add(m["day"])
        eligible = BAND_LO <= fair <= BAND_HI
        if not eligible:
            continue
        agg["eligible"] += 1
        bid, ask = fair - hs, fair + hs                 # buy YES @ bid, buy NO @ 1-ask
        if not (0 < bid and ask < 1):
            continue
        # capital tied if both rest-orders fill: bid + (1-ask) per share = 1-2·hs
        agg["capital"] += size * (1.0 - 2.0 * hs)
        score = balanced_two_sided(size, spread_cents, v_cents)
        agg["score"] += score
        agg["per_market_score"] = score                # identical per market (same shape)
        # both sides fill -> hold 1 YES + 1 NO, redeem $1, profit = ask-bid = 2·hs
        agg["both_fill_pnl"] += 2.0 * hs * size
        # worst case: only the LOSING side fills (full adverse selection)
        if m["resolved_yes"] == 1:                      # YES won -> our NO-buy is the loser
            agg["adverse_pnl"] -= (1.0 - ask) * size
        else:                                           # NO won -> our YES-buy is the loser
            agg["adverse_pnl"] -= bid * size
    return agg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/paper.db")
    ap.add_argument("--half-spread", type=float, default=0.02, help="quote offset from fair")
    ap.add_argument("--band", type=float, default=0.03, help="reward max spread (price units)")
    ap.add_argument("--size", type=float, default=50.0, help="shares per side")
    ap.add_argument("--daily-pool", type=float, default=0.0,
                    help="per-market reward pool $/day (supply to dollarise reward)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"db not found: {args.db}")
    markets = load_markets(args.db)
    if not markets:
        sys.exit("no settled markets with model_prob + resolution in this db")
    a = simulate(markets, args.half_spread, args.band, args.size)
    n_days = max(len(a["days"]), 1)

    print(f"\nLP reward-MM backtest  ({args.db})")
    print(f"  markets touched ....... {a['n']}  over {n_days} day(s)")
    print(f"  reward-eligible ....... {a['eligible']}  "
          f"({100*a['eligible']/max(a['n'],1):.0f}% — fair in [{BAND_LO},{BAND_HI}])")
    print(f"  quote shape ........... ±{args.half_spread:.3f} around fair, "
          f"band {args.band:.3f}, {args.size:.0f} shares/side")
    print(f"  capital deployed ...... ${a['capital']:.2f}  "
          f"(if all rest-orders fill)")
    print(f"  total reward score .... {a['score']:.2f}  "
          f"(Q_min per eligible market = {a['per_market_score']:.4f})")
    print("\n  inventory P&L band (from REAL resolutions):")
    print(f"    best  (both sides fill, capture spread) .. +${a['both_fill_pnl']:.2f}")
    print(f"    worst (only losing side fills, adverse) .. ${a['adverse_pnl']:.2f}")

    if args.daily_pool > 0:
        print(f"\n  estimated reward @ ${args.daily_pool:.2f}/market/day pool "
              f"(assumption — competition sensitivity):")
        per = a["per_market_score"]
        for comp_label, comp in (("alone (100%)", 0.0),
                                 ("even (50%)", per),
                                 ("crowded (20%)", per * 4)):
            daily = sum(estimate_daily_reward(per, args.daily_pool, comp)
                        for _ in range(a["eligible"]))
            apr = (daily * 365 / a["capital"] * 100) if a["capital"] else 0.0
            print(f"    {comp_label:<16} ${daily:.2f}/day  (~{apr:.0f}% APR on capital)")
    else:
        print("\n  (supply --daily-pool to dollarise the reward; the live daemon "
              "reads each market's real rewardsDailyRate.)")
    print("\n  Note: reward is EARNED regardless of fill; the inventory band shows "
          "the directional risk you take on while resting. Net edge = reward + the "
          "spread you actually capture (between the two bounds above).")


if __name__ == "__main__":
    main()
