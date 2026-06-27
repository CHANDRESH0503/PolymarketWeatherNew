"""Smart-money agreement signal.

We profiled the top Polymarket weather traders (see WALLETS.md). A few of them —
notably `automatedAItradingbot` — trade *our exact* daily-high markets on the same
day-before horizon and are currently profitable. Rather than copy them (they carry
losers too), we use their live entries as a second opinion: for each of our
candidate trades, did a proven peer just take the *same* side, the *opposite*
side, or stay out?

Matching is exact and model-free: Polymarket reports each trade's `asset` (the
CLOB token id), which is the same id `gamma.parse_event` gives our markets. So we
index peer trades by token id and read off net exposure per outcome — no slug
parsing, no station mapping.

Advisory by design: `agreement()` returns a label, and `size_multiplier()` turns
it into a gentle stake nudge. It never creates or vetoes a trade on its own.
"""
from __future__ import annotations

import time

from ..config import PEER_WALLETS, PEER_LOOKBACK_HOURS
from ..polymarket.data_api import get_activity
from ..polymarket.gamma import TempMarket

# How a confirm/against label nudges the stake. Deliberately mild — confirmation
# is a tilt, not a green light; disagreement trims but doesn't veto.
CONFIRM_MULT = 1.15
AGAINST_MULT = 0.60
MIN_PEER_USDC = 5.0   # ignore dust; a peer must have put real size on the token


def fetch_peer_book(wallets: list[str] | None = None,
                    hours: float | None = None) -> dict[str, dict]:
    """Aggregate recent peer trades into {token_id: {net, buy, sell, n, price}}.

    `net` is signed USDC (BUY +, SELL −) across all peers — positive means the
    peers are net-long that token (outcome)."""
    wallets = PEER_WALLETS if wallets is None else wallets
    hours = PEER_LOOKBACK_HOURS if hours is None else hours
    cutoff = time.time() - hours * 3600
    book: dict[str, dict] = {}
    for w in wallets:
        try:
            acts = get_activity(w, limit=500)
        except Exception:  # noqa: BLE001 — a flaky peer fetch must not break scanning
            continue
        for a in acts:
            if a.get("type") != "TRADE" or a.get("timestamp", 0) < cutoff:
                continue
            tok = a.get("asset")
            if not tok:
                continue
            usdc = float(a.get("usdcSize", 0) or 0)
            is_buy = a.get("side") == "BUY"
            e = book.setdefault(tok, {"net": 0.0, "buy": 0.0, "sell": 0.0,
                                      "n": 0, "price": a.get("price")})
            e["net"] += usdc if is_buy else -usdc
            e["buy" if is_buy else "sell"] += usdc
            e["n"] += 1
    return book


def _net_long(book: dict, token_id: str) -> bool:
    e = book.get(token_id)
    return bool(e and e["net"] >= MIN_PEER_USDC)


def agreement(book: dict, market: TempMarket, side: str) -> str:
    """Peer stance on *our* chosen side for this market.

    'confirm'  — peer net-long the same outcome we're buying
    'against'  — peer net-long the opposite outcome
    'mixed'    — peer active on both outcomes
    '-'        — peer hasn't traded this market in the lookback window
    """
    if not book:
        return "-"
    peer_yes = _net_long(book, market.yes_token_id)
    peer_no = _net_long(book, market.no_token_id)
    if not (peer_yes or peer_no):
        return "-"
    if peer_yes and peer_no:
        return "mixed"
    peer_side = "Yes" if peer_yes else "No"
    return "confirm" if peer_side == side else "against"


def size_multiplier(label: str) -> float:
    if label == "confirm":
        return CONFIRM_MULT
    if label == "against":
        return AGAINST_MULT
    return 1.0
