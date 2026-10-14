"""Learning loop, evaluation/proposals, position reviews, alerts, persistence tooling.
All MOCK data; no network."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from paperbot import alerts, evaluation, learning
from paperbot.budget import AIBudget
from paperbot.config import load_config
from paperbot.db import Database
from paperbot.fixtures import MockSession
from paperbot.live_check import run_check
from paperbot.persistence_check import manifest, verify
from paperbot.polymarket import PolymarketClient

from .conftest import NOW, FakeResponse, FakeSession, raw_market
from .test_integration import Clock, World, engine
from .test_strategies import GOOD, FakeClaude, claude_response


def _cfg(tmp_path, **over):
    base = {"database_path": str(tmp_path / "l.sqlite"), "api": {"min_interval_s": 0, "max_retries": 0},
            "execution": {"latency_s": 0, "latency_jitter_s": 0, "verify_delay_s": 0,
                          "random_reject_rate": 0.0}}
    for k, v in over.items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
    return load_config(overrides=base)


def _forecast(db, *, created, market_id, p=0.7, conf=0.8, side="YES", all_in=0.55, edge=0.10,
              shares=5.0, category="politics", decision="EXECUTED", purpose="entry"):
    m = SimpleNamespace(market_id=market_id, condition_id="c", question=f"Q{market_id}",
                        fee_category=category, end_date=created + timedelta(days=10))
    r = SimpleNamespace(status="OK", probability=p, confidence=conf, summary="s",
                        evidence=[{"claim": "x", "source_url": "https://e.org", "direction": "supports_yes"}])
    sig = SimpleNamespace(outcome=side, shares=shares, cost_usd=all_in * shares,
                          model_prob=p if side == "YES" else 1 - p, edge=edge) if side else None
    return learning.record_forecast(db, now=created, purpose=purpose, market=m, model="claude-opus-5-5",
                                    prompt_version="v2", research=r, signal=sig, decision=decision)


def _resolve_client(outcomes: dict):
    s = FakeSession()
    for mid, yes_wins in outcomes.items():
        s.route("GET", f"/markets/{mid}", lambda p, j, mid=mid, w=yes_wins: FakeResponse(
            200, raw_market(mid, f"y{mid}", f"n{mid}", closed=True, umaResolutionStatus="resolved",
                            outcomePrices='["1","0"]' if w else '["0","1"]')))
    return PolymarketClient(load_config().api, session=s, sleep=lambda x: None)


# ------------------------------------------------------------- forecast ledger
def test_forecast_resolution_scores_and_tags(tmp_path):
    db = Database(tmp_path / "f.sqlite")
    _forecast(db, created=NOW, market_id="11", p=0.8, conf=0.8, side="YES", all_in=0.6, edge=0.2)
    _forecast(db, created=NOW, market_id="12", p=0.8, conf=0.9, side="YES", all_in=0.6, edge=0.05)
    _forecast(db, created=NOW, market_id="13", p=0.3, conf=0.7, side="NO", all_in=0.5, edge=0.2,
              decision="REJECTED")
    done = learning.resolve_forecasts(db, _resolve_client({"11": True, "12": False, "13": False}), NOW)
    assert len(done) == 3
    f = {r["market_id"]: dict(r) for r in db.q("SELECT * FROM forecasts")}
    assert f["11"]["brier"] == pytest.approx(0.04) and f["11"]["direction_correct"] == 1
    assert f["11"]["hypothetical_return"] == pytest.approx((1 - 0.6) / 0.6)
    assert {"WRONG_DIRECTION", "OVERCONFIDENT", "THIN_EDGE_LOST"} <= set(json.loads(f["12"]["mistake_tags_json"]))
    assert "MISSED_WINNER" in json.loads(f["13"]["mistake_tags_json"])
    assert "Resolved NO" in f["12"]["review_text"] and "Direction wrong" in f["12"]["review_text"]


def test_unresolved_market_is_left_pending(tmp_path):
    db = Database(tmp_path / "f.sqlite")
    _forecast(db, created=NOW, market_id="21")
    s = FakeSession()
    s.route("GET", "/markets/21", lambda p, j: FakeResponse(200, raw_market("21")))
    assert learning.resolve_forecasts(db, PolymarketClient(load_config().api, session=s,
                                                           sleep=lambda x: None), NOW) == []


def test_track_record_notes_and_lessons(tmp_path):
    db = Database(tmp_path / "f.sqlite")
    for i in range(4):
        _forecast(db, created=NOW, market_id=f"3{i}", p=0.8, conf=0.8)
    learning.resolve_forecasts(db, _resolve_client({f"3{i}": i < 1 for i in range(4)}), NOW)
    db.conn.execute("UPDATE forecasts SET lesson='Check official schedules first.' WHERE market_id='31'")
    notes = learning.track_record_notes(db, "politics", 5)
    assert "Resolved forecasts so far: 4" in notes
    assert "right 1/4 times" in notes and "Check official schedules first." in notes


def test_llm_postmortem_is_budgeted_and_recorded(tmp_path):
    cfg = _cfg(tmp_path, research={"enabled": True})
    db = Database(cfg.database_path)
    _forecast(db, created=NOW, market_id="41", p=0.8, side="YES")
    learning.resolve_forecasts(db, _resolve_client({"41": False}), NOW)
    answer = json.dumps({"assessment": "Ignored late polls.", "mistake_type": "missed_evidence",
                         "lesson": "Weight the latest polls more."})
    fake = FakeClaude([claude_response(answer, searches=0, inp=2000, out=200)])
    n = learning.run_postmortems(cfg, db, AIBudget(db, cfg.budget, cfg.research), lambda: fake, NOW, 2)
    assert n == 1
    row = db.q1("SELECT lesson, postmortem_status FROM forecasts")
    assert row["lesson"] == "Weight the latest polls more." and row["postmortem_status"] == "DONE"
    cost = db.q1("SELECT purpose, cost_usd FROM api_costs")
    assert cost["purpose"] == "postmortem" and cost["cost_usd"] == pytest.approx(2000 * 4e-6 + 200 * 20e-6)
    assert "tools" not in fake.calls[0]                       # no web search in post-mortems


def test_postmortems_respect_daily_limit_and_cap(tmp_path):
    cfg = _cfg(tmp_path, research={"enabled": True}, learning={"max_postmortems_per_day": 0})
    db = Database(cfg.database_path)
    _forecast(db, created=NOW, market_id="42")
    learning.resolve_forecasts(db, _resolve_client({"42": True}), NOW)
    fake = FakeClaude([])
    assert learning.run_postmortems(cfg, db, AIBudget(db, cfg.budget, cfg.research),
                                    lambda: fake, NOW, 2) == 0 and fake.calls == []


# --------------------------------------------------------- proposals & testing
def _history(db, start, n, *, low_edge_loses=True):
    """n resolved forecasts: low-edge ones (edge 0.06) lose, high-edge ones (0.12) win."""
    outcomes = {}
    for i in range(n):
        mid = f"{start.day}{i:03d}"
        low = i % 2 == 0
        _forecast(db, created=start + timedelta(minutes=i), market_id=mid,
                  edge=0.06 if low else 0.12, side="YES", all_in=0.5)
        outcomes[mid] = not (low and low_edge_loses)
    return outcomes


def test_proposal_created_from_training_then_applied_only_after_out_of_sample_pass(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.database_path)
    train = _history(db, NOW - timedelta(days=20), 20)
    learning.resolve_forecasts(db, _resolve_client(train), NOW, max_markets=100)
    pid = evaluation.maybe_propose(db, cfg, NOW)
    prop = db.q1("SELECT * FROM proposals")
    assert pid and prop["status"] == "TESTING"
    assert json.loads(prop["params_json"])["min_edge"] > cfg.research.min_edge
    assert evaluation.current_params(db, cfg)["min_edge"] == cfg.research.min_edge  # not applied yet
    # Not enough NEW data yet -> still testing (old data is never reused for the test).
    assert evaluation.test_proposals(db, cfg, NOW)[0]["status"] == "TESTING"
    # New forecasts made AFTER the proposal show the same pattern -> passes and is applied.
    new = _history(db, NOW + timedelta(days=1), 24)
    learning.resolve_forecasts(db, _resolve_client(new), NOW + timedelta(days=10), max_markets=100)
    res = evaluation.test_proposals(db, cfg, NOW + timedelta(days=10))[0]
    assert res["status"] == "APPLIED" and res["n_new"] == 24
    assert evaluation.current_params(db, cfg)["min_edge"] == json.loads(prop["params_json"])["min_edge"]
    assert evaluation.params_version(db) == 1


def test_proposal_rejected_when_it_does_not_hold_on_new_data(tmp_path):
    cfg = _cfg(tmp_path, learning={"max_test_samples": 24})
    db = Database(cfg.database_path)
    train = _history(db, NOW - timedelta(days=20), 20)
    learning.resolve_forecasts(db, _resolve_client(train), NOW, max_markets=100)
    assert evaluation.maybe_propose(db, cfg, NOW)
    new = _history(db, NOW + timedelta(days=1), 24, low_edge_loses=False)  # pattern was noise
    learning.resolve_forecasts(db, _resolve_client(new), NOW + timedelta(days=10), max_markets=100)
    assert evaluation.test_proposals(db, cfg, NOW + timedelta(days=10))[0]["status"] == "REJECTED"
    assert evaluation.params_version(db) == 0


def test_no_proposal_without_enough_training_data(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.database_path)
    learning.resolve_forecasts(db, _resolve_client(_history(db, NOW - timedelta(days=5), 6)), NOW)
    assert evaluation.maybe_propose(db, cfg, NOW) is None


def test_awaiting_approval_when_auto_apply_off(tmp_path):
    cfg = _cfg(tmp_path, learning={"auto_apply": False})
    db = Database(cfg.database_path)
    learning.resolve_forecasts(db, _resolve_client(_history(db, NOW - timedelta(days=20), 20)), NOW, 100)
    evaluation.maybe_propose(db, cfg, NOW)
    learning.resolve_forecasts(db, _resolve_client(_history(db, NOW + timedelta(days=1), 24)),
                               NOW + timedelta(days=10), 100)
    assert evaluation.test_proposals(db, cfg, NOW + timedelta(days=10))[0]["status"] == "AWAITING_APPROVAL"
    a = alerts.evaluate_alerts(db, {"status": "OK"}, NOW)
    assert alerts.needs_attention(a) and a[0]["code"] == "PROPOSAL_AWAITING_APPROVAL"


def test_learned_params_cannot_leave_bounds(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        evaluation.apply_params(cfg, {"min_edge": 0.5, "min_confidence": 0.6, "blocked_categories": []})
    eff = evaluation.apply_params(cfg, {"min_edge": 0.10, "min_confidence": 0.7,
                                        "blocked_categories": ["crypto"]})
    assert eff.research.min_edge == 0.10 and eff.research.blocked_categories == ("crypto",)
    assert eff.risk == cfg.risk                                   # risk limits untouched


def test_evaluation_report_renders(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.database_path)
    learning.resolve_forecasts(db, _resolve_client(_history(db, NOW - timedelta(days=2), 6)), NOW, 100)
    path = evaluation.write_evaluation(db, cfg, tmp_path / "ev")
    text = path.read_text()
    assert "## Strategies" in text and "By category" in text and "Calibration" in text
    ev = json.loads((tmp_path / "ev" / "evaluation.json").read_text())
    assert ev["resolved_forecasts"] == 6 and ev["by_edge"]


# ------------------------------------------------------------ position reviews
def _research_world_run(tmp_path, claude_responses, research_over=None, **cfg_over):
    cfg = _cfg(tmp_path, research={"enabled": True, **(research_over or {})}, **cfg_over)
    w, clock = World(), Clock()
    del w.markets["1"]
    w.books["y2"] = ([(0.49, 500)], [(0.50, 500)])
    fake = FakeClaude(claude_responses)
    env = {"PAPER_MODE": "true", "ANTHROPIC_API_KEY": "sk-ant-test"}
    return cfg, w, clock, fake, env


def test_full_research_trade_then_hold_then_fair_value_exit(tmp_path, paper_env):
    cfg, w, clock, fake, env = _research_world_run(tmp_path, [claude_response(json.dumps(GOOD))])
    s1 = engine(cfg, w, clock, claude=fake, env=env).run_once()
    assert s1["research"]["executed"] == 1
    db = Database(cfg.database_path)
    f = db.q1("SELECT * FROM forecasts")
    assert f["decision"] == "EXECUTED" and f["position_id"] and f["side"] == "YES"
    db.close()
    clock.sleep(3600)
    s2 = engine(cfg, w, clock, claude=fake, env=env).run_once()   # bid 0.49 < 0.70: hold
    assert s2["position_reviews"][0]["decision"] == "HOLD"
    clock.sleep(3600)
    w.books["y2"] = ([(0.75, 500)], [(0.76, 500)])               # market reached our 70%
    s3 = engine(cfg, w, clock, claude=fake, env=env).run_once()
    rv = s3["position_reviews"][0]
    assert rv["decision"] == "SELL" and rv["reason"] == "fair_value" and rv["verification"] == "CONFIRMED"
    db = Database(cfg.database_path)
    pos = db.q1("SELECT * FROM positions WHERE book='MAIN'")
    assert pos["status"] == "CLOSED" and pos["realized_pnl"] > 0
    assert pos["close_reason"].startswith("fair_value")
    assert db.q1("SELECT COUNT(*) c FROM position_reviews")["c"] == 2
    db.close()


def test_re_research_can_weaken_thesis_and_trigger_exit(tmp_path, paper_env):
    weaker = {**GOOD, "probability_yes": 0.45}
    cfg, w, clock, fake, env = _research_world_run(
        tmp_path, [claude_response(json.dumps(GOOD)), claude_response(json.dumps(weaker))],
        research_over={"max_markets_per_run": 1}, learning={"review_research_hours": 1})
    engine(cfg, w, clock, claude=fake, env=env).run_once()
    clock.sleep(2 * 3600)
    w.books["y2"] = ([(0.48, 500)], [(0.50, 500)])
    s2 = engine(cfg, w, clock, claude=fake, env=env).run_once()
    rv = s2["position_reviews"][0]
    assert rv["decision"] == "SELL" and rv["reason"] == "thesis_weakened"
    db = Database(cfg.database_path)
    assert db.q1("SELECT COUNT(*) c FROM forecasts WHERE purpose='review'")["c"] == 1
    assert db.q1("SELECT purpose FROM api_costs ORDER BY id DESC LIMIT 1")["purpose"] == "review"
    db.close()


def test_research_skips_markets_already_held_and_blocked_categories(tmp_path, paper_env):
    cfg, w, clock, fake, env = _research_world_run(
        tmp_path, [claude_response(json.dumps(GOOD))], research_over={"blocked_categories": ["politics"]})
    s = engine(cfg, w, clock, claude=fake, env=env).run_once()
    assert s["research"]["researched"] == 0 and fake.calls == []


# --------------------------------------------------------------------- alerts
def test_alerts_for_repeated_problems_and_halt(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    for i in range(3):
        db.start_run(f"r{i}", NOW + timedelta(minutes=i))
        db.finish_run(f"r{i}", NOW, "DEGRADED", [], {}, {})
    db.log_event(NOW, "ERROR", "HALTED_MAX_DRAWDOWN", "x", "r2")
    codes = {a["code"] for a in alerts.evaluate_alerts(db, {"status": "DEGRADED", "run_id": "r2"}, NOW)}
    assert {"REPEATED_PROBLEMS", "HALTED_MAX_DRAWDOWN"} <= codes
    budget_only = alerts.evaluate_alerts(Database(tmp_path / "b.sqlite"), {
        "status": "OK", "research": {"skipped": "AI_BUDGET_EXHAUSTED: spent"}}, NOW)
    assert budget_only[0]["level"] == "INFO" and not alerts.needs_attention(budget_only)


# ---------------------------------------------------------- persistence tools
def test_persistence_check_passes_and_detects_tampering(tmp_path, paper_env):
    cfg = _cfg(tmp_path)
    w, clock = World(), Clock()
    engine(cfg, w, clock).run_once()
    db = Database(cfg.database_path)
    before = manifest(db)
    db.close()
    clock.sleep(3600)
    engine(cfg, w, clock).run_once()
    db = Database(cfg.database_path)
    assert all(c["passed"] for c in verify(before, db))
    db.conn.execute("UPDATE ledger SET amount=amount+1 WHERE id=1")   # tamper with history
    failed = {c["check"] for c in verify(before, db) if not c["passed"]}
    assert "earlier ledger rows unchanged" in failed
    db.close()


def test_mock_and_live_data_never_mix(tmp_path, paper_env):
    from paperbot.engine import Engine
    cfg = _cfg(tmp_path)
    client = PolymarketClient(cfg.api, session=MockSession(NOW), sleep=lambda s: None)
    clock = Clock()
    Engine(cfg, client=client, clock=clock, sleep=clock.sleep, data_source="MOCK",
           env={"PAPER_MODE": "true"}).run_once()
    with pytest.raises(RuntimeError, match="never mix"):
        Engine(cfg, client=client, clock=clock, sleep=clock.sleep, data_source="LIVE",
               env={"PAPER_MODE": "true"}).run_once()


def test_mock_fixture_world_trades_and_live_check_logic(tmp_path, paper_env):
    cfg = _cfg(tmp_path)
    client = PolymarketClient(cfg.api, session=MockSession(NOW), sleep=lambda s: None)
    r = run_check(client, cfg, NOW)          # live_check code path, exercised on MOCK data here
    assert r["ok"] and r["markets"]["binary_yes_no"] == 2
    assert any(b["arbitrage_verdict"] == ["OK"] for b in r["books"])
