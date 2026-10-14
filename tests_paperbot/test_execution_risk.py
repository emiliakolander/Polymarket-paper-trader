from __future__ import annotations

from datetime import timedelta

import pytest

from paperbot.db import Database
from paperbot.execution import (
    FAILED_API, FAILED_BAD_BOOK, FAILED_MIN_SIZE, FAILED_NO_LIQUIDITY, FAILED_REJECTED,
    FAILED_STALE, FILLED, PARTIAL, FillResult, PaperExecutor,
)
from paperbot.polymarket import PolymarketClient
from paperbot.portfolio import Portfolio, liquidation_value, resolution_prices
from paperbot.orderbook import parse_book
from paperbot.risk import PortfolioState, RiskEngine, TradeProposal
from paperbot.safety import SafetyError

from .conftest import NOW, FakeResponse, FakeSession, raw_book, raw_market


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


def make_exec(cfg, session, clock=None, db=None):
    db = db or Database(cfg.database_path)
    db.initialize(cfg.starting_capital_usd, NOW)
    client = PolymarketClient(cfg.api, session=session, sleep=lambda s: None)
    clock = clock or Clock()
    ex = PaperExecutor(client, db, cfg.execution, cfg.risk, clock=clock, sleep=clock.advance)
    return ex, db, client, clock


def book_route(session, token, bids, asks, ts_ms=None):
    session.route("GET", "/book", lambda p, j: FakeResponse(
        200, raw_book(p["token_id"], bids, asks, ts_ms=ts_ms)))


def buy(ex, shares=20, limit=0.55, token="tokY", fee=0.04):
    return ex.place(run_id="r", signal_id="s1", market_id="101", token_id=token, outcome="YES",
                    side="BUY", shares=shares, limit_price=limit, signal_price=0.50,
                    fee_rate=fee, position_info={"strategy": "research", "fee_rate": fee})


# ----------------------------------------------------------------- execution
def test_full_fill_updates_cash_position_and_fees(cfg, paper_env):
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 100)])
    ex, db, _, _ = make_exec(cfg, s)
    r = buy(ex, shares=20)
    assert r.status == FILLED and r.shares == pytest.approx(20)
    fee = 20 * 0.04 * 0.5 * 0.5
    assert r.fee == pytest.approx(fee)
    assert db.cash() == pytest.approx(1000 - 10 - fee)
    pos = db.open_positions()[0]
    assert pos["shares"] == pytest.approx(20)
    assert pos["cost_basis"] == pytest.approx(10 + fee)


def test_partial_fill_due_to_participation_and_limit(cfg, paper_env):
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 10), (0.60, 1000)])
    ex, db, _, _ = make_exec(cfg, s)
    r = buy(ex, shares=20, limit=0.55)  # only 10*0.5 participation = 5 within limit
    assert r.status == PARTIAL and r.shares == pytest.approx(5)
    assert db.q1("SELECT status FROM orders")["status"] == PARTIAL


def test_empty_book_fails_no_liquidity_and_moves_no_money(cfg, paper_env):
    s = FakeSession()
    book_route(s, "tokY", bids=[], asks=[])
    ex, db, _, _ = make_exec(cfg, s)
    r = buy(ex)
    assert r.status == FAILED_NO_LIQUIDITY and r.shares == 0
    assert db.cash() == pytest.approx(1000)
    assert db.open_positions() == []


def test_price_moved_beyond_limit_fails(cfg, paper_env):
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.58, 100)], asks=[(0.60, 100)])
    ex, db, _, _ = make_exec(cfg, s)
    assert buy(ex, limit=0.51).status == FAILED_NO_LIQUIDITY


def test_below_min_order_size_fails(cfg, paper_env):
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 6)])  # 6*0.5 = 3 < min 5
    ex, db, _, _ = make_exec(cfg, s)
    assert buy(ex).status == FAILED_MIN_SIZE


def test_stale_quote_fails(cfg, paper_env):
    s = FakeSession()
    old = int((NOW - timedelta(seconds=120)).timestamp() * 1000)
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 100)], ts_ms=old)
    ex, db, _, _ = make_exec(cfg, s)
    assert buy(ex).status == FAILED_STALE
    assert db.cash() == pytest.approx(1000)


def test_api_failure_and_bad_book(cfg, paper_env):
    s = FakeSession()
    s.route("GET", "/book", lambda p, j: FakeResponse(503))
    ex, db, _, _ = make_exec(cfg, s)
    assert buy(ex).status == FAILED_API
    s.route("GET", "/book", lambda p, j: FakeResponse(200, raw_book(p["token_id"], [(0.6, 1)], [(0.5, 1)])))
    assert buy(ex).status == FAILED_BAD_BOOK
    assert db.cash() == pytest.approx(1000)


def test_delayed_execution_latency_is_recorded(cfg, paper_env):
    from paperbot.config import load_config
    cfg2 = load_config(overrides={"database_path": cfg.database_path,
                                  "execution": {"latency_s": 2.5, "random_reject_rate": 0.0}})
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 100)])
    ex, db, _, clock = make_exec(cfg2, s)
    r = buy(ex)
    assert r.latency_ms == pytest.approx(2500)
    assert clock() == NOW + timedelta(seconds=2.5)


