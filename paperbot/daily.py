"""Daily report (UTC day), in five strictly separated sections:

 1. Hypothetical opportunities  - what the scanners saw. NOT achievable P&L.
 2. Conservatively simulated executions - every order with its full decision
    record: intended order, liquidity at decision / execution / verification,
    fill status, verification result, fees, resulting position, exit
    conditions, realized and mark-to-market P&L.
 3. Completed positions         - closed or settled during the day (MAIN book).
 4. Unresolved positions        - still open at the end of the day, valued at
    their liquidation value on the bid side (MAIN book); plus the SHADOW book
    of unverified fills, reported separately and never counted.
 5. Net performance after all costs - equity change (includes trading fees and
    slippage) minus AI API costs and infrastructure costs, for the day and
    cumulatively since start.
"""

from __future__ import annotations

import calendar
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

from paperbot.db import Database, iso, parse_iso
from paperbot.report import DISCLAIMER


def _usd(x: Optional[float]) -> str:
    if x is None:
        return "–"
    x = round(x, 2) + 0.0
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _obs(liquidity: list, label: str) -> Optional[dict]:
    return next((o for o in liquidity if o.get("label") == label), None)


def _equity_before(db: Database, ts: datetime) -> Optional[float]:
    row = db.q1("SELECT equity FROM equity_snapshots WHERE ts<? ORDER BY ts DESC LIMIT 1", (iso(ts),))
    return float(row["equity"]) if row else None


