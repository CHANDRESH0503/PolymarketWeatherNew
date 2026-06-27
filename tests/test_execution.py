"""Tests for depth-aware fills + resolution-audit aggregation."""
from src.polymarket.clob import walk_asks
from src.paper.engine import capped_budget
from src.analysis.resolution_audit import _round_half_up, summarize


def _book(asks):
    return {"asks": [{"price": p, "size": s} for p, s in asks], "bids": []}


def test_walk_asks_vwap_across_levels():
    # buy with a big budget; should consume cheapest levels first → blended price
    book = _book([(0.50, 100), (0.52, 100)])
    shares, avg, cost = walk_asks(book, limit_price=0.60, budget_usdc=51.0)
    # 100 sh @0.50 = $50, then $1 more @0.52 ≈ 1.92 sh
    assert abs(cost - 51.0) < 1e-6
    assert 100 < shares < 102
    assert 0.50 < avg < 0.51


def test_walk_asks_respects_limit_price():
    # only the 0.50 level is at/below the limit; the 0.55 level is skipped
    book = _book([(0.50, 10), (0.55, 1000)])
    shares, avg, cost = walk_asks(book, limit_price=0.52, budget_usdc=100.0)
    assert abs(shares - 10) < 1e-6
    assert abs(avg - 0.50) < 1e-6
    assert abs(cost - 5.0) < 1e-6


def test_walk_asks_partial_when_thin():
    # classic weather-market reality: only ~$0.11 of depth available
    book = _book([(0.40, 0.275)])              # 0.275 sh * 0.40 = $0.11
    shares, avg, cost = walk_asks(book, limit_price=0.45, budget_usdc=50.0)
    assert cost < 0.12 and shares < 0.3        # partial fill, not the full $50


def test_walk_asks_empty_book():
    assert walk_asks(None, 0.5, 100) == (0.0, 0.0, 0.0)
    assert walk_asks(_book([]), 0.5, 100) == (0.0, 0.0, 0.0)


def test_round_half_up_matches_resolution_rule():
    assert _round_half_up(24.5) == 25      # half rounds UP, not banker's even
    assert _round_half_up(25.4) == 25
    assert _round_half_up(-0.5) == 0


def test_summarize_audit():
    rows = [
        {"station": "RKSI", "date": "2026-06-01", "delta": 0, "matched": 1},
        {"station": "RKSI", "date": "2026-06-02", "delta": 1, "matched": 0},
        {"station": "RJTT", "date": "2026-06-01", "delta": 0, "matched": 1},
        {"station": "RJTT", "date": "2026-06-02", "delta": 0, "matched": 1},
    ]
    s = summarize(rows)
    assert s["n"] == 4 and s["matched"] == 3
    assert abs(s["match_rate"] - 0.75) < 1e-9
    assert abs(s["within1_rate"] - 1.0) < 1e-9        # all within ±1
    assert abs(s["mean_abs_delta"] - 0.25) < 1e-9
    assert {h["delta"]: h["count"] for h in s["hist"]} == {0: 3, 1: 1}
    per = {p["station"]: p for p in s["per_station"]}
    assert per["RJTT"]["match_rate"] == 1.0
    assert per["RKSI"]["mean_delta"] == 0.5


def test_summarize_empty():
    assert summarize([]) == {"n": 0}


def test_portfolio_sizing_respects_buffer(monkeypatch):
    import src.strategy.edge as edge
    from src.strategy.edge import Signal, _apply_portfolio_sizing
    monkeypatch.setattr(edge, "CORR_KELLY", True)
    monkeypatch.setattr(edge, "BANKROLL", 1000.0)
    monkeypatch.setattr(edge, "CASH_BUFFER", 0.10)   # investable = 900
    sigs = [Signal(None, "No", f"t{i}", 0.2, 0.2, 0.6, 50.0) for i in range(5)]
    _apply_portfolio_sizing(sigs)
    total = sum(s.stake for s in sigs)
    assert total <= 900 + 1e-6        # never exceeds the buffer-adjusted bankroll
    assert all(s.stake >= 0 for s in sigs)


def test_capped_budget_limits():
    # stake is the binding limit when cash + day room are ample
    assert capped_budget(50, cash=1000, cash_floor=200, day_deployed=0, day_cap=1000) == 50
    # cash reserve binds: only 1000-900=100 spendable
    assert capped_budget(50, cash=1000, cash_floor=900, day_deployed=0, day_cap=1000) == 50
    assert capped_budget(500, cash=1000, cash_floor=900, day_deployed=0, day_cap=1000) == 100
    # per-day cap binds: day already has 980 of a 1000 cap → only 20 left
    assert capped_budget(50, cash=5000, cash_floor=0, day_deployed=980, day_cap=1000) == 20
    # day full / over → zero, never negative
    assert capped_budget(50, cash=5000, cash_floor=0, day_deployed=1000, day_cap=1000) == 0
    assert capped_budget(50, cash=5000, cash_floor=0, day_deployed=1200, day_cap=1000) == 0


def test_capped_budget_funds_new_day_while_old_day_full():
    # June 5 maxed at its cap, but June 6 (day_deployed=0) still gets funded
    start, day_cap, floor = 2000, 1000, 200
    cash = 2000 - 1000          # June-5 took its 1000 cap
    j6 = capped_budget(100, cash=cash, cash_floor=floor,
                       day_deployed=0, day_cap=day_cap)
    assert j6 == 100            # <-- the fix: a new day is no longer starved


def test_capped_budget_city_cap_binds():
    # city already holds 480 of a 500 city cap → only 20 more, even with day/cash room
    assert capped_budget(50, cash=5000, cash_floor=0, day_deployed=0, day_cap=5000,
                         city_deployed=480, city_cap=500) == 20
    # city full → zero (one heat-wave city can't take over the book)
    assert capped_budget(50, cash=5000, cash_floor=0, day_deployed=0, day_cap=5000,
                         city_deployed=500, city_cap=500) == 0
    # default (no city cap supplied) is a no-op — back-compat with the day/cash limits
    assert capped_budget(50, cash=5000, cash_floor=0, day_deployed=0, day_cap=5000) == 50


def test_portfolio_sizing_noop_when_off(monkeypatch):
    import src.strategy.edge as edge
    from src.strategy.edge import Signal, _apply_portfolio_sizing
    monkeypatch.setattr(edge, "CORR_KELLY", False)
    sigs = [Signal(None, "No", "t", 0.2, 0.2, 0.6, 42.0)]
    _apply_portfolio_sizing(sigs)
    assert sigs[0].stake == 42.0      # independent per-bet Kelly stands
