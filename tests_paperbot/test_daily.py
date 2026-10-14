from __future__ import annotations

import json
from datetime import timedelta

import pytest

from paperbot.daily import build_daily, render_daily, write_daily
from paperbot.db import Database

from .conftest import NOW, FakeResponse, raw_book
from .test_integration import Clock, World, engine


def _two_day_history(cfg):
    """Day 1 (2026-10-14): arbitrage bought. Day 2: market resolves YES and settles."""
    w, clock = World(), Clock()
    engine(cfg, w, clock).run_once()
    clock.sleep(6 * 3600)
    engine(cfg, w, clock).run_once()                       # mark again later on day 1
    clock.t = NOW.replace(hour=0) + timedelta(days=1, hours=9)
    w.markets["1"].update(closed=True, outcomePrices='["1","0"]', umaResolutionStatus="resolved")
    engine(cfg, w, clock).run_once()
    return w, clock


def test_daily_report_sections_day_one(cfg, paper_env):
    _two_day_history(cfg)
    db = Database(cfg.database_path)
    r = build_daily(db, NOW.date())
    # 1. hypothetical opportunities recorded separately from executions
    ho = r["hypothetical_opportunities"]
    assert ho["count"] >= 1 and any(o["kind"] == "arbitrage" for o in ho["items"])
    # 2. every order carries a full decision record
    decisions = r["simulated_executions"]["decisions"]
    assert len(decisions) == 2
    for d in decisions:
        assert d["decision_at"] and d["intended"]["shares"] == pytest.approx(10.52)
        assert set(d["liquidity"]) == {"decision", "execution", "verify_1", "verify_2"}
        assert d["status"] == "FILLED" and d["verification"] == "CONFIRMED"
        assert d["position"]["book"] == "MAIN"
        assert "hold YES+NO pair to resolution" in d["position"]["exit_conditions"]["rule"]
        assert d["timing"]["simulated_latency_s"] >= 0
    # 3./4. nothing completed on day 1; both legs unresolved at end of day, marked at bids
    assert r["completed_positions"] == []
    un = r["unresolved_positions"]
    assert len(un) == 2 and all(p["liquidation_value"] is not None for p in un)
    assert all(p["mtm_pnl"] < 0 for p in un)              # bids below cost: conservative
    # 5. net performance = equity change; no AI/infra costs configured
    n = r["net_performance"]
    assert n["trading_pnl_day"] == pytest.approx(n["equity_end_of_day"] - 1000)
    assert n["net_after_all_costs_day"] == pytest.approx(n["trading_pnl_day"])
    db.close()


def test_daily_report_day_two_completed_and_net(cfg, paper_env):
    _two_day_history(cfg)
    db = Database(cfg.database_path)
    day2 = NOW.date() + timedelta(days=1)
    db.record_api_cost(now=NOW + timedelta(days=1, hours=10), provider="anthropic", model="m",
                       purpose="research", market_id="2", input_tokens=1, output_tokens=1,
                       cache_read_tokens=0, cache_write_tokens=0, web_searches=0, cost_usd=0.25,
                       status="end_turn", request_id=None)
    r = build_daily(db, day2, infra_cost_usd_per_month=31.0)
    comp = r["completed_positions"]
    assert len(comp) == 1 and comp[0]["strategy"] == "arbitrage" and len(comp[0]["legs"]) == 2
    assert comp[0]["realized_pnl"] == pytest.approx(10.52 * 0.05, abs=0.01)  # pair = one trade
    assert r["unresolved_positions"] == []
    n = r["net_performance"]
    assert n["ai_cost_day"] == pytest.approx(0.25) and n["infra_cost_day"] == pytest.approx(1.0)
    assert n["net_after_all_costs_day"] == pytest.approx(n["trading_pnl_day"] - 1.25)
    assert n["net_after_all_costs_cumulative"] == pytest.approx(
        n["equity_end_of_day"] - 1000 - 0.25 - 2.0)        # infra for 2 days
    assert n["equity_end_of_day"] == pytest.approx(1000 + 10.52 * 0.05, abs=0.01)
    db.close()


def test_unverified_fills_reported_but_not_counted(cfg, paper_env):
    w, clock = World(), Clock()
    calls = {"n": 0}
    original = w._book

    def flaky(p, j):  # NO-leg liquidity vanishes after the execution snapshot
        if p["token_id"] == "n1":
            calls["n"] += 1
            if calls["n"] >= 2:
                return FakeResponse(200, raw_book("n1", [(0.45, 500)], [(0.70, 500)]))
        return original(p, j)

    w.session.route("GET", "/book", flaky)
    engine(cfg, w, clock).run_once()
    db = Database(cfg.database_path)
    r = build_daily(db, NOW.date())
    statuses = {(d["outcome"], d["side"]): d["verification"] for d in r["simulated_executions"]["decisions"]}
    assert statuses[("NO", "BUY")] == "UNCERTAIN"
    assert len(r["unverified_shadow"]["open"]) == 1
    assert r["unresolved_positions"] == []                 # YES leg was unwound in MAIN
    text = render_daily(r)
    assert "Unverified fills (shadow book" in text and "UNCERTAIN" in text
    db.close()


def test_render_and_cli_write_files(cfg, paper_env, tmp_path):
    from paperbot.__main__ import main
    _two_day_history(cfg)
    db = Database(cfg.database_path)
    text = render_daily(build_daily(db, NOW.date()))
    heads = ["## 1. Hypothetical opportunities", "## 2. Conservatively simulated executions",
             "## 3. Completed positions", "## 4. Unresolved positions",
             "## 5. Net performance after all costs"]
    pos = [text.index(h) for h in heads]
    assert pos == sorted(pos)
    path = write_daily(db, tmp_path, NOW.date())
    assert path.exists() and json.loads(path.with_suffix(".json").read_text())["date"] == "2026-10-14"
    db.close()
    assert main(["--db", cfg.database_path, "daily-report", "--out", str(tmp_path / "c"),
                 "--date", "2026-10-15"]) == 0
    assert (tmp_path / "c" / "daily" / "2026-10-15.md").exists()


def test_win_rate_ignores_shadow_book(cfg, paper_env):
    from paperbot.report import compute_metrics
    db = Database(cfg.database_path)
    db.initialize(1000, NOW)
    for book, pnl in (("MAIN", 1.0), ("SHADOW", -5.0)):
        db.conn.execute(
            "INSERT INTO positions(position_id,token_id,market_id,outcome,strategy,shares,cost_basis,"
            "realized_pnl,opened_at,closed_at,status,book) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (book, "t" + book, "m1", "YES", "research", 0, 0, pnl, NOW.isoformat(),
             NOW.isoformat(), "SETTLED", book))
    m = compute_metrics(db, NOW)
    assert m["closed_trades"] == 1 and m["win_rate"] == 1.0
    db.close()
