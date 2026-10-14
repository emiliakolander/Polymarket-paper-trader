from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from paperbot import arbitrage
from paperbot.budget import AIBudget, month_bounds
from paperbot.config import load_config
from paperbot.db import Database
from paperbot.execution import PaperExecutor
from paperbot.orderbook import parse_book
from paperbot.polymarket import PolymarketClient
from paperbot.research import ResearchAgent, ResearchError, ResearchResult, compute_signal, parse_answer
from paperbot.scanner import MarketScanner, parse_market

from .conftest import NOW, FakeResponse, FakeSession, raw_book, raw_market


def market_with_books(cfg, yes_asks, no_asks, yes_bids=((0.30, 100),), no_bids=((0.30, 100),),
                      category="Geopolitics", **kw):
    m, _ = parse_market(raw_market(category=category, **kw), cfg)
    m.yes_book = parse_book(raw_book(m.yes_token, list(yes_bids), list(yes_asks)), m.yes_token, NOW)
    m.no_book = parse_book(raw_book(m.no_token, list(no_bids), list(no_asks)), m.no_token, NOW)
    return m


# ------------------------------------------------------------------ scanner
@pytest.mark.parametrize("override,reason", [
    ({"outcomes": json.dumps(["A", "B", "C"]), "clobTokenIds": json.dumps(["1", "2", "3"])}, "NOT_BINARY"),
    ({"outcomes": json.dumps(["Trump", "Harris"])}, "NOT_YES_NO"),
    ({"closed": True}, "NOT_ACTIVE"),
    ({"acceptingOrders": False}, "NOT_ACCEPTING_ORDERS"),
    ({"clobTokenIds": json.dumps(["x", "x"])}, "BAD_TOKENS"),
    ({"clobTokenIds": "not json"}, "NOT_BINARY"),
    ({"conditionId": ""}, "NO_CONDITION_ID"),
])
def test_parse_market_rejects_non_equivalent_contracts(cfg, override, reason):
    m, why = parse_market(raw_market(**override), cfg)
    assert m is None and why == reason


def test_scanner_end_to_end_with_fake_api(cfg):
    s = FakeSession()
    s.route("GET", "/markets", lambda p, j: FakeResponse(200, [
        raw_market("1", "y1", "n1"), raw_market("2", "y2", "n2", liquidity=10),
        raw_market("3", "y3", "n3", end=NOW - timedelta(days=1)), {"garbage": True},
    ] if p["offset"] == 0 else []))
    s.route("POST", "/books", lambda p, j: FakeResponse(200, [
        raw_book(x["token_id"], [(0.45, 100)], [(0.55, 100)]) for x in j]))
    db = Database(cfg.database_path)
    sc = MarketScanner(cfg, PolymarketClient(cfg.api, session=s, sleep=lambda x: None), db)
    res = sc.scan("run1", NOW)
    assert [m.market_id for m in res.markets] == ["1"]
    assert res.rejected == {"LOW_LIQUIDITY_OR_VOLUME": 1, "PAST_END_DATE": 1, "MALFORMED": 1}
    assert db.q1("SELECT COUNT(*) c FROM book_snapshots")["c"] == 2
    assert db.q1("SELECT COUNT(*) c FROM market_snapshots")["c"] == 1


def test_scanner_skips_market_with_bad_book(cfg):
    s = FakeSession()
    s.route("GET", "/markets", lambda p, j: FakeResponse(200, [raw_market("1", "y1", "n1")]))
    s.route("POST", "/books", lambda p, j: FakeResponse(200, [
        raw_book("y1", [(0.6, 1)], [(0.5, 1)])]))  # crossed, and n1 missing
    db = Database(cfg.database_path)
    res = MarketScanner(cfg, PolymarketClient(cfg.api, session=s, sleep=lambda x: None), db).scan("r", NOW)
    assert res.markets == [] and res.book_errors == 1


# ---------------------------------------------------------------- arbitrage
def test_no_arb_when_asks_sum_above_one(cfg):
    m = market_with_books(cfg, yes_asks=[(0.52, 100)], no_asks=[(0.50, 100)])
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW)
    assert not ev.ok and "NO_EDGE_AFTER_FEES" in ev.reasons


def test_fees_can_kill_apparent_arb(cfg):
    # 0.49 + 0.50 = 0.99 < 1, but crypto fees (7%) add ~0.035/pair.
    m = market_with_books(cfg, yes_asks=[(0.49, 100)], no_asks=[(0.50, 100)], category="Crypto")
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW)
    assert not ev.ok and "NO_EDGE_AFTER_FEES" in ev.reasons