def _positions_open_at(db: Database, end: datetime, book: str) -> list[dict]:
    rows = db.q(
        "SELECT * FROM positions WHERE book=? AND opened_at<? AND (closed_at IS NULL OR closed_at>=?)"
        " ORDER BY opened_at", (book, iso(end), iso(end)))
    out = []
    for p in rows:
        mark = db.q1("SELECT * FROM position_marks WHERE position_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                     (p["position_id"], iso(end)))
        shares = mark["shares"] if mark else p["shares"]
        basis = mark["cost_basis"] if mark else p["cost_basis"]
        value = mark["liquidation_value"] if mark else None
        out.append({
            "position_id": p["position_id"], "market_id": p["market_id"], "question": p["question"],
            "outcome": p["outcome"], "strategy": p["strategy"], "opened_at": p["opened_at"],
            "shares": shares, "cost_basis": basis, "liquidation_value": value,
            "mtm_pnl": (value - basis) if value is not None else None,
            "marked_at": mark["ts"] if mark else None,
            "exit_conditions": json.loads(p["exit_conditions_json"] or "{}"),
            "resolution_date": p["end_date"],
        })
    return out


def build_daily(db: Database, day: date, infra_cost_usd_per_month: float = 0.0) -> dict:
    start, end = _bounds(day)
    s, e = iso(start), iso(end)

    # 1. hypothetical opportunities
    sigs = db.q("SELECT * FROM signals WHERE ts>=? AND ts<? ORDER BY ts", (s, e))
    opportunities = [{
        "ts": r["ts"], "kind": r["kind"], "market_id": r["market_id"], "question": r["question"],
        "outcome": r["outcome"], "status": r["status"], "reasons": json.loads(r["reasons_json"] or "[]"),
        "edge": r["edge"], "theoretical_net_usd": r["expected_net_usd"], "cost_usd": r["cost_usd"],
        "details": json.loads(r["details_json"] or "{}"),
    } for r in sigs]
    by_status: dict[str, int] = {}
    for o in opportunities:
        k = f"{o['kind']}:{o['status']}"
        by_status[k] = by_status.get(k, 0) + 1

    # 2. simulated executions (decision log)
    orders = db.q("SELECT * FROM orders WHERE created_at>=? AND created_at<? ORDER BY created_at", (s, e))
    decisions = []
    for o in orders:
        liq = json.loads(o["liquidity_json"] or "[]")
        pos = db.q1("SELECT * FROM positions WHERE position_id=?", (o["position_id"],)) \
            if o["position_id"] else None
        mtm = None
        if pos is not None and pos["status"] == "OPEN" and pos["mark_value"] is not None:
            mtm = pos["mark_value"] - pos["cost_basis"]
        decisions.append({
            "decision_at": o["decision_at"] or o["created_at"], "order_id": o["order_id"],
            "strategy": o["strategy"], "market_id": o["market_id"], "token_id": o["token_id"],
            "side": o["side"], "outcome": o["outcome"],
            "intended": {"shares": o["requested_shares"], "limit_price": o["limit_price"],
                         "notional": o["intended_notional"], "signal_price": o["signal_price"]},
            "liquidity": {lbl: _obs(liq, lbl) for lbl in ("decision", "execution", "verify_1",
                                                         "verify_2", "verify_3")
                          if _obs(liq, lbl)},
            "status": o["status"], "verification": o["verification"],
            "confirmed_shares": o["filled_shares"], "avg_price": o["avg_price"],
            "notional": o["notional"], "fee": o["fee"],
            "uncertain_shares": o["uncertain_shares"], "uncertain_notional": o["uncertain_notional"],
            "latency_ms": o["latency_ms"], "reason": o["reason"],
            "timing": json.loads(o["timing_json"] or "{}"),
            "position": None if pos is None else {
                "position_id": pos["position_id"], "book": pos["book"], "status": pos["status"],
                "shares_now": pos["shares"], "exit_conditions": json.loads(pos["exit_conditions_json"] or "{}"),
                "realized_pnl": pos["realized_pnl"], "mtm_pnl": mtm, "close_reason": pos["close_reason"],
            },
        })
    exec_summary = {
        "orders": len(decisions),
        "by_status": {}, "by_verification": {},
        "confirmed_notional": sum(d["notional"] for d in decisions),
        "fees": sum(d["fee"] for d in decisions),
        "uncertain_notional": sum(d["uncertain_notional"] or 0 for d in decisions),
        "slippage_vs_signal_usd": sum(
            (d["avg_price"] - d["intended"]["signal_price"]) * d["confirmed_shares"]
            for d in decisions if d["side"] == "BUY" and d["avg_price"] and d["intended"]["signal_price"]),
    }
    for d in decisions:
        exec_summary["by_status"][d["status"]] = exec_summary["by_status"].get(d["status"], 0) + 1
        v = d["verification"] or "N/A"
        exec_summary["by_verification"][v] = exec_summary["by_verification"].get(v, 0) + 1

    # 3. completed positions (grouped as trades: an arbitrage pair is one trade)
    closed = db.q("SELECT * FROM positions WHERE closed_at>=? AND closed_at<? ORDER BY closed_at", (s, e))
    groups: dict[tuple, dict] = {}
    for p in closed:
        key = (p["book"], p["market_id"], p["strategy"])
        g = groups.setdefault(key, {"book": p["book"], "market_id": p["market_id"],
                                    "question": p["question"], "strategy": p["strategy"],
                                    "legs": [], "realized_pnl": 0.0, "fees": 0.0})
        g["legs"].append({"outcome": p["outcome"], "status": p["status"],
                          "close_reason": p["close_reason"], "settle_price": p["settle_price"],
                          "realized_pnl": p["realized_pnl"], "opened_at": p["opened_at"],
                          "closed_at": p["closed_at"]})
        g["realized_pnl"] += p["realized_pnl"]
        g["fees"] += p["fees_paid"]
    completed = [g for g in groups.values() if g["book"] == "MAIN"]
    completed_shadow = [g for g in groups.values() if g["book"] == "SHADOW"]

    # 4. unresolved positions at end of day
    unresolved = _positions_open_at(db, end, "MAIN")
    unresolved_shadow = _positions_open_at(db, end, "SHADOW")

    # 5. net performance after all costs
    starting = db.starting_capital()
    eq_start = _equity_before(db, start)
    eq_end = _equity_before(db, end)
    if eq_start is None:
        eq_start = starting
    if eq_end is None:
        eq_end = eq_start
    first_snap = db.q1("SELECT MIN(ts) t FROM equity_snapshots")["t"]
    ai_day = db.ai_spend_between(start, end)
    ai_cum = float(db.q1("SELECT COALESCE(SUM(cost_usd),0) s FROM api_costs WHERE ts<?", (e,))["s"])
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    infra_day = infra_cost_usd_per_month / days_in_month
    first_day = parse_iso(first_snap).date() if first_snap else day
    infra_cum = infra_day * max(0, (day - first_day).days + 1)
    fees_day = -float(db.q1("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind='FEE' AND book='MAIN' "
                            "AND ts>=? AND ts<?", (s, e))["s"])
    net = {
        "starting_capital": starting,
        "equity_start_of_day": eq_start, "equity_end_of_day": eq_end,
        "trading_pnl_day": eq_end - eq_start,
        "trading_fees_day": fees_day,
        "realized_pnl_on_trades_closed_today": sum(g["realized_pnl"] for g in completed),
        "unrealized_pnl_end_of_day": sum(p["mtm_pnl"] or 0 for p in unresolved),
        "ai_cost_day": ai_day, "infra_cost_day": infra_day,
        "net_after_all_costs_day": (eq_end - eq_start) - ai_day - infra_day,
        "net_after_all_costs_cumulative": (eq_end - starting) - ai_cum - infra_cum,
        "ai_cost_cumulative": ai_cum, "infra_cost_cumulative": infra_cum,
        "shadow_unverified_mtm_end_of_day": sum(p["mtm_pnl"] or 0 for p in unresolved_shadow),
        "shadow_realized_today": sum(g["realized_pnl"] for g in completed_shadow),
    }
    return {
        "date": day.isoformat(), "generated_at": iso(datetime.now(timezone.utc)),
        "disclaimer": DISCLAIMER,
        "hypothetical_opportunities": {"count": len(opportunities), "by_status": by_status,
                                       "items": opportunities},
        "simulated_executions": {"summary": exec_summary, "decisions": decisions},
        "completed_positions": completed,
        "unresolved_positions": unresolved,
        "unverified_shadow": {"open": unresolved_shadow, "completed": completed_shadow},
        "net_performance": net,
    }


def _short(x: Optional[str], n: int = 60) -> str:
    x = (x or "").replace("|", "/")
    return x if len(x) <= n else x[: n - 1] + "…"


def render_daily(r: dict) -> str:
    n = r["net_performance"]
    L = [f"# Daily paper-trading report — {r['date']} (UTC)", "", f"> {r['disclaimer']}", "",
         f"**Summary:** net after all costs today {_usd(n['net_after_all_costs_day'])}, "
         f"since start {_usd(n['net_after_all_costs_cumulative'])} "
         f"(equity {_usd(n['equity_end_of_day'])} of {_usd(n['starting_capital'])} virtual).", ""]
    ho = r["hypothetical_opportunities"]
    L += ["## 1. Hypothetical opportunities (not achievable P&L)", "",
          f"{ho['count']} opportunities evaluated and recorded. By status: {ho['by_status'] or 'none'}.", ""]
    if ho["items"]:
        L += ["| Time | Kind | Market | Side | Status | Theoretical net | Reasons |",
              "|---|---|---|---|---|---|---|"]
        for o in ho["items"][:60]:
            L.append(f"| {o['ts'][11:16]} | {o['kind']} | {_short(o['question'])} | {o['outcome']} | "
                     f"{o['status']} | {_usd(o['theoretical_net_usd'])} | {_short(', '.join(o['reasons']), 80)} |")
        L.append("")

    se = r["simulated_executions"]
    sm = se["summary"]
    L += ["## 2. Conservatively simulated executions", "",
          f"{sm['orders']} orders. Status: {sm['by_status'] or 'none'}. "
          f"Verification: {sm['by_verification'] or 'none'}.",
          f"Confirmed notional {_usd(sm['confirmed_notional'])}, fees {_usd(sm['fees'])}, "
          f"slippage vs signal {_usd(sm['slippage_vs_signal_usd'])}, "
          f"unverified (shadow) notional {_usd(sm['uncertain_notional'])}.", ""]
    if se["decisions"]:
        L += ["| Decision | Strategy | Order | Liquidity dec/exec/verify (shares @limit) | Status / verification "
              "| Confirmed @ avg | Fee | Uncertain | Position | Exit rule | Realized | MTM |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for d in se["decisions"]:
            liq = d["liquidity"]
            us = lambda k: f"{liq[k]['usable_shares']:g}" if k in liq else "–"
            vs = "/".join(us(k) for k in ("verify_1", "verify_2", "verify_3") if k in liq) or "–"
            pos = d["position"] or {}
            ex = (pos.get("exit_conditions") or {}).get("rule", "–")
            avg = f"{d['avg_price']:.4f}" if d["avg_price"] else "–"
            L.append(
                f"| {d['decision_at'][11:19]} | {d['strategy'] or '–'} | {d['side']} {d['outcome']} "
                f"{d['intended']['shares']:g} @≤{d['intended']['limit_price']:g} | "
                f"{us('decision')} / {us('execution')} / {vs} | {d['status']} / {d['verification'] or '–'} | "
                f"{d['confirmed_shares']:g} @ {avg} | {_usd(d['fee'])} | {d['uncertain_shares']:g} | "
                f"{pos.get('book', '–')} {pos.get('status', '')} | {_short(ex, 50)} | "
                f"{_usd(pos.get('realized_pnl'))} | {_usd(pos.get('mtm_pnl'))} |")
        L.append("")

    L += ["## 3. Completed positions (closed or settled today)", ""]
    if r["completed_positions"]:
        L += ["| Market | Strategy | Legs | Realized P&L | Fees |", "|---|---|---|---|---|"]
        for g in r["completed_positions"]:
            legs = ", ".join(f"{l['outcome']} {l['status']} ({_short(l['close_reason'], 30)})" for l in g["legs"])
            L.append(f"| {_short(g['question'])} | {g['strategy']} | {legs} | {_usd(g['realized_pnl'])} | "
                     f"{_usd(g['fees'])} |")
    else:
        L.append("None.")
    L += ["", "## 4. Unresolved positions (open at end of day)", ""]
    if r["unresolved_positions"]:
        L += ["| Market | Side | Strategy | Shares | Cost | Liquidation value | MTM P&L | Resolves | Exit rule |",
              "|---|---|---|---|---|---|---|---|---|"]
        for p in r["unresolved_positions"]:
            L.append(f"| {_short(p['question'])} | {p['outcome']} | {p['strategy']} | {p['shares']:g} | "
                     f"{_usd(p['cost_basis'])} | {_usd(p['liquidation_value'])} | {_usd(p['mtm_pnl'])} | "
                     f"{(p['resolution_date'] or '–')[:10]} | {_short(p['exit_conditions'].get('rule'), 50)} |")
    else:
        L.append("None.")
    sh = r["unverified_shadow"]
    L += ["", "### Unverified fills (shadow book — tracked, never counted)", "",
          f"Open: {len(sh['open'])}, completed today: {len(sh['completed'])}.", ""]
    for p in sh["open"]:
        L.append(f"- {_short(p['question'])} {p['outcome']} {p['shares']:g} sh, cost {_usd(p['cost_basis'])}, "
                 f"MTM {_usd(p['mtm_pnl'])}")
    L.append("")
    L += ["## 5. Net performance after all costs", "",
         "| | Today | Since start |", "|---|---|---|",
         f"| Equity (liquidation value) | {_usd(n['equity_start_of_day'])} → {_usd(n['equity_end_of_day'])} "
         f"| {_usd(n['starting_capital'])} → {_usd(n['equity_end_of_day'])} |",
         f"| Trading P&L (after fees & slippage) | {_usd(n['trading_pnl_day'])} "
         f"| {_usd(n['equity_end_of_day'] - n['starting_capital'])} |",
         f"| Trading fees paid | {_usd(n['trading_fees_day'])} | |",
         f"| AI API costs | {_usd(n['ai_cost_day'])} | {_usd(n['ai_cost_cumulative'])} |",
         f"| Infrastructure costs | {_usd(n['infra_cost_day'])} | {_usd(n['infra_cost_cumulative'])} |",
         f"| **Net after all costs** | **{_usd(n['net_after_all_costs_day'])}** "
         f"| **{_usd(n['net_after_all_costs_cumulative'])}** |",
         "",
         f"Realized on trades closed today: {_usd(n['realized_pnl_on_trades_closed_today'])}. "
         f"Unrealized at end of day: {_usd(n['unrealized_pnl_end_of_day'])}. "
         f"Unverified (shadow) fills, NOT included above: mark-to-market "
         f"{_usd(n['shadow_unverified_mtm_end_of_day'])}, realized today {_usd(n['shadow_realized_today'])}.",
         ""]

    return "\n".join(L)


def write_daily(db: Database, out_dir: str | Path, day: date,
                infra_cost_usd_per_month: float = 0.0) -> Path:
    out = Path(out_dir) / "daily"
    out.mkdir(parents=True, exist_ok=True)
    r = build_daily(db, day, infra_cost_usd_per_month)
    (out / f"{day.isoformat()}.json").write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    path = out / f"{day.isoformat()}.md"
    path.write_text(render_daily(r), encoding="utf-8")
    return path
