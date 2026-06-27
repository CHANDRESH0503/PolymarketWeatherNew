"""LiveBroker — the paper engine with real CLOB fills.

It subclasses PaperBroker and overrides exactly ONE method, `_fill`: instead of
simulating a fill against the order book, it sizes the same marketable order and
sends it through `clob.place_order`. Every other decision — position dedup, the
cash-reserve / per-day / per-city caps, equity-based sizing, marking, settlement,
the profit sweep — is inherited unchanged, so live trades are decided by the
exact code that produced the paper results.

Safety:
  * Uses a separate book DB (data/live.db) so it never touches the paper book.
  * `clob.place_order` is itself DRY_RUN-guarded — with DRY_RUN=1 it logs instead
    of sending, and LiveBroker records the simulated fill, i.e. it behaves like
    paper (a safe "shadow" mode). Real orders go out only with DRY_RUN=0.
  * On any order rejection/empty book it records NO fill (returns 0 shares).

Known limitation (documented, not hidden): with DRY_RUN=0 the recorded fill is
the marketable-limit estimate (depth taken within the limit). Real fills can be
partial or unfilled if the book moves; reconciling the local ledger against
on-chain fills/positions (src/polymarket/data_api.py) is the next hardening step.
"""
from __future__ import annotations

from ..config import ROOT
from ..polymarket import clob
from ..paper.engine import PaperBroker, Signal

# Cents of slippage allowed past the quote we sized on — matches the paper
# engine's depth-aware fill (sig.price + 0.02).
LIVE_SLIPPAGE = 0.02

LIVE_DB = ROOT / "data" / "live.db"


def _order_ok(resp: dict | None) -> bool:
    """Best-effort success check on a place_order response.
    DRY_RUN stubs (`{"dry_run": True, ...}`) count as ok so shadow mode records a
    fill exactly like paper. For a real response, treat an explicit error or a
    falsy `success` as failure; otherwise assume the order was accepted."""
    if not resp:
        return False
    if resp.get("dry_run"):
        return True
    if resp.get("error") or resp.get("errorMsg"):
        return False
    if "success" in resp:
        return bool(resp["success"])
    return True


class LiveBroker(PaperBroker):
    def __init__(self, db_path=None):
        super().__init__(db_path or LIVE_DB)

    def _fill(self, sig: Signal, budget: float) -> tuple[float, float, float]:
        book = self._books.get(sig.token_id)
        if book is None:                       # not prefetched (e.g. PAPER_DEPTH off)
            try:
                book = clob.get_books([sig.token_id]).get(sig.token_id)
            except Exception as e:  # noqa: BLE001
                print(f"  ! live book fetch failed ({e}); skipping {sig.token_id}")
                return 0.0, 0.0, 0.0
        # Size the marketable order exactly as paper would (depth taken within the
        # slippage limit). This is what we expect to fill and what we record.
        limit = round(sig.price + LIVE_SLIPPAGE, 3)
        shares, avg, cost = clob.walk_asks(book, limit, budget)
        if shares <= 0 or cost <= 0:
            return 0.0, 0.0, 0.0
        resp = clob.place_order(sig.token_id, "BUY", limit, cost)
        if not _order_ok(resp):
            print(f"  ! live order rejected, no fill recorded: {resp}")
            return 0.0, 0.0, 0.0
        return shares, avg, cost