def test_random_rejection_is_deterministic(cfg, paper_env, monkeypatch):
    from paperbot.config import load_config
    cfg2 = load_config(overrides={"database_path": cfg.database_path,
                                  "execution": {"latency_s": 0, "random_reject_rate": 1.0}})
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 100)])
    ex, db, _, _ = make_exec(cfg2, s)
    assert buy(ex).status == FAILED_REJECTED


def test_execution_refuses_without_paper_mode(cfg, monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "false")
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 100)])
    ex, db, _, _ = make_exec(cfg, s)
    with pytest.raises(SafetyError):
        buy(ex)
    assert db.q1("SELECT COUNT(*) c FROM orders")["c"] == 0


def test_sell_realizes_pnl_with_fees(cfg, paper_env):
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 100)])
    ex, db, _, _ = make_exec(cfg, s)
    b = buy(ex, shares=20)
    book_route(s, "tokY", bids=[(0.70, 100)], asks=[(0.72, 100)])
    r = ex.place(run_id="r", signal_id=None, market_id="101", token_id="tokY", outcome="YES",
                 side="SELL", shares=20, limit_price=0.01, signal_price=0.70, fee_rate=0.04)
    assert r.status == FILLED
    sell_fee = 20 * 0.04 * 0.7 * 0.3
    expected = 20 * 0.70 - sell_fee - (10 + b.fee)
    assert db.realized_pnl() == pytest.approx(expected)
    assert db.open_positions() == []
    assert db.cash() == pytest.approx(1000 + expected)


# ------------------------------------------------------- persistence/restart
def test_state_persists_across_restart(cfg, paper_env):
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 100)])
    ex, db, _, _ = make_exec(cfg, s)
    buy(ex, shares=20)
    cash = db.cash()
    db.close()
    db2 = Database(cfg.database_path)
    db2.initialize(cfg.starting_capital_usd, NOW)  # must NOT deposit again
    assert db2.cash() == pytest.approx(cash)
    assert len(db2.open_positions()) == 1
    assert db2.q1("SELECT COUNT(*) c FROM ledger WHERE kind='DEPOSIT'")["c"] == 1


def test_crash_mid_order_is_recovered_without_money_moving(cfg, paper_env):
    db = Database(cfg.database_path)
    db.initialize(1000, NOW)
    db.record_pending_order(order_id="abc123abc123", signal_id=None, run_id="r1", now=NOW,
                    market_id="1", token_id="t", outcome="YES", side="BUY",
                    requested_shares=10, limit_price=0.5, signal_price=0.5)
    db.close()  # simulated crash: never finalised
    db2 = Database(cfg.database_path)
    assert db2.recover_interrupted_orders(NOW) == 1
    assert db2.q1("SELECT status FROM orders")["status"] == "INTERRUPTED"
    assert db2.cash() == pytest.approx(1000)


def test_finalize_is_atomic_on_error(cfg, paper_env):
    db = Database(cfg.database_path)
    db.initialize(1000, NOW)
    db.record_pending_order(order_id="o1", signal_id=None, run_id="r", now=NOW, market_id="1",
                    token_id="t", outcome="YES", side="SELL", requested_shares=10,
                    limit_price=0.1, signal_price=0.5)
    bad = FillResult(FILLED, shares=10, notional=5, fee=0, avg_price=0.5, fills=[(0.5, 10)])
    with pytest.raises(ValueError):  # selling a position we don't have
        db.finalize_order("o1", NOW, bad)
    assert db.q1("SELECT status FROM orders")["status"] == "PENDING"
    assert db.cash() == pytest.approx(1000)
    assert db.q1("SELECT COUNT(*) c FROM fills")["c"] == 0


def test_run_lock_prevents_overlap_and_expires(cfg):
    db = Database(cfg.database_path)
    assert db.acquire_lock("a", NOW, ttl_s=60)
    assert not db.acquire_lock("b", NOW + timedelta(seconds=30))
    assert db.acquire_lock("b", NOW + timedelta(seconds=61))  # stale lock taken over
    db.release_lock("b")
    assert db.acquire_lock("c", NOW + timedelta(seconds=62))


# ---------------------------------------------------------- valuation/settle
def test_liquidation_value_uses_bids_not_mid():
    b = parse_book(raw_book("T", bids=[(0.40, 10)], asks=[(0.60, 10)]), "T")
    assert b.mid == pytest.approx(0.50)
    v = liquidation_value(b, 20, fee_rate=0.0)
    assert v == pytest.approx(4.0)  # 10 sold at 0.40, remaining 10 valued at 0


