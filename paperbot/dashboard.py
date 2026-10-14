"""Static, self-contained HTML dashboard (no external scripts, no server).

Open `site/index.html` in a browser, or publish it as a CI artifact.
All text from the database / Polymarket is HTML-escaped.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Optional

from paperbot.db import Database
from paperbot.report import DISCLAIMER, compute_metrics

CSS = """
:root{--surface:#fcfcfb;--page:#f4f4f2;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;--rule:#e4e3df;
--series:#2a78d6;--good:#0ca30c;--warn:#b07800;--crit:#d03b3b;--ref:#a3a29d}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--surface:#1a1a19;--page:#111110;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;--rule:#2e2e2c;--series:#3987e5;--good:#0ca30c;
--warn:#fab219;--crit:#e66767;--ref:#6b6a65}}
:root[data-theme="dark"]{--surface:#1a1a19;--page:#111110;--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;
--rule:#2e2e2c;--series:#3987e5;--good:#0ca30c;--warn:#fab219;--crit:#e66767;--ref:#6b6a65}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.sub{color:var(--ink2);font-size:13px}.banner{background:var(--surface);border:1px solid var(--rule);
border-left:4px solid var(--warn);padding:10px 12px;margin:14px 0;color:var(--ink2);font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.tile{background:var(--surface);border:1px solid var(--rule);border-radius:8px;padding:12px}
.tile .k{color:var(--ink2);font-size:12px}.tile .v{font-size:22px;font-weight:600;margin-top:2px;
font-variant-numeric:tabular-nums}.tile .n{color:var(--muted);font-size:12px}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:8px;padding:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--rule);vertical-align:top}th{color:var(--ink2);font-weight:500}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;
border:1px solid currentColor}.GOOD{color:var(--good)}.WARNING{color:var(--warn)}
.CRITICAL,.UNKNOWN{color:var(--crit)}
svg text{fill:var(--ink2);font-size:11px}.grid line{stroke:var(--rule)}
#tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--rule);
padding:6px 8px;border-radius:6px;font-size:12px;display:none;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.chartwrap{position:relative;overflow-x:auto}.chartwrap svg{min-width:560px}details{margin-top:8px}summary{cursor:pointer;color:var(--ink2)}
.reasons{color:var(--ink2);font-size:12px}
"""

JS = """
(function(){const d=JSON.parse(document.getElementById('eqdata').textContent);
const svg=document.getElementById('eq');if(!svg||d.pts.length<1)return;
const tip=document.getElementById('tip'),hl=document.getElementById('hl'),dot=document.getElementById('hd');
svg.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect();
const x=(e.clientX-r.left)*(d.w/r.width);let best=0,bd=1e9;
d.pts.forEach((p,i)=>{const dd=Math.abs(p[0]-x);if(dd<bd){bd=dd;best=i}});
const p=d.pts[best];hl.setAttribute('x1',p[0]);hl.setAttribute('x2',p[0]);hl.style.display='';
dot.setAttribute('cx',p[0]);dot.setAttribute('cy',p[1]);dot.style.display='';
tip.style.display='block';tip.textContent=p[2]+'  ·  $'+p[3].toFixed(2);
tip.style.left=Math.min(e.clientX-r.left+12,r.width-170)+'px';tip.style.top=(e.clientY-r.top-34)+'px';});
svg.addEventListener('mouseleave',()=>{tip.style.display='none';hl.style.display='none';dot.style.display='none'});
})();
"""


def _e(x) -> str:
    return html.escape(str(x), quote=True)


def _usd(x: Optional[float]) -> str:
    if x is None:
        return "–"
    x = round(x, 2) + 0.0  # avoid "-$0.00"
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _chart(curve: list, start: float) -> str:
    w, h, pl, pr, pt, pb = 900, 260, 64, 16, 12, 28
    if not curve:
        return '<p class="sub">No equity data yet. Run the bot once.</p>'
    vals = [v for _, v in curve] + [start]
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.1, start * 0.002)
    lo, hi = lo - pad, hi + pad
    n = len(curve)
    xs = lambda i: pl + (w - pl - pr) * (i / (n - 1) if n > 1 else 0.5)
    ys = lambda v: pt + (h - pt - pb) * (1 - (v - lo) / (hi - lo))
    pts = [(round(xs(i), 1), round(ys(v), 1), ts[:16].replace("T", " "), v)
           for i, (ts, v) in enumerate(curve)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x},{y}" for i, (x, y, _, _) in enumerate(pts))
    grid = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = ys(v)
        grid.append(f'<line x1="{pl}" x2="{w - pr}" y1="{y:.1f}" y2="{y:.1f}"/>'
                    f'<text x="{pl - 8}" y="{y + 4:.1f}" text-anchor="end">${v:,.0f}</text>')
    xlabels = (f'<text x="{pl}" y="{h - 8}">{_e(pts[0][2])} UTC</text>'
               f'<text x="{w - pr}" y="{h - 8}" text-anchor="end">{_e(pts[-1][2])} UTC</text>')
    yref = ys(start)
    marker = (f'<circle cx="{pts[0][0]}" cy="{pts[0][1]}" r="4" fill="var(--series)"/>'
              if n == 1 else "")
    data = json.dumps({"w": w, "pts": pts}).replace("</", "<\\/")
    return (
        f'<div class="chartwrap"><svg id="eq" viewBox="0 0 {w} {h}" width="100%" role="img" '
        f'aria-label="Equity over time, liquidation value in US dollars">'
        f'<g class="grid">{"".join(grid)}</g>'
        f'<line x1="{pl}" x2="{w - pr}" y1="{yref:.1f}" y2="{yref:.1f}" stroke="var(--ref)" '
        f'stroke-dasharray="4 4" stroke-width="1"/>'
        f'<text x="{w - pr}" y="{yref - 6:.1f}" text-anchor="end">start {_usd(start)}</text>'
        f'<path d="{path}" fill="none" stroke="var(--series)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>{marker}'
        f'<line id="hl" y1="{pt}" y2="{h - pb}" stroke="var(--muted)" stroke-width="1" style="display:none"/>'
        f'<circle id="hd" r="4" fill="var(--series)" stroke="var(--surface)" stroke-width="2" style="display:none"/>'
        f'{xlabels}</svg><div id="tip"></div></div>'
        f'<script type="application/json" id="eqdata">{data}</script>'
    )


def _table(headers: list[str], rows: list[list], num_cols: tuple = ()) -> str:
    if not rows:
        return '<p class="sub">None.</p>'
    th = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i in num_cols else ""}">{c}</td>'
                         for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def render(db: Database, cap_usd: float) -> str:
    m = compute_metrics(db, cap_usd=cap_usd)
    wr = f"{m['win_rate']:.0%}" if m["win_rate"] is not None else "–"
    tiles = [
        ("Equity", _usd(m["equity"]), "liquidation value at bids"),
        ("Net P&L", _usd(m["net_pnl"]), f"{m['net_pnl_pct']:+.2%} vs start"),
        ("Realized P&L", _usd(m["realized_pnl"]), "closed + settled"),
        ("Unrealized P&L", _usd(m["unrealized_pnl"]), "open, after exit fees"),
        ("Max drawdown", f"{m['max_drawdown']:.1%}", f"now {m['current_drawdown']:.1%}"),
        ("Win rate", wr, f"{m['closed_trades']} closed trades (arb pair = 1)"),
        ("Fees paid", _usd(m["fees_paid"]), "simulated taker fees"),
        ("AI cost (month)", _usd(m["ai_cost_month"]), f"cap {_usd(m['ai_cap'])}"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div>'
                         f'<div class="n">{_e(n)}</div></div>' for k, v, n in tiles)

    pos_rows = [[_e(p["question"][:80]), _e(p["outcome"]), _e(p["strategy"]), f"{p['shares']:.2f}",
                 _usd(p["cost_basis"]), _usd(p["mark_value"]) if p["mark_value"] is not None else "unmarked"]
                for p in db.open_positions()]
    rej = db.q("SELECT ts, kind, question, reasons_json, edge FROM signals WHERE status='REJECTED' "
               "ORDER BY ts DESC LIMIT 25")
    rej_rows = [[_e(r["ts"][:16].replace("T", " ")), _e(r["kind"]), _e((r["question"] or "")[:70]),
                 f'<span class="reasons">{_e(", ".join(json.loads(r["reasons_json"])))}</span>']
                for r in rej]
    sig = db.q("SELECT ts, kind, question, outcome, status, expected_net_usd, cost_usd FROM signals "
               "WHERE status!='REJECTED' ORDER BY ts DESC LIMIT 25")
    sig_rows = [[_e(r["ts"][:16].replace("T", " ")), _e(r["kind"]), _e((r["question"] or "")[:60]),
                 _e(r["outcome"]), _e(r["status"]), _usd(r["cost_usd"]), _usd(r["expected_net_usd"])]
                for r in sig]
    orders = db.q("SELECT created_at, side, outcome, status, requested_shares, filled_shares, avg_price, "
                  "fee, latency_ms, reason FROM orders ORDER BY created_at DESC LIMIT 30")
    ord_rows = [[_e(o["created_at"][:16].replace("T", " ")), _e(o["side"]), _e(o["outcome"]),
                 _e(o["status"]), f"{o['filled_shares']:.2f}/{o['requested_shares']:.2f}",
                 f"{o['avg_price']:.4f}" if o["avg_price"] else "–", _usd(o["fee"]),
                 f"{(o['latency_ms'] or 0):.0f}", _e((o["reason"] or "")[:60])] for o in orders]
    runs = db.q("SELECT started_at, status, errors_json, api_stats_json FROM runs "
                "ORDER BY started_at DESC LIMIT 12")
    run_rows = [[_e(r["started_at"][:16].replace("T", " ")), f'<span class="badge {"GOOD" if r["status"] == "OK" else "WARNING" if r["status"] == "DEGRADED" else "CRITICAL"}">{_e(r["status"])}</span>',
                 str(len(json.loads(r["errors_json"]))), _e(r["api_stats_json"])] for r in runs]
    eq_rows = [[_e(ts[:16].replace("T", " ")), _usd(v)] for ts, v in m["equity_curve"][-50:][::-1]]

    halted = ('<div class="banner" style="border-left-color:var(--crit)"><strong>New entries halted</strong>'
              ' — maximum drawdown reached. Review, then run <code>python -m paperbot resume</code>.</div>'
              if m["halted"] else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Paperbot Dashboard</title>
<style>{CSS}</style></head><body><main>
<h1>Paperbot — paper trading dashboard</h1>
<div class="sub">Generated {_e(m['generated_at'][:19].replace('T', ' '))} UTC ·
Health <span class="badge {_e(m['health'])}">{_e(m['health'])}</span> ·
last run {_e(m['last_run']['started_at'][:16].replace('T', ' ') if m['last_run'] else 'never')} ·
errors 24h: {m['errors_24h']}</div>
<div class="banner">{_e(DISCLAIMER)}</div>{halted}
<div class="tiles">{tiles_html}</div>
<h2>Equity</h2><div class="card">{_chart(m['equity_curve'], m['starting_capital'])}
<details><summary>Table view</summary>{_table(['Time (UTC)', 'Equity'], eq_rows, (1,))}</details></div>
<h2>Open positions</h2><div class="card">{_table(['Market', 'Side', 'Strategy', 'Shares', 'Cost', 'Value now'], pos_rows, (3, 4, 5))}</div>
<h2>Signals acted on</h2><div class="card">{_table(['Time', 'Kind', 'Market', 'Side', 'Status', 'Cost', 'Expected net'], sig_rows, (5, 6))}</div>
<h2>Rejected signals</h2><div class="card">{_table(['Time', 'Kind', 'Market', 'Reasons'], rej_rows)}</div>
<h2>Orders (incl. failed fills)</h2><div class="card">{_table(['Time', 'Side', 'Out', 'Status', 'Filled', 'Avg px', 'Fee', 'Latency ms', 'Note'], ord_rows, (4, 5, 6, 7))}</div>
<h2>Bot health — recent runs</h2><div class="card">{_table(['Started', 'Status', 'Errors', 'Polymarket API stats'], run_rows, (2,))}</div>
</main><script>{JS}</script></body></html>"""


def write_dashboard(db: Database, out_dir: str | Path, cap_usd: float) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "index.html"
    path.write_text(render(db, cap_usd), encoding="utf-8")
    return path
