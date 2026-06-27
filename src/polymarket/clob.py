"""Order execution via the Polymarket CLOB.

This is intentionally a thin, GUARDED wrapper. Live trading is OFF unless you
(1) fill in wallet creds in .env and (2) set DRY_RUN=0. By default every order
is logged, not sent.

Setup once:
    python -m src.polymarket.clob --create-api-key
This derives CLOB_API_KEY/SECRET/PASSPHRASE from your PK; paste them into .env.
"""
from __future__ import annotations

import math

import requests

from ..config import (CLOB_API, PK, POLY_PROXY_ADDRESS, DRY_RUN, SIGNATURE_TYPE,
                      CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE)

# Decimal places implied by each CLOB tick size (price grid resolution).
_TICK_DECIMALS = {"0.1": 1, "0.01": 2, "0.001": 3, "0.0001": 4}
# Shares ("size"/taker amount) accuracy the CLOB accepts is 2 decimals for every
# tick (RoundConfig.size == 2). Over-precise amounts are rejected:
#   "the market buy orders maker amount supports a max accuracy of 2 decimals,
#    taker amount a max of 4 decimals"
_SHARE_DECIMALS = 2

# Per-token market metadata cache so we don't re-fetch tick/neg_risk every order.
_META_CACHE: dict[str, tuple[str, bool]] = {}


def _round_down(x: float, decimals: int) -> float:
    """Truncate toward zero so a sized stake never rounds UP past its budget."""
    f = 10 ** decimals
    return math.floor(x * f) / f


def _align_price(price: float, tick: str) -> float:
    """Snap a price onto the market's tick grid and clamp into (tick, 1-tick).
    Sending an off-grid price is the other half of the 'invalid amounts'
    rejection — the maker amount must land on the rounding the CLOB expects."""
    d = _TICK_DECIMALS.get(tick, 2)
    t = float(tick)
    q = round(round(price / t) * t, d)
    return min(max(q, t), round(1 - t, d))


def _market_meta(client, token_id: str) -> tuple[str, bool]:
    """(tick_size, neg_risk) for a token, cached. Passing both into create_order
    makes amount rounding deterministic (no per-order re-fetch / guesswork) and
    routes neg-risk markets to the right exchange contract."""
    meta = _META_CACHE.get(token_id)
    if meta is None:
        tick = str(client.get_tick_size(token_id))
        neg = bool(client.get_neg_risk(token_id))
        meta = _META_CACHE[token_id] = (tick, neg)
    return meta


# ---- read-only order book (no auth needed) --------------------------------
def get_books(token_ids: list[str]) -> dict[str, dict]:
    """Fetch live order books for many tokens in one batched call.
    Returns {token_id: book}, where book has 'bids' and 'asks' (price/size)."""
    if not token_ids:
        return {}
    r = requests.post(f"{CLOB_API}/books",
                      json=[{"token_id": t} for t in token_ids], timeout=20)
    r.raise_for_status()
    return {b.get("asset_id"): b for b in r.json()}


def best_ask(book: dict | None) -> tuple[float, float] | None:
    """(price, size) of the lowest ask — the price/size we could BUY at."""
    asks = (book or {}).get("asks") or []
    if not asks:
        return None
    a = min(asks, key=lambda x: float(x["price"]))
    return float(a["price"]), float(a["size"])


def best_bid(book: dict | None) -> tuple[float, float] | None:
    """(price, size) of the highest bid — the price/size we could SELL at."""
    bids = (book or {}).get("bids") or []
    if not bids:
        return None
    b = max(bids, key=lambda x: float(x["price"]))
    return float(b["price"]), float(b["size"])


def walk_asks(book: dict | None, limit_price: float, budget_usdc: float
              ) -> tuple[float, float, float]:
    """Simulate a marketable BUY: spend up to `budget_usdc`, taking asks priced
    at or below `limit_price`, cheapest first. Returns (shares, avg_price, cost).

    This is what makes a paper fill realistic — you cross the spread and eat depth
    rather than magically filling the whole size at the top-of-book quote. Returns
    (0, 0, 0) if nothing is takeable within the limit."""
    asks = sorted(((float(a["price"]), float(a["size"]))
                   for a in (book or {}).get("asks") or []), key=lambda x: x[0])
    shares = cost = 0.0
    remaining = budget_usdc
    for price, size in asks:
        if price > limit_price + 1e-9 or remaining <= 1e-9:
            break
        take = min(size, remaining / price)       # shares we can afford at this level
        if take <= 0:
            break
        shares += take
        cost += take * price
        remaining -= take * price
    avg = cost / shares if shares > 0 else 0.0
    return round(shares, 4), round(avg, 5), round(cost, 4)


