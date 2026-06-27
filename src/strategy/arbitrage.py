"""Coherence / negative-risk arbitrage on weather events.

The buckets in one "highest temperature in <city>" event are mutually exclusive
and exhaustive — exactly one degree range contains the day's max. Therefore:

        sum_i  P(YES_i) == 1

So in the order book:
  * if  sum_i best_ask(YES_i) < 1  -> buy 1 share of EVERY bucket's YES for
    `cost`; exactly one resolves to $1, the rest to $0 -> riskless profit (1-cost).
  * if  sum_i best_bid(YES_i) > 1  -> the basket is over-priced (sell side; needs
    minted shares / the neg-risk convert), flagged for completeness.

This is a *non-forecast* edge: pure linear algebra on live quotes, no model. A
2025 study found ~42% of Polymarket negative-risk markets showed such
opportunities, median ~60c/$ (arXiv:2508.03474), and Polymarket charges no fees.

Caveats: requires a quote on EVERY bucket to complete the long basket; executable
size is bounded by the thinnest leg; deep baskets move the book (we size to the
best level only — conservative).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..polymarket.clob import get_books, best_ask, best_bid
from ..polymarket.gamma import parse_event, TempMarket


@dataclass
class ArbOpportunity:
    event_slug: str
    kind: str            # "underpriced" (buy-all-YES) | "overpriced" (sell-all-YES)
    n_buckets: int
    price_sum: float     # sum of best asks (under) or best bids (over)
    profit_per_basket: float
    roi_pct: float       # profit / capital deployed
    max_size: float      # shares of each leg executable at the best level
    est_profit: float    # profit_per_basket * max_size
    missing_quotes: int  # legs with no quote (0 ⇒ basket completable)
    legs: list = None    # [(token_id, ask_price)] for the buy-all-YES basket

    def __str__(self) -> str:
        return (f"[{self.kind:11}] {self.event_slug[:46]:46} "
                f"Σ={self.price_sum:.3f}  edge={self.profit_per_basket*100:+.1f}%  "
                f"ROI={self.roi_pct:+.1f}%  size≈{self.max_size:.0f}  "
                f"≈${self.est_profit:.2f}"
                + (f"  (+{self.missing_quotes} legs unquoted)" if self.missing_quotes else ""))


def scan_event(ev: dict, min_edge: float = 0.015) -> list[ArbOpportunity]:
    markets: list[TempMarket] = parse_event(ev)
    if len(markets) < 3:
        return []
    slug = ev.get("slug", "")
    books = get_books([m.yes_token_id for m in markets])

    asks, bids = [], []
    ask_sizes, bid_sizes = [], []
    legs = []
    missing_ask = missing_bid = 0
    for m in markets:
        bk = books.get(m.yes_token_id)
        ba, bb = best_ask(bk), best_bid(bk)
        if ba:
            asks.append(ba[0]); ask_sizes.append(ba[1])
            legs.append((m.yes_token_id, ba[0]))
        else:
            missing_ask += 1
        if bb:
            bids.append(bb[0]); bid_sizes.append(bb[1])
        else:
            missing_bid += 1

    out: list[ArbOpportunity] = []
    n = len(markets)

    # Long basket: buy every YES. Only completable if every leg has an ask.
    if missing_ask == 0 and asks:
        cost = sum(asks)
        if cost < 1 - min_edge:
            size = min(ask_sizes)
            profit = 1 - cost
            out.append(ArbOpportunity(
                slug, "underpriced", n, cost, profit,
                profit / cost * 100, size, profit * size, 0, legs))

    # Short basket flag: sum of bids > 1 (needs minted shares / convert).
    if missing_bid == 0 and bids:
        rev = sum(bids)
        if rev > 1 + min_edge:
            size = min(bid_sizes)
            profit = rev - 1
            out.append(ArbOpportunity(
                slug, "overpriced", n, rev, profit,
                profit * 100, size, profit * size, 0, None))

    return out


def execute_opportunity(opp: ArbOpportunity, max_capital: float) -> dict:
    """Place the buy-all-YES basket. Routes through clob.place_order, which is
    DRY_RUN-guarded — no real order unless DRY_RUN=0 + funded PK + ARB_EXECUTE.

    PARTIAL-FILL RISK: the basket is only riskless if EVERY leg fills. We place
    each leg as a limit buy at its quoted ask; if some legs miss, you hold
    directional exposure. We report fills so a supervisor can react.
    """
    from ..polymarket.clob import place_order  # local import (optional dep)

    if opp.kind != "underpriced" or not opp.legs:
        return {"skipped": "not an executable long basket"}

    # size each leg equally; cap total capital at min(depth, max_capital/cost)
    by_cost = max_capital / max(opp.price_sum, 1e-6)
    size = max(0.0, min(opp.max_size, by_cost))
    if size < 1:
        return {"skipped": f"size {size:.2f} below 1 share"}

    results = []
    for token_id, price in opp.legs:
        stake = round(size * price, 2)
        results.append(place_order(token_id, "BUY", price, stake))
    filled = sum(1 for r in results if r)
    return {"legs": len(opp.legs), "submitted": filled, "size": round(size, 2),
            "cost": round(size * opp.price_sum, 2),
            "expected_profit": round(size * opp.profit_per_basket, 2),
            "partial_fill_risk": filled < len(opp.legs)}
