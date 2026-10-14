"""Learning loop, part 2: strategy evaluation and validated self-improvement.

1. Evaluation (every run, free): performance by strategy, research method
   (model + prompt version), category, confidence bucket, edge bucket and side;
   calibration table; mistake-tag counts; recent lessons.
2. Proposals (at most every `proposal_interval_days`, free): using only resolved
   forecasts known at that time (TRAINING data), search a small grid of bounded
   strategy filters - min_edge, min_confidence, blocked categories - for a change
   that would have improved results. At most one proposal is under test at a time.
   Risk limits are NEVER proposed or changed.
3. Out-of-sample test: a proposal is judged ONLY on forecasts created AFTER it was
   proposed (new data it could not have been fitted to). It passes when, on at
   least `min_test_samples` new resolved forecasts, it beats the unchanged baseline
   by `improvement_margin` per $1 staked. Otherwise it keeps testing until
   `max_test_samples`, then is rejected.
4. Applying: a passed proposal is written to `strategy_params` (versioned, with the
   proposal id) if `auto_apply` is on; otherwise it waits for owner approval.

Metric: hypothetical return per $1 staked at the decision-time executable price
(fees included), summed over the forecasts a policy would have traded.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from paperbot.config import Config
from paperbot.db import Database, iso, new_id, parse_iso

PARAM_KEYS = ("min_edge", "min_confidence", "blocked_categories")


# ------------------------------------------------------------------ params
def current_params(db: Database, cfg: Config) -> dict:
    base = {"min_edge": cfg.research.min_edge, "min_confidence": cfg.research.min_confidence,
            "blocked_categories": sorted(cfg.research.blocked_categories)}
    row = db.q1("SELECT params_json FROM strategy_params ORDER BY version DESC LIMIT 1")
    if row:
        base.update({k: v for k, v in json.loads(row["params_json"]).items() if k in PARAM_KEYS})
    return base


def validate_params(params: dict, cfg: Config) -> None:
    lo, hi = cfg.learning.min_edge_bounds
    if not (lo - 1e-9 <= params["min_edge"] <= hi + 1e-9):
        raise ValueError(f"min_edge {params['min_edge']} outside bounds {lo}-{hi}")
    lo, hi = cfg.learning.min_confidence_bounds
    if not (lo - 1e-9 <= params["min_confidence"] <= hi + 1e-9):
        raise ValueError(f"min_confidence {params['min_confidence']} outside bounds {lo}-{hi}")
    if not isinstance(params["blocked_categories"], list):
        raise ValueError("blocked_categories must be a list")


def apply_params(cfg: Config, params: dict) -> Config:
    """Effective config for this run: validated strategy filters over the YAML values."""
    validate_params(params, cfg)
    research = dataclasses.replace(
        cfg.research, min_edge=params["min_edge"], min_confidence=params["min_confidence"],
        blocked_categories=tuple(params["blocked_categories"]))
    return dataclasses.replace(cfg, research=research)


def params_version(db: Database) -> int:
    row = db.q1("SELECT MAX(version) v FROM strategy_params")
    return int(row["v"] or 0)


# ------------------------------------------------------------------ policies
def takes(f: dict, p: dict) -> bool:
    return (f["side"] is not None and f["edge"] is not None and f["edge"] >= p["min_edge"] - 1e-12
            and (f["confidence"] or 0) >= p["min_confidence"] - 1e-12
            and (f["category"] or "") not in set(p["blocked_categories"]))


def policy_result(forecasts: list[dict], p: dict) -> dict:
    taken = [f for f in forecasts if takes(f, p) and f["hypothetical_return"] is not None]
    total = sum(f["hypothetical_return"] for f in taken)
    return {"n_taken": len(taken), "sum_return_per_usd": round(total, 4),
            "mean_return": round(total / len(taken), 4) if taken else None,
            "wins": sum(1 for f in taken if (f["side_payout"] or 0) >= 0.999)}


def resolved_forecasts(db: Database, created_after: Optional[datetime] = None,
                       created_before: Optional[datetime] = None) -> list[dict]:
    sql = "SELECT * FROM forecasts WHERE resolved_at IS NOT NULL AND side IS NOT NULL"
    args: list = []
    if created_after:
        sql += " AND created_at > ?"
        args.append(iso(created_after))
    if created_before:
        sql += " AND created_at <= ?"
        args.append(iso(created_before))
    return [dict(r) for r in db.q(sql + " ORDER BY created_at", tuple(args))]


# ------------------------------------------------------------------ proposals
def _candidates(base: dict, cfg: Config, train: list[dict]) -> list[tuple[dict, str]]:
    out = []
    lo, hi = cfg.learning.min_edge_bounds
    for e in (0.03, 0.05, 0.07, 0.10, 0.13, 0.16, 0.20):
        if lo - 1e-9 <= e <= hi + 1e-9 and abs(e - base["min_edge"]) > 1e-9:
            out.append(({**base, "min_edge": e}, f"change min_edge {base['min_edge']:.2f} -> {e:.2f}"))
    lo, hi = cfg.learning.min_confidence_bounds
    for c in (0.5, 0.6, 0.7, 0.8, 0.9):
        if lo - 1e-9 <= c <= hi + 1e-9 and abs(c - base["min_confidence"]) > 1e-9:
            out.append(({**base, "min_confidence": c},
                        f"change min_confidence {base['min_confidence']:.2f} -> {c:.2f}"))
    cats: dict[str, list[float]] = {}
    for f in train:
        if takes(f, base):
            cats.setdefault(f["category"] or "", []).append(f["hypothetical_return"])
    for cat, rets in cats.items():
        if cat and len(rets) >= 5 and sum(rets) < 0 and cat not in base["blocked_categories"]:
            out.append(({**base, "blocked_categories": sorted(base["blocked_categories"] + [cat])},
                        f"stop researching category '{cat}' (lost {sum(rets):.2f}/$ over {len(rets)})"))
    return out


def maybe_propose(db: Database, cfg: Config, now: datetime) -> Optional[str]:
    lc = cfg.learning
    if db.q1("SELECT 1 FROM proposals WHERE status='TESTING'"):
        return None
    last = db.q1("SELECT MAX(created_at) t FROM proposals")["t"]
    if last and now - parse_iso(last) < timedelta(days=lc.proposal_interval_days):
        return None
    train = resolved_forecasts(db, created_before=now)
    if len(train) < lc.min_train_samples:
        return None
    base = current_params(db, cfg)
    base_res = policy_result(train, base)
    best = None
    for params, why in _candidates(base, cfg, train):
        res = policy_result(train, params)
        gain = res["sum_return_per_usd"] - base_res["sum_return_per_usd"]
        if res["n_taken"] >= 5 and gain >= lc.improvement_margin * max(base_res["n_taken"], 1):
            if best is None or gain > best[2]:
                best = (params, why, gain, res)
    if best is None:
        return None
    params, why, gain, res = best
    pid = new_id()
    db.conn.execute(
        "INSERT INTO proposals(proposal_id,created_at,status,params_json,baseline_json,rationale,"
        "train_json) VALUES(?,?,?,?,?,?,?)",
        (pid, iso(now), "TESTING", json.dumps(params), json.dumps(base),
         f"{why}. On {len(train)} resolved past forecasts (training data) this would have "
         f"changed the result by {gain:+.2f} per $1 staked. Must now prove itself on NEW data.",
         json.dumps({"n_train": len(train), "baseline": base_res, "candidate": res})))
    db.log_event(now, "INFO", "PROPOSAL_CREATED", why)
    return pid


def test_proposals(db: Database, cfg: Config, now: datetime) -> list[dict]:
    """Out-of-sample evaluation of proposals under test, on forecasts made after proposing."""
    lc = cfg.learning
    out = []
    for p in db.q("SELECT * FROM proposals WHERE status='TESTING'"):
        created = parse_iso(p["created_at"])
        new = resolved_forecasts(db, created_after=created)
        cand, base = json.loads(p["params_json"]), json.loads(p["baseline_json"])
        rc, rb = policy_result(new, cand), policy_result(new, base)
        gain = rc["sum_return_per_usd"] - rb["sum_return_per_usd"]
        test = {"n_new": len(new), "candidate": rc, "baseline": rb, "gain": round(gain, 4)}
        status, note = "TESTING", f"{len(new)}/{lc.min_test_samples} new resolved forecasts"
        if len(new) >= lc.min_test_samples:
            if gain >= lc.improvement_margin * len(new) and rc["n_taken"] >= 3:
                status = "PASSED"
                note = f"beat baseline by {gain:+.2f}/$ on {len(new)} new forecasts"
            elif len(new) >= lc.max_test_samples:
                status = "REJECTED"
                note = f"no out-of-sample improvement after {len(new)} new forecasts ({gain:+.2f}/$)"
        if status == "PASSED":
            if lc.auto_apply:
                validate_params(cand, cfg)
                db.conn.execute("INSERT INTO strategy_params(applied_at,params_json,proposal_id,note) "
                                "VALUES(?,?,?,?)", (iso(now), json.dumps(cand), p["proposal_id"], note))
                status = "APPLIED"
                db.log_event(now, "INFO", "PROPOSAL_APPLIED", f"{p['rationale'][:200]} | {note}")
            else:
                status = "AWAITING_APPROVAL"
                db.log_event(now, "WARN", "PROPOSAL_AWAITING_APPROVAL", note)
        db.conn.execute("UPDATE proposals SET status=?, test_json=?, decision_note=?, decided_at=? "
                        "WHERE proposal_id=?",
                        (status, json.dumps(test), note,
                         iso(now) if status != "TESTING" else None, p["proposal_id"]))
        out.append({"proposal_id": p["proposal_id"], "status": status, **test})
    return out


# ------------------------------------------------------------------ evaluation
def _bucket(x: Optional[float], edges: list[float]) -> str:
    if x is None:
        return "n/a"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= x < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return f">={edges[-1]:.2f}"


def evaluate(db: Database, cfg: Config) -> dict:
    fs = [dict(r) for r in db.q("SELECT * FROM forecasts WHERE resolved_at IS NOT NULL")]
    params = current_params(db, cfg)

    def group(keyfn) -> dict:
        g: dict[str, list[dict]] = {}
        for f in fs:
            g.setdefault(keyfn(f), []).append(f)
        out = {}
        for k, rows in sorted(g.items()):
            traded = [r for r in rows if r["side"]]
            out[k] = {
                "n": len(rows),
                "brier": round(sum(r["brier"] for r in rows) / len(rows), 4),
                "direction_right": sum(r["direction_correct"] or 0 for r in traded),
                "with_side": len(traded),
                "hypothetical_return_per_usd": round(sum(r["hypothetical_return"] or 0 for r in traded), 4),
                "realized_pnl": round(sum(r["realized_pnl"] or 0 for r in rows), 4),
            }
        return out

    calib = []
    for lo, hi in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)):
        b = [f for f in fs if lo <= (f["probability_yes"] or 0) < hi]
        if b:
            calib.append({"bucket": f"{lo:.0%}-{min(hi, 1):.0%}", "n": len(b),
                          "mean_forecast": round(sum(f["probability_yes"] for f in b) / len(b), 3),
                          "actual_yes_rate": round(sum(f["outcome_yes"] for f in b) / len(b), 3)})
    tags: dict[str, int] = {}
    for f in fs:
        for t in json.loads(f["mistake_tags_json"] or "[]"):
            tags[t] = tags.get(t, 0) + 1

    strategies = {}
    for strat in ("research", "arbitrage"):
        groups = db.q("SELECT market_id, SUM(realized_pnl) pnl, SUM(fees_paid) fees, "
                      "SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) open_legs FROM positions "
                      "WHERE book='MAIN' AND strategy=? GROUP BY market_id", (strat,))
        closed = [g for g in groups if g["open_legs"] == 0]
        strategies[strat] = {
            "closed_trades": len(closed), "open_trades": len(groups) - len(closed),
            "wins": sum(1 for g in closed if g["pnl"] > 1e-9),
            "realized_pnl": round(sum(g["pnl"] for g in closed), 4),
            "fees": round(sum(g["fees"] for g in groups), 4),
        }
    arb_sig = db.q("SELECT status, COUNT(*) c FROM signals WHERE kind='arbitrage' GROUP BY status")
    strategies["arbitrage"]["signal_outcomes"] = {r["status"]: r["c"] for r in arb_sig}

    return {
        "resolved_forecasts": len(fs),
        "unresolved_forecasts": db.q1("SELECT COUNT(*) c FROM forecasts WHERE resolved_at IS NULL")["c"],
        "strategies": strategies,
        "by_method": group(lambda f: f"{f['model']} / {f['prompt_version']}"),
        "by_category": group(lambda f: f["category"] or "unknown"),
        "by_confidence": group(lambda f: _bucket(f["confidence"], [0, .5, .6, .7, .8, .9])),
        "by_edge": group(lambda f: _bucket(f["edge"], [0, .03, .05, .08, .12, .2])),
        "by_side": group(lambda f: f["side"] or "none"),
        "by_purpose": group(lambda f: f["purpose"]),
        "calibration": calib,
        "mistake_tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])),
        "lessons": [dict(r) for r in db.q(
            "SELECT question, lesson, resolved_at FROM forecasts WHERE lesson IS NOT NULL "
            "AND lesson!='' ORDER BY resolved_at DESC LIMIT 10")],
        "current_params": params, "params_version": params_version(db),
        "proposals": [dict(r) for r in db.q("SELECT * FROM proposals ORDER BY created_at DESC LIMIT 10")],
    }


def render(ev: dict) -> str:
    def table(title: str, groups: dict) -> list[str]:
        if not groups:
            return [f"### {title}", "", "No resolved forecasts yet.", ""]
        L = [f"### {title}", "", "| Group | n | Brier | Side right | Hypothetical return/$ | Realized P&L |",
             "|---|---|---|---|---|---|"]
        for k, g in groups.items():
            L.append(f"| {k} | {g['n']} | {g['brier']:.3f} | {g['direction_right']}/{g['with_side']} | "
                     f"{g['hypothetical_return_per_usd']:+.2f} | ${g['realized_pnl']:+.2f} |")
        return L + [""]

    L = ["# Strategy evaluation", "",
         "> Lower Brier is better (0 perfect, 0.25 = always saying 50%). 'Hypothetical return/$' "
         "is what $1 per forecast at the decision-time executable price (after fees) would "
         "have returned; it covers rejected ideas too and is used only for evaluation.", "",
         f"Resolved forecasts: {ev['resolved_forecasts']}, awaiting resolution: {ev['unresolved_forecasts']}.",
         f"Current strategy filters (version {ev['params_version']}): {ev['current_params']}", "",
         "## Strategies (MAIN book, verified fills only)", "",
         "| Strategy | Closed trades | Wins | Realized P&L | Fees | Open |", "|---|---|---|---|---|---|"]
    for k, s in ev["strategies"].items():
        L.append(f"| {k} | {s['closed_trades']} | {s['wins']} | ${s['realized_pnl']:+.2f} | "
                 f"${s['fees']:.2f} | {s['open_trades']} |")
    L += ["", "## Research methods and forecast quality", ""]
    for title, key in (("By method (model / prompt)", "by_method"), ("By category", "by_category"),
                       ("By confidence", "by_confidence"), ("By edge at entry", "by_edge"),
                       ("By side", "by_side"), ("Entry research vs position re-reviews", "by_purpose")):
        L += table(title, ev[key])
    L += ["### Calibration", ""]
    if ev["calibration"]:
        L += ["| Forecast bucket | n | Mean forecast | Actual YES rate |", "|---|---|---|---|"]
        L += [f"| {c['bucket']} | {c['n']} | {c['mean_forecast']:.0%} | {c['actual_yes_rate']:.0%} |"
              for c in ev["calibration"]]
    else:
        L.append("No data yet.")
    L += ["", "## Mistakes and lessons", "", f"Mistake tags: {ev['mistake_tags'] or 'none yet'}", ""]
    L += [f"- {l['lesson']} _(from: {l['question'][:70]})_" for l in ev["lessons"]] or ["No lessons yet."]
    L += ["", "## Improvement proposals (tested on new data before applying)", ""]
    if ev["proposals"]:
        L += ["| Created | Status | Rationale | Out-of-sample result |", "|---|---|---|---|"]
        for p in ev["proposals"]:
            L.append(f"| {p['created_at'][:10]} | {p['status']} | {p['rationale']} | "
                     f"{p['decision_note'] or 'testing'} |")
    else:
        L.append("None yet. Proposals start after "
                 "enough resolved forecasts exist (see learning.min_train_samples).")
    return "\n".join(L) + "\n"


def write_evaluation(db: Database, cfg: Config, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ev = evaluate(db, cfg)
    (out / "evaluation.json").write_text(json.dumps(ev, indent=2, default=str), encoding="utf-8")
    path = out / "evaluation.md"
    path.write_text(render(ev), encoding="utf-8")
    return path