def test_midpoint_arb_illusion_is_rejected(cfg):
    # Mids are 0.45 + 0.45 = 0.90 (looks like 10% "arb") but asks sum to 1.00.
    m = market_with_books(cfg, yes_asks=[(0.50, 100)], no_asks=[(0.50, 100)],
                          yes_bids=[(0.40, 100)], no_bids=[(0.40, 100)])
    assert m.yes_book.mid + m.no_book.mid == pytest.approx(0.90)
    assert not arbitrage.evaluate(m, cfg.arbitrage, NOW).ok


def test_real_arb_sized_by_depth_and_net_of_fees(cfg):
    # Geopolitics: 0% fee. Level 1: 0.45+0.50 = 0.95; level 2 for YES at 0.52 -> 1.02 stop.
    m = market_with_books(cfg, yes_asks=[(0.45, 40), (0.52, 1000)], no_asks=[(0.50, 1000)])
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW)
    assert ev.ok, ev.reasons
    assert ev.shares == pytest.approx(20)  # 40 displayed * 50% participation
    assert ev.cost_usd == pytest.approx(20 * 0.95)
    assert ev.net_usd == pytest.approx(20 * 0.05)
    assert ev.yes_limit == 0.45 and ev.no_limit == 0.50


def test_arb_with_fees_accounts_each_leg(cfg):
    m = market_with_books(cfg, yes_asks=[(0.40, 1000)], no_asks=[(0.50, 1000)], category="Politics")
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW, budget_usd=30)
    unit = 0.40 + 0.50 + 0.04 * 0.4 * 0.6 + 0.04 * 0.5 * 0.5
    assert ev.cost_usd == pytest.approx(30)
    assert ev.shares == pytest.approx(30 / unit)
    assert ev.fees_usd == pytest.approx(ev.shares * (unit - 0.90))
    assert "Not risk-free" in ev.details()["risk_notes"][0]


def test_arb_rejects_thin_depth_and_empty_book(cfg):
    m = market_with_books(cfg, yes_asks=[(0.45, 4)], no_asks=[(0.50, 1000)])
    assert "INSUFFICIENT_DEPTH" in arbitrage.evaluate(m, cfg.arbitrage, NOW).reasons
    m2 = market_with_books(cfg, yes_asks=[], no_asks=[(0.50, 1000)])
    assert arbitrage.evaluate(m2, cfg.arbitrage, NOW).reasons == ["EMPTY_BOOK"]


def _exec(cfg, session):
    db = Database(cfg.database_path)
    db.initialize(1000, NOW)
    client = PolymarketClient(cfg.api, session=session, sleep=lambda s: None)
    return PaperExecutor(client, db, cfg.execution, cfg.risk, clock=lambda: NOW,
                         sleep=lambda s: None), db


def test_arb_execution_hedged(cfg, paper_env):
    m = market_with_books(cfg, yes_asks=[(0.45, 40)], no_asks=[(0.50, 1000)])
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW)
    s = FakeSession()
    books = {m.yes_token: raw_book(m.yes_token, [(0.30, 100)], [(0.45, 40)]),
             m.no_token: raw_book(m.no_token, [(0.30, 100)], [(0.50, 1000)])}
    s.route("GET", "/book", lambda p, j: FakeResponse(200, books[p["token_id"]]))
    ex, db = _exec(cfg, s)
    out = arbitrage.execute(ex, ev, "r", "sig", unwind_unhedged=True)
    assert out.status == "HEDGED"
    assert out.hedged_shares == pytest.approx(20)
    assert db.cash() == pytest.approx(1000 - 20 * 0.95)


def test_arb_leg_risk_second_leg_vanishes_then_unwound(cfg, paper_env):
    m = market_with_books(cfg, yes_asks=[(0.45, 40)], no_asks=[(0.50, 1000)])
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW)
    s = FakeSession()
    # YES is the thinner leg -> bought first. NO liquidity disappears before leg 2.
    books = {m.yes_token: raw_book(m.yes_token, [(0.40, 100)], [(0.45, 40)]),
             m.no_token: raw_book(m.no_token, [(0.30, 100)], [(0.60, 1000)])}
    s.route("GET", "/book", lambda p, j: FakeResponse(200, books[p["token_id"]]))
    ex, db = _exec(cfg, s)
    out = arbitrage.execute(ex, ev, "r", "sig", unwind_unhedged=True)
    assert out.legs[1].status == "FAILED_NO_LIQUIDITY"
    assert out.status == "LEG_RISK_UNWOUND"
    # Bought 20 YES at 0.45 (=9.00), sold 20 at 0.40 (=8.00): a real $1 loss, no fee (geopolitics).
    assert db.cash() == pytest.approx(1000 - 1.0)
    assert db.realized_pnl() == pytest.approx(-1.0)
    assert db.open_positions() == []


