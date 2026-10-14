"""End-to-end cycles against a fake Polymarket API (no network)."""

from __future__ import annotations

import json
import pathlib
import re
from datetime import timedelta

import pytest

from paperbot.config import load_config
from paperbot.dashboard import write_dashboard
from paperbot.db import Database
from paperbot.engine import Engine
from paperbot.polymarket import PolymarketClient
from paperbot.report import write_report

from .conftest import NOW, FakeResponse, FakeSession, raw_book, raw_market
from .test_strategies import GOOD, FakeClaude, claude_response

PKG = pathlib.Path(__file__).resolve().parent.parent / "paperbot"


class World:
    """A tiny fake Polymarket: markets + books, mutable between runs."""

    def __init__(self):
        self.markets = {
            # Market 1: real (fee-free geopolitics) complement edge: 0.45 + 0.50.
            "1": raw_market("1", "y1", "n1", category="Geopolitics"),
            # Market 2: fairly priced, no edge.
            "2": raw_market("2", "y2", "n2", category="Politics"),
        }
        self.books = {
            "y1": ([(0.40, 500)], [(0.45, 200)]), "n1": ([(0.45, 500)], [(0.50, 500)]),
            "y2": ([(0.48, 500)], [(0.50, 500)]), "n2": ([(0.48, 500)], [(0.52, 500)]),
        }
        self.down = False
        self.session = FakeSession()
        s = self.session
        s.route("GET", "/markets", self._markets)
        s.route("POST", "/books", self._books)
        s.route("GET", "/book", self._book)
        for mid in ("1", "2"):
            s.route("GET", f"/markets/{mid}", lambda p, j, mid=mid: self._down() or FakeResponse(200, self.markets[mid]))

    def _down(self):
        return FakeResponse(503) if self.down else None

    def _markets(self, p, j):
        return self._down() or FakeResponse(200, list(self.markets.values()) if p["offset"] == 0 else [])

    def _raw(self, t):
        bids, asks = self.books[t]
        return raw_book(t, bids, asks)

    def _books(self, p, j):
        return self._down() or FakeResponse(200, [self._raw(x["token_id"]) for x in j if x["token_id"] in self.books])

    def _book(self, p, j):
        return self._down() or FakeResponse(200, self._raw(p["token_id"]))


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def engine(cfg, world, clock, claude=None, env=None):
    client = PolymarketClient(cfg.api, session=world.session, sleep=lambda s: None)
    return Engine(cfg, client=client, clock=clock, sleep=clock.sleep,
                  claude_factory=(lambda: claude) if claude else None,
                  env=env or {"PAPER_MODE": "true"})


