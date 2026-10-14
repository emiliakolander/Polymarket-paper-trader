"""Serious-error and action-required detection.

The bot runs unattended. It only asks for the owner's attention when:
  SERIOUS          - the run failed, 3 runs in a row were not OK, or new entries
                     were halted by the drawdown limit;
  ACTION_REQUIRED  - something needs an approval (e.g. a validated improvement while
                     auto_apply is off).
INFO items (e.g. the monthly AI budget is used up, so research paused) are reported
but do not raise an alert.

`python -m paperbot run` exits with code 3 when a SERIOUS or ACTION_REQUIRED alert is
active, which makes the GitHub Actions run fail and GitHub e-mails the owner.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from paperbot.db import Database, iso


def evaluate_alerts(db: Database, summary: dict, now: datetime) -> list[dict]:
    alerts = []
    status = summary.get("status")
    if status in ("FAILED",):
        alerts.append({"level": "SERIOUS", "code": "RUN_FAILED",
                       "message": f"Run failed: {summary.get('errors')}"})
    last = [r["status"] for r in db.q("SELECT status FROM runs ORDER BY started_at DESC LIMIT 3")]
    if len(last) == 3 and all(s != "OK" for s in last):
        alerts.append({"level": "SERIOUS", "code": "REPEATED_PROBLEMS",
                       "message": f"Last 3 runs were not OK: {last}"})
    run_id = summary.get("run_id")
    if run_id and db.q1("SELECT 1 FROM events WHERE run_id=? AND kind='HALTED_MAX_DRAWDOWN'", (run_id,)):
        alerts.append({"level": "SERIOUS", "code": "HALTED_MAX_DRAWDOWN",
                       "message": "Maximum drawdown reached; new entries halted until "
                                  "`python -m paperbot resume`."})
    if db.q1("SELECT 1 FROM proposals WHERE status='AWAITING_APPROVAL'"):
        alerts.append({"level": "ACTION_REQUIRED", "code": "PROPOSAL_AWAITING_APPROVAL",
                       "message": "A strategy improvement passed out-of-sample testing and "
                                  "awaits approval (learning.auto_apply is off)."})
    skipped = (summary.get("research") or {}).get("skipped") or ""
    if skipped.startswith("AI_BUDGET_EXHAUSTED"):
        alerts.append({"level": "INFO", "code": "AI_BUDGET_EXHAUSTED", "message": skipped})
    return alerts


def needs_attention(alerts: list[dict]) -> bool:
    return any(a["level"] in ("SERIOUS", "ACTION_REQUIRED") for a in alerts)


def write_alerts(alerts: list[dict], out_dir: str | Path, now: datetime) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "ALERTS.md"
    lines = [f"# Alerts — {iso(now)[:16]} UTC", ""]
    lines += [f"- **{a['level']}** `{a['code']}`: {a['message']}" for a in alerts] or ["No alerts."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
