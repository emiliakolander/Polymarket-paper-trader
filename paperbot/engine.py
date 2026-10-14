"""One complete, idempotent bot cycle. Designed to be run by a scheduler.

Order of work:
  safety checks -> lock -> crash recovery -> settle resolved markets ->
  mark positions & stop-losses -> scan markets/books -> arbitrage ->
  research (if enabled and within budget) -> final valuation snapshot.

Each stage is isolated: a failure is logged and the run continues where it
is safe to (status DEGRADED). If the process is killed mid-run, the next run
detects the stale lock/run and pending orders and repairs state.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from paperbot import arbitrage
from paperbot.budget import AIBudget
from paperbot.config import Config
from paperbot.db import Database, new_id
from paperbot.execution import PaperExecutor, utcnow
from paperbot.polymarket import PolymarketClient
from paperbot.portfolio import Portfolio, exit_conditions, hours_until
from paperbot.research import ResearchAgent, anthropic_client_factory, compute_signal
from paperbot.risk import RiskEngine, TradeProposal
from paperbot.safety import startup_checks
from paperbot.scanner import Market, MarketScanner

LOCK_TTL_S = 1800


class Engine:
    def __init__(
        self,
        cfg: Config,
        *,
        client: Optional[PolymarketClient] = None,
        claude_factory: Optional[Callable[[], Any]] = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        env: Optional[dict] = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.sleep = sleep
        self.env = env
        self.client = client or PolymarketClient(cfg.api)
        self.claude_factory = claude_factory or anthropic_client_factory

    # ------------------------------------------------------------------ run
    def run_once(self) -> dict:
        startup_checks(self.env)
        now = self.clock()
        db = Database(self.cfg.database_path)
        db.initialize(self.cfg.starting_capital_usd, now)
        run_id = new_id()
        if not db.acquire_lock(run_id, now, LOCK_TTL_S):
            db.log_event(now, "WARN", "RUN_SKIPPED_LOCKED", "another run holds the lock")
            db.close()
            return {"run_id": run_id, "status": "SKIPPED_LOCKED"}

        errors: list[dict] = []
        summary: dict[str, Any] = {"run_id": run_id}
        status = "OK"
        try:
            summary["crashed_runs_repaired"] = db.mark_stale_runs(now)
            summary["interrupted_orders_repaired"] = db.recover_interrupted_orders(now)
            db.start_run(run_id, now)
            ctx = _RunContext(self, db, run_id, errors, summary)
            ctx.execute()
            if errors:
                status = "DEGRADED"
        except Exception as exc:  # pragma: no cover - last-resort guard
            status = "FAILED"
            errors.append({"stage": "run", "error": f"{type(exc).__name__}: {exc}",
                           "trace": traceback.format_exc()[-2000:]})
        finally:
            end = self.clock()
            summary["status"] = status
            try:
                db.finish_run(run_id, end, status, errors, self.client.stats.as_dict(), summary)
                for e in errors:
                    db.log_event(end, "ERROR", f"STAGE_{e['stage'].upper()}_FAILED", e["error"], run_id)
            finally:
                db.release_lock(run_id)
                db.close()
        summary["errors"] = errors
        return summary


class _RunContext:
    def __init__(self, engine: Engine, db: Database, run_id: str, errors: list, summary: dict):
        self.e = engine
        self.cfg = engine.cfg
        self.db = db
        self.run_id = run_id
        self.errors = errors
        self.summary = summary
        self.portfolio = Portfolio(db, engine.client, self.cfg.starting_capital_usd)
        self.risk = RiskEngine(self.cfg.risk)
        self.executor = PaperExecutor(engine.client, db, self.cfg.execution, self.cfg.risk,
                                      clock=engine.clock, sleep=engine.sleep)
        self.books: dict = {}
        self.positions_value = 0.0

    def stage(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            self.errors.append({"stage": name, "error": f"{type(exc).__name__}: {exc}"[:1000]})
            return None

    def now(self) -> datetime:
        return self.e.clock()

    def remark(self) -> dict:
        mark = self.portfolio.mark_all(self.now(), self.books, self.run_id)
        self.positions_value = mark["positions_value"]
        return mark

    def execute(self) -> None:
        self.summary["settled"] = self.stage("settle", lambda: self.portfolio.settle_resolved(self.now())) or []
        self.stage("mark", self.remark)
        self.summary["exits"] = self.stage("exits", self.apply_exits) or []
        self.summary["unhedged_repair"] = self.stage(
            "unhedged_repair", lambda: arbitrage.repair_unhedged(self.executor, self.db, self.run_id)) or []
        self.stage("drawdown", self.check_halt)

        scan = self.stage("scan", lambda: MarketScanner(self.cfg, self.e.client, self.db)
                          .scan(self.run_id, self.now()))
        markets: list[Market] = scan.markets if scan else []
        for m in markets:
            self.books[m.yes_token] = m.yes_book
            self.books[m.no_token] = m.no_book
        if scan:
            self.summary["scan"] = {"considered": scan.considered, "with_books": len(markets),
                                    "rejected": scan.rejected, "book_errors": scan.book_errors}

        self.summary["arbitrage"] = self.stage("arbitrage", lambda: self.run_arbitrage(markets)) or {}
        if self.cfg.research.enabled:
            self.summary["research"] = self.stage("research", lambda: self.run_research(markets)) or {}
        else:
            self.summary["research"] = {"enabled": False}

        mark = self.stage("final_mark", self.remark) or {"positions_value": self.positions_value,
                                                           "unmarked": -1}
        snap = self.portfolio.snapshot(self.now(), self.run_id, mark)
        self.summary["equity"] = {k: round(v, 4) if isinstance(v, float) else v for k, v in snap.items()}
        self.summary["shadow"] = {"value": round(mark.get("shadow_value", 0.0), 4),
                                  "unmarked": mark.get("shadow_unmarked", 0)}
        self.stage("drawdown_final", self.check_halt)
        days = self.cfg.scanner.snapshot_retention_days
        self.summary["pruned_snapshots"] = self.stage(
            "maintenance", lambda: self.db.prune_snapshots(self.now() - timedelta(days=days)))

    # -------------------------------------------------------------- stages
    def check_halt(self) -> None:
        st = self.portfolio.state(self.now(), self.positions_value)
        if st.peak_equity > 0 and (st.peak_equity - st.equity) / st.peak_equity >= self.cfg.risk.max_drawdown_pct:
            if self.db.get_meta("halted", "0") != "1":
                self.db.set_meta("halted", "1")
                self.db.log_event(self.now(), "ERROR", "HALTED_MAX_DRAWDOWN",
                                  f"equity {st.equity:.2f} vs peak {st.peak_equity:.2f}; "
                                  "new entries halted until `python -m paperbot resume`", self.run_id)

    def apply_exits(self) -> list[dict]:
        """Execute exits whose stored exit conditions are triggered (verified sells)."""
        done = []
        for pos, why in self.portfolio.exits_due(self.cfg.risk):
            r = self.executor.place(
                run_id=self.run_id, signal_id=None, market_id=pos["market_id"],
                token_id=pos["token_id"], outcome=pos["outcome"], side="SELL",
                shares=pos["shares"], limit_price=0.0, signal_price=0.0,
                fee_rate=pos["fee_rate"] or 0.0, reason=why, strategy=pos["strategy"],
                decision_at=self.now(),
            )
            self.db.log_event(self.now(), "INFO", why.upper(),
                              f"{pos['question']}: {r.status}/{r.verification} "
                              f"{r.shares:.2f} of {pos['shares']:.2f} shares sold", self.run_id)
            done.append({"position_id": pos["position_id"], "trigger": why, "status": r.status,
                         "verification": r.verification, "sold": r.shares})
        if done:
            self.remark()
        return done

    def _signal(self, **kw) -> str:
        return self.db.insert_signal(run_id=self.run_id, now=self.now(), **kw)

    def run_arbitrage(self, markets: list[Market]) -> dict:
        out = {"evaluated": 0, "near_misses": 0, "approved": 0, "rejected": 0, "executed": {}}
        for m in markets:
            out["evaluated"] += 1
            now = self.now()
            st = self.portfolio.state(now, self.positions_value, m.market_id)
            ev = arbitrage.evaluate(m, self.cfg.arbitrage, now, self.risk.max_order_usd(st),
                                    self.cfg.execution)
            if not ev.ok:
                # Only persist interesting misses: asks summed below $1 but fees/depth killed it.
                if ev.best_ask_sum is not None and ev.best_ask_sum < 1.0:
                    out["near_misses"] += 1
                    self._signal(kind="arbitrage", market_id=m.market_id, question=m.question,
                                 outcome="YES+NO", expected_net_usd=ev.net_usd, edge=ev.edge_pct,
                                 cost_usd=ev.cost_usd, status="REJECTED", reasons=ev.reasons,
                                 details=ev.details())
                continue
            proposal = TradeProposal(
                kind="arbitrage", market_id=m.market_id, cost_usd=ev.cost_usd, edge=ev.edge_pct,
                quote_age_s=ev.quote_age_s, hours_to_resolution=hours_until(m.end_date, now),
                max_days_to_resolution=self.cfg.arbitrage.max_days_to_resolution,
            )
            decision = self.risk.check(proposal, st)
            if m.market_id in st.open_markets:
                # We never really traded, so the book still shows the liquidity we
                # "took" earlier. Re-entering would trade against phantom depth.
                decision.approved = False
                decision.reasons.append("ALREADY_IN_MARKET")
            sid = self._signal(kind="arbitrage", market_id=m.market_id, question=m.question,
                               outcome="YES+NO", expected_net_usd=ev.net_usd, edge=ev.edge_pct,
                               cost_usd=ev.cost_usd,
                               status="APPROVED" if decision.approved else "REJECTED",
                               reasons=decision.reasons, details=ev.details())
            if not decision.approved:
                out["rejected"] += 1
                continue
            out["approved"] += 1
            res = arbitrage.execute(self.executor, ev, self.run_id, sid,
                                    self.cfg.execution.unwind_unhedged)
            self.db.update_signal(
                sid, "EXEC_FAILED" if res.status == "FAILED" else f"EXECUTED_{res.status}",
                [f"LEG{i + 1}_{l.status}_{l.verification}" for i, l in enumerate(res.legs)]
                + ([f"UNWIND_{res.unwind.status}_{res.unwind.verification}"] if res.unwind else [])
                + ([res.note] if res.note else []))
            out["executed"][res.status] = out["executed"].get(res.status, 0) + 1
            self.remark()
        return out

    def run_research(self, markets: list[Market]) -> dict:
        out = {"enabled": True, "researched": 0, "skipped": None, "signals": 0, "executed": 0}
        if not (self.e.env or os.environ).get("ANTHROPIC_API_KEY"):
            raise RuntimeError("research.enabled is true but ANTHROPIC_API_KEY is not set")
        budget = AIBudget(self.db, self.cfg.budget, self.cfg.research)
        agent = ResearchAgent(self.cfg, self.db, budget, self.e.claude_factory)
        for m in agent.select_candidates(markets, self.now()):
            r = agent.research(m, self.now(), m.description)
            if r.status == "SKIPPED":
                out["skipped"] = r.error
                break
            out["researched"] += 1
            now = self.now()
            st = self.portfolio.state(now, self.positions_value, m.market_id)
            sig = compute_signal(m, r, self.cfg, st.equity, self.risk.max_order_usd(st))
            details = {"model_probability_yes": r.probability, "confidence": r.confidence,
                       "summary": r.summary, "evidence": r.evidence,
                       "uncertainties": r.uncertainties, "research_cost_usd": r.cost_usd,
                       "vwap": sig.vwap, "limit_price": sig.limit_price, "shares": sig.shares,
                       "fee_rate": m.fee_rate, "error": r.error}
            if not sig.ok:
                self._signal(kind="research", market_id=m.market_id, question=m.question,
                             outcome=sig.outcome or "-", expected_net_usd=sig.expected_net_usd,
                             edge=sig.edge, cost_usd=sig.cost_usd, status="REJECTED",
                             reasons=sig.reasons, details=details)
                continue
            out["signals"] += 1
            book = m.yes_book if sig.outcome == "YES" else m.no_book
            proposal = TradeProposal(
                kind="research", market_id=m.market_id, cost_usd=sig.cost_usd, edge=sig.edge,
                quote_age_s=book.age_seconds(now), hours_to_resolution=hours_until(m.end_date, now),
                spread=sig.spread,
            )
            decision = self.risk.check(proposal, st)
            if m.market_id in st.open_markets:
                decision.approved = False
                decision.reasons.append("ALREADY_IN_MARKET")
            sid = self._signal(kind="research", market_id=m.market_id, question=m.question,
                               outcome=sig.outcome, expected_net_usd=sig.expected_net_usd,
                               edge=sig.edge, cost_usd=sig.cost_usd,
                               status="APPROVED" if decision.approved else "REJECTED",
                               reasons=decision.reasons, details=details)
            if not decision.approved:
                continue
            end = m.end_date.isoformat() if m.end_date else None
            fill = self.executor.place(
                run_id=self.run_id, signal_id=sid, market_id=m.market_id, token_id=sig.token_id,
                outcome=sig.outcome, side="BUY", shares=sig.shares, limit_price=sig.limit_price,
                signal_price=sig.signal_price, fee_rate=m.fee_rate, decision_book=book,
                min_order_size=m.order_min_size, tick_size=m.tick_size, strategy="research",
                decision_at=now,
                position_info={"condition_id": m.condition_id, "question": m.question,
                               "strategy": "research", "fee_rate": m.fee_rate, "end_date": end,
                               "exit_conditions": exit_conditions("research", self.cfg.risk, end)},
            )
            self.db.update_signal(sid, "EXECUTED" if fill.ok else
                                  ("EXEC_UNVERIFIED" if fill.uncertain_shares > 0 else "EXEC_FAILED"),
                                  [f"{fill.status}_{fill.verification}"])
            if fill.ok:
                out["executed"] += 1
                self.remark()
        return out
