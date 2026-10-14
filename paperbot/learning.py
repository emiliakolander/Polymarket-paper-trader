"""Learning loop, part 1: the forecast ledger.

Every research result becomes a FORECAST row, whether or not it led to a trade:
probability, confidence, reasoning, evidence, chosen side, executable all-in price,
edge, the decision (executed / rejected + reasons) and links to the signal and
position. When the market resolves, the forecast is scored against what actually
happened:

- outcome (YES=1 / NO=0 / 50-50=0.5), Brier score of the YES probability,
- whether the chosen side was right,
- hypothetical P&L at the decision-time executable price (for EVERY forecast, so
  rejected ideas can be evaluated too - used for out-of-sample testing),
- realized P&L of the actual MAIN-book trade, net of fees,
- deterministic mistake tags and a plain-language comparison of reasoning vs outcome,
- optionally a short Claude post-mortem with a lesson (paid, budgeted).

Lessons and calibration statistics are fed back to the research agent as data.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Optional

from paperbot.budget import AIBudget, day_bounds
from paperbot.config import Config
from paperbot.db import Database, iso, new_id
from paperbot.polymarket import ApiError, PolymarketClient
from paperbot.portfolio import resolution_prices


# ------------------------------------------------------------------ recording
def record_forecast(db: Database, *, now: datetime, purpose: str, market: Any, model: str,
                    prompt_version: str, research: Any, signal: Any = None,
                    decision: str = "", reasons: Optional[list] = None,
                    signal_id: Optional[str] = None) -> str:
    fid = new_id()
    side = getattr(signal, "outcome", "") or None
    shares = getattr(signal, "shares", 0.0) or 0.0
    all_in = (signal.cost_usd / shares) if side and shares > 0 else None
    db.conn.execute(
        "INSERT INTO forecasts(forecast_id,created_at,purpose,market_id,condition_id,question,category,"
        "end_date,model,prompt_version,research_status,probability_yes,confidence,summary,evidence_json,"
        "side,side_prob,all_in_price,shares,edge,decision,reasons_json,signal_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, iso(now), purpose, market.market_id, market.condition_id, market.question,
         market.fee_category, iso(market.end_date) if market.end_date else None, model,
         prompt_version, research.status, research.probability, research.confidence,
         research.summary, json.dumps(research.evidence),
         side, getattr(signal, "model_prob", None) if side else None, all_in,
         shares if side else None, getattr(signal, "edge", None) if side else None,
         decision, json.dumps(reasons or []), signal_id),
    )
    return fid


def link_position(db: Database, forecast_id: str, position_id: Optional[str]) -> None:
    db.conn.execute("UPDATE forecasts SET position_id=? WHERE forecast_id=?",
                    (position_id, forecast_id))


# ------------------------------------------------------------------ resolution
def _mistake_tags(f: dict, payout: float, realized: Optional[float],
                  close_reasons: list[str]) -> list[str]:
    tags = []
    if f["side"] is None:
        return ["NO_TRADEABLE_SIDE"]
    won = payout >= 0.999
    lost = payout <= 0.001
    if lost:
        tags.append("WRONG_DIRECTION")
        if (f["confidence"] or 0) >= 0.75:
            tags.append("OVERCONFIDENT")
        if (f["edge"] or 0) < 0.08:
            tags.append("THIN_EDGE_LOST")
    if won:
        tags.append("RIGHT_DIRECTION")
    if abs(payout - 0.5) < 1e-9:
        tags.append("RESOLVED_50_50")
    exited_early = any(r and r.split(":")[0] in ("stop_loss", "take_profit", "fair_value",
                                                  "thesis_weakened") for r in close_reasons)
    if exited_early and won:
        tags.append("EXITED_EARLY_ON_WINNER")
    if exited_early and lost:
        tags.append("EXIT_SAVED_LOSS")
    if realized is not None and f["all_in_price"] is not None and f["shares"]:
        gross = (payout - f["all_in_price"]) * f["shares"]
        if gross > 0 > realized:
            tags.append("COSTS_TURNED_WIN_INTO_LOSS")
    if f["decision"] and f["decision"].startswith("REJECTED") and won:
        tags.append("MISSED_WINNER")
    if f["decision"] and f["decision"].startswith("REJECTED") and lost:
        tags.append("CORRECTLY_SKIPPED")
    return tags


def _review_text(f: dict, outcome_yes: float, payout: float, brier: float,
                 realized: Optional[float], tags: list[str]) -> str:
    res = "YES" if outcome_yes >= 0.999 else "NO" if outcome_yes <= 0.001 else "50/50"
    side = f["side"] or "none"
    parts = [
        f"Forecast {f['probability_yes']:.0%} YES (confidence {f['confidence'] or 0:.2f}); "
        f"chosen side {side}" + (f" at all-in {f['all_in_price']:.3f}" if f["all_in_price"] else "")
        + f"; decision {f['decision'] or '-'}.",
        f"Resolved {res}. Brier score {brier:.3f} (0 = perfect, 0.25 = coin flip).",
    ]
    if f["side"]:
        parts.append("Direction " + ("right." if payout >= 0.999 else "wrong." if payout <= 0.001
                                     else "neutral (50/50)."))
    if realized is not None:
        parts.append(f"Realized P&L of the paper trade, net of fees: ${realized:+.2f}.")
    try:
        ev = json.loads(f["evidence_json"] or "[]")
    except ValueError:
        ev = []
    if ev:
        dirs = [e.get("direction") for e in ev]
        parts.append(f"Evidence cited: {len(ev)} items ({dirs.count('supports_yes')} for YES, "
                     f"{dirs.count('supports_no')} for NO).")
    parts.append("Tags: " + ", ".join(tags) + ".")
    return " ".join(parts)


def resolve_forecasts(db: Database, client: PolymarketClient, now: datetime,
                      max_markets: int = 20) -> list[str]:
    """Score unresolved forecasts whose markets have definitively resolved."""
    rows = db.q("SELECT market_id, MIN(end_date) e FROM forecasts WHERE resolved_at IS NULL "
                "GROUP BY market_id ORDER BY e LIMIT ?", (max_markets,))
    resolved = []
    for r in rows:
        try:
            market = client.get_market(r["market_id"])
        except ApiError as exc:
            db.log_event(now, "WARN", "FORECAST_RESOLVE_FAILED", f"{r['market_id']}: {exc}")
            continue
        prices = resolution_prices(market)
        if prices is None:
            continue
        tokens = json.loads(market["clobTokenIds"]) if isinstance(market.get("clobTokenIds"), str) \
            else market.get("clobTokenIds")
        outcome_yes = prices[str(tokens[0])]
        for f in db.q("SELECT * FROM forecasts WHERE market_id=? AND resolved_at IS NULL",
                      (r["market_id"],)):
            f = dict(f)
            payout = (outcome_yes if f["side"] == "YES" else 1 - outcome_yes) if f["side"] else 0.0
            brier = ((f["probability_yes"] or 0.5) - outcome_yes) ** 2
            hyp = hyp_ret = None
            if f["side"] and f["all_in_price"] and f["shares"]:
                hyp = (payout - f["all_in_price"]) * f["shares"]
                hyp_ret = (payout - f["all_in_price"]) / f["all_in_price"]
            realized, reasons = None, []
            if f["signal_id"]:
                pos = db.q("SELECT p.realized_pnl, p.status, p.close_reason FROM positions p "
                           "JOIN orders o ON o.position_id=p.position_id "
                           "WHERE o.signal_id=? AND p.book='MAIN' AND o.side='BUY' "
                           "GROUP BY p.position_id", (f["signal_id"],))
                if pos:
                    realized = sum(p["realized_pnl"] for p in pos if p["status"] != "OPEN") \
                        if all(p["status"] != "OPEN" for p in pos) else None
                    reasons = [p["close_reason"] or "" for p in pos]
            tags = _mistake_tags(f, payout, realized, reasons)
            text = _review_text(f, outcome_yes, payout, brier, realized, tags)
            db.conn.execute(
                "UPDATE forecasts SET resolved_at=?, outcome_yes=?, side_payout=?, brier=?, "
                "direction_correct=?, hypothetical_pnl=?, hypothetical_return=?, realized_pnl=?, "
                "mistake_tags_json=?, review_text=?, postmortem_status=? WHERE forecast_id=?",
                (iso(now), outcome_yes, payout if f["side"] else None, brier,
                 None if not f["side"] else int(payout >= 0.999), hyp, hyp_ret, realized,
                 json.dumps(tags), text, "PENDING", f["forecast_id"]),
            )
            resolved.append(f["forecast_id"])
    return resolved


# --------------------------------------------------------------- post-mortems
POSTMORTEM_SYSTEM = """You review a past prediction-market forecast after its outcome is known.
Be specific and honest. Distinguish bad reasoning from bad luck: a well-reasoned 70% forecast
that resolved NO is not automatically a mistake. Respond with ONLY a JSON object:
{"assessment": "1-3 sentences: what the reasoning got right or wrong",
 "mistake_type": "none" | "bad_luck" | "missed_evidence" | "misread_resolution" |
                 "overconfidence" | "stale_information" | "other",
 "lesson": "one sentence, a reusable rule for future forecasts"}"""


def run_postmortems(cfg: Config, db: Database, budget: AIBudget,
                    client_factory: Callable[[], Any], now: datetime, max_n: int) -> int:
    """Short Claude post-mortems for resolved forecasts that were traded or high-signal.
    No web search, small output; each call is budget-authorised and cost-recorded."""
    from paperbot.research import _final_text, _usage_numbers, parse_json_object
    rows = db.q("SELECT * FROM forecasts WHERE postmortem_status='PENDING' AND side IS NOT NULL "
                "ORDER BY (position_id IS NULL), resolved_at LIMIT ?", (max_n,))
    done = 0
    worst = budget.cost(cfg.research.model, 3000, 600)
    for f in rows:
        ok, why = budget.authorize(now, "postmortem", cfg.learning.max_postmortems_per_day, worst)
        if not ok:
            break
        prompt = (f"Question: {f['question']}\nOriginal forecast and reasoning: "
                  f"{f['probability_yes']:.0%} YES, confidence {f['confidence']}. {f['summary']}\n"
                  f"Evidence cited: {f['evidence_json'][:2500]}\n"
                  f"What happened: {f['review_text']}")
        status, cost, usage = "FAILED", 0.0, None
        try:
            resp = client_factory().messages.create(
                model=cfg.research.model, max_tokens=600, system=POSTMORTEM_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low"})
            usage = _usage_numbers(resp)
            cost = budget.cost(cfg.research.model, **usage)
            data = parse_json_object(_final_text(resp))
            db.conn.execute(
                "UPDATE forecasts SET llm_postmortem=?, lesson=?, postmortem_status='DONE' "
                "WHERE forecast_id=?",
                (json.dumps(data)[:3000], str(data.get("lesson", ""))[:400], f["forecast_id"]))
            status = getattr(resp, "stop_reason", "end_turn")
            done += 1
        except Exception as exc:
            db.conn.execute("UPDATE forecasts SET postmortem_status='FAILED' WHERE forecast_id=?",
                            (f["forecast_id"],))
            db.log_event(now, "WARN", "POSTMORTEM_FAILED", f"{type(exc).__name__}: {exc}"[:300])
        u = usage or {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                      "cache_write_tokens": 0, "web_searches": 0}
        db.record_api_cost(now=now, provider="anthropic", model=cfg.research.model,
                           purpose="postmortem", market_id=f["market_id"], cost_usd=cost,
                           status=str(status), request_id=None, **u)
    return done


def skip_postmortems(db: Database) -> None:
    """When paid post-mortems are off, keep the deterministic review only."""
    db.conn.execute("UPDATE forecasts SET postmortem_status='DETERMINISTIC_ONLY' "
                    "WHERE postmortem_status='PENDING'")


# --------------------------------------------------------------- track record
def track_record_notes(db: Database, category: Optional[str], n_lessons: int) -> str:
    """Calibration statistics and recent lessons, given to the research agent as data."""
    rows = db.q("SELECT probability_yes, confidence, outcome_yes, direction_correct, category "
                "FROM forecasts WHERE resolved_at IS NOT NULL")
    if not rows:
        return ""
    lines = [f"Resolved forecasts so far: {len(rows)}. "
             f"Mean Brier score {sum((r['probability_yes'] - r['outcome_yes']) ** 2 for r in rows) / len(rows):.3f}."]
    hi = [r for r in rows if (r["confidence"] or 0) >= 0.75 and r["direction_correct"] is not None]
    if hi:
        lines.append(f"When confidence >= 0.75, the chosen side was right "
                     f"{sum(r['direction_correct'] for r in hi)}/{len(hi)} times.")
    for lo, hi_ in ((0.0, 0.3), (0.3, 0.7), (0.7, 1.01)):
        b = [r for r in rows if lo <= r["probability_yes"] < hi_]
        if len(b) >= 3:
            lines.append(f"Forecasts {lo:.0%}-{min(hi_, 1):.0%} YES resolved YES "
                         f"{sum(r['outcome_yes'] for r in b) / len(b):.0%} of the time (n={len(b)}).")
    lessons = db.q("SELECT lesson, category FROM forecasts WHERE lesson IS NOT NULL AND lesson!='' "
                   "ORDER BY (category=?) DESC, resolved_at DESC LIMIT ?", (category or "", n_lessons))
    if lessons:
        lines.append("Lessons from past post-mortems:")
        lines += [f"- {l['lesson']}" for l in lessons]
    return "\n".join(lines)


def postmortems_today(db: Database, now: datetime) -> int:
    ds, de = day_bounds(now)
    return db.ai_calls_between(ds, de, ("postmortem",))