# ------------------------------------------------------------------- budget
def _budget(cfg, db=None, **research):
    c = load_config(overrides={"database_path": cfg.database_path, "research": research} if research
                    else {"database_path": cfg.database_path})
    db = db or Database(cfg.database_path)
    return AIBudget(db, c.budget, c.research), db, c


def test_cost_formula(cfg):
    b, _, _ = _budget(cfg)
    assert b.cost("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert b.cost("claude-opus-5-5", 0, 1_000_000) == pytest.approx(20.0)
    assert b.cost("claude-opus-5-5", 0, 0, web_searches=3) == pytest.approx(0.03)
    assert b.cost("unknown-model", 1_000_000, 0) == pytest.approx(10.0)  # conservative


def test_worst_case_estimate(cfg):
    b, _, _ = _budget(cfg)
    # (3000 + 3*25000) * 4/1e6 + 4000 * 20/1e6 + 3*0.01
    assert b.worst_case_call() == pytest.approx(0.312 + 0.08 + 0.03)


def _spend(db, amount, when=NOW):
    db.record_api_cost(now=when, provider="anthropic", model="m", purpose="research",
                       market_id="1", input_tokens=0, output_tokens=0, cache_read_tokens=0,
                       cache_write_tokens=0, web_searches=0, cost_usd=amount, status="end_turn",
                       request_id=None)


def test_budget_blocks_when_worst_case_would_exceed_cap(cfg):
    b, db, _ = _budget(cfg, max_calls_per_day=100)
    _spend(db, 9.50, NOW - timedelta(days=3))
    ok, why = b.authorize(NOW)
    assert ok  # 9.50 + 0.422 <= 10
    _spend(db, 0.20, NOW - timedelta(days=2))
    ok, why = b.authorize(NOW)
    assert not ok and why.startswith("AI_BUDGET_EXHAUSTED")


def test_budget_resets_next_month_and_persists(cfg):
    b, db, _ = _budget(cfg, max_calls_per_day=100)
    _spend(db, 10.0, NOW)
    assert not b.authorize(NOW)[0]
    db.close()
    b2, db2, _ = _budget(cfg, max_calls_per_day=100)  # restart
    assert not b2.authorize(NOW)[0]
    start, end = month_bounds(NOW)
    assert b2.authorize(end + timedelta(hours=1))[0]


def test_daily_call_limit(cfg):
    b, db, _ = _budget(cfg)
    _spend(db, 0.1)
    _spend(db, 0.1)
    assert b.authorize(NOW) == (False, "AI_DAILY_CALL_LIMIT")


# ----------------------------------------------------------------- research
GOOD = {
    "probability_yes": 0.70, "confidence": 0.8, "summary": "Polls favour YES.",
    "base_rate_reasoning": "Incumbents usually win.",
    "evidence": [
        {"claim": "Poll A shows 60%", "source_url": "https://example.org/a", "source_date": "2026-10-01",
         "direction": "supports_yes"},
        {"claim": "Official schedule", "source_url": "https://example.gov/b", "source_date": "unknown",
         "direction": "neutral"},
    ],
    "key_uncertainties": ["late swing"],
}


def test_parse_answer_valid_and_invalid():
    assert parse_answer("Here: " + json.dumps(GOOD), 2)["probability"] == 0.70
    with pytest.raises(ResearchError):
        parse_answer("no json here", 2)
    with pytest.raises(ResearchError):
        parse_answer(json.dumps({**GOOD, "probability_yes": 1.0}), 2)
    with pytest.raises(ResearchError):
        parse_answer(json.dumps({**GOOD, "evidence": [{"claim": "x", "source_url": "ftp://x"}]}), 1)
    with pytest.raises(ResearchError):
        parse_answer(json.dumps({**GOOD, "evidence": GOOD["evidence"][:1]}), 2)


class FakeClaude:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def claude_response(text, stop="end_turn", inp=20000, out=1500, searches=2):
    return SimpleNamespace(
        content=[SimpleNamespace(type="server_tool_use", name="web_search")] * searches
        + [SimpleNamespace(type="text", text=text)],
        stop_reason=stop, id="msg_1",
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0,
                              server_tool_use=SimpleNamespace(web_search_requests=searches)),
    )


def _agent(cfg, responses, **research):
    b, db, c = _budget(cfg, enabled=True, **research)
    fake = FakeClaude(responses)
    return ResearchAgent(c, db, b, lambda: fake), fake, db, c