def test_full_cycle_executes_arbitrage_and_persists(cfg, paper_env, tmp_path):
    w, clock = World(), Clock()
    summary = engine(cfg, w, clock).run_once()
    assert summary["status"] == "OK", summary["errors"]
    assert summary["arbitrage"]["executed"] == {"HEDGED": 1}
    db = Database(cfg.database_path)
    # 0.95 per pair, budget capped at 5% of equity -> $50 max cost.
    assert db.cash() == pytest.approx(1000 - 50, abs=0.01)
    assert {p["outcome"] for p in db.open_positions()} == {"YES", "NO"}
    snap = db.q1("SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
    # Liquidation value at bids: shares*(0.40+0.45) < cost -> conservative unrealized loss shown.
    assert snap["unrealized_pnl"] < 0
    assert db.q1("SELECT COUNT(*) c FROM book_snapshots")["c"] == 4
    m = write_report(db, tmp_path / "rep", 10.0, now=clock())
    html_path = write_dashboard(db, tmp_path / "site", 10.0)
    assert m["open_positions"] == 2 and m["health"] == "GOOD"
    assert "Paperbot" in html_path.read_text()
    db.close()


def test_restart_does_not_duplicate_state_and_settles_later(cfg, paper_env):
    w, clock = World(), Clock()
    engine(cfg, w, clock).run_once()
    clock.sleep(3600)
    # Market 1 resolves YES. Restarted process (new Engine) must settle it once.
    w.markets["1"].update(closed=True, outcomePrices='["1","0"]', umaResolutionStatus="resolved")
    s2 = engine(cfg, w, clock).run_once()
    assert len(s2["settled"]) == 2
    db = Database(cfg.database_path)
    shares = db.q1("SELECT SUM(settle_price) s, COUNT(*) c FROM positions WHERE status='SETTLED'")
    assert shares["c"] == 2
    # Bought ~52.63 pairs for $50 -> payout = shares * $1.
    assert db.realized_pnl() == pytest.approx(50 / 0.95 - 50, abs=0.01)
    assert db.q1("SELECT COUNT(*) c FROM ledger WHERE kind='DEPOSIT'")["c"] == 1
    clock.sleep(3600)
    s3 = engine(cfg, w, clock).run_once()
    assert s3["settled"] == []  # no double settlement
    db.close()


def test_api_outage_degrades_without_trading(cfg, paper_env):
    w, clock = World(), Clock()
    w.down = True
    s = engine(cfg, w, clock).run_once()
    assert s["status"] == "DEGRADED"
    assert any(e["stage"] == "scan" for e in s["errors"])
    db = Database(cfg.database_path)
    assert db.cash() == pytest.approx(1000)
    assert db.q1("SELECT status FROM runs")["status"] == "DEGRADED"
    assert db.q1("SELECT COUNT(*) c FROM events WHERE level='ERROR'")["c"] >= 1
    db.close()


def test_rate_limited_api_degrades(cfg, paper_env):
    w, clock = World(), Clock()
    w.session.route("GET", "/markets", lambda p, j: FakeResponse(429, headers={"Retry-After": "1"}))
    s = engine(cfg, w, clock).run_once()
    assert s["status"] == "DEGRADED"
    assert "RateLimited" in s["errors"][0]["error"]


def test_concurrent_run_is_skipped(cfg, paper_env):
    w, clock = World(), Clock()
    db = Database(cfg.database_path)
    db.acquire_lock("other", NOW, ttl_s=600)
    db.close()
    assert engine(cfg, w, clock).run_once()["status"] == "SKIPPED_LOCKED"


def test_crashed_run_is_repaired_on_next_start(cfg, paper_env):
    db = Database(cfg.database_path)
    db.initialize(1000, NOW)
    db.start_run("dead", NOW - timedelta(hours=2))
    db.acquire_lock("dead", NOW - timedelta(hours=2), ttl_s=60)  # expired lock
    db.record_pending_order(order_id="deadbeef0000", signal_id=None, run_id="dead",
                            now=NOW - timedelta(hours=2), market_id="1", token_id="y1",
                            outcome="YES", side="BUY", requested_shares=10, limit_price=0.5,
                            signal_price=0.5)
    db.close()
    w, clock = World(), Clock()
    s = engine(cfg, w, clock).run_once()
    assert s["crashed_runs_repaired"] == 1 and s["interrupted_orders_repaired"] == 1
    db = Database(cfg.database_path)
    assert db.q1("SELECT status FROM runs WHERE run_id='dead'")["status"] == "CRASHED"
    assert db.q1("SELECT status FROM orders WHERE order_id='deadbeef0000'")["status"] == "INTERRUPTED"
    db.close()


def test_refuses_to_run_without_paper_mode_or_with_wallet_key(cfg):
    from paperbot.safety import SafetyError
    w, clock = World(), Clock()
    with pytest.raises(SafetyError):
        engine(cfg, w, clock, env={"PAPER_MODE": "false"}).run_once()
    with pytest.raises(SafetyError):
        engine(cfg, w, clock, env={"PAPER_MODE": "true", "POLYMARKET_PRIVATE_KEY": "x"}).run_once()
    assert not pathlib.Path(cfg.database_path).exists()  # nothing touched


def test_halt_blocks_new_entries(cfg, paper_env):
    w, clock = World(), Clock()
    db = Database(cfg.database_path)
    db.initialize(1000, NOW)
    db.set_meta("halted", "1")
    db.close()
    s = engine(cfg, w, clock).run_once()
    assert s["arbitrage"]["rejected"] == 1 and s["arbitrage"]["executed"] == {}
    db = Database(cfg.database_path)
    assert "HALTED" in db.q1("SELECT reasons_json FROM signals WHERE status='REJECTED'")["reasons_json"]
    db.close()


def test_research_cycle_with_fake_claude(paper_env, tmp_path):
    cfg = load_config(overrides={
        "database_path": str(tmp_path / "r.sqlite"),
        "api": {"min_interval_s": 0, "max_retries": 0},
        "execution": {"latency_s": 0, "random_reject_rate": 0.0},
        "research": {"enabled": True},
    })
    w, clock = World(), Clock()
    del w.markets["1"]  # keep only the fair market for research
    w.books["y2"] = ([(0.49, 500)], [(0.50, 500)])
    fake = FakeClaude([claude_response(json.dumps(GOOD))])  # model says 70% YES
    env = {"PAPER_MODE": "true", "ANTHROPIC_API_KEY": "sk-ant-test"}
    s = engine(cfg, w, clock, claude=fake, env=env).run_once()
    assert s["status"] == "OK", s["errors"]
    assert s["research"]["researched"] == 1 and s["research"]["executed"] == 1
    db = Database(cfg.database_path)
    sig = db.q1("SELECT * FROM signals WHERE kind='research'")
    details = json.loads(sig["details_json"])
    assert details["evidence"][0]["source_url"].startswith("https://")
    assert db.ai_spend_total() > 0
    db.close()


def test_research_enabled_without_key_is_reported(paper_env, tmp_path):
    cfg = load_config(overrides={"database_path": str(tmp_path / "r.sqlite"),
                                 "api": {"min_interval_s": 0},
                                 "execution": {"latency_s": 0, "random_reject_rate": 0.0},
                                 "research": {"enabled": True}})
    s = engine(cfg, World(), Clock(), env={"PAPER_MODE": "true"}).run_once()
    assert s["status"] == "DEGRADED"
    assert "ANTHROPIC_API_KEY" in s["errors"][0]["error"]


# ------------------------------------------------------------- honesty checks
RISK_FREE = re.compile(r"(?<!not )(?<!no )(?<!never )risk[- ]free", re.IGNORECASE)


def test_no_risk_free_profit_claims_in_code_or_outputs(cfg, paper_env, tmp_path):
    w, clock = World(), Clock()
    engine(cfg, w, clock).run_once()
    db = Database(cfg.database_path)
    write_report(db, tmp_path / "rep", 10.0)
    write_dashboard(db, tmp_path / "site", 10.0)
    texts = [f.read_text() for f in PKG.glob("*.py")]
    texts += [(tmp_path / "rep" / "report.md").read_text(), (tmp_path / "site" / "index.html").read_text()]
    texts += [r["details_json"] for r in db.q("SELECT details_json FROM signals")]
    for t in texts:
        for match in RISK_FREE.finditer(t):
            ctx = t[max(0, match.start() - 40):match.end()]
            assert re.search(r"(not|no|never|isn't|is NOT)\b[^.]{0,40}risk[- ]free", ctx, re.I), ctx
    db.close()


def test_midpoint_never_used_for_pnl():
    """`.mid` may exist for display, but no P&L/valuation module may reference it."""
    for f in PKG.glob("*.py"):
        if f.name == "orderbook.py":
            continue
        assert not re.search(r"\.mid\b", f.read_text()), f"midpoint used in {f.name}"


def test_old_snapshots_pruned_but_history_kept(cfg, paper_env):
    w, clock = World(), Clock()
    engine(cfg, w, clock).run_once()
    clock.sleep(15 * 86400)
    s = engine(cfg, w, clock).run_once()
    assert s["pruned_snapshots"] >= 4
    db = Database(cfg.database_path)
    assert db.q1("SELECT COUNT(*) c FROM equity_snapshots")["c"] == 2
    assert db.q1("SELECT COUNT(*) c FROM orders")["c"] >= 2
    db.close()


def test_cli_status_and_halt_resume(cfg, paper_env, capsys):
    from paperbot.__main__ import main
    w, clock = World(), Clock()
    engine(cfg, w, clock).run_once()
    assert main(["--db", cfg.database_path, "status"]) == 0
    assert "equity=" in capsys.readouterr().out
    assert main(["--db", cfg.database_path, "halt"]) == 0
    db = Database(cfg.database_path)
    assert db.get_meta("halted") == "1"
    db.close()
    assert main(["--db", cfg.database_path, "resume"]) == 0
    db = Database(cfg.database_path)
    assert db.get_meta("halted") == "0"
    db.close()


def test_cli_refuses_without_paper_mode(cfg, monkeypatch):
    from paperbot.__main__ import main
    monkeypatch.setenv("PAPER_MODE", "false")
    assert main(["--db", cfg.database_path, "status"]) == 2


def test_win_rate_counts_arbitrage_pair_once(cfg, paper_env):
    from paperbot.report import compute_metrics
    w, clock = World(), Clock()
    engine(cfg, w, clock).run_once()
    clock.sleep(3600)
    w.markets["1"].update(closed=True, outcomePrices='["1","0"]', umaResolutionStatus="resolved")
    engine(cfg, w, clock).run_once()
    db = Database(cfg.database_path)
    m = compute_metrics(db, clock())
    assert m["closed_trades"] == 1 and m["wins"] == 1 and m["win_rate"] == 1.0
    db.close()


def test_usd_format_has_no_negative_zero():
    from paperbot.dashboard import _usd
    assert _usd(-0.0001) == "$0.00" and _usd(-1.234) == "-$1.23"
