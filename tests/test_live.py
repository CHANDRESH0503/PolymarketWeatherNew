"""Live execution engine (LiveBroker) — same decisions as paper, real fills.

These run entirely offline: clob.place_order is monkeypatched, books are injected.
They assert the LiveBroker reuses the paper engine's dedup + caps + recording and
only swaps the fill, so live decisions match the validated paper run.
"""
import src.paper.store as store
import src.polymarket.clob as clob
from src.live.engine import LiveBroker, _order_ok
from src.polymarket.gamma import TempMarket
from src.strategy.edge import Signal


def _market():
    return TempMarket(
        event_slug="ev", market_slug="mk",
        question="Will the high temperature in Tokyo be 25°C on June 27?",
        condition_id="cond1", yes_token_id="yes1", no_token_id="no1",
        yes_price=0.30, no_price=0.70, bucket_kind="exact",
        threshold_c=25, station_code="RJTT", end_date="2026-06-27T12:00:00Z")


def _signal(stake=5.0):
    m = _market()
    return Signal(m, "No", m.no_token_id, model_prob=0.05, price=0.70,
                  edge=0.25, stake=stake, station="RJTT", date="2026-06-27")


def _book(size=100):
    return {"asks": [{"price": 0.70, "size": size}], "bids": []}


def test_order_ok_variants():
    assert _order_ok({"dry_run": True}) is True
    assert _order_ok({"success": True}) is True
    assert _order_ok({"success": False}) is False
    assert _order_ok({"error": "nope"}) is False
    assert _order_ok({"orderID": "abc"}) is True       # accepted, no explicit flag
    assert _order_ok(None) is False
    assert _order_ok({}) is False


def test_live_fill_records_like_paper(tmp_path, monkeypatch):
    sent = {}

    def fake_place(token_id, side, price, size_usdc):
        sent.update(token_id=token_id, side=side, price=price, size=size_usdc)
        return {"dry_run": True}

    monkeypatch.setattr(clob, "place_order", fake_place)
    b = LiveBroker(tmp_path / "live.db")
    b._books = {"no1": _book()}

    assert b.execute(_signal(5.0)) is True
    # order sent for the No token, ~$5, at the slippage limit (quote + 0.02)
    assert sent["token_id"] == "no1" and sent["side"] == "BUY"
    assert abs(sent["size"] - 5.0) < 1e-6
    assert abs(sent["price"] - 0.72) < 1e-6
    # one open lot recorded, cash deducted from the $100 base
    row = b.con.execute("SELECT token_id, cost, status FROM fills").fetchone()
    assert row["token_id"] == "no1" and row["status"] == "open"
    assert abs(row["cost"] - 5.0) < 1e-6
    assert abs(store.get_meta(b.con, "cash") - 95.0) < 1e-6
    # dedup: the same token is not bought again (inherited from PaperBroker)
    assert b.execute(_signal(5.0)) is False


def test_live_rejected_order_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(clob, "place_order", lambda *a, **k: {"error": "rejected"})
    b = LiveBroker(tmp_path / "live.db")
    b._books = {"no1": _book()}
    assert b.execute(_signal(5.0)) is False
    assert b.con.execute("SELECT COUNT(*) c FROM fills").fetchone()["c"] == 0
    assert abs(store.get_meta(b.con, "cash") - 100.0) < 1e-6     # untouched


def test_live_respects_city_cap(tmp_path, monkeypatch):
    # A $60 stake on a $100 book: per-city cap (25% = $25) binds first, so the
    # spend is capped exactly as it would be in paper — caps are inherited.
    monkeypatch.setattr(clob, "place_order", lambda *a, **k: {"dry_run": True})
    b = LiveBroker(tmp_path / "live.db")
    b._books = {"no1": _book(size=1000)}        # deep book, so the cap binds (not depth)
    assert b.execute(_signal(60.0)) is True
    cost = b.con.execute("SELECT cost FROM fills").fetchone()["cost"]
    assert abs(cost - 25.0) < 0.05               # 25% city cap, not the $60 stake
