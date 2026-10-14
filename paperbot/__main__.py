"""Command line interface.

  python -m paperbot run [--report-dir DIR] [--site-dir DIR]   one cycle (for cron/CI)
  python -m paperbot loop --interval 300                        always-on mode
  python -m paperbot report  --out DIR                          write report.md + metrics.json
  python -m paperbot dashboard --out DIR                        write index.html
  python -m paperbot status                                     one-line summary
  python -m paperbot halt | resume                              manual kill switch for new entries
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from paperbot.config import load_config
from paperbot.dashboard import write_dashboard
from paperbot.db import Database
from paperbot.engine import Engine
from paperbot.report import as_text_summary, compute_metrics, write_report
from paperbot.safety import SafetyError, startup_checks


def _outputs(cfg, args) -> None:
    db = Database(cfg.database_path)
    try:
        if getattr(args, "report_dir", None):
            write_report(db, args.report_dir, cfg.budget.monthly_ai_cap_usd)
        if getattr(args, "site_dir", None):
            write_dashboard(db, args.site_dir, cfg.budget.monthly_ai_cap_usd)
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paperbot", description="Polymarket PAPER trading bot")
    ap.add_argument("--config", default=None, help="path to paperbot.yaml")
    ap.add_argument("--db", default=None, help="override database path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "loop"):
        p = sub.add_parser(name)
        p.add_argument("--report-dir")
        p.add_argument("--site-dir")
        p.add_argument("--enable-research", action="store_true",
                       help="turn on the PAID Claude research agent for this process")
        if name == "loop":
            p.add_argument("--interval", type=int, default=300)
    for name in ("report", "dashboard"):
        sub.add_parser(name).add_argument("--out", required=True)
    for name in ("status", "halt", "resume"):
        sub.add_parser(name)
    args = ap.parse_args(argv)

    overrides: dict = {}
    if args.db:
        overrides["database_path"] = args.db
    if getattr(args, "enable_research", False):
        overrides["research"] = {"enabled": True}
    cfg = load_config(args.config, overrides)

    try:
        startup_checks()
    except SafetyError as exc:
        print(f"REFUSING TO RUN: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "run":
        summary = Engine(cfg).run_once()
        _outputs(cfg, args)
        print(json.dumps({k: summary.get(k) for k in ("run_id", "status", "equity", "errors")},
                         default=str, indent=2))
        return 0 if summary["status"] in ("OK", "DEGRADED", "SKIPPED_LOCKED") else 1
    if args.cmd == "loop":
        engine = Engine(cfg)
        while True:
            summary = engine.run_once()
            _outputs(cfg, args)
            print(f"[{summary['status']}] {summary.get('equity', {})}", flush=True)
            time.sleep(max(60, args.interval))
    db = Database(cfg.database_path)
    try:
        if args.cmd == "report":
            write_report(db, args.out, cfg.budget.monthly_ai_cap_usd)
        elif args.cmd == "dashboard":
            print(write_dashboard(db, args.out, cfg.budget.monthly_ai_cap_usd))
        elif args.cmd == "status":
            print(as_text_summary(compute_metrics(db, cap_usd=cfg.budget.monthly_ai_cap_usd)))
        elif args.cmd == "halt":
            db.set_meta("halted", "1")
            print("New entries halted.")
        elif args.cmd == "resume":
            db.set_meta("halted", "0")
            # Reset the drawdown reference so the halt does not immediately re-trigger.
            last = db.q1("SELECT equity FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
            if last:
                db.set_meta("peak_equity", last["equity"])
            print("Resumed. Peak equity reset to current equity.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
