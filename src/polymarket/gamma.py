"""Market discovery via Polymarket's Gamma API.

Finds open daily-temperature events, and parses each sub-market into a typed
bucket (exact / at-or-above / at-or-below) plus the resolution station.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal, Optional

import requests

from ..config import GAMMA_API

_RETRY_STATUS = {429, 500, 502, 503, 504}


def _gamma_get(url: str, params: dict, *, timeout: int = 20,
               retries: int = 4, backoff: float = 2.0) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code in _RETRY_STATUS and attempt < retries - 1:
                wait = float(r.headers.get("Retry-After") or backoff * (2 ** attempt))
                time.sleep(min(wait, 30.0))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            raise
    raise last_exc  # type: ignore[misc]

BucketKind = Literal["exact", "gte", "lte"]

# Wunderground/ICAO station code is the final path segment of the resolution URL.
# The number of intermediate segments varies: Asian cities use country/city
# (…/jp/tokyo/RJTT) while US cities add a state (…/us/ny/new-york-city/KLGA), so
# we match one-or-more lowercase path segments then the trailing 4-letter ICAO.
_STATION_RE = re.compile(
    r"wunderground\.com/history/daily/(?:[\w%-]+/)+([A-Z]{4})(?![A-Za-z0-9])")
_TEMP_RE = re.compile(r"(-?\d+)\s*°?\s*C", re.IGNORECASE)


@dataclass
class TempMarket:
    event_slug: str
    market_slug: str
    question: str
    condition_id: str
    yes_token_id: str          # CLOB token id for the "Yes" outcome
    no_token_id: str
    yes_price: float           # current market price of "Yes"
    no_price: float
    bucket_kind: BucketKind
    threshold_c: int           # the degree in the question
    station_code: Optional[str]
    end_date: str

    def implied_yes_prob(self) -> float:
        return self.yes_price


def _parse_bucket(group_title: str, question: str) -> tuple[BucketKind, Optional[int]]:
    text = (group_title or question or "").lower()
    m = _TEMP_RE.search(text)
    if not m:
        return "exact", None
    deg = int(m.group(1))
    if "or higher" in text or "or above" in text:
        return "gte", deg
    if "or below" in text or "or lower" in text:
        return "lte", deg
    return "exact", deg


WEATHER_TAG_ID = 84  # Polymarket "Weather" tag


def fetch_open_temperature_events(limit: int = 300) -> list[dict]:
    """Pull open daily high-temperature events under the Weather tag.

    The events endpoint returns each event with its nested bucket markets fully
    populated (prices + clobTokenIds), so one paged call gives everything.
    """
    out, offset = [], 0
    while offset < limit * 3:
        r = _gamma_get(
            f"{GAMMA_API}/events",
            params={"closed": "false", "tag_id": WEATHER_TAG_ID,
                    "limit": 100, "offset": offset,
                    "order": "startDate", "ascending": "false"},
        )
        batch = r.json()
        if not batch:
            break
        out += [ev for ev in batch if "highest-temperature-in" in ev.get("slug", "")]
        offset += 100
        if len(batch) < 100:
            break
    return out


def parse_event(ev: dict) -> list[TempMarket]:
    markets = []
    for m in ev.get("markets", []):
        if m.get("closed") or m.get("umaResolutionStatus") not in (None, "", "proposed"):
            # skip already-resolving markets
            pass
        try:
            prices = _as_list(m.get("outcomePrices"))
            tokens = _as_list(m.get("clobTokenIds"))
            outcomes = _as_list(m.get("outcomes"))
            yes_idx = outcomes.index("Yes") if "Yes" in outcomes else 0
            no_idx = 1 - yes_idx
            kind, deg = _parse_bucket(m.get("groupItemTitle", ""), m.get("question", ""))
            if deg is None:
                continue
            station = _parse_station(m.get("description", "") or ev.get("description", ""))
            markets.append(TempMarket(
                event_slug=ev.get("slug", ""),
                market_slug=m.get("slug", ""),
                question=m.get("question", ""),
                condition_id=m.get("conditionId", ""),
                yes_token_id=tokens[yes_idx],
                no_token_id=tokens[no_idx],
                yes_price=float(prices[yes_idx]),
                no_price=float(prices[no_idx]),
                bucket_kind=kind,
                threshold_c=deg,
                station_code=station,
                end_date=m.get("endDate", ev.get("endDate", "")),
            ))
        except (ValueError, IndexError, TypeError):
            continue
    return markets


def _parse_station(description: str) -> Optional[str]:
    m = _STATION_RE.search(description or "")
    return m.group(1) if m else None


def _as_list(v):
    """Gamma returns these fields as JSON-encoded strings sometimes."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        import json
        return json.loads(v)
    return []
