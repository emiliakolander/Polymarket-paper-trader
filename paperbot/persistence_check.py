"""Cross-run persistence verification (used by the paperbot-verify workflow).

  python -m paperbot.persistence_check manifest --db DB --out manifest.json
  python -m paperbot.persistence_check verify   --db DB --before manifest.json [--report out.md]

`manifest` captures the state after run N. `verify` (after run N+1, typically on a
different machine) checks that the history was preserved and only appended to:
same starting capital and a single deposit, earlier ledger rows byte-identical,
cash reconciles with the ledger, every earlier order and position still present
(finished orders unchanged; open positions either unchanged or closed with a
recorded reason), and exactly one more run and equity snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from paperbot.db import Database


def _ledger_digest(db: Database, max_id: int) -> str:
    h = hashlib.sha256()
    for r in db.q("SELECT id,ts,kind,amount,order_id,token_id,book FROM ledger WHERE id<=? ORDER BY id",
                  (max_id,)):
        h.update(json.dumps([r[k] for k in r.keys()], default=str).encode())
    return h.hexdigest()


def manifest(db: Database) -> dict:
    max_id = db.q1("SELECT COALESCE(MAX(id),0) m FROM ledger")["m"]
    return {
        "data_source": db.get_meta("data_source"),
        "starting_capital": db.starting_capital(),
        "deposits": db.q1("SELECT COUNT(*) c FROM ledger WHERE kind='DEPOSIT'")["c"],
        "cash": round(db.cash(), 8),
        "ledger_max_id": max_id,
        "ledger_digest": _ledger_digest(db, max_id),
        "runs": [r["run_id"] for r in db.q("SELECT run_id FROM runs ORDER BY started_at")],
        "orders": {r["order_id"]: [r["status"], r["filled_shares"], r["notional"], r["fee"],
                                   r["verification"]]
                   for r in db.q("SELECT * FROM orders")},
        "positions": {r["position_id"]: {"status": r["status"], "shares": r["shares"],
                                         "cost_basis": r["cost_basis"], "book": r["book"],
                                         "realized_pnl": r["realized_pnl"]}
                      for r in db.q("SELECT * FROM positions")},
        "counts": {t: db.q1(f"SELECT COUNT(*) c FROM {t}")["c"]
                   for t in ("signals", "forecasts", "equity_snapshots", "position_marks",
                             "market_snapshots", "events")},
    }


def verify(before: dict, db: Database, expect_new_runs: int = 1) -> list[dict]:
    after = manifest(db)
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    check("same data source", after["data_source"] == before["data_source"],
          f"{before['data_source']} -> {after['data_source']}")
    check("virtual capital is $1,000 and deposited once",
          after["starting_capital"] == 1000.0 and after["deposits"] == 1,
          f"capital {after['starting_capital']}, deposits {after['deposits']}")
    check("earlier runs kept, exactly one new run",
          after["runs"][: len(before["runs"])] == before["runs"]
          and len(after["runs"]) == len(before["runs"]) + expect_new_runs,
          f"{len(before['runs'])} -> {len(after['runs'])} runs")
    check("earlier ledger rows unchanged",
          _ledger_digest(db, before["ledger_max_id"]) == before["ledger_digest"],
          f"{before['ledger_max_id']} rows hashed")
    new_flow = db.q1("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE id>? AND book='MAIN'",
                     (before["ledger_max_id"],))["s"]
    check("cash reconciles with ledger", abs(after["cash"] - (before["cash"] + new_flow)) < 1e-6,
          f"{before['cash']:.4f} + {new_flow:.4f} = {after['cash']:.4f}")
    missing = [o for o in before["orders"] if o not in after["orders"]]
    changed = [o for o, v in before["orders"].items()
               if o in after["orders"] and v[0] != "PENDING" and after["orders"][o] != v]
    check("all earlier orders present and unchanged", not missing and not changed,
          f"{len(before['orders'])} earlier orders; missing {len(missing)}, changed {len(changed)}")
    bad_pos = []
    for pid, v in before["positions"].items():
        a = after["positions"].get(pid)
        if a is None:
            bad_pos.append(f"{pid} missing")
        elif v["status"] != "OPEN" and a != v:
            bad_pos.append(f"{pid} closed position changed")
        elif v["status"] == "OPEN" and a["status"] == "OPEN" and abs(a["shares"] - v["shares"]) > 1e-9 \
                and a["shares"] > v["shares"]:
            bad_pos.append(f"{pid} grew unexpectedly")
    reasons_ok = all(
        r["close_reason"] for r in db.q("SELECT close_reason FROM positions WHERE status!='OPEN'"))
    check("earlier positions present; closures have a recorded reason", not bad_pos and reasons_ok,
          f"{len(before['positions'])} earlier positions; problems: {bad_pos[:3]}")
    check("one more equity snapshot",
          after["counts"]["equity_snapshots"] == before["counts"]["equity_snapshots"] + expect_new_runs,
          f"{before['counts']['equity_snapshots']} -> {after['counts']['equity_snapshots']}")
    grew = all(after["counts"][k] >= before["counts"][k] for k in ("signals", "forecasts", "events"))
    check("decision history only appended", grew,
          f"signals {before['counts']['signals']}->{after['counts']['signals']}, "
          f"forecasts {before['counts']['forecasts']}->{after['counts']['forecasts']}")
    return checks


def render(checks: list[dict], label: str) -> str:
    ok = all(c["passed"] for c in checks)
    lines = [f"# Persistence check ({label}): {'PASSED' if ok else 'FAILED'}", "",
             "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {c['check']} | {'pass' if c['passed'] else '**FAIL**'} | {c['detail']} |" for c in checks]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="paperbot.persistence_check")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--db", required=True)
    m.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--db", required=True)
    v.add_argument("--before", required=True)
    v.add_argument("--report")
    v.add_argument("--label", default="")
    args = ap.parse_args(argv)
    db = Database(args.db)
    try:
        if args.cmd == "manifest":
            Path(args.out).write_text(json.dumps(manifest(db), indent=2, default=str))
            print(f"manifest written: {args.out}")
            return 0
        checks = verify(json.loads(Path(args.before).read_text()), db)
        text = render(checks, args.label or db.get_meta("data_source") or "")
        print(text)
        if args.report:
            Path(args.report).write_text(text)
        return 0 if all(c["passed"] for c in checks) else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
