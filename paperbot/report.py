"""Metrics and the plain-language performance / failure report.

Every number comes from the SQLite database. Equity values are liquidation
values (what positions would fetch on the bid side today, after fees), so
they are conservative and never based on midpoints.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from paperbot.budget import month_bounds
from paperbot.db import Database, parse_iso

DISCLAIMER = (
    "PAPER TRADING SIMULATION with virtual money. No real funds, wallets or orders are "
    "involved. Simulated fills are approximations of what might have happened. No strategy "
    "here is risk-free, and simulated results do not predict real results."
)


def compute_metrics(db: Database, now: Optional[datetime] = None, cap_usd: float = 10.0) -> dict:
    now = now or datetime.now(timezone.utc)
    start = db.starting_capital()
    last = db.q1("SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
    equity = float(last["equity"]) if last else db.cash()
    snaps = db.q("SELECT ts, equity, drawdown FROM equity_snapshots ORDER BY ts")
    max_dd = max((float(s["drawdown"]) for s in snaps), default=0.0)

    # A "trade" is one strategy entry in one market: an arbitrage YES+NO pair counts
    # once (its legs are summed), so a winning pair is not reported as 1 win + 1 loss.
    groups = db.q(
        "SELECT market_id, strategy, SUM(realized_pnl) pnl, "
        "SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) open_legs "
        "FROM positions GROUP BY market_id, strategy")
    closed = [g for g in groups if g["open_legs"] == 0]
    wins = sum(1 for g in closed if g["pnl"] > 1e-9)
    ms, me = month_bounds(now)
    ai_month = db.ai_spend_between(ms, me)
    ai_total = db.ai_spend_total()

    orders = db.q("SELECT status, COUNT(*) c FROM orders GROUP BY status")
    order_counts = {r["status"]: r["c"] for r in orders}
    slippage = db.q1(
        "SELECT COALESCE(SUM((avg_price - signal_price) * filled_shares), 0) s FROM orders "
        "WHERE side='BUY' AND filled_shares > 0 AND signal_price > 0")["s"]

    sig_rows = db.q("SELECT kind, status, reasons_json FROM signals")
    signal_counts: dict[str, int] = {}
    reject_reasons: dict[str, int] = {}
    for r in sig_rows:
        key = f"{r['kind']}:{r['status']}"
        signal_counts[key] = signal_counts.get(key, 0) + 1
        if r["status"] == "REJECTED":
            for reason in json.loads(r["reasons_json"] or "[]"):
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1

    runs = db.q("SELECT status, COUNT(*) c FROM runs GROUP BY status")
    last_run = db.q1("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    last_ok = db.q1("SELECT started_at FROM runs WHERE status='OK' ORDER BY started_at DESC LIMIT 1")
    errors_24h = db.q1("SELECT COUNT(*) c FROM events WHERE level='ERROR' AND ts>=?",
                       ((now - timedelta(hours=24)).isoformat(),))["c"]
    api_stats = json.loads(last_run["api_stats_json"]) if last_run else {}

    health = "UNKNOWN"
    if last_run:
        age_h = (now - parse_iso(last_run["started_at"])).total_seconds() / 3600
        if last_run["status"] in ("FAILED", "CRASHED") or age_h > 3:
            health = "CRITICAL"
        elif last_run["status"] == "DEGRADED" or errors_24h > 0:
            health = "WARNING"
        elif last_run["status"] == "OK":
            health = "GOOD"

    net = equity - start
    return {
        "generated_at": now.isoformat(),
        "starting_capital": start,
        "equity": equity,
        "cash": db.cash(),
        "positions_value": float(last["positions_value"]) if last else 0.0,
        "net_pnl": net,
        "net_pnl_pct": net / start if start else 0.0,
        "net_pnl_after_ai_costs": net - ai_total,
        "realized_pnl": db.realized_pnl(),
        "unrealized_pnl": float(last["unrealized_pnl"]) if last else 0.0,
        "fees_paid": db.total_fees(),
        "slippage_vs_signal_usd": float(slippage),
        "ai_cost_month": ai_month,
        "ai_cost_total": ai_total,
        "ai_cap": cap_usd,
        "max_drawdown": max_dd,
        "current_drawdown": float(last["drawdown"]) if last else 0.0,
        "closed_trades": len(closed),
        "wins": wins,
        "win_rate": wins / len(closed) if closed else None,
        "open_positions": len(db.open_positions()),
        "unmarked_positions": int(last["unmarked_positions"]) if last else 0,
        "orders": order_counts,
        "signals": signal_counts,
        "reject_reasons": dict(sorted(reject_reasons.items(), key=lambda kv: -kv[1])),
        "runs": {r["status"]: r["c"] for r in runs},
        "last_run": dict(last_run) if last_run else None,
        "last_ok_run": last_ok["started_at"] if last_ok else None,
        "errors_24h": errors_24h,
        "api_stats_last_run": api_stats,
        "health": health,
        "halted": db.get_meta("halted", "0") == "1",
        "equity_curve": [(s["ts"], float(s["equity"])) for s in snaps],
    }


def _usd(x: float) -> str:
    x = round(x, 2) + 0.0  # avoid "-$0.00"
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def render_markdown(m: dict, db: Database) -> str:
    wr = f"{m['win_rate']:.0%} ({m['wins']}/{m['closed_trades']})" if m["win_rate"] is not None \
        else "n/a (no closed trades yet)"
    lines = [
        "# Paperbot report",
        "",
        f"_Generated {m['generated_at']}_",
        "",
        f"> {DISCLAIMER}",
        "",
        "## Net results",
        "",
        "| Metric | Value |", "|---|---|",
        f"| Starting virtual capital | {_usd(m['starting_capital'])} |",
        f"| Equity (liquidation value) | {_usd(m['equity'])} |",
        f"| Net P&L | {_usd(m['net_pnl'])} ({m['net_pnl_pct']:+.2%}) |",
        f"| Net P&L after AI costs | {_usd(m['net_pnl_after_ai_costs'])} |",
        f"| Realized P&L | {_usd(m['realized_pnl'])} |",
        f"| Unrealized P&L (at bids, after fees) | {_usd(m['unrealized_pnl'])} |",
        f"| Trading fees paid (simulated) | {_usd(m['fees_paid'])} |",
        f"| Price slippage vs signal | {_usd(m['slippage_vs_signal_usd'])} |",
        f"| AI API cost this month / cap | {_usd(m['ai_cost_month'])} / {_usd(m['ai_cap'])} |",
        f"| Max drawdown | {m['max_drawdown']:.2%} |",
        f"| Win rate (closed/settled positions) | {wr} |",
        f"| Open positions | {m['open_positions']} |",
        "",
        "## Bot health",
        "",
        f"- Health: **{m['health']}**" + (" — **NEW ENTRIES HALTED (max drawdown)**" if m["halted"] else ""),
        f"- Last run: {m['last_run']['started_at'] if m['last_run'] else 'never'} "
        f"({m['last_run']['status'] if m['last_run'] else '-'})",
        f"- Last fully OK run: {m['last_ok_run'] or 'never'}",
        f"- Errors in last 24h: {m['errors_24h']}",
        f"- Runs by status: {m['runs']}",
        f"- Polymarket API (last run): {m['api_stats_last_run']}",
        "",
        "## Failures",
        "",
        f"- Orders by status: {m['orders'] or 'none'}",
        f"- Signals by status: {m['signals'] or 'none'}",
        f"- Top rejection reasons: {dict(list(m['reject_reasons'].items())[:10]) or 'none'}",
        "",
        "### Recent errors",
        "",
    ]
    events = db.q("SELECT ts, kind, message FROM events WHERE level='ERROR' ORDER BY ts DESC LIMIT 15")
    lines += [f"- `{e['ts'][:19]}` **{e['kind']}** — {e['message'][:300]}" for e in events] or ["- none"]
    lines += ["", "## Open positions", ""]
    pos = db.open_positions()
    if pos:
        lines += ["| Market | Side | Strategy | Shares | Cost | Value now |", "|---|---|---|---|---|---|"]
        for p in pos:
            mv = _usd(p["mark_value"]) if p["mark_value"] is not None else "unmarked"
            lines.append(f"| {p['question'][:70]} | {p['outcome']} | {p['strategy']} | "
                         f"{p['shares']:.2f} | {_usd(p['cost_basis'])} | {mv} |")
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def write_report(db: Database, out_dir: str | Path, cap_usd: float,
                 now: Optional[datetime] = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    m = compute_metrics(db, now, cap_usd)
    (out / "report.md").write_text(render_markdown(m, db), encoding="utf-8")
    slim = {k: v for k, v in m.items() if k != "equity_curve"}
    (out / "metrics.json").write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
    return m


def as_text_summary(m: dict) -> str:
    return (f"equity={_usd(m['equity'])} net={_usd(m['net_pnl'])} "
            f"fees={_usd(m['fees_paid'])} ai_month={_usd(m['ai_cost_month'])} "
            f"health={m['health']}")

