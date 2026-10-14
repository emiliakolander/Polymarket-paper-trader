"""Verified execution: fills are checked against SUBSEQUENT order-book snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from paperbot import arbitrage
from paperbot.config import load_config
from paperbot.db import Database
from paperbot.execution import (
    CONFIRMED, FAILED_MIN_NOTIONAL, FAILED_MIN_SIZE, PARTIALLY_CONFIRMED, UNCERTAIN, PaperExecutor,
)
from paperbot.polymarket import PolymarketClient
from paperbot.portfolio import Portfolio, exit_conditions
from paperbot.research import ResearchResult, compute_signal

from .conftest import NOW, FakeResponse, FakeSession, raw_book, raw_market
from .test_strategies import market_with_books


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class BookSeq:
    """Per-token sequence of /book responses; the last one repeats. Items may be
    (bids, asks) tuples or an int HTTP status to simulate failures."""

    def __init__(self, session, seqs):
        self.seqs = {k: list(v) for k, v in seqs.items()}
        self.calls = []
        session.route("GET", "/book", self.handle)

    def handle(self, params, _json):
        tok = params["token_id"]
        self.calls.append(tok)
        seq = self.seqs[tok]
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, int):
            return FakeResponse(item)
        bids, asks = item
        return FakeResponse(200, raw_book(tok, bids, asks))


def setup(tmp_path, monkeypatch, seqs, **exec_over):
    monkeypatch.setenv("PAPER_MODE", "true")
    ex_cfg = {"latency_s": 0.5, "latency_jitter_s": 0, "verify_delay_s": 1.0, "verify_samples": 2,
              "random_reject_rate": 0.0}
    ex_cfg.update(exec_over)
    cfg = load_config(overrides={"database_path": str(tmp_path / "v.sqlite"),
                                 "api": {"min_interval_s": 0, "max_retries": 0},
                                 "execution": ex_cfg})
    s = FakeSession()
    seq = BookSeq(s, seqs)
    db = Database(cfg.database_path)
    db.initialize(1000, NOW)
    clock = Clock()
    client = PolymarketClient(cfg.api, session=s, sleep=lambda x: None)
    ex = PaperExecutor(client, db, cfg.execution, cfg.risk, clock=clock, sleep=clock.sleep)
    return cfg, ex, db, seq, clock


def buy(ex, token="T", shares=20, limit=0.55, fee=0.0, **kw):
    return ex.place(run_id="r", signal_id="s", market_id="101", token_id=token, outcome="YES",
                    side="BUY", shares=shares, limit_price=limit, signal_price=0.50, fee_rate=fee,
                    position_info={"strategy": "research"}, **kw)


DEEP = ([(0.48, 500)], [(0.50, 100)])


# ---------------------------------------------------------------- verification
def test_persistent_liquidity_is_confirmed_and_fully_audited(tmp_path, monkeypatch):
    cfg, ex, db, seq, clock = setup(tmp_path, monkeypatch, {"T": [DEEP]})
    r = buy(ex)
    assert r.verification == CONFIRMED and r.shares == pytest.approx(20)
    assert seq.calls == ["T", "T", "T"]  # execution book + 2 verification samples
    assert [o["label"] for o in r.observations] == ["execution", "verify_1", "verify_2"]
    row = db.q1("SELECT * FROM orders")
    assert row["verification"] == CONFIRMED and row["position_id"]
    timing = json.loads(row["timing_json"])
    assert timing["simulated_latency_s"] == 0.5 and len(timing["verification_at"]) == 2
    assert clock() == NOW + timedelta(seconds=2.5)
    assert db.q1("SELECT book FROM positions")["book"] == "MAIN"


def test_vanished_liquidity_is_uncertain_not_a_fill(tmp_path, monkeypatch):
    # Liquidity present at arrival, gone in the next snapshot -> cannot be verified.
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch,
                                {"T": [DEEP, ([(0.48, 500)], [(0.60, 100)])]})
    r = buy(ex)
    assert r.verification == UNCERTAIN and r.shares == 0 and r.uncertain_shares == pytest.approx(20)
    assert not r.ok
    assert db.cash() == pytest.approx(1000)                    # MAIN untouched
    assert db.open_positions("MAIN") == []
    shadow = db.open_positions("SHADOW")
    assert len(shadow) == 1 and shadow[0]["shares"] == pytest.approx(20)
    assert db.q1("SELECT uncertain_shares FROM orders")["uncertain_shares"] == pytest.approx(20)


def test_shrinking_liquidity_is_partially_confirmed(tmp_path, monkeypatch):
    # Arrival: 100 displayed (50 usable). Verify: 30 displayed (15 usable).
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch,
                                {"T": [DEEP, ([(0.48, 500)], [(0.50, 30)])]})
    r = buy(ex)
    assert r.verification == PARTIALLY_CONFIRMED
    assert r.shares == pytest.approx(15) and r.uncertain_shares == pytest.approx(5)
    assert db.open_positions("MAIN")[0]["shares"] == pytest.approx(15)
    assert db.open_positions("SHADOW")[0]["shares"] == pytest.approx(5)
    assert db.cash() == pytest.approx(1000 - 15 * 0.50)


def test_verification_snapshot_failure_marks_uncertain(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [DEEP, 503]})
    r = buy(ex)
    assert r.verification == UNCERTAIN and "verification snapshot 1 unavailable" in r.reason
    assert db.cash() == pytest.approx(1000)


def test_confirmed_fill_priced_at_worst_snapshot(tmp_path, monkeypatch):
    worse = ([(0.48, 500)], [(0.50, 10), (0.52, 200)])
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [DEEP, worse]})
    r = buy(ex, shares=20, limit=0.53)
    assert r.verification == CONFIRMED
    assert r.avg_price == pytest.approx((5 * 0.50 + 15 * 0.52) / 20)  # not the 0.50 arrival quote


def test_unverified_sell_keeps_position_open(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [DEEP]})
    buy(ex)
    seq.seqs["T"] = [([(0.48, 500)], [(0.50, 100)]), ([], [(0.50, 100)])]  # bids vanish
    r = ex.place(run_id="r", signal_id=None, market_id="101", token_id="T", outcome="YES",
                 side="SELL", shares=20, limit_price=0.0, signal_price=0.48, fee_rate=0.0)
    assert r.verification == UNCERTAIN and r.shares == 0
    assert db.open_positions("MAIN")[0]["shares"] == pytest.approx(20)


# ------------------------------------------------------------ market minimums
def test_three_dollar_order_allowed_only_if_it_meets_minimum(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [DEEP]})
    ok = buy(ex, shares=3 / 0.50, limit=0.50)            # 6 shares >= min 5
    assert ok.verification == CONFIRMED and ok.shares == pytest.approx(6)
    seq.calls.clear()
    small = buy(ex, shares=3 / 0.70, limit=0.70, decision_book=None, min_order_size=5)
    assert small.status == FAILED_MIN_SIZE and "market minimum 5" in small.reason
    assert seq.calls == []                               # rejected before any "sending"
    assert db.q1("SELECT COUNT(*) c FROM orders WHERE status='FAILED_MIN_SIZE'")["c"] == 1


def test_minimum_notional_enforced(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [([(0.08, 500)], [(0.10, 500)])]})
    r = buy(ex, shares=5, limit=0.10)                    # $0.50 < $1
    assert r.status == FAILED_MIN_NOTIONAL


def test_tick_and_share_rounding(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [DEEP]})
    buy(ex, shares=10.239, limit=0.537)
    row = db.q1("SELECT requested_shares, limit_price FROM orders")
    assert row["requested_shares"] == pytest.approx(10.23)
    assert row["limit_price"] == pytest.approx(0.53)


def test_latency_jitter_is_seeded_and_recorded(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [DEEP]},
                                latency_s=1.0, latency_jitter_s=0.5)
    r = buy(ex)
    lat = r.timing["simulated_latency_s"]
    assert 1.0 <= lat <= 1.5
    assert ex._latency(r.order_id) == pytest.approx(lat, abs=1e-3)


def test_research_signal_three_dollars_respects_minimum(cfg):
    m = market_with_books(cfg, yes_asks=[(0.50, 500)], no_asks=[(0.52, 500)],
                          yes_bids=[(0.49, 500)], no_bids=[(0.47, 500)])
    r = ResearchResult(m.market_id, "OK", probability=0.70, confidence=0.9)
    sig = compute_signal(m, r, cfg, equity=1000, max_order_usd=50)
    assert sig.ok and sig.shares == pytest.approx(6.0) and sig.cost_usd <= 3.0 + 1e-9
    m2 = market_with_books(cfg, yes_asks=[(0.70, 500)], no_asks=[(0.32, 500)],
                           yes_bids=[(0.69, 500)], no_bids=[(0.29, 500)])
    r2 = ResearchResult(m2.market_id, "OK", probability=0.90, confidence=0.9)
    sig2 = compute_signal(m2, r2, cfg, equity=1000, max_order_usd=50)
    assert not sig2.ok and "BELOW_MARKET_MINIMUM" in sig2.reasons   # 4.28 shares < 5


# ------------------------------------------------------------------ arbitrage
def arb_market(cfg):
    m = market_with_books(cfg, yes_asks=[(0.45, 40)], no_asks=[(0.50, 1000)],
                          yes_bids=[(0.40, 500)], no_bids=[(0.45, 500)])
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW, exec_cfg=cfg.execution)
    assert ev.ok, ev.reasons
    return m, ev


def test_arb_second_leg_unverifiable_is_one_leg_failure(tmp_path, monkeypatch):
    y = ([(0.40, 500)], [(0.45, 40)])
    n_ok, n_gone = ([(0.45, 500)], [(0.50, 1000)]), ([(0.45, 500)], [(0.58, 1000)])
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"tokY": [y], "tokN": [n_ok, n_gone]})
    m, ev = arb_market(cfg)
    out = arbitrage.execute(ex, ev, "r", "sig", unwind_unhedged=True)
    assert out.legs[1].verification == UNCERTAIN
    assert out.status == "LEG_RISK_UNWOUND" and "one-leg failure" in out.note
    assert db.open_positions("MAIN") == []                       # leg 1 sold back
    assert db.realized_pnl() == pytest.approx(-ev.shares * 0.05)  # 0.45 -> 0.40
    assert [p["outcome"] for p in db.open_positions("SHADOW")] == ["NO"]


def test_arb_first_leg_unverified_second_leg_not_sent(tmp_path, monkeypatch):
    y_ok, y_gone = ([(0.40, 500)], [(0.45, 40)]), ([(0.40, 500)], [(0.60, 40)])
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch,
                                {"tokY": [y_ok, y_gone], "tokN": [([(0.45, 500)], [(0.50, 1000)])]})
    m, ev = arb_market(cfg)
    out = arbitrage.execute(ex, ev, "r", "sig", unwind_unhedged=True)
    assert out.status == "LEG1_UNVERIFIED" and len(out.legs) == 1
    assert "tokN" not in seq.calls
    assert db.cash() == pytest.approx(1000)


def test_arb_rejected_when_one_leg_loss_too_high(cfg):
    m = market_with_books(cfg, yes_asks=[(0.45, 40)], no_asks=[(0.50, 1000)],
                          yes_bids=[(0.05, 500)], no_bids=[(0.05, 500)])
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW, exec_cfg=cfg.execution)
    assert "ONE_LEG_RISK_TOO_HIGH" in ev.reasons


def test_arb_rejected_when_leg_has_no_bids_to_unwind(cfg):
    m = market_with_books(cfg, yes_asks=[(0.45, 40)], no_asks=[(0.50, 1000)], yes_bids=[])
    ev = arbitrage.evaluate(m, cfg.arbitrage, NOW, exec_cfg=cfg.execution)
    assert ev.one_leg_loss["YES_first"] == pytest.approx(ev.yes_cost)
    assert "ONE_LEG_RISK_TOO_HIGH" in ev.reasons


def test_unhedged_excess_repaired_next_run(tmp_path, monkeypatch):
    books = {"A": [([(0.40, 500)], [(0.45, 500)])], "B": [([(0.45, 500)], [(0.50, 500)])]}
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, books)
    info = lambda: {"strategy": "arbitrage", "exit_conditions": {"min_order_size": 5}}
    for tok, outcome, n in (("A", "YES", 12), ("B", "NO", 6)):
        ex.place(run_id="r", signal_id="s", market_id="7", token_id=tok, outcome=outcome,
                 side="BUY", shares=n, limit_price=0.6, signal_price=0.5, fee_rate=0.0,
                 position_info=info(), min_order_size=1)
    out = arbitrage.repair_unhedged(ex, db, "r2")
    assert out[0]["status"] == "FILLED" and out[0]["sold"] == pytest.approx(6)
    pos = {p["outcome"]: p["shares"] for p in db.open_positions("MAIN")}
    assert pos == {"YES": pytest.approx(6), "NO": pytest.approx(6)}


def test_unhedged_excess_below_minimum_is_held(tmp_path, monkeypatch):
    books = {"A": [([(0.40, 500)], [(0.45, 500)])], "B": [([(0.45, 500)], [(0.50, 500)])]}
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, books)
    info = {"strategy": "arbitrage", "exit_conditions": {"min_order_size": 5}}
    for tok, outcome, n in (("A", "YES", 8), ("B", "NO", 6)):
        ex.place(run_id="r", signal_id="s", market_id="7", token_id=tok, outcome=outcome,
                 side="BUY", shares=n, limit_price=0.6, signal_price=0.5, fee_rate=0.0,
                 position_info=info, min_order_size=1)
    assert arbitrage.repair_unhedged(ex, db, "r2")[0]["status"] == "BELOW_MIN_HELD"


# --------------------------------------------- exits, marks, shadow settlement
def test_exit_conditions_stored_and_triggered(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [DEEP]})
    risk = load_config(overrides={"risk": {"take_profit_pct": 0.2}}).risk
    cond = exit_conditions("research", risk, "2026-12-01")
    ex.place(run_id="r", signal_id="s", market_id="101", token_id="T", outcome="YES", side="BUY",
             shares=20, limit_price=0.55, signal_price=0.5, fee_rate=0.0,
             position_info={"strategy": "research", "exit_conditions": cond})
    pos = db.open_positions()[0]
    assert json.loads(pos["exit_conditions_json"])["stop_loss_pct"] == risk.stop_loss_pct
    pf = Portfolio(db, ex.client, 1000)
    db.set_mark(pos["position_id"], 13.0, NOW)            # cost 10 -> +30%
    assert pf.exits_due(risk)[0][1] == "take_profit"
    db.set_mark(pos["position_id"], 6.0, NOW)             # -40%
    assert pf.exits_due(risk)[0][1] == "stop_loss"
    assert db.q1("SELECT COUNT(*) c FROM position_marks")["c"] == 2


def test_shadow_positions_settle_without_touching_cash(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"tokY": [DEEP, ([], [(0.9, 1)])]})
    buy(ex, token="tokY")                                 # -> SHADOW only
    assert db.cash() == pytest.approx(1000)
    ex.client._session.route("GET", "/markets/101", lambda p, j: FakeResponse(200, raw_market(
        closed=True, outcomePrices='["1","0"]', umaResolutionStatus="resolved")))
    out = Portfolio(db, ex.client, 1000).settle_resolved(NOW)
    assert out[0]["book"] == "SHADOW" and out[0]["payout"] == pytest.approx(20)
    assert db.cash() == pytest.approx(1000) and db.realized_pnl() == 0
    shadow = db.q1("SELECT realized_pnl, status FROM positions WHERE book='SHADOW'")
    assert shadow["status"] == "SETTLED" and shadow["realized_pnl"] == pytest.approx(10)


def test_mark_all_records_history_for_both_books(tmp_path, monkeypatch):
    cfg, ex, db, seq, _ = setup(tmp_path, monkeypatch, {"T": [DEEP, ([(0.48, 500)], [(0.50, 30)])]})
    buy(ex)                                               # 15 MAIN + 5 SHADOW
    ex.client._session.route("POST", "/books", lambda p, j: FakeResponse(
        200, [raw_book("T", [(0.45, 500)], [(0.50, 100)])]))
    mark = Portfolio(db, ex.client, 1000).mark_all(NOW, run_id="r")
    assert mark["positions_value"] == pytest.approx(15 * 0.45)
    assert mark["shadow_value"] == pytest.approx(5 * 0.45)
    assert db.q1("SELECT COUNT(*) c FROM position_marks WHERE marked=1")["c"] == 2


# ------------------------------------------------------------------- migration
def test_v1_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE orders (order_id TEXT PRIMARY KEY, signal_id TEXT, run_id TEXT,
      created_at TEXT NOT NULL, done_at TEXT, market_id TEXT NOT NULL, token_id TEXT NOT NULL,
      outcome TEXT, side TEXT NOT NULL, requested_shares REAL NOT NULL, limit_price REAL NOT NULL,
      signal_price REAL, status TEXT NOT NULL, filled_shares REAL NOT NULL DEFAULT 0, avg_price REAL,
      notional REAL NOT NULL DEFAULT 0, fee REAL NOT NULL DEFAULT 0, latency_ms REAL,
      book_hash TEXT, reason TEXT);
    CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
      amount REAL NOT NULL, order_id TEXT, token_id TEXT, note TEXT);
    INSERT INTO ledger(ts,kind,amount) VALUES('2026-10-01','DEPOSIT',1000);
    """)
    con.commit()
    con.close()
    db = Database(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(orders)")}
    assert {"verification", "uncertain_shares", "liquidity_json", "position_id"} <= cols
    assert db.cash() == pytest.approx(1000)  # old rows default to MAIN