def test_research_success_records_cost_and_hides_price(cfg):
    agent, fake, db, c = _agent(cfg, [claude_response(json.dumps(GOOD))])
    m = market_with_books(c, yes_asks=[(0.55, 500)], no_asks=[(0.47, 500)])
    r = agent.research(m, NOW, "Resolves YES if X.")
    assert r.status == "OK" and r.probability == 0.70
    expected = 20000 * 4e-6 + 1500 * 20e-6 + 2 * 0.01
    assert r.cost_usd == pytest.approx(expected)
    assert db.ai_spend_total() == pytest.approx(expected)
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "0.55" not in prompt and "Resolves YES if X." in prompt
    assert fake.calls[0]["tools"][0]["max_uses"] == c.research.max_web_searches
    assert db.q1("SELECT status FROM research")["status"] == "OK"


def test_research_invalid_answer_still_costs_and_is_not_traded(cfg):
    agent, fake, db, c = _agent(cfg, [claude_response("I think maybe yes?")])
    m = market_with_books(c, yes_asks=[(0.55, 500)], no_asks=[(0.47, 500)])
    r = agent.research(m, NOW)
    assert r.status == "INVALID" and db.ai_spend_total() > 0
    assert not compute_signal(m, r, c, 1000, 50).ok


def test_research_api_error_recorded(cfg):
    agent, fake, db, c = _agent(cfg, [RuntimeError("overloaded")])
    m = market_with_books(c, yes_asks=[(0.55, 500)], no_asks=[(0.47, 500)])
    r = agent.research(m, NOW)
    assert r.status == "FAILED" and "overloaded" in r.error
    assert db.q1("SELECT status FROM api_costs")["status"] == "error"


def test_research_refusal_and_pause_turn(cfg):
    agent, fake, db, c = _agent(cfg, [claude_response("", stop="pause_turn"),
                                      claude_response(json.dumps(GOOD))], max_calls_per_day=5)
    m = market_with_books(c, yes_asks=[(0.55, 500)], no_asks=[(0.47, 500)])
    assert agent.research(m, NOW).status == "OK"
    assert len(fake.calls) == 2 and db.q1("SELECT COUNT(*) c FROM api_costs")["c"] == 2
    agent2, _, _, _ = _agent(cfg, [claude_response("", stop="refusal")], max_calls_per_day=5)
    assert agent2.research(m, NOW + timedelta(days=4)).status == "REFUSED"


def test_research_skipped_when_budget_exhausted(cfg):
    agent, fake, db, c = _agent(cfg, [claude_response(json.dumps(GOOD))])
    _spend(db, 9.9)
    m = market_with_books(c, yes_asks=[(0.55, 500)], no_asks=[(0.47, 500)])
    r = agent.research(m, NOW)
    assert r.status == "SKIPPED" and fake.calls == []


def test_signal_uses_executable_price_and_fees(cfg):
    c = load_config(overrides={"database_path": cfg.database_path})
    m = market_with_books(c, yes_asks=[(0.55, 500)], no_asks=[(0.47, 500)],
                          yes_bids=[(0.53, 500)], category="Politics")
    r = ResearchResult(m.market_id, "OK", probability=0.70, confidence=0.8)
    sig = compute_signal(m, r, c, equity=1000, max_order_usd=50)
    assert sig.ok and sig.outcome == "YES"
    fee_ps = 0.04 * 0.55 * 0.45
    assert sig.edge == pytest.approx(0.70 - 0.55 - fee_ps)
    assert sig.cost_usd <= 40 + 1e-9  # research.max_trade_usd


def test_signal_buys_no_when_yes_overpriced(cfg):
    c = load_config(overrides={"database_path": cfg.database_path})
    m = market_with_books(c, yes_asks=[(0.80, 500)], no_asks=[(0.22, 500)], yes_bids=[(0.78, 500)],
                          no_bids=[(0.20, 500)])
    r = ResearchResult(m.market_id, "OK", probability=0.60, confidence=0.9)
    sig = compute_signal(m, r, c, equity=1000, max_order_usd=50)
    assert sig.ok and sig.outcome == "NO"


def test_signal_rejects_small_edge_and_low_confidence(cfg):
    c = load_config(overrides={"database_path": cfg.database_path})
    m = market_with_books(c, yes_asks=[(0.55, 500)], no_asks=[(0.47, 500)], yes_bids=[(0.53, 500)])
    r = ResearchResult(m.market_id, "OK", probability=0.58, confidence=0.4)
    sig = compute_signal(m, r, c, equity=1000, max_order_usd=50)
    assert not sig.ok
    assert {"LOW_CONFIDENCE", "EDGE_BELOW_MINIMUM"} <= set(sig.reasons)