def test_resolution_prices_requires_clean_resolution():
    m = raw_market(closed=True, outcomePrices='["1","0"]', umaResolutionStatus="resolved")
    assert resolution_prices(m) == {"tokY": 1.0, "tokN": 0.0}
    assert resolution_prices(raw_market(closed=False, outcomePrices='["1","0"]')) is None
    assert resolution_prices(raw_market(closed=True, outcomePrices='["0.97","0.03"]')) is None
    assert resolution_prices(raw_market(closed=True, outcomePrices='["1","0"]',
                                        umaResolutionStatus="disputed")) is None
    assert resolution_prices(raw_market(closed=True, outcomePrices='["0.5","0.5"]')) == {
        "tokY": 0.5, "tokN": 0.5}


def test_settlement_pays_out_and_realizes(cfg, paper_env):
    s = FakeSession()
    book_route(s, "tokY", bids=[(0.48, 100)], asks=[(0.50, 100)])
    ex, db, client, _ = make_exec(cfg, s)
    b = buy(ex, shares=20)
    s.route("GET", "/markets/101", lambda p, j: FakeResponse(200, raw_market(
        closed=True, outcomePrices='["1","0"]', umaResolutionStatus="resolved")))
    pf = Portfolio(db, client, 1000)
    out = pf.settle_resolved(NOW)
    assert out[0]["payout"] == pytest.approx(20)
    assert db.realized_pnl() == pytest.approx(20 - 10 - b.fee)
    assert db.cash() == pytest.approx(1000 + 20 - 10 - b.fee)


# ---------------------------------------------------------------------- risk
def state(**kw):
    base = dict(equity=1000, cash=1000, peak_equity=1000, day_start_equity=1000,
                open_positions=0, open_markets=frozenset(), exposure_usd=0,
                market_exposure_usd=0, trades_today=0)
    base.update(kw)
    return PortfolioState(**base)


def prop(**kw):
    base = dict(kind="arbitrage", market_id="m1", cost_usd=40, edge=0.02, quote_age_s=2,
                hours_to_resolution=24 * 10, max_days_to_resolution=90)
    base.update(kw)
    return TradeProposal(**base)


def test_risk_approves_clean_trade(cfg):
    assert RiskEngine(cfg.risk).check(prop(), state()).approved


@pytest.mark.parametrize("p_kw,s_kw,reason", [
    ({"cost_usd": 60}, {}, "POSITION_TOO_LARGE"),
    ({"cost_usd": 2}, {}, "BELOW_MIN_TRADE"),
    ({}, {"exposure_usd": 480}, "TOTAL_EXPOSURE_LIMIT"),
    ({}, {"market_exposure_usd": 50}, "MARKET_EXPOSURE_LIMIT"),
    ({}, {"cash": 220}, "CASH_RESERVE"),
    ({"quote_age_s": 31}, {}, "STALE_QUOTE"),
    ({"edge": 0}, {}, "NO_POSITIVE_EDGE"),
    ({"hours_to_resolution": 1}, {}, "TOO_CLOSE_TO_RESOLUTION"),
    ({"hours_to_resolution": 24 * 200}, {}, "TOO_FAR_FROM_RESOLUTION"),
    ({"hours_to_resolution": None}, {}, "UNKNOWN_RESOLUTION_DATE"),
    ({}, {"equity": 840, "cash": 840}, "MAX_DRAWDOWN"),
    ({}, {"equity": 965, "cash": 965, "peak_equity": 1000}, "DAILY_LOSS_LIMIT"),
    ({}, {"trades_today": 10}, "MAX_TRADES_PER_DAY"),
    ({}, {"open_positions": 10}, "MAX_OPEN_POSITIONS"),
    ({}, {"halted": True}, "HALTED"),
    ({}, {"paper_mode": False}, "NOT_PAPER_MODE"),
    ({"kind": "research", "spread": 0.08}, {}, "SPREAD_TOO_WIDE"),
    ({"kind": "research", "spread": None}, {}, "NO_TWO_SIDED_MARKET"),
    ({"cost_usd": float("nan")}, {}, "INVALID_INPUT"),
])
def test_risk_rejections(cfg, p_kw, s_kw, reason):
    d = RiskEngine(cfg.risk).check(prop(**p_kw), state(**s_kw))
    assert not d.approved
    assert reason in d.reasons


def test_risk_lists_all_violations(cfg):
    d = RiskEngine(cfg.risk).check(prop(cost_usd=2, quote_age_s=100), state(halted=True))
    assert {"BELOW_MIN_TRADE", "STALE_QUOTE", "HALTED"} <= set(d.reasons)


def test_max_order_usd_respects_all_caps(cfg):
    r = RiskEngine(cfg.risk)
    assert r.max_order_usd(state()) == pytest.approx(50)  # 5% of 1000
    assert r.max_order_usd(state(exposure_usd=480)) == pytest.approx(20)
    assert r.max_order_usd(state(cash=210)) == pytest.approx(10)
    assert r.max_order_usd(state(equity=0)) == 0


def test_stop_loss(cfg):
    r = RiskEngine(cfg.risk)
    assert r.should_stop_loss(100, 64)
    assert not r.should_stop_loss(100, 70)
    assert not r.should_stop_loss(100, None)
