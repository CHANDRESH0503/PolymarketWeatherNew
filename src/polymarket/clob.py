"""Order execution via the Polymarket CLOB.

This is intentionally a thin, GUARDED wrapper. Live trading is OFF unless you
(1) fill in wallet creds in .env and (2) set DRY_RUN=0. By default every order
is logged, not sent.

Setup once:
    python -m src.polymarket.clob --create-api-key
This derives CLOB_API_KEY/SECRET/PASSPHRASE from your PK; paste them into .env.
"""
from __future__ import annotations

import requests

from ..config import (CLOB_API, PK, POLY_PROXY_ADDRESS, DRY_RUN,
                      CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE)


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
        signature_type=2, funder=POLY_PROXY_ADDRESS,
    )
    if CLOB_API_KEY:
        client.set_api_creds(ApiCreds(CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE))
    return client


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
    shares = round(size_usdc / price, 2)
    order = {"token_id": token_id, "side": "BUY", "price": round(price, 3), "size": shares}

    if DRY_RUN or not PK:
        print(f"   [DRY_RUN] would place {order}")
        return {"dry_run": True, **order}

    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY

    client = _client()
    signed = client.create_order(OrderArgs(
        token_id=token_id, price=round(price, 3), size=shares, side=BUY))
    resp = client.post_order(signed)
    print(f"   [LIVE] order resp: {resp}")
    return resp


def place_maker(token_id: str, price: float, shares: float) -> dict:
    """Post a resting BUY limit (maker) order for `shares` at `price`.
    DRY_RUN-guarded like place_order. Used by the LP daemon for two-sided quoting
    (bid = buy YES; ask = buy NO at 1-ask)."""
    order = {"token_id": token_id, "side": "BUY", "price": round(price, 3),
             "size": round(shares, 2)}
    if DRY_RUN or not PK:
        print(f"   [DRY_RUN] would quote {order}")
        return {"dry_run": True, **order}

    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY

    client = _client()
    signed = client.create_order(OrderArgs(
        token_id=token_id, price=round(price, 3), size=round(shares, 2), side=BUY))
    return client.post_order(signed)


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
    else:
        print(__doc__)