def _client():
    """Lazily build a py-clob-client. Imported here so the rest of the bot runs
    without the trading dependency installed."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    client = ClobClient(
        CLOB_API, key=PK, chain_id=137,
        signature_type=SIGNATURE_TYPE, funder=POLY_PROXY_ADDRESS,
    )
    if CLOB_API_KEY:
        client.set_api_creds(ApiCreds(CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE))
    return client


def get_balance() -> dict:
    """Read the wallet's collateral (pUSD) balance + allowance from the CLOB.

    Uses the configured SIGNATURE_TYPE/funder, so it's the exact probe that
    confirmed the smart-wallet account (sig=3 -> $200.53 pUSD). Requires CLOB API
    creds. Returns {'balance': float_usd, 'raw': ..., 'allowances': ...} or
    raises with a clear message if creds/deps are missing."""
    if not (PK and CLOB_API_KEY):
        raise RuntimeError(
            "balance check needs PK + CLOB_API_KEY/SECRET/PASSPHRASE in .env "
            "(derive with: python -m src.polymarket.clob --create-api-key)")
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    client = _client()
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL, signature_type=SIGNATURE_TYPE)
    resp = client.get_balance_allowance(params)
    raw = resp.get("balance") if isinstance(resp, dict) else None
    bal = int(raw) / 1e6 if raw not in (None, "") else None
    return {"balance": bal, "raw": raw, "signature_type": SIGNATURE_TYPE,
            "funder": POLY_PROXY_ADDRESS, "response": resp}


def create_api_key() -> None:
    client = _client()
    creds = client.create_or_derive_api_creds()
    print("Add these to .env:")
    print(f"CLOB_API_KEY={creds.api_key}")
    print(f"CLOB_API_SECRET={creds.api_secret}")
    print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")


def place_order(token_id: str, side: str, price: float, size_usdc: float) -> dict:
    """Buy `size_usdc` worth of `token_id` at limit `price`.

    side is always BUY here (we buy Yes or No tokens directly). Returns the API
    response, or a dry-run stub.
    """
    if DRY_RUN or not PK:
        shares = _round_down(size_usdc / price, _SHARE_DECIMALS)
        order = {"token_id": token_id, "side": "BUY",
                 "price": round(price, 4), "size": shares}
        print(f"   [DRY_RUN] would place {order}")
        return {"dry_run": True, **order}

    from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY

    client = _client()
    tick, neg_risk = _market_meta(client, token_id)
    px = _align_price(price, tick)
    shares = _round_down(size_usdc / px, _SHARE_DECIMALS)   # taker amount, ≤2 dp
    if shares <= 0:
        print(f"   ! stake ${size_usdc:.2f} @ {px} rounds to 0 shares; skipping")
        return {"error": "size_rounds_to_zero", "token_id": token_id}

    # Explicit tick_size + neg_risk → deterministic amount rounding (maker amount
    # within the CLOB's accepted accuracy). Default post_order type is GTC; we
    # never pass time_in_force (that kwarg isn't supported and raises).
    signed = client.create_order(
        OrderArgs(token_id=token_id, price=px, size=shares, side=BUY),
        PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk))
    resp = client.post_order(signed)
    print(f"   [LIVE] order resp: {resp}")
    return resp


def place_maker(token_id: str, price: float, shares: float) -> dict:
    """Post a resting BUY limit (maker) order for `shares` at `price`.
    DRY_RUN-guarded like place_order. Used by the LP daemon for two-sided quoting
    (bid = buy YES; ask = buy NO at 1-ask)."""
    if DRY_RUN or not PK:
        order = {"token_id": token_id, "side": "BUY", "price": round(price, 4),
                 "size": _round_down(shares, _SHARE_DECIMALS)}
        print(f"   [DRY_RUN] would quote {order}")
        return {"dry_run": True, **order}

    from py_clob_client.clob_types import (OrderArgs, PartialCreateOrderOptions,
                                           OrderType)
    from py_clob_client.order_builder.constants import BUY

    client = _client()
    tick, neg_risk = _market_meta(client, token_id)
    px = _align_price(price, tick)
    sz = _round_down(shares, _SHARE_DECIMALS)
    if sz <= 0:
        return {"error": "size_rounds_to_zero", "token_id": token_id}
    signed = client.create_order(
        OrderArgs(token_id=token_id, price=px, size=sz, side=BUY),
        PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk))
    # post_only: a maker quote must rest, never cross into a taker fill.
    return client.post_order(signed, OrderType.GTC, post_only=True)


def cancel_all() -> dict:
    """Cancel all open orders (clean slate before re-quoting). DRY_RUN-guarded."""
    if DRY_RUN or not PK:
        print("   [DRY_RUN] would cancel all open orders")
        return {"dry_run": True}
    return _client().cancel_all()


if __name__ == "__main__":
    import sys
    if "--create-api-key" in sys.argv:
        create_api_key()
    elif "--balance" in sys.argv:
        try:
            info = get_balance()
            print(f"funder         = {info['funder']}")
            print(f"signature_type = {info['signature_type']}")
            print(f"collateral     = {info['balance']} pUSD  (raw {info['raw']})")
        except Exception as e:  # noqa: BLE001
            print(f"balance check failed: {e}")
            sys.exit(1)
    else:
        print(__doc__)
