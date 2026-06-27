"""Read-only access to the Polymarket Data API (positions, activity, PnL).

Used both for tracking our own bot's positions and for studying other traders
(e.g. the @lactesting profile this project was modeled on).
"""
from __future__ import annotations

import requests

from ..config import DATA_API


def get_positions(wallet: str, size_threshold: float = 0.1) -> list[dict]:
    r = requests.get(
        f"{DATA_API}/positions",
        params={"user": wallet, "sizeThreshold": size_threshold,
                "limit": 500, "sortBy": "CURRENT", "sortDirection": "DESC"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def get_activity(wallet: str, limit: int = 500) -> list[dict]:
    r = requests.get(
        f"{DATA_API}/activity",
        params={"user": wallet, "limit": limit,
                "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def resolve_username(username: str) -> str | None:
    """Best-effort username -> proxy wallet. The public HTML embeds the wallet
    as `"proxyWallet":"0x..."`; we scrape that since the JSON endpoints are gated.
    """
    import re
    html = requests.get(
        f"https://polymarket.com/@{username}",
        headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
    ).text
    m = re.search(r'"proxyWallet":"(0x[a-fA-F0-9]{40})"', html)
    return m.group(1) if m else None
